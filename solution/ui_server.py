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
- {"type": "replay_start"|"replay_end"}  batch history without per-event layout
- {"type": "replay", "events": [...], "truncated": bool}  one history frame
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
REPLAY_LIMIT = 300  # matches the browser's DOM_LIMIT


class CaptionUI:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 token: str | None = None):
        self.host = host
        self.port = port
        self.token = token
        self.clients: dict[asyncio.Queue, tuple] = {}  # outbox -> (sender task, ws)
        self.control_clients: set[asyncio.Queue] = set()
        self.viewer_ids: dict[asyncio.Queue, set[int]] = {}
        self.private = False
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
        if self.private:
            public = None
        self.share = {k: v for k, v in (("lan", lan), ("public", public)) if v}
        await self.emit({"type": "share", **self.share, "private": self.private})

    async def disconnect_viewers(self, code: int, reason: str) -> None:
        closing = []
        for outbox, (task, ws) in list(self.clients.items()):
            if outbox in self.control_clients:
                continue
            self.clients.pop(outbox, None)
            self.viewer_ids.pop(outbox, None)
            task.cancel()  # Also stop an initial replay already waiting to send.
            closing.extend((task, asyncio.create_task(ws.close(code=code, reason=reason))))
        await asyncio.gather(*closing, return_exceptions=True)

    async def set_private(self, enabled: bool) -> None:
        if self.private != enabled:
            self.private = enabled
            if enabled:
                self.share.pop("public", None)
            # Reconnect at the new replay policy, without interrupting hosts.
            await self.disconnect_viewers(1012, "Privacy mode changed")

    async def reset_link(self, token: str) -> None:
        self.token = token
        await self.disconnect_viewers(4001, "Link reset; request a new link")

    async def set_pair(self, pair: str) -> None:
        self.pair = pair
        await self.emit({"type": "pair_state", "pair": pair})

    async def start(self) -> None:
        html = open(HTML_PATH, encoding="utf-8").read().encode("utf-8")
        def process_request(connection, request):
            if ((self.token and request.path != f"/{self.token}")
                    or (self.private and ("Cf-Connecting-Ip" in request.headers
                                          or "Cf-Ray" in request.headers))):
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
                        # Share links carry private meetings: keep them out of
                        # search indexes and caches even if a link is posted.
                        "X-Robots-Tag": "noindex, nofollow, noarchive",
                    }),
                    html,
                )
            return None

        async def ws_handler(ws):
            host = (ws.remote_address or ("",))[0]
            req_headers = getattr(getattr(ws, "request", None), "headers", {})
            tunneled = "Cf-Connecting-Ip" in req_headers or "Cf-Ray" in req_headers
            is_host = (host in ("127.0.0.1", "::1") and not tunneled
                       and "X-Forwarded-For" not in req_headers
                       and "Forwarded" not in req_headers)
            # A handshake begun before rotation must not register afterwards.
            if self.token and ws.request.path != f"/{self.token}":
                await ws.close(code=4001, reason="Link reset; request a new link")
                return
            if self.private and tunneled:
                await ws.close(code=1008, reason="Private session; LAN only")
                return
            # Select whole sentence IDs, including every translation stage.
            ids = list(dict.fromkeys(ev["id"] for ev in self.history
                                     if ev["type"] == "committed"))
            truncated = not is_host and len(ids) > REPLAY_LIMIT
            selected = (set() if self.private and not is_host
                        else set(ids[-REPLAY_LIMIT:]) if truncated else None)
            events = [{"type": "replay_start"},
                      {"type": "share", **self.share, "private": self.private}]
            if self.pair:
                events.append({"type": "pair_state", "pair": self.pair})
            events.extend(ev for ev in self.history
                          if selected is None or ev["id"] in selected)
            events.append({"type": "replay_end"})
            replay = json.dumps({"type": "replay", "events": events,
                                 "truncated": truncated,
                                 "live_only": self.private and not is_host}, ensure_ascii=False)
            # each client has a bounded outbox; a stalled client is dropped,
            # never allowed to backpressure the caption pipeline
            outbox: asyncio.Queue = asyncio.Queue(maxsize=100)

            async def sender(initial):
                try:
                    await ws.send(initial)
                    del initial  # do not retain a per-viewer snapshot all meeting
                    while True:
                        await ws.send(await outbox.get())
                except Exception:
                    pass

            task = asyncio.create_task(sender(replay))
            self.viewer_ids[outbox] = set(ids if selected is None else selected)
            del replay, events, ids, selected
            try:
                # Snapshot, sender creation and registration contain no await.
                # Live events queue immediately, always AFTER replay_end in the
                # sender's first frame. No replay-time gap or concurrent sends.
                if is_host:
                    outbox.put_nowait(json.dumps(
                        {"type": "control_token", "token": self.control_token}))
                    self.control_clients.add(outbox)
                self.clients[outbox] = (task, ws)
                try:
                    async for raw in ws:
                        if not self.on_control:
                            continue
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if (is_host and isinstance(msg, dict)
                                and msg.get("token") == self.control_token):
                            await self.on_control(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass  # client gone (or force-closed as too slow); cleanup below
            finally:
                self.control_clients.discard(outbox)
                self.clients.pop(outbox, None)
                self.viewer_ids.pop(outbox, None)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.server = await websockets.serve(
            ws_handler, self.host, self.port, process_request=process_request,
            ping_timeout=60,
        )

    async def emit(self, event: dict) -> None:
        if event["type"] in ("committed", "translation"):
            self.history.append(event)  # replayed to late-joining browsers
        msg = json.dumps(event, ensure_ascii=False)
        for outbox, (task, ws) in list(self.clients.items()):
            if outbox not in self.control_clients and self.private:
                if event["type"] == "committed":
                    self.viewer_ids[outbox].add(event["id"])
                elif (event["type"] == "translation"
                      and event["id"] not in self.viewer_ids[outbox]):
                    continue  # No late refinement of a pre-join sentence.
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                # too slow: actually CLOSE the connection (code 1013), so the
                # browser's onclose fires and its reconnect+replay path
                # catches the viewer back up. Just cancelling the sender
                # would leave an open-but-dead socket: frozen page, green dot.
                task.cancel()
                self.clients.pop(outbox, None)
                self.viewer_ids.pop(outbox, None)
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
                self.viewer_ids.pop(outbox, None)
                asyncio.create_task(ws.close(code=1013))
