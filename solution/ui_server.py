"""Local caption UI: a two-pane web page served by the pipeline process.

One port serves both: plain HTTP GET returns the page (ui.html), WebSocket
upgrades push caption events. Same-origin WS keeps LAN/tunnel sharing simple.
With --share, a random token path is required for both HTTP and WS.

Events:

- {"type": "interim", "text": str}        live ASR hypothesis (originals pane)
- {"type": "draft", "text": str}          live rough translation
- {"type": "status", "text": str}         pipeline status (e.g. ASR reconnecting)
- {"type": "share", "lan": str, "public": str}
- {"type": "pair_state", "pair": "zh-en"|"en-vi"|"zh-vi"}
- {"type": "devices", ...}                host-only audio device state
- {"type": "committed", "id": int, "lang": "zh"|"en"|"vi", "source": str, "speaker": str|None}
- {"type": "translation", "id": int, "text": str}
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

HTML_PATH = os.path.join(os.path.dirname(__file__), "ui.html")


class CaptionUI:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None):
        self.host = host
        self.port = port
        self.token = token
        self.clients: dict[asyncio.Queue, tuple] = {}  # outbox -> (sender task, ws)
        self.control_clients: set[asyncio.Queue] = set()
        self.history: list[dict] = []  # replayed to late-joining browsers
        self.share: dict = {}  # {"lan": url, "public": url}, sent to every client
        self.pair: str | None = None
        self.on_control = None  # async callback for UI control messages
        # Control token: required on every control message. It is printed to
        # the local terminal and injected into the page ONLY for direct
        # loopback connections — cloudflared forwards public traffic to
        # loopback, so source IP alone cannot prove "host" (audit F1).
        self.control_token = secrets.token_urlsafe(8)

    async def set_share(self, lan: str | None, public: str | None) -> None:
        self.share = {k: v for k, v in (("lan", lan), ("public", public)) if v}
        if self.share:
            await self.emit({"type": "share", **self.share})

    async def set_pair(self, pair: str) -> None:
        self.pair = pair
        await self.emit({"type": "pair_state", "pair": pair})

    async def start(self) -> None:
        html = open(HTML_PATH, encoding="utf-8").read().encode("utf-8")
        token = self.token

        def process_request(connection, request):
            if token and request.path != f"/{token}":
                return Response(404, "Not Found",
                                Headers({"Content-Type": "text/plain"}), b"404")
            upgrade = request.headers.get("Upgrade", "").lower()
            if upgrade != "websocket":
                return Response(
                    200,
                    "OK",
                    Headers({
                        "Content-Type": "text/html; charset=utf-8",
                        "Cache-Control": "no-store",  # never serve a stale UI
                    }),
                    html,
                )
            return None

        async def ws_handler(ws):
            # each client has a bounded outbox; a stalled client is dropped,
            # never allowed to backpressure the caption pipeline
            outbox: asyncio.Queue = asyncio.Queue(maxsize=100)

            async def sender():
                try:
                    while True:
                        await ws.send(await outbox.get())
                except Exception:
                    pass

            task = asyncio.create_task(sender())
            try:
                # replay directly with awaited sends: the outbox is bounded and
                # history can exceed it; blocking here only stalls this client's
                # own handshake, never the pipeline. seenIds dedupes any overlap
                # with live events once the outbox registers below.
                if self.share:
                    await ws.send(json.dumps(
                        {"type": "share", **self.share}, ensure_ascii=False))
                if self.pair:
                    await ws.send(json.dumps(
                        {"type": "pair_state", "pair": self.pair},
                        ensure_ascii=False))
                for event in self.history:
                    await ws.send(json.dumps(event, ensure_ascii=False))
                self.clients[outbox] = (task, ws)
                # control channel: corrections, toggles. A connection earns
                # the control token only if it is loopback AND not tunneled
                # (cloudflared marks forwarded traffic with Cf-* headers).
                host = (ws.remote_address or ("",))[0]
                req_headers = getattr(getattr(ws, "request", None), "headers", {})
                tunneled = "Cf-Connecting-Ip" in req_headers or "Cf-Ray" in req_headers
                if host in ("127.0.0.1", "::1") and not tunneled:
                    self.control_clients.add(outbox)
                    await ws.send(json.dumps(
                        {"type": "control_token", "token": self.control_token}))
                try:
                    async for raw in ws:
                        if not self.on_control:
                            continue
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if (isinstance(msg, dict)
                                and msg.get("token") == self.control_token):
                            await self.on_control(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass  # client gone (or force-closed as too slow); cleanup below
            finally:
                self.control_clients.discard(outbox)
                self.clients.pop(outbox, None)
                task.cancel()

        self.server = await websockets.serve(
            ws_handler, self.host, self.port, process_request=process_request
        )

    async def emit(self, event: dict) -> None:
        if event["type"] in ("committed", "translation"):
            self.history.append(event)  # replayed to late-joining browsers
        msg = json.dumps(event, ensure_ascii=False)
        for outbox, (task, ws) in list(self.clients.items()):
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                # too slow: actually CLOSE the connection (code 1013), so the
                # browser's onclose fires and its reconnect+replay path
                # catches the viewer back up. Just cancelling the sender
                # would leave an open-but-dead socket: frozen page, green dot.
                task.cancel()
                self.clients.pop(outbox, None)
                asyncio.create_task(ws.close(code=1013))

    async def emit_control(self, event: dict) -> None:
        """Push host controls only to direct loopback clients."""
        msg = json.dumps(event, ensure_ascii=False)
        for outbox in list(self.control_clients):
            client = self.clients.get(outbox)
            if not client:
                self.control_clients.discard(outbox)
                continue
            task, ws = client
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                task.cancel()
                self.control_clients.discard(outbox)
                self.clients.pop(outbox, None)
                asyncio.create_task(ws.close(code=1013))
