"""Volcengine Seed-ASR streaming client (WebSocket binary protocol).

Protocol reference: https://docs.volcengine.com/docs/6561/1354869

Uses the optimized bidirectional streaming endpoint (bigmodel_async) with
two-pass recognition (enable_nonstream): interim results arrive fast, and
each VAD-closed segment is re-recognized by the non-streaming model for
accuracy, marked with "definite": true in the utterances list.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import struct
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

import websockets

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

# Binary protocol nibbles (see the doc's protocol table).
PROTOCOL_VERSION = 0x1
HEADER_SIZE = 0x1  # unit: 4 bytes
MSG_FULL_CLIENT_REQUEST = 0x1
MSG_AUDIO_ONLY = 0x2
MSG_FULL_SERVER_RESPONSE = 0x9
MSG_SERVER_ERROR = 0xF
FLAG_NONE = 0x0
FLAG_POS_SEQUENCE = 0x1
FLAG_LAST_NO_SEQUENCE = 0x2
FLAG_NEG_SEQUENCE = 0x3
SER_NONE = 0x0
SER_JSON = 0x1
COMP_GZIP = 0x1


def _header(msg_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes(
        [
            PROTOCOL_VERSION << 4 | HEADER_SIZE,
            msg_type << 4 | flags,
            serialization << 4 | compression,
            0x00,
        ]
    )


class AsrError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"ASR server error {code}: {message}")
        self.code = code
        self.message = message


@dataclass
class AsrConfig:
    api_key: str | None = None  # new console: X-Api-Key (access-control API Key with 语音技术 scope)
    app_key: str | None = None  # old console: APP ID (X-Api-App-Key)
    access_key: str | None = None  # old console: Access Token (X-Api-Access-Key)
    resource_id: str = "volc.seedasr.sauc.duration"  # Seed-ASR 2.0, hourly billing
    boosting_table_id: str | None = None  # 自学习平台热词表, lifts the 100-token direct cap
    endpoint: str = ENDPOINT
    hotwords: list[str] = field(default_factory=list)
    enable_nonstream: bool = True  # two-pass: fast interim + accurate definite
    enable_speaker_info: bool = True  # speaker clustering, needs nonstream + ssd 200
    ssd_version: str = "200"
    end_window_size: int = 800  # ms of silence that closes a segment
    enable_ddc: bool = False  # disfluency smoothing
    uid: str = "babel"


@dataclass
class AsrEvent:
    text: str  # incremental text (result_type=single)
    utterances: list[dict]  # utterance info (show_utterances=true)
    is_last: bool
    duration_ms: int


def _build_full_request(config: AsrConfig) -> bytes:
    payload: dict = {
        "user": {"uid": config.uid},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_punc": True,
            "enable_itn": True,
            "enable_nonstream": config.enable_nonstream,
            "enable_speaker_info": config.enable_speaker_info,
            "ssd_version": config.ssd_version,
            "end_window_size": config.end_window_size,
            "show_utterances": True,
            "result_type": "single",
            "enable_ddc": config.enable_ddc,
        },
    }
    corpus: dict = {}
    if config.hotwords:
        hotwords = [{"word": w} for w in config.hotwords]
        corpus["context"] = json.dumps({"hotwords": hotwords}, ensure_ascii=False)
    if config.boosting_table_id:
        corpus["boosting_table_id"] = config.boosting_table_id
    if corpus:
        payload["request"]["corpus"] = corpus
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return (
        _header(MSG_FULL_CLIENT_REQUEST, FLAG_NONE, SER_JSON, COMP_GZIP)
        + struct.pack(">I", len(body))
        + body
    )


def _build_audio_request(chunk: bytes, last: bool) -> bytes:
    flags = FLAG_LAST_NO_SEQUENCE if last else FLAG_NONE
    body = gzip.compress(chunk)
    return (
        _header(MSG_AUDIO_ONLY, flags, SER_NONE, COMP_GZIP)
        + struct.pack(">I", len(body))
        + body
    )


def _parse_response(frame: bytes) -> tuple[int, int, bytes]:
    """Return (msg_type, flags, payload). Raises AsrError on error frames."""
    b1, b2 = frame[1], frame[2]
    msg_type, flags = b1 >> 4, b1 & 0xF
    compression = b2 & 0xF
    offset = 4  # fixed 4-byte header
    if msg_type == MSG_SERVER_ERROR:
        code = struct.unpack(">I", frame[offset : offset + 4])[0]
        size = struct.unpack(">I", frame[offset + 4 : offset + 8])[0]
        message = frame[offset + 8 : offset + 8 + size].decode("utf-8")
        raise AsrError(code, message)
    if flags in (FLAG_POS_SEQUENCE, FLAG_NEG_SEQUENCE):
        offset += 4  # skip sequence number
    size = struct.unpack(">I", frame[offset : offset + 4])[0]
    payload = frame[offset + 4 : offset + 4 + size]
    if compression == COMP_GZIP:
        payload = gzip.decompress(payload)
    return msg_type, flags, payload


class VolcAsrClient:
    def __init__(self, config: AsrConfig):
        self.config = config
        self._ws = None  # live connection, kept so close() can force a restart

    async def close(self) -> None:
        """Drop the current connection; the caller's reconnect loop then
        restarts the session with whatever config changed (e.g. enable_ddc)."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def transcribe(
        self, audio_chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[AsrEvent]:
        """Stream PCM chunks (16kHz mono s16le) and yield recognition events."""
        headers = {
            "X-Api-Resource-Id": self.config.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key
        elif self.config.app_key and self.config.access_key:
            headers["X-Api-App-Key"] = self.config.app_key
            headers["X-Api-Access-Key"] = self.config.access_key
        else:
            raise ValueError("ASR auth missing: set api_key, or app_key + access_key")
        ws_ctx = websockets.connect(
            self.config.endpoint, additional_headers=headers, max_size=None,
            proxy=None,  # bypass system/env proxies (SOCKS breaks the handshake)
        )

        async with ws_ctx as ws:
            self._ws = ws
            logid = ws.response.headers.get("X-Tt-Logid", "") if ws.response else ""
            if logid:
                print(f"[asr] connected, logid={logid}")
            await ws.send(_build_full_request(self.config))

            async def _send() -> None:
                async for chunk in audio_chunks:
                    await ws.send(_build_audio_request(chunk, last=False))
                await ws.send(_build_audio_request(b"", last=True))

            sender = asyncio.ensure_future(_send())
            try:
                async for frame in ws:
                    if isinstance(frame, str):
                        continue
                    msg_type, flags, payload = _parse_response(frame)
                    if msg_type != MSG_FULL_SERVER_RESPONSE:
                        continue
                    data = json.loads(payload.decode("utf-8")) if payload else {}
                    result = data.get("result", {})
                    yield AsrEvent(
                        text=result.get("text", ""),
                        utterances=result.get("utterances", []),
                        is_last=(flags == FLAG_NEG_SEQUENCE),
                        duration_ms=data.get("audio_info", {}).get("duration", 0),
                    )
                    if flags == FLAG_NEG_SEQUENCE:
                        break
            finally:
                sender.cancel()
                self._ws = None
