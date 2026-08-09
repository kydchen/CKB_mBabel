"""Babel: live bilingual captions for zh/en meetings.

Pipeline: mic or wav file -> Volcengine Seed-ASR (streaming, hotwords)
        -> translation API (glossary-injected) -> terminal captions.

Interim ASR text is shown live; when the ASR marks an utterance as definite
(VAD-closed, two-pass re-recognized), it is translated and committed.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import signal
import sys
import time
import wave
import webbrowser
from collections import deque
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosedError, InvalidStatus

from asr_client import AsrConfig, AsrError, VolcAsrClient
from translate import ArkTranslator, Glossary, build_translator
from ui_server import CaptionUI


def load_dotenv(path: str) -> None:
    """Minimal .env loader: KEY=value lines, '#' comments. Later duplicate
    keys override earlier ones — deliberate: the file may append a 主账号
    section reusing the same key names, and the later (root) pair must win."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def migrate_audio_config(config_path: str, legacy: str | None = None) -> None:
    """Copy the old repo-local settings once; OneDrive must not own device state."""
    if legacy is None:
        legacy = os.path.join(os.path.dirname(__file__), "audio_config.json")
    if os.path.exists(config_path) or not os.path.exists(legacy):
        return
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        shutil.copy2(legacy, config_path)
    except OSError as e:
        print(f"[audio] cannot migrate device settings: {e}", file=sys.stderr)


def load_hotwords_dir(path: str) -> list[tuple[str, int]]:
    """Read hotwords with optional ``#priority high|normal|low`` sections."""
    words: list[tuple[str, int]] = []
    if not os.path.isdir(path):
        return words
    for name in sorted(os.listdir(path)):
        if not name.endswith(".txt") or name == "boosting_table.txt":
            continue
        priority = 1
        for line in open(os.path.join(path, name), encoding="utf-8"):
            word = line.strip()
            marker = re.fullmatch(r"#priority(?:\s*[:=]?\s*(high|normal|low))?",
                                  word, re.IGNORECASE)
            if marker:
                priority = {"high": 2, "normal": 1, "low": 0}.get(
                    (marker.group(1) or "high").lower(), 2
                )
            elif word and not word.startswith("#"):
                words.append((word, priority))
    return words

SAMPLE_RATE = 16000
CHUNK_MS = 200  # 200ms per packet is optimal for the bidirectional endpoint
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2  # s16le mono
RECONNECT_TAIL_CHUNKS = 3000 // CHUNK_MS
REPLAY_GUARD_SECONDS = 10.0
REPLAY_GUARD_FRAGMENTS = 5
CLAUSE_FLUSH_CHARS = 80
CLAUSE_ENDINGS = ("，", "、", "；", ",", ";")
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".mbabel")


def session_file_path(port: int) -> str:
    return os.path.join(SESSION_DIR, f"session-{port}.json")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def session_http_alive(url: str, port: int) -> bool:
    """Probe only this port's loopback page; registry contents never select a host."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port != port):
            return False
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.0)
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.request("GET", path)
            response = conn.getresponse()
            response.read()
            return response.status == 200
        finally:
            conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return False


def clear_session_file(port: int, expected_pid: int | None = None) -> None:
    path = session_file_path(port)
    if expected_pid is not None:
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("pid") != expected_pid:
                    return
        except (OSError, TypeError, ValueError, AttributeError):
            return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def register_session(port: int, url: str) -> str:
    os.makedirs(SESSION_DIR, mode=0o700, exist_ok=True)
    path = session_file_path(port)
    temp_path = f"{path}.{os.getpid()}.tmp"
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "port": port, "url": url,
                       "started_at": time.time()}, f)
        os.replace(temp_path, path)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
    return path


def live_session(port: int) -> dict | None:
    path = session_file_path(port)
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        pid = record.get("pid")
        if (not isinstance(pid, int) or record.get("port") != port
                or not isinstance(record.get("url"), str)):
            clear_session_file(port)
            return None
    except (OSError, TypeError, ValueError, AttributeError):
        clear_session_file(port)
        return None
    if process_alive(pid) and session_http_alive(record["url"], port):
        return record
    clear_session_file(port, expected_pid=pid)
    return None


def reopen_existing_session(port: int) -> bool:
    record = live_session(port)
    if not record:
        return False
    url = record["url"]
    print(f"[ui] 已有会话正在进行，正在打开字幕页。\n"
          f"[ui] A session is already running; reopening captions: {url}")
    try:
        if not webbrowser.open(url):
            print(f"[ui] 浏览器未响应，请手动打开 / Browser did not respond; open: {url}")
    except OSError as e:
        print(f"[ui] 无法打开浏览器，请手动打开 / Cannot open browser; open {url}: {e}")
    return True


def collect_reconnect_tail(sent_tail, queue: asyncio.Queue,
                           keep: int = RECONNECT_TAIL_CHUNKS) -> tuple[list, int]:
    """Collect the newest sent and pending audio for the next ASR session."""
    chunks = list(sent_tail)
    while True:
        try:
            chunk = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if chunk is not None:
            chunks.append(chunk)
    tail = chunks[-keep:]
    return tail, len(chunks) - len(tail)


def discard_queued_audio(queue: asyncio.Queue) -> int:
    """Drop queued audio while preserving the WAV end sentinel."""
    count = 0
    ended = False
    while True:
        try:
            chunk = queue.get_nowait()
        except asyncio.QueueEmpty:
            if ended:
                queue.put_nowait(None)
            return count
        if chunk is None:
            ended = True
        else:
            count += 1


def is_replay_duplicate(text: str, previous: str) -> bool:
    """Return whether a reconnect fragment repeats the prior sentence tail."""
    normalize = lambda value: re.sub(r"[\W_]+", "", value).casefold()
    candidate = normalize(text)
    prior = normalize(previous)
    # Short acknowledgements are too ambiguous: visible duplication is safer
    # than dropping a legitimate repeated "yes" / "好".
    if len(candidate) < 4 or not prior or len(candidate) > len(prior):
        return False
    tail = prior[-len(candidate):]
    return difflib.SequenceMatcher(
        None, candidate, tail, autojunk=False
    ).ratio() > 0.8


def merge_reconnect_partial(partial: str, replay: str) -> str:
    """Join a pre-disconnect interim with the re-recognized audio tail."""
    prefix = partial.strip()
    suffix = replay.strip()
    if not prefix or not suffix:
        return prefix or suffix
    blocks = difflib.SequenceMatcher(
        None, prefix.casefold(), suffix.casefold(), autojunk=False
    ).get_matching_blocks()
    overlaps = [block for block in blocks
                if block.size >= 4 and block.a + block.size == len(prefix)]
    if overlaps:
        overlap = max(overlaps, key=lambda block: block.size)
        return prefix + suffix[overlap.b + overlap.size:]
    separator = (" " if prefix[-1].isascii() and prefix[-1].isalnum()
                 and suffix[0].isascii() and suffix[0].isalnum() else "")
    return prefix + separator + suffix


def should_flush_clause(current: str, fragment: str) -> bool:
    """Return whether a long live line reached a natural clause boundary."""
    return (len(current) >= CLAUSE_FLUSH_CHARS
            and fragment.rstrip().endswith(CLAUSE_ENDINGS))


def print_safe(*args, **kwargs) -> None:
    """print() that survives a dead pty: closing the terminal window delivers
    SIGHUP with the tty already gone, so status prints during cleanup raise
    EIO — and must never abort the transcript/glossary writes that follow."""
    try:
        print(*args, **kwargs)
    except OSError:
        pass


# ANSI styles
DIM = "\033[2m"
BOLD = "\033[1m"
CLEAR_LINE = "\033[2K\r"
RESET = "\033[0m"

CJK_RE = re.compile(r"[一-鿿]")


def hotword_token_cost(word: str) -> int:
    """Conservative ASR estimate: each CJK char and whitespace word is a token."""
    cjk = len(CJK_RE.findall(word))
    latin = len(CJK_RE.sub(" ", word).split())
    return max(1, cjk + latin)


def trim_hotwords(entries: list[tuple[str, int]], limit: int = 100
                  ) -> tuple[list[str], list[str], int]:
    """Keep higher-priority hotwords first without exceeding the token budget."""
    ranked = sorted(enumerate(entries), key=lambda item: (-item[1][1], item[0]))
    kept: list[str] = []
    dropped: list[str] = []
    used = 0
    for index, (_, (word, _priority)) in enumerate(ranked):
        cost = hotword_token_cost(word)
        if used + cost <= limit:
            kept.append(word)
            used += cost
        else:
            dropped.extend(item[0] for _, item in ranked[index:])
            break
    return kept, dropped, used


LATIN_RE = re.compile(r"[A-Za-z]")


def detect_direction(text: str) -> tuple[str, str]:
    """Return (source_lang, target_lang). Any CJK char means the speaker
    used Chinese (possibly code-switched), so translate to English."""
    if CJK_RE.search(text):
        return "zh", "en"
    return "en", "zh"


def dominant_lang(text: str) -> str:
    """Rough majority vote by character class, for language-boundary splits."""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    return "zh" if cjk >= latin else "en"


# Python's \w matches CJK, which would let a latin run swallow following
# Chinese text; keep every character class strictly ASCII.
LONG_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'’&+\-]*(?:\s+[A-Za-z0-9'’&+\-.,?!…]+)*")


def split_lang_runs(text: str) -> list[tuple[str, str]]:
    """Split text into (lang, chunk) pieces. A latin stretch counts as
    English speech only when it is at least 15 letters AND 4 words — short
    runs and multi-letter terms (Lightning Network, Cell model) embedded in
    Chinese stay with the Chinese chunk. Anything containing a CJK
    character counts as Chinese."""
    pieces: list[tuple[str, str]] = []
    pos = 0
    for m in LONG_LATIN.finditer(text):
        letters = sum(1 for c in m.group(0) if c.isascii() and c.isalpha())
        words = len(m.group(0).split())
        if letters < 15 or words < 4 or CJK_RE.search(m.group(0)):
            continue  # term or too short, or (defensively) contains CJK
        pre = text[pos : m.start()]
        if pre:
            pieces.append(("zh" if CJK_RE.search(pre) else "en", pre))
        pieces.append(("en", m.group(0)))
        pos = m.end()
    rest = text[pos:]
    if rest:
        pieces.append(("zh" if CJK_RE.search(rest) else "en", rest))
    return pieces


def downmix_to_mono(data: bytes, channels: int) -> bytes:
    """Average N interleaved s16le channels into mono."""
    import array

    samples = array.array("h")
    samples.frombytes(data)
    out = array.array("h", bytes(len(samples) // channels * 2))
    for i in range(0, len(samples), channels):
        out[i // channels] = int(sum(samples[i : i + channels]) / channels)
    return out.tobytes()


def mix_pcm_s16(microphone: bytes, system: bytes) -> bytes:
    """Sum two mono s16le blocks with int32 accumulation and clipping."""
    import array

    mic = array.array("h")
    mic.frombytes(microphone)
    other = array.array("h")
    other.frombytes(system)
    out = array.array("h", bytes(len(mic) * 2))
    for i, sample in enumerate(mic):
        mixed = sample + (other[i] if i < len(other) else 0)
        out[i] = max(-32768, min(32767, mixed))
    return out.tobytes()


_COREAUDIO = None


def _coreaudio_api():
    if sys.platform != "darwin":
        return None

    import ctypes

    global _COREAUDIO
    if _COREAUDIO is None:
        class Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32),
                        ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        ca = ctypes.CDLL(
            "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
        )
        cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
        ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        ca.AudioObjectSetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(Address), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
        ]
        ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
        ca.AudioHardwareCreateAggregateDevice.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        ca.AudioHardwareCreateAggregateDevice.restype = ctypes.c_int32
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFNumberCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p,
        ]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFArrayCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p,
        ]
        cf.CFArrayCreate.restype = ctypes.c_void_p
        cf.CFDictionaryCreate.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        cf.CFDictionaryCreate.restype = ctypes.c_void_p
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _COREAUDIO = ca, cf, Address
    return _COREAUDIO


def coreaudio_devices() -> dict[int, str] | None:
    """Return live macOS CoreAudio device IDs and names; None if unavailable."""
    api = _coreaudio_api()
    if api is None:
        return None

    import ctypes

    ca, cf, Address = api
    fourcc = lambda value: int.from_bytes(value.encode(), "big")
    device_list = Address(fourcc("dev#"), fourcc("glob"), 0)
    size = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyDataSize(
        1, ctypes.byref(device_list), 0, None, ctypes.byref(size)
    ):
        return None
    ids = (ctypes.c_uint32 * (size.value // 4))()
    if ca.AudioObjectGetPropertyData(
        1, ctypes.byref(device_list), 0, None, ctypes.byref(size), ids
    ):
        return None

    devices = {}
    name_property = Address(fourcc("lnam"), fourcc("glob"), 0)
    for device_id in ids:
        name_ref = ctypes.c_void_p()
        name_size = ctypes.c_uint32(ctypes.sizeof(name_ref))
        if ca.AudioObjectGetPropertyData(
            device_id, ctypes.byref(name_property), 0, None,
            ctypes.byref(name_size), ctypes.byref(name_ref),
        ):
            continue
        try:
            name = ctypes.create_string_buffer(1024)
            if cf.CFStringGetCString(name_ref, name, len(name), 0x08000100):
                devices[int(device_id)] = name.value.decode()
        finally:
            cf.CFRelease(name_ref)
    return devices


def coreaudio_string_property(device_id: int, selector: str) -> str | None:
    """Read a CFString-valued device property."""
    api = _coreaudio_api()
    if api is None:
        return None

    import ctypes

    ca, cf, Address = api
    fourcc = lambda value: int.from_bytes(value.encode(), "big")
    address = Address(fourcc(selector), fourcc("glob"), 0)
    value = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(value))
    if ca.AudioObjectGetPropertyData(
        device_id, ctypes.byref(address), 0, None,
        ctypes.byref(size), ctypes.byref(value),
    ) or not value:
        return None
    try:
        text = ctypes.create_string_buffer(1024)
        if cf.CFStringGetCString(value, text, len(text), 0x08000100):
            return text.value.decode()
    finally:
        cf.CFRelease(value)
    return None


def coreaudio_device_channels(device_id: int, scope: str) -> int:
    """Read the live channel count from a CoreAudio AudioBufferList."""
    api = _coreaudio_api()
    if api is None:
        return 0

    import ctypes

    ca, _, Address = api
    fourcc = lambda value: int.from_bytes(value.encode(), "big")
    address = Address(fourcc("slay"), fourcc(scope), 0)
    size = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyDataSize(
        device_id, ctypes.byref(address), 0, None, ctypes.byref(size)
    ):
        return 0
    raw = ctypes.create_string_buffer(size.value)
    if ca.AudioObjectGetPropertyData(
        device_id, ctypes.byref(address), 0, None, ctypes.byref(size), raw
    ):
        return 0

    class AudioBuffer(ctypes.Structure):
        _fields_ = [("channels", ctypes.c_uint32),
                    ("byte_size", ctypes.c_uint32),
                    ("data", ctypes.c_void_p)]

    count = ctypes.c_uint32.from_buffer(raw).value
    alignment = ctypes.alignment(AudioBuffer)
    offset = (ctypes.sizeof(ctypes.c_uint32) + alignment - 1) // alignment * alignment
    return sum(AudioBuffer.from_buffer(
        raw, offset + i * ctypes.sizeof(AudioBuffer)
    ).channels for i in range(count))


def coreaudio_default_device(selector: str) -> int | None:
    api = _coreaudio_api()
    if api is None:
        return None

    import ctypes

    ca, _, Address = api
    fourcc = lambda value: int.from_bytes(value.encode(), "big")
    address = Address(fourcc(selector), fourcc("glob"), 0)
    size = ctypes.c_uint32(4)
    value = ctypes.c_uint32()
    if ca.AudioObjectGetPropertyData(
        1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)
    ):
        return None
    return int(value.value)


def coreaudio_set_default_output(device_id: int) -> None:
    """Set the macOS default (non-alert) output device."""
    api = _coreaudio_api()
    if api is None:
        raise RuntimeError("automatic output routing requires macOS")

    import ctypes

    ca, _, Address = api
    fourcc = lambda value: int.from_bytes(value.encode(), "big")
    address = Address(fourcc("dOut"), fourcc("glob"), 0)
    value = ctypes.c_uint32(device_id)
    status = ca.AudioObjectSetPropertyData(
        1, ctypes.byref(address), 0, None,
        ctypes.sizeof(value), ctypes.byref(value),
    )
    if status:
        raise OSError(status, "CoreAudio could not change the default output")


def multi_output_description(speaker_uid: str, blackhole_uid: str) -> dict:
    """Build the CoreAudio aggregate description for a Multi-Output device."""
    digest = hashlib.sha256(f"{speaker_uid}\0{blackhole_uid}".encode()).hexdigest()[:16]
    return {
        "uid": f"com.mbabel.multi-output.{digest}",
        "name": "mBabel Multi-Output",
        "subdevices": [
            {"uid": speaker_uid, "drift": False},
            {"uid": blackhole_uid, "drift": True},
        ],
        "master": speaker_uid,
        "private": False,
        # StackedOutput is CoreAudio's Multi-Output layout: every sub-device
        # receives the same output channels instead of exposing their sum.
        "stacked": True,
    }


def coreaudio_create_multi_output(speaker_id: int, blackhole_id: int) -> int:
    """Create or reuse mBabel's published Multi-Output device."""
    api = _coreaudio_api()
    devices = coreaudio_devices()
    if api is None or devices is None:
        raise RuntimeError("automatic output routing requires macOS")

    import ctypes

    ca, cf, _ = api
    speaker_uid = coreaudio_string_property(speaker_id, "uid ")
    blackhole_uid = coreaudio_string_property(blackhole_id, "uid ")
    if not speaker_uid or not blackhole_uid:
        raise RuntimeError("CoreAudio device UID is unavailable")
    description = multi_output_description(speaker_uid, blackhole_uid)
    for device_id in devices:
        if coreaudio_string_property(device_id, "uid ") == description["uid"]:
            return device_id

    key_callbacks = ctypes.addressof(
        ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    )
    value_callbacks = ctypes.addressof(
        ctypes.c_byte.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    )
    array_callbacks = ctypes.addressof(
        ctypes.c_byte.in_dll(cf, "kCFTypeArrayCallBacks")
    )

    def make_cf(value):
        if isinstance(value, str):
            ref = cf.CFStringCreateWithCString(None, value.encode(), 0x08000100)
            if not ref:
                raise MemoryError("cannot create CoreFoundation string")
            return ref, True
        if isinstance(value, bool):
            number = ctypes.c_int32(int(value))
            ref = cf.CFNumberCreate(None, 3, ctypes.byref(number))  # SInt32
            if not ref:
                raise MemoryError("cannot create CoreFoundation number")
            return ref, True
        if isinstance(value, list):
            children = [make_cf(item) for item in value]
            refs = (ctypes.c_void_p * len(children))(*(item[0] for item in children))
            ref = cf.CFArrayCreate(None, refs, len(children), array_callbacks)
        elif isinstance(value, dict):
            pairs = [(make_cf(str(key)), make_cf(item)) for key, item in value.items()]
            keys = (ctypes.c_void_p * len(pairs))(*(pair[0][0] for pair in pairs))
            values = (ctypes.c_void_p * len(pairs))(*(pair[1][0] for pair in pairs))
            ref = cf.CFDictionaryCreate(
                None, keys, values, len(pairs), key_callbacks, value_callbacks,
            )
            children = [child for pair in pairs for child in pair]
        else:
            raise TypeError(f"unsupported CoreFoundation value: {type(value)}")
        for child, owned in children:
            if owned:
                cf.CFRelease(child)
        if not ref:
            raise MemoryError("cannot create CoreFoundation container")
        return ref, True

    root, _ = make_cf(description)
    device_id = ctypes.c_uint32()
    try:
        status = ca.AudioHardwareCreateAggregateDevice(root, ctypes.byref(device_id))
    finally:
        cf.CFRelease(root)
    if status:
        raise OSError(status, "CoreAudio could not create Multi-Output device")
    return int(device_id.value)


class CoreAudioOutputRouter:
    """Temporarily route default output to speaker + BlackHole, then restore."""

    def __init__(self):
        self.original_device = None
        self.route_device = None
        self.fallback_device = None

    @staticmethod
    def _output_id(name: str) -> int | None:
        devices = coreaudio_devices() or {}
        return next((device_id for device_id, device_name in devices.items()
                     if device_name == name
                     and coreaudio_device_channels(device_id, "outp") > 0), None)

    def enable(self, speaker: str, blackhole: str) -> None:
        speaker_id = self._output_id(speaker)
        blackhole_id = self._output_id(blackhole)
        if speaker_id is None or blackhole_id is None:
            raise RuntimeError("selected output or BlackHole is unavailable")
        if self.route_device is None:
            current = coreaudio_default_device("dOut")
            current_uid = (coreaudio_string_property(current, "uid ")
                           if current is not None else None)
            # Recover from a prior SIGKILL: its finally could not restore the
            # output, so the stale mBabel route must not become the new target.
            stale = current_uid and current_uid.startswith("com.mbabel.multi-output.")
            self.original_device = speaker_id if stale else current
        route = coreaudio_create_multi_output(speaker_id, blackhole_id)
        coreaudio_set_default_output(route)
        self.route_device = route
        self.fallback_device = speaker_id

    def disable(self) -> None:
        if self.route_device is None:
            return
        current = coreaudio_default_device("dOut")
        devices = coreaudio_devices() or {}
        target = (self.original_device if self.original_device in devices
                  else self.fallback_device if self.fallback_device in devices
                  else None)
        # Respect a manual output change made while mBabel was running.
        if current == self.route_device and target is not None:
            coreaudio_set_default_output(target)
        self.original_device = self.route_device = self.fallback_device = None


def discover_audio_devices() -> dict:
    """Return current real microphones, outputs, defaults, and BlackHole."""
    devices = coreaudio_devices()
    if devices is not None:
        details = [{
            "id": device_id,
            "name": name,
            "inputs": coreaudio_device_channels(device_id, "inpt"),
            "outputs": coreaudio_device_channels(device_id, "outp"),
        } for device_id, name in devices.items()]
        default_input_id = coreaudio_default_device("dIn ")
        default_output_id = coreaudio_default_device("dOut")
    else:
        import sounddevice as sd

        raw = sd.query_devices()
        details = [{
            "id": i,
            "name": str(info["name"]),
            "inputs": int(info["max_input_channels"]),
            "outputs": int(info["max_output_channels"]),
        } for i, info in enumerate(raw)]
        default_input_id, default_output_id = sd.default.device

    virtual = ("blackhole", "multi-output", "aggregate", "iflyrec")
    real = lambda item: not any(v in item["name"].lower() for v in virtual)
    microphones = [d["name"] for d in details if d["inputs"] > 0 and real(d)]
    outputs = [d["name"] for d in details if d["outputs"] > 0 and real(d)]
    blackhole = next((d["name"] for d in details
                      if d["inputs"] > 0 and "blackhole" in d["name"].lower()), None)
    by_id = {d["id"]: d["name"] for d in details}
    return {
        "microphones": microphones,
        "outputs": outputs,
        "default_input": by_id.get(default_input_id),
        "default_output": by_id.get(default_output_id),
        "blackhole": blackhole,
    }


class AudioMixer:
    """Hot-switchable mic + optional BlackHole mixer feeding the ASR queue."""

    def __init__(self, queue: asyncio.Queue, config_path: str):
        self.queue = queue
        self.config_path = config_path
        self.loop = None
        self.mic_stream = None
        self.system_stream = None
        self.system_chunks = []
        self.discovery = {"microphones": [], "outputs": [],
                          "default_input": None, "default_output": None,
                          "blackhole": None}
        self.warning = ""
        self.paused = False
        self.on_state = None
        self.lock = asyncio.Lock()
        self.output_router = CoreAudioOutputRouter() if sys.platform == "darwin" else None
        self.stats = {"system_underflow": 0, "system_drop": 0, "queue_drop": 0}
        self.last_scan = time.monotonic()
        self.last_stats = time.monotonic()
        try:
            with open(config_path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError, TypeError):
            saved = {}
        self.microphone = saved.get("microphone")
        self.capture_system = bool(saved.get("capture_system", False))
        self.speaker = saved.get("speaker")
        self.panel_seen = bool(saved.get("panel_seen", False))

    def snapshot(self) -> dict:
        return {
            "type": "devices",
            "microphones": self.discovery["microphones"],
            "outputs": self.discovery["outputs"],
            "microphone": self.microphone,
            "capture_system": self.capture_system,
            "speaker": self.speaker,
            "blackhole": bool(self.discovery["blackhole"]),
            "warning": self.warning,
            "first_run": not self.panel_seen,
            **self.stats,
        }

    def persist(self) -> None:
        data = {
            "microphone": self.microphone,
            "capture_system": self.capture_system,
            "speaker": self.speaker,
            "panel_seen": self.panel_seen,
        }
        tmp = self.config_path + ".tmp"
        try:
            parent = os.path.dirname(self.config_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.config_path)
        except OSError as e:
            print(f"[audio] cannot persist device settings: {e}", file=sys.stderr)

    async def notify(self) -> None:
        if self.on_state:
            await self.on_state(self.snapshot())

    def _fallbacks(self) -> tuple[str | None, str | None]:
        microphones = self.discovery["microphones"]
        outputs = self.discovery["outputs"]
        default_mic = self.discovery["default_input"]
        default_output = self.discovery["default_output"]
        microphone = default_mic if default_mic in microphones else next(iter(microphones), None)
        speaker = default_output if default_output in outputs else next(iter(outputs), None)
        return microphone, speaker

    def _sync_output_route(self, enabled: bool | None = None) -> None:
        if self.output_router is None:
            return
        try:
            active = self.capture_system if enabled is None else enabled
            if active and self.speaker and self.discovery["blackhole"]:
                self.output_router.enable(self.speaker, self.discovery["blackhole"])
                if self.warning.startswith("Automatic output routing failed"):
                    self.warning = ""
            else:
                self.output_router.disable()
        except Exception as e:
            self.warning = f"Automatic output routing failed: {e}"
            print(f"[audio] {self.warning}", file=sys.stderr)

    async def scan(self, initial: bool = False, notify: bool = True) -> None:
        self.discovery = discover_audio_devices()
        fallback_mic, fallback_speaker = self._fallbacks()
        restart = False
        changed = False
        route_changed = False
        if self.microphone not in self.discovery["microphones"]:
            old = self.microphone
            self.microphone = fallback_mic
            restart = not initial
            changed = True
            if old:
                self.warning = f"Microphone disappeared: {old}; switched to {fallback_mic or 'none'}"
                print(f"[audio] {self.warning}", file=sys.stderr)
        if self.speaker not in self.discovery["outputs"]:
            if self.speaker != fallback_speaker:
                old = self.speaker
                self.speaker = fallback_speaker
                changed = True
                route_changed = True
                if old:
                    self.warning = (f"Listening output disappeared: {old}; "
                                    f"switched to {fallback_speaker or 'none'}")
                    print(f"[audio] {self.warning}", file=sys.stderr)
        if self.capture_system and not self.discovery["blackhole"]:
            self.capture_system = False
            restart = not initial
            changed = True
            route_changed = True
            self.warning = "BlackHole disappeared; system-audio capture was turned off"
            print(f"[audio] {self.warning}", file=sys.stderr)
        if not self.microphone:
            raise RuntimeError("no usable microphone detected")
        if initial or changed:
            self.persist()
        if restart:
            await self.restart_streams(refresh=True)
        if route_changed and not initial:
            self._sync_output_route()
        if notify:
            await self.notify()

    async def apply(self, microphone: str | None, capture_system: bool,
                    speaker: str | None) -> None:
        await self.scan(notify=False)
        restart = False
        route_changed = False
        if microphone in self.discovery["microphones"] and microphone != self.microphone:
            self.microphone = microphone
            restart = True
        requested_system = bool(capture_system and self.discovery["blackhole"])
        if requested_system != self.capture_system:
            self.capture_system = requested_system
            restart = True
            route_changed = True
        if speaker in self.discovery["outputs"] and speaker != self.speaker:
            self.speaker = speaker
            route_changed = True
        self.panel_seen = True
        self.warning = ("BlackHole 2ch is not installed"
                        if capture_system and not self.discovery["blackhole"] else "")
        self.persist()
        if restart:
            try:
                await self.restart_streams(refresh=True)
            except Exception as e:
                failed = self.microphone
                fallback, _ = self._fallbacks()
                if not fallback or fallback == failed:
                    raise
                self.microphone = fallback
                self.warning = f"Cannot open {failed}; switched to {fallback}: {e}"
                self.persist()
                await self.restart_streams(refresh=True)
        if route_changed:
            self._sync_output_route()
        await self.notify()

    async def mark_seen(self) -> None:
        self.panel_seen = True
        self.persist()
        await self.notify()

    def _close_streams(self) -> None:
        for stream in (self.mic_stream, self.system_stream):
            if stream is None:
                continue
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        self.mic_stream = self.system_stream = None
        self.system_chunks.clear()

    @staticmethod
    def _portaudio_input(sd, name: str) -> tuple[int, int]:
        for index, info in enumerate(sd.query_devices()):
            if str(info["name"]) == name and int(info["max_input_channels"]) > 0:
                return index, int(info["max_input_channels"])
        raise RuntimeError(f"input device is unavailable to PortAudio: {name}")

    def _callback(self, source: str, channels: int):
        def callback(indata, frames, time_info, status):
            data = bytes(indata)
            if channels > 1:
                data = downmix_to_mono(data, channels)
            if status:
                print(f"[audio] {source} stream status: {status}", file=sys.stderr)
            handler = self._receive_mic if source == "microphone" else self._receive_system
            self.loop.call_soon_threadsafe(handler, data)
        return callback

    def _receive_system(self, data: bytes) -> None:
        if self.paused:
            return
        if len(self.system_chunks) >= 2:  # 400ms ceiling at the 200ms block size
            self.system_chunks.pop(0)
            self.stats["system_drop"] += 1
        self.system_chunks.append(data)

    def _receive_mic(self, data: bytes) -> None:
        if self.paused:
            return
        if self.capture_system and self.system_stream is not None:
            if self.system_chunks:
                data = mix_pcm_s16(data, self.system_chunks.pop(0))
            else:
                self.stats["system_underflow"] += 1
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(data)
            except Exception:
                pass
            self.stats["queue_drop"] += 1
            if self.stats["queue_drop"] % 50 == 1:
                print(f"[audio] queue full, dropped oldest chunks: "
                      f"{self.stats['queue_drop']}", file=sys.stderr)

    async def restart_streams(self, refresh: bool) -> None:
        import sounddevice as sd

        async with self.lock:
            self._close_streams()
            if refresh:
                sd._terminate()
                sd._initialize()
            mic_index, mic_channels = self._portaudio_input(sd, self.microphone)
            self.mic_stream = sd.RawInputStream(
                device=mic_index, samplerate=SAMPLE_RATE,
                blocksize=SAMPLE_RATE * CHUNK_MS // 1000,
                dtype="int16", channels=mic_channels,
                callback=self._callback("microphone", mic_channels),
            )
            self.mic_stream.start()
            if self.capture_system and self.discovery["blackhole"]:
                try:
                    system_index, system_channels = self._portaudio_input(
                        sd, self.discovery["blackhole"]
                    )
                    self.system_stream = sd.RawInputStream(
                        device=system_index, samplerate=SAMPLE_RATE,
                        blocksize=SAMPLE_RATE * CHUNK_MS // 1000,
                        dtype="int16", channels=system_channels,
                        callback=self._callback("system", system_channels),
                    )
                    self.system_stream.start()
                except Exception as e:
                    self.capture_system = False
                    self.warning = f"Cannot open BlackHole; system-audio capture is off: {e}"
                    self.persist()
            print(f"[audio] microphone={self.microphone} ({mic_channels}ch), "
                  f"system={'on' if self.system_stream else 'off'}")

    async def _reopen(self) -> None:
        """Reopen the streams after one stopped. A Bluetooth mic (AirPods)
        vanishes together with its stream, so reopening the same name can
        raise; clear it and let scan() fall back to another microphone
        instead of killing the pipeline."""
        try:
            await self.restart_streams(refresh=True)
        except Exception as e:
            print(f"[audio] reopen failed: {e}", file=sys.stderr)
            self.microphone = None
            await self.scan()

    async def run(self) -> None:
        import sounddevice as sd

        self.loop = asyncio.get_running_loop()
        sd.query_devices()  # initializes CoreAudio before native enumeration
        try:
            await self.scan(initial=True)
            await self.restart_streams(refresh=False)
            self._sync_output_route()
            await self.notify()
            while True:
                await asyncio.sleep(0.5)
                if not self.mic_stream or not self.mic_stream.active:
                    self.warning = "Microphone stream stopped; reopening"
                    await self._reopen()
                    await self.notify()
                elif (self.capture_system
                      and (not self.system_stream or not self.system_stream.active)):
                    self.warning = "System-audio stream stopped; reopening"
                    await self._reopen()
                    await self.notify()
                now = time.monotonic()
                if now - self.last_scan >= 3:
                    self.last_scan = now
                    await self.scan()
                if now - self.last_stats >= 60:
                    print(f"[audio] mixer counters: {self.stats}")
                    self.last_stats = now
        finally:
            self._sync_output_route(enabled=False)
            self._close_streams()


async def wav_chunks(path: str, queue: asyncio.Queue) -> None:
    """Feed a 16kHz mono s16le wav file in real time, then send None."""
    with wave.open(path, "rb") as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise ValueError("wav must be 16kHz mono 16-bit; convert with: "
                             "ffmpeg -i in.wav -ar 16000 -ac 1 -c:a pcm_s16le out.wav")
        while True:
            data = wf.readframes(CHUNK_BYTES // 2)
            if not data:
                break
            await queue.put(data)
            await asyncio.sleep(CHUNK_MS / 1000)  # real-time pacing
    await queue.put(None)


async def chunk_iterator(queue: asyncio.Queue):
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


def _append_jsonl(path: str, record: dict) -> None:
    """Synchronous file append, meant for asyncio.to_thread so a stalling
    File Provider (OneDrive) never blocks the event loop."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def lan_ip() -> str:
    import socket
    import subprocess

    for iface in ("en0", "en1"):  # macOS: prefer the real Wi-Fi interface
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=2)
            ip = out.stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def maybe_tunnel(port: int):
    """Start a cloudflared quick tunnel if the binary exists; return
    (proc, public_url). Quick tunnels need no account or config."""
    import shutil

    exe = shutil.which("cloudflared")
    if not exe:
        return None, None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            for _ in range(60):
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
                if not line:
                    break
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              line.decode(errors="ignore"))
                if m:
                    return proc, m.group(0)
        except asyncio.CancelledError:
            proc.terminate()  # Ctrl-C during tunnel setup: no orphan
            raise
        except Exception:
            pass
        proc.terminate()  # no URL found in time: never leave an orphan tunnel
    except Exception:
        return None, None
    return None, None


async def run(args) -> None:
    if not args.no_ui and reopen_existing_session(args.port):
        return

    glossary = Glossary.load(args.glossary)
    # Merge hotword files: English/proper nouns become identity translation
    # terms (kept as-is, e.g. "Meepo"), Chinese entries only feed ASR.
    # ASR hotword priority comes from #priority sections; glossary-only terms
    # are the lowest tier, so the 100-token cap preserves curated names.
    file_entries = load_hotwords_dir(args.hotwords_dir)
    file_words = [word for word, _priority in file_entries]
    sources = {t["source"] for t in glossary.terms}
    for word in file_words:
        if word not in sources:
            if not CJK_RE.search(word):
                glossary.terms.append({"source": word, "target": word})
            sources.add(word)
    priorities: dict[str, int] = {}
    for word, priority in file_entries + [(t["source"], -1) for t in glossary.terms]:
        priorities[word] = max(priority, priorities.get(word, -1))
    hotwords_all = list(priorities)

    # Split hotwords into two channels: direct transmission (cap 100 tokens,
    # for table-incompatible entries and English proper nouns) and a
    # 自学习平台 boosting table (thousands of entries, but strict format:
    # <10 chars, no punctuation, digits written as Chinese characters).
    DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")

    def table_form(word: str) -> str | None:
        w = word.translate(DIGITS)
        if re.search(r"[^\w一-鿿]", w):  # punctuation/symbols not allowed
            return None
        if len(w) >= 10 or len(w.encode("utf-8")) > 30:
            return None
        return w

    table_words: list[str] = []
    incompatible: list[str] = []
    for w in hotwords_all:
        tf = table_form(w)
        if tf is None:
            incompatible.append(w)
        else:
            table_words.append(tf)
    direct_candidates = list(dict.fromkeys(
        incompatible + [w for w in hotwords_all
                        if table_form(w) is not None and not CJK_RE.search(w)]
    ))
    direct, dropped, direct_tokens = trim_hotwords(
        [(word, priorities[word]) for word in direct_candidates]
    )
    if dropped:
        print(f"[hotwords] direct budget: {direct_tokens}/100 estimated tokens "
              f"across {len(direct)} terms; dropped {len(dropped)} terms after "
              f"priority ordering: {', '.join(dropped)}")
    table_words = list(dict.fromkeys(table_words))
    table_path = os.path.join(args.hotwords_dir, "boosting_table.txt")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write("\n".join(table_words) + "\n")
    boosting_table_id = os.environ.get("VOLC_BOOSTING_TABLE_ID")
    if not boosting_table_id:
        print(f"[hotwords] {len(table_words)} table-format words at "
              f"{os.path.normpath(table_path)}; upload in speech console 自学习平台 "
              f"and set VOLC_BOOSTING_TABLE_ID to activate zh hotwords")

    # ASR correction map: fix systematic mishearings (e.g. MATE -> Matt,
    # CKC -> CKCon) client-side before display and translation. Rebuildable
    # at runtime: the UI can push new corrections mid-meeting.
    corr_map = {k.lower(): v for k, v in glossary.corrections.items()}
    new_corrections: dict = {}  # pushed via UI this session, persisted on exit

    def build_corr_re():
        if not corr_map:
            return None
        patterns = []
        for wrong in sorted(corr_map, key=len, reverse=True):
            pat = re.escape(wrong)
            if wrong.isascii():
                # \b fails at CJK<->latin boundaries (Python \w matches CJK),
                # so bound by non-alphanumeric lookarounds instead
                pat = rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"
            patterns.append(pat)
        return re.compile("|".join(patterns), re.IGNORECASE)

    corr = {"re": build_corr_re()}

    def correct(text: str) -> str:
        if not corr["re"]:
            return text
        return corr["re"].sub(lambda m: corr_map[m.group(0).lower()], text)

    if args.translator == "volc-mt" and not os.environ.get("VOLC_ASR_API_KEY"):
        sys.exit(
            "volc-mt 后端需要 VOLC_ASR_API_KEY(语音控制台 API Key)。\n"
            "The volc-mt backend requires VOLC_ASR_API_KEY from the speech "
            "console. With old-console app credentials, use --translator ark."
        )
    translator = build_translator(args.translator, glossary, model=args.model)

    ui: CaptionUI | None = None
    tunnel_proc = None
    share_task: asyncio.Task | None = None
    session_registered = False
    if not args.no_ui:
        # with --share the page is served only under a random token path
        share_token = secrets.token_urlsafe(6) if args.share else None
        suffix = f"/{share_token}" if share_token else ""
        ui = CaptionUI(host="0.0.0.0" if args.share else "127.0.0.1",
                       port=args.port, token=share_token)
        try:
            await ui.start()
        except OSError as e:
            if reopen_existing_session(args.port):
                return
            raise SystemExit(
                f"[ui] 字幕端口 {args.port} 已被占用，mBabel 未启动。\n"
                f"[ui] Caption port {args.port} is unavailable; mBabel did not "
                f"start. Use --port to choose another port. ({e})"
            ) from e
        url = f"http://127.0.0.1:{ui.port}{suffix}"
        try:
            register_session(ui.port, url)
            session_registered = True
        except OSError as e:
            raise SystemExit(
                f"[ui] 无法登记当前会话，mBabel 未启动。\n"
                f"[ui] Cannot register this session; mBabel did not start. ({e})"
            ) from e
        print(f"[ui] captions at {url}")
        print(f"[control] Lab token: {ui.control_token} "
              f"(the local page receives it automatically; never shared)")
        if args.share:
            lan_url = f"http://{lan_ip()}:{ui.port}{suffix}"
            print(f"[share] LAN link: {lan_url}")
            await ui.set_share(lan_url, None)

            async def _tunnel_later() -> None:
                # cloudflared can take >10s to hand out a URL; never block
                # startup or the ASR connection on it
                nonlocal tunnel_proc
                proc, public_url = await maybe_tunnel(ui.port)
                tunnel_proc = proc
                if public_url:
                    public_url = public_url + suffix
                    print(f"[share] public link: {public_url}  (cloudflared quick tunnel)")
                    if ui:
                        await ui.set_share(lan_url, public_url)
                else:
                    print("[share] no public link (cloudflared unavailable or timed out)")

            share_task = asyncio.create_task(_tunnel_later())
        try:
            webbrowser.open(url)
        except OSError as e:
            print(f"[ui] cannot open browser; open {url} manually ({e})")
    asr_key = os.environ.get("VOLC_ASR_API_KEY")
    app_key = os.environ.get("VOLC_ASR_APP_KEY")
    access_key = os.environ.get("VOLC_ASR_ACCESS_KEY")
    if not asr_key and not (app_key and access_key):
        sys.exit(
            "ASR credentials are not set. Either create an access-control API Key "
            "whose scope includes 语音技术 and set VOLC_ASR_API_KEY, or create an app "
            "in the speech console (old console) and set VOLC_ASR_APP_KEY + "
            "VOLC_ASR_ACCESS_KEY (APP ID + Access Token)."
        )
    asr = VolcAsrClient(
        AsrConfig(
            api_key=asr_key,
            app_key=app_key,
            access_key=access_key,
            hotwords=direct,
            boosting_table_id=boosting_table_id,
            end_window_size=args.end_window,
        )
    )

    # Refined translation engine (ark with sentence context since W1); the
    # volc-mt translator stays as fallback when ark credentials are absent.
    ark_polisher = None
    try:
        ark_polisher = ArkTranslator(
            glossary, model=os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215")
        )
    except Exception:
        print("[translate] ark backend unavailable; refined falls back to volc-mt")

    def stats_snapshot() -> dict:
        now = time.time()
        return {k: sum(1 for t in dq if now - t < 60)
                for k, dq in req_stats.items()}

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    audio = (None if args.wav else AudioMixer(queue, args.audio_config))
    if audio and ui:
        audio.on_state = ui.emit_control
    paused = {"v": False}
    resume_event = asyncio.Event()
    resume_event.set()

    async def on_control(msg: dict) -> None:
        """Control messages from the UI (token-guarded in ui_server)."""
        kind = msg.get("type")
        if kind == "correction":
            wrong = (msg.get("wrong") or "").strip()
            right = (msg.get("right") or "").strip()
            if wrong and right and wrong.lower() != right.lower():
                corr_map[wrong.lower()] = right
                new_corrections[wrong] = right
                corr["re"] = build_corr_re()
                print(f"[corrections] {wrong} -> {right}")
                if ui:
                    await ui.emit({"type": "status", "text": f"correction: {wrong} → {right}"})
        elif kind == "ddc":
            enabled = bool(msg.get("enabled"))
            if asr.config.enable_ddc != enabled:
                asr.config.enable_ddc = enabled
                print(f"[asr] enable_ddc={enabled}; restarting session")
                if ui:
                    await ui.emit({"type": "status",
                                   "text": f"ddc {'on' if enabled else 'off'}, ASR restarting…"})
                await asr.close()
        elif kind == "pause":
            requested = bool(msg.get("paused"))
            if requested != paused["v"]:
                if requested:
                    paused["v"] = True
                    if audio:
                        audio.paused = True
                        audio.system_chunks.clear()
                    resume_event.clear()
                    # speech spoken BEFORE the pause stays on record: settle
                    # the live line now — closing the session means its
                    # definite re-recognition will never arrive
                    if not line_parts and last_live_line["text"]:
                        line_parts.append(last_live_line["text"])
                    if line_parts:
                        await flush_line()
                    replay_audio.clear()
                    sent_audio_tail.clear()
                    reconnect_partial["text"] = ""
                    dropped = discard_queued_audio(queue)
                    print(f"[asr] paused; discarded {dropped} queued audio chunks")
                    await asr.close()
                else:
                    dropped = discard_queued_audio(queue)
                    if audio:
                        audio.system_chunks.clear()
                        audio.paused = False
                    paused["v"] = False
                    resume_event.set()
                    print(f"[asr] resuming; discarded {dropped} paused audio chunks")
                if ui:
                    await ui.emit({"type": "status",
                                   "text": "paused" if requested else "ASR resuming…"})
            if ui:
                await ui.emit_control({"type": "pause_state", "paused": paused["v"]})
        elif kind == "pause_get":
            if ui:
                await ui.emit_control({"type": "pause_state", "paused": paused["v"]})
        elif kind == "stats":
            if ui:
                await ui.emit({"type": "stats", **stats_snapshot()})
        elif kind == "devices_get" and audio and ui:
            await ui.emit_control(audio.snapshot())
        elif kind == "devices_seen" and audio:
            await audio.mark_seen()
        elif kind == "audio_settings" and audio:
            await audio.apply(msg.get("microphone"),
                              bool(msg.get("capture_system")),
                              msg.get("speaker"))

    if ui:
        ui.on_control = on_control

    run_task = asyncio.current_task()
    assert run_task is not None
    audio_failure = {"error": None}
    producer = (
        asyncio.create_task(wav_chunks(args.wav, queue))
        if args.wav
        else asyncio.create_task(audio.run())
    )
    def audio_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            audio_failure["error"] = error
            print(f"[audio] input failed: {error}", file=sys.stderr)
            run_task.cancel()

    producer.add_done_callback(audio_done)

    committed: set[tuple[int, int, str]] = set()
    translate_tasks: list[asyncio.Task] = []
    draft_tasks: list[asyncio.Task] = []  # cancelled on flush: finals have priority
    cache: dict[str, str] = {}
    recent_context: deque[dict] = deque(maxlen=4)
    last_committed = {"text": ""}
    last_live_line = {"text": ""}
    reconnect_partial = {"text": ""}
    replay_guard = {"until": 0.0, "remaining": 0, "previous": ""}
    session_t0 = time.time()
    seg_seq = 0
    draft_seq = 0
    last_draft = {"text": "", "time": 0.0}
    last_draft_emit = {"seq": 0}
    # Sentence accumulator: definite ASR fragments are buffered until a
    # sentence-final punctuation or a length cap, then committed as ONE
    # line and translated ONCE. This avoids fragmenting natural speech
    # (mid-sentence pauses) into tiny cards and flooding the translator.
    line_parts: list[str] = []
    line_speaker: dict = {"id": None}  # speaker_id of the current live line
    line_misrec = {"v": False}  # lid said English but the text came out Chinese
    FINAL_PUNCT = ("。", "！", "？", ".", "!", "?", "…")
    FILLER_RE = re.compile(r"^[嗯哦呃啊唉哼哈唔呐吧嘛呀哎\s,.，。！？!?…~～]+$")
    MAX_LINE_CHARS = 200
    FLUSH_SILENCE_S = 2.0  # flush the live line after this much ASR quiet time
    last_activity = {"t": 0.0}

    draft_sem = asyncio.Semaphore(3)  # drafts never starve the refined pass
    fail_stats: dict = {}
    # request counters for the Lab stats line (60s sliding window per channel)
    req_stats = {"volc_draft": deque(), "volc_refined": deque(), "ark": deque(),
                 "ratelimit": deque()}
    last_draft_result = {"text": "", "source": "", "src": "", "time": 0.0}

    async def draft_translate(text: str, seq: int) -> None:
        """Translate the still-growing sentence for a live draft. Any draft
        newer than what is on screen is shown, even if a newer one is
        already in flight; this keeps the draft cadence at translation
        speed instead of collapsing to sentence end."""
        try:
            src, tgt = detect_direction(text)
            req_stats["volc_draft"].append(time.time())
            async with draft_sem:
                translated = await translator.translate(text, src, tgt, lite=True)
        except Exception:
            return
        last_draft_result.update(
            text=translated, source=text, src=src, time=time.time()
        )
        if ui and seq > last_draft_emit["seq"]:
            last_draft_emit["seq"] = seq
            await ui.emit({"type": "draft", "text": translated, "lang": src})

    async def commit(text: str, seg_id: int, speaker: str | None,
                     misrec: bool = False, draft_snap: dict | None = None) -> None:
        src, tgt = detect_direction(text)
        arrow = "中→英" if src == "zh" else "EN→中"
        ts = round(time.time() - session_t0, 1)
        if ui:
            await ui.emit({"type": "committed", "id": seg_id, "lang": src,
                           "source": text, "speaker": speaker, "ts": ts,
                           "misrec": misrec})

        # W1 draft promotion: a sufficiently complete draft of THIS sentence
        # becomes the ≈ provisional translation until the refined pass lands.
        provisional = None
        ld = draft_snap or {"text": "", "source": "", "src": "", "time": 0.0}
        if (ld["text"] and ld["src"] == src
                and time.time() - ld["time"] < 3.0
                and len(ld.get("source", "")) / len(text) >= 0.6):
            provisional = ld["text"]
            if ui:
                await ui.emit({"type": "translation", "id": seg_id,
                               "text": provisional, "provisional": True})

        ctx = [dict(item) for item in recent_context]
        context_entry = {"seq": seg_id, "source_lang": src, "source": text,
                         "translation": provisional or ""}
        recent_context.append(context_entry)
        # refined pass: ark with the previous sentences as context (default
        # since W1); volc-mt stays as fallback when ark credentials are absent
        translated = None
        if text in cache:
            translated = cache[text]
        else:
            engine = ark_polisher if ark_polisher is not None else translator
            waits = [1, 3]
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(waits[attempt - 1])
                try:
                    req_stats["ark" if engine is ark_polisher else "volc_refined"].append(time.time())
                    if engine is ark_polisher:
                        translated = await engine.translate(text, src, tgt, context=ctx)
                    else:
                        translated = await engine.translate(text, src, tgt)
                    break
                except Exception as e:
                    key = type(e).__name__
                    fail_stats[key] = fail_stats.get(key, 0) + 1
                    if key == "RateLimitError":
                        req_stats["ratelimit"].append(time.time())
                    print(f"[translate] attempt {attempt + 1} failed: {e} "
                          f"(totals: {fail_stats})", file=sys.stderr)
                    if key == "RateLimitError":
                        waits = [5, 15]
            if translated is None:
                # both engines failed: the provisional draft is still a
                # readable line; schedule quiet backfills
                translated = provisional or "⚠ 翻译失败 translation unavailable"
                translate_tasks.append(
                    asyncio.create_task(
                        backfill(text, seg_id, src, tgt, ctx, context_entry)
                    )
                )
            else:
                cache[text] = translated  # never cache the failure placeholder
        if not translated.startswith("⚠ "):
            context_entry["translation"] = translated
        if ui and translated != provisional:
            await ui.emit({"type": "translation", "id": seg_id, "text": translated})
        elif ui and provisional is None:
            await ui.emit({"type": "translation", "id": seg_id, "text": translated})
        record = {"seq": seg_id, "speaker": speaker, "lang": src,
                  "source": text, "translation": translated,
                  "ts": ts, "time": time.strftime("%H:%M:%S")}
        transcript_records.append(record)
        await asyncio.to_thread(_append_jsonl, jsonl_path, record)
        sys.stdout.write(
            f"{CLEAR_LINE}{DIM}[{arrow}] {text}{RESET}\n{BOLD}{translated}{RESET}\n"
        )
        sys.stdout.flush()

    async def backfill(text: str, seg_id: int, src: str, tgt: str,
                     ctx: list[dict], context_entry: dict) -> None:
        """A failed refined translation is retried quietly every 15s for up
        to ~3 minutes, same engine as the refined pass (ark with context
        when available); on success the line updates in place."""
        engine = ark_polisher if ark_polisher is not None else translator
        for _ in range(12):
            await asyncio.sleep(15)
            try:
                if engine is ark_polisher:
                    fixed = await engine.translate(text, src, tgt, context=ctx)
                else:
                    fixed = await engine.translate(text, src, tgt)
            except Exception:
                continue
            cache[text] = fixed
            context_entry["translation"] = fixed
            if ui:
                await ui.emit({"type": "translation", "id": seg_id, "text": fixed})
            for r in transcript_records:
                if r["seq"] == seg_id:
                    r["translation"] = fixed
            await asyncio.to_thread(_append_jsonl, jsonl_path,
                                    {"seq": seg_id, "backfill": fixed})
            return

    async def flush_line() -> None:
        nonlocal seg_seq
        text = "".join(line_parts).strip()
        speaker = line_speaker["id"]
        misrec = line_misrec["v"]
        line_parts.clear()
        last_live_line["text"] = ""
        line_speaker["id"] = None
        line_misrec["v"] = False
        for task in draft_tasks:  # stale drafts are obsolete once a line commits
            task.cancel()
        draft_tasks.clear()
        # snapshot the draft that belongs to THIS line and clear the slot:
        # a short next sentence committing within 3s must never promote the
        # previous sentence's draft as its own provisional translation
        draft_snap = dict(last_draft_result)
        last_draft_result.update(text="", source="", src="", time=0.0)
        last_draft.update(text="")
        if not text or FILLER_RE.match(text):
            return  # drop pure fillers (嗯/哦/呃…) entirely
        seg_seq += 1
        last_committed["text"] = text
        translate_tasks.append(
            asyncio.create_task(commit(text, seg_seq, speaker, misrec, draft_snap)))

    async def silence_watchdog() -> None:
        """Flush the live line when the ASR has been quiet for a while; real
        speech rarely ends with clean sentence-final punctuation."""
        while True:
            await asyncio.sleep(0.5)
            if (not paused["v"] and line_parts
                    and asyncio.get_running_loop().time() - last_activity["t"] > FLUSH_SILENCE_S):
                await flush_line()

    watchdog = asyncio.create_task(silence_watchdog())

    # Transcript auto-save: every committed line is appended as JSONL, and a
    # Markdown rendering is written on exit. Export from the UI is now a
    # convenience, not the only copy.
    transcript_records: list[dict] = []
    transcript_dir = os.path.join(os.path.dirname(__file__), "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    jsonl_path = os.path.join(transcript_dir, f"babel-{stamp}.jsonl")

    reconnects = 0
    ever_connected = False
    announced_connected = True  # flips False while disconnected
    sent_audio_tail = deque(maxlen=RECONNECT_TAIL_CHUNKS)
    replay_audio: list[bytes] = []

    async def reconnect_chunks():
        while replay_audio:
            chunk = replay_audio.pop(0)
            sent_audio_tail.append(chunk)
            yield chunk
        async for chunk in chunk_iterator(queue):
            sent_audio_tail.append(chunk)
            yield chunk

    async def wait_while_paused() -> None:
        if not paused["v"]:
            return
        while paused["v"]:
            discard_queued_audio(queue)
            try:
                await asyncio.wait_for(resume_event.wait(), 0.2)
            except asyncio.TimeoutError:
                pass
        discard_queued_audio(queue)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, run_task.cancel)
    try:
        while True:  # ASR reconnect loop: survive network blips mid-meeting
            await wait_while_paused()
            got_last = False
            sent_audio_tail.clear()
            try:
                async for event in asr.transcribe(reconnect_chunks()):
                    if not ever_connected:
                        ever_connected = True
                    if not announced_connected:
                        announced_connected = True
                        replay_guard.update(
                            until=loop.time() + REPLAY_GUARD_SECONDS,
                            remaining=REPLAY_GUARD_FRAGMENTS,
                            previous=last_committed["text"],
                        )
                        if ui:
                            await ui.emit({"type": "status", "text": "connected"})
                    if event.text or event.utterances:
                        last_activity["t"] = asyncio.get_running_loop().time()
                    packet_seg_seq = seg_seq
                    for utt in event.utterances:
                        key = (utt.get("start_time", 0), utt.get("end_time", 0),
                               (utt.get("text") or "")[:16])
                        if utt.get("definite") and key not in committed and utt.get("text"):
                            committed.add(key)
                            frag = correct(utt["text"])
                            if (replay_guard["remaining"] > 0
                                    and loop.time() < replay_guard["until"]):
                                replay_guard["remaining"] -= 1
                                if is_replay_duplicate(frag, replay_guard["previous"]):
                                    print(f"[asr] skipped replayed fragment after reconnect: {frag}")
                                    continue
                            if reconnect_partial["text"]:
                                frag = merge_reconnect_partial(
                                    reconnect_partial["text"], frag
                                )
                                reconnect_partial["text"] = ""
                            speaker = (utt.get("additions") or {}).get("speaker_id")
                            # a speaker change is a natural turn boundary
                            if (speaker is not None and line_speaker["id"] is not None
                                    and speaker != line_speaker["id"] and line_parts):
                                await flush_line()
                            # language-boundary split, also inside a single fragment:
                            # a speaker can switch languages mid-utterance
                            lid = (utt.get("additions") or {}).get("lid_lang")
                            for piece_lang, piece in split_lang_runs(frag):
                                # lid says English speech but the text came out
                                # Chinese: LLM-ASR translated instead of
                                # transcribing. Mark the line as suspect.
                                if lid == "speech_en" and piece_lang == "zh":
                                    line_misrec["v"] = True
                                if (line_parts and len(piece.strip()) >= 6
                                        and piece_lang != dominant_lang("".join(line_parts))):
                                    await flush_line()
                                if not line_parts:
                                    line_speaker["id"] = speaker
                                line_parts.append(piece)
                                current = "".join(line_parts).strip()
                                if (current.endswith(FINAL_PUNCT)
                                        or len(current) >= MAX_LINE_CHARS
                                        or should_flush_clause(current, piece)):
                                    await flush_line()
                    # result.text can repeat a definite utterance from this
                    # same ASR packet. Once that utterance committed, treating
                    # the repeated text as a new partial launches a stale draft
                    # after the committed event has already cleared the strip.
                    packet_committed = seg_seq != packet_seg_seq
                    if line_parts or (event.text and not packet_committed):
                        live_text = "" if packet_committed else correct(event.text)
                        if reconnect_partial["text"] and not line_parts:
                            line = merge_reconnect_partial(
                                reconnect_partial["text"], live_text
                            )
                        else:
                            line = "".join(line_parts) + live_text
                        last_live_line["text"] = line.strip()
                        text = line.strip()
                        now = asyncio.get_running_loop().time()
                        # Draft budget: QPM is per-account and reserved for
                        # drafts. Fire at most every 0.6s, and skip when the
                        # line grew by <6 chars in <2s (nothing new to say).
                        # While rate-limited, slow further to 2.5s.
                        streak = getattr(translator, "fail_streak", 0)
                        debounce = 2.5 if streak >= 2 else 0.6
                        grown = len(text) - len(last_draft["text"])
                        due = now - last_draft["time"]
                        if (ui and len(text) >= 4 and text != last_draft["text"]
                                and (grown >= 6 or due >= 2.0)
                                and due >= debounce):
                            last_draft.update(text=text, time=now)
                            draft_seq += 1
                            draft_tasks.append(
                                asyncio.create_task(draft_translate(text, draft_seq))
                            )
                        if ui:
                            await ui.emit({"type": "interim", "text": line})
                        if args.no_ui and event.text:
                            sys.stdout.write(f"{CLEAR_LINE}{DIM}… {line}{RESET}")
                            sys.stdout.flush()
                    if event.is_last:
                        got_last = True
                        break
            except InvalidStatus as e:
                # handshake rejected (401/403): bad key or service not
                # activated — reconnecting would loop forever, so exit
                sys.exit(f"[asr] handshake rejected (HTTP {e.response.status_code}); "
                         f"check VOLC_ASR_API_KEY and service activation")
            except (AsrError, ConnectionClosedError, OSError) as e:
                if paused["v"]:
                    pass
                elif not ever_connected:
                    # failing before the first event (auth, params) is fatal,
                    # not a reconnect case
                    sys.exit(f"[asr] failed before any result: {e}")
                else:
                    print(f"\n[asr] connection problem: {e}", file=sys.stderr)
            if paused["v"]:
                for task in draft_tasks:
                    task.cancel()
                draft_tasks.clear()
                line_parts.clear()
                last_live_line["text"] = ""
                reconnect_partial["text"] = ""
                last_draft.update(text="")
                last_draft_result.update(text="", source="", src="", time=0.0)
                replay_audio.clear()
                sent_audio_tail.clear()
                committed.clear()
                announced_connected = False
                continue
            if got_last and args.wav:
                break
            reconnects += 1
            announced_connected = False
            if last_live_line["text"]:
                reconnect_partial["text"] = last_live_line["text"]
                line_parts.clear()
                print(f"[asr] preserved interim across reconnect: "
                      f"{reconnect_partial['text']}", file=sys.stderr)
            # Keep the newest three seconds so a network flap does not eat the
            # sentence crossing the disconnect. The bounded replay guard above
            # absorbs a near-duplicate of the last sentence if ASR sees it again.
            replay_audio, dropped = collect_reconnect_tail(sent_audio_tail, queue)
            print(f"[asr] reconnect audio tail: kept {len(replay_audio)} chunks, "
                  f"dropped {dropped}", file=sys.stderr)
            committed.clear()
            delay = min(2 * reconnects, 10)
            print(f"[asr] reconnecting in {delay}s (attempt {reconnects})", file=sys.stderr)
            if ui:
                await ui.emit({"type": "status", "text": f"ASR reconnecting… ({reconnects})"})
            await asyncio.sleep(delay)
        await flush_line()
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        if audio_failure["error"] is not None and ui:
            await ui.emit({"type": "status", "text":
                           f"audio input failed: {audio_failure['error']}"})
            await asyncio.sleep(0)
    finally:
        if reconnect_partial["text"] and not line_parts:
            line_parts.append(reconnect_partial["text"])
            reconnect_partial["text"] = ""
        await flush_line()  # Ctrl-C must not drop the line still accumulating
        producer.cancel()
        watchdog.cancel()
        cleanup_tasks = [producer, watchdog]
        if share_task:
            share_task.cancel()  # terminates an in-flight cloudflared too
            cleanup_tasks.append(share_task)
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        if tunnel_proc:
            tunnel_proc.terminate()
        for task in draft_tasks:
            task.cancel()
        if translate_tasks:
            await asyncio.gather(*translate_tasks, return_exceptions=True)
        if new_corrections:
            # merge into the on-disk glossary without dumping runtime-added
            # identity terms back into the curated file
            try:
                with open(args.glossary, encoding="utf-8") as f:
                    disk = json.load(f)
                disk.setdefault("corrections", {}).update(new_corrections)
                with open(args.glossary, "w", encoding="utf-8") as f:
                    json.dump(disk, f, ensure_ascii=False, indent=2)
                print_safe(f"[corrections] persisted {len(new_corrections)} to glossary.json")
            except OSError as e:
                print_safe(f"[corrections] cannot persist: {e}", file=sys.stderr)
        if transcript_records:
            md_path = jsonl_path.replace(".jsonl", ".md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"# Babel transcript {stamp}\n\n")
                for r in transcript_records:
                    who = (f"Speaker {int(r['speaker']) + 1}"
                           if r["speaker"] is not None and str(r["speaker"]).isdigit()
                           else r["speaker"] or "")
                    when = r.get("time", "")
                    f.write(f"**{who}** ({r['lang']}) [{when}]: {r['source']}\n\n"
                            f"> {r['translation']}\n\n")
            print_safe(f"[transcript] saved {jsonl_path} and {md_path}")
        # per-feature token accounting for the session
        usage_report = {}
        if hasattr(translator, "usage"):
            u = translator.usage
            if isinstance(u, dict):
                for k, (p, c, n) in u.items():
                    usage_report[f"volc-mt/{k}"] = {"prompt": p, "completion": c, "calls": n}
        if ark_polisher is not None and getattr(ark_polisher, "usage", None):
            p, c, n = ark_polisher.usage
            usage_report["ark/polish"] = {"prompt": p, "completion": c, "calls": n}
        if usage_report:
            print_safe(f"[usage] {json.dumps(usage_report, ensure_ascii=False)}")
            await asyncio.to_thread(
                _append_jsonl,
                os.path.join(transcript_dir, "usage.jsonl"),
                {"stamp": stamp, **usage_report},
            )
        if session_registered:
            clear_session_file(args.port, expected_pid=os.getpid())
        print_safe()
    if audio_failure["error"] is not None:
        raise SystemExit(f"[audio] input failed: {audio_failure['error']}")


def main() -> None:
    audio_config = os.path.join(os.path.expanduser("~"), ".mbabel", "audio_config.json")
    parser = argparse.ArgumentParser(description="Live bilingual meeting captions")
    parser.add_argument("--wav", help="test with a 16kHz mono s16le wav file instead of the mic")
    parser.add_argument("--glossary", default=os.path.join(os.path.dirname(__file__), "glossary.json"))
    parser.add_argument("--translator", choices=["volc-mt", "ark", "qwen-mt"], default="volc-mt",
                        help="volc-mt: speech-product MT, same key as ASR (default); "
                             "ark: Doubao LLM on Ark; qwen-mt: Alibaba")
    parser.add_argument("--model", help="override the translation model id")
    parser.add_argument("--end-window", type=int, default=800,
                        help="ms of silence that closes an ASR fragment; sentences are "
                             "reassembled by the accumulator, so keep this moderate")
    parser.add_argument("--env", default=os.path.join(os.path.dirname(__file__), "..", ".env"),
                        help="path to the .env file with API credentials")
    parser.add_argument("--hotwords-dir", default=os.path.join(os.path.dirname(__file__), "..", "hotwords"),
                        help="directory with *.txt hotword files (merged with the glossary)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio input devices and exit")
    parser.add_argument("--audio-config",
                        default=audio_config,
                        help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8765,
                        help="caption UI port (default 8765); pick another to run "
                             "two instances side by side")
    parser.add_argument("--no-ui", action="store_true",
                        help="disable the browser caption UI (terminal output only)")
    parser.add_argument("--share", action="store_true",
                        help="share the caption page: print a LAN link, and a public "
                             "cloudflared quick-tunnel link when cloudflared is installed")
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    if args.audio_config == audio_config:
        migrate_audio_config(audio_config)
    load_dotenv(args.env)
    try:
        asyncio.run(run(args))
    finally:
        # Also covers setup failures after the UI binds but before the main
        # transcript-cleanup block is entered. PID matching cannot remove a
        # different live instance's registration.
        clear_session_file(args.port, expected_pid=os.getpid())


if __name__ == "__main__":
    main()
