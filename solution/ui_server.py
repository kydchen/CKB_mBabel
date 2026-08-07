"""Local caption UI: a two-pane web page served by the pipeline process.

One port serves both: plain HTTP GET returns the page (ui.html), WebSocket
upgrades push caption events. Same-origin WS keeps LAN/tunnel sharing simple.
With --share, a random token path is required for both HTTP and WS.

Events:

- {"type": "interim", "text": str}        live ASR hypothesis (originals pane)
- {"type": "draft", "text": str}          live rough translation
- {"type": "status", "text": str}         pipeline status (e.g. ASR reconnecting)
- {"type": "share", "lan": str, "public": str}
- {"type": "committed", "id": int, "lang": "zh"|"en", "source": str, "speaker": str|None}
- {"type": "translation", "id": int, "text": str}
"""

from __future__ import annotations

import asyncio
import json
import os

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
        self.clients: dict[asyncio.Queue, asyncio.Task] = {}
        self.history: list[dict] = []  # replayed to late-joining browsers
        self.share: dict = {}  # {"lan": url, "public": url}, sent to every client

    async def set_share(self, lan: str | None, public: str | None) -> None:
        self.share = {k: v for k, v in (("lan", lan), ("public", public)) if v}
        if self.share:
            await self.emit({"type": "share", **self.share})

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
                    Headers({"Content-Type": "text/html; charset=utf-8"}),
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
                for event in self.history:
                    await ws.send(json.dumps(event, ensure_ascii=False))
                self.clients[outbox] = task
                await ws.wait_closed()
            finally:
                self.clients.pop(outbox, None)
                task.cancel()

        self.server = await websockets.serve(
            ws_handler, self.host, self.port, process_request=process_request
        )

    async def emit(self, event: dict) -> None:
        if event["type"] in ("committed", "translation"):
            self.history.append(event)  # replayed to late-joining browsers
        msg = json.dumps(event, ensure_ascii=False)
        for outbox, task in list(self.clients.items()):
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                task.cancel()  # too slow: cut the client off
                self.clients.pop(outbox, None)
