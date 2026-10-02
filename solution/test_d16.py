"""Offline check: replay boundary precedes live captions without losing any.

Run: .venv/bin/python test_d16.py. No socket, microphone or cloud requests.
The full browser/performance check is documented in the D16 audit delivery.
"""

import asyncio
import json
from unittest.mock import patch

from ui_server import CaptionUI


async def check():
    ui = CaptionUI()

    async def caption(seq):
        await ui.emit({"type": "committed", "id": seq, "source": str(seq)})

    await caption(1)
    await caption(2)
    handler = None

    async def serve(callback, *args, **kwargs):
        nonlocal handler
        handler = callback

    with patch("ui_server.websockets.serve", serve):
        await ui.start()

    class Socket:
        remote_address = ("192.0.2.1", 1)  # viewer: no host control token

        def __init__(self, live_id):
            self.events = []
            self.live_id = live_id
            self.drained = asyncio.Event()

        async def send(self, raw):
            event = json.loads(raw)
            self.events.append(event)
            if event.get("id") == 2 and self.live_id == 4:
                await caption(3)  # arrives while awaiting a historical send
            if event.get("id") == self.live_id:
                self.drained.set()
            await asyncio.sleep(0)

        def __aiter__(self):
            return self

        async def __anext__(self):
            await caption(self.live_id)  # first live event after registration
            await asyncio.wait_for(self.drained.wait(), .5)
            raise StopAsyncIteration

    for live_id in (4, 5):
        socket = Socket(live_id)
        await handler(socket)
        events = socket.events
        assert events[0] == {"type": "replay_start"}
        boundary = events.index({"type": "replay_end"})
        assert [ev["id"] for ev in events[1:boundary]] == list(range(1, live_id))
        assert events[boundary + 1:] == [{"type": "committed", "id": live_id,
                                         "source": str(live_id)}]
        assert not ui.clients and not ui.control_clients
    assert [ev["id"] for ev in ui.history] == [1, 2, 3, 4, 5]


if __name__ == "__main__":
    asyncio.run(check())
    print("D16: replay/live boundary, concurrent commit, reconnect and full history pass")
