"""Offline D17 checks. Run .venv/bin/python test_d17.py (loopback sockets only)."""

import asyncio
import contextlib
from contextlib import redirect_stderr
import io
import json
import random
import string
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import websockets

import main as babel
from translate import ArkTranslator
from ui_server import CaptionUI


LIVE_DURING_REPLAY = 150  # more than the outbox bound of 100


async def replay_check():
    ui = CaptionUI(port=0, token="offline-d17")
    rng = random.Random(17)
    for seq in range(1, 3001):
        text = "".join(rng.choices(string.ascii_letters, k=600))
        for event in ({"type": "committed", "id": seq, "source": text, "lang": "en"},
                      {"type": "translation", "id": seq, "text": text, "provisional": True},
                      {"type": "translation", "id": seq, "text": text, "provisional": False}):
            await ui.emit(event)
    # Late stages are not adjacent to their committed event: select by ID.
    await ui.emit({"type": "translation", "id": 1, "text": "old refinement"})
    await ui.emit({"type": "translation", "id": 2701, "text": "retained refinement"})
    await ui.start()
    port = ui.server.sockets[0].getsockname()[1]
    destination = f"ws://127.0.0.1:{port}/offline-d17"
    active_relays = set()

    async def relay(reader, writer):
        task = asyncio.current_task()
        active_relays.add(task)
        backend_reader, backend_writer = await asyncio.open_connection("127.0.0.1", port)

        async def pipe(source, target, limited=False):
            while data := await source.read(16 * 1024):
                if limited:
                    await asyncio.sleep(len(data) / (200 * 1024))
                target.write(data)
                await target.drain()

        pipes = [asyncio.create_task(pipe(reader, backend_writer)),
                 asyncio.create_task(pipe(backend_reader, writer, True))]
        try:
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for child in pipes:
                child.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
            for stream in (writer, backend_writer):
                stream.close()
                await stream.wait_closed()
            active_relays.discard(task)

    proxy = await asyncio.start_server(relay, "127.0.0.1", 0)
    proxy_port = proxy.sockets[0].getsockname()[1]
    try:
        async with websockets.connect(destination, max_size=None) as host:
            replay = json.loads(await host.recv())
            assert replay["type"] == "replay" and not replay["truncated"]
            events = replay["events"]
            assert events[0]["type"] == "replay_start" and events[-1]["type"] == "replay_end"
            assert len([ev for ev in events if ev["type"] == "committed"]) == 3000
            assert ui.server.connections and all(ws.ping_timeout == 60 for ws in ui.server.connections)
            assert json.loads(await host.recv())["type"] == "control_token"

        async def viewer(index):
            started = time.monotonic()
            # Disable compression so the relay enforces a meaningful wire load.
            async with websockets.connect(
                    f"ws://127.0.0.1:{proxy_port}/offline-d17", max_size=None,
                    compression=None, additional_headers={"Cf-Connecting-Ip": "192.0.2.1"},
                    ping_timeout=60) as ws:
                replay = json.loads(await asyncio.wait_for(ws.recv(), 60))
                assert replay["type"] == "replay" and replay["truncated"]
                events = replay["events"]
                assert events[0]["type"] == "replay_start" and events[-1]["type"] == "replay_end"
                ids = [ev["id"] for ev in events if ev["type"] == "committed"]
                assert ids == list(range(2701, 3001))
                assert all(2701 <= ev["id"] <= 3000 for ev in events if "id" in ev)
                for seq in ids:
                    stages = [ev for ev in events if ev.get("id") == seq]
                    assert [ev["type"] for ev in stages[:3]] == ["committed", "translation", "translation"]
                    assert stages[1]["provisional"] and not stages[2]["provisional"]
                assert events[-2]["text"] == "retained refinement"
                assert not any(ev["type"] == "control_token" for ev in events)
                # Live events queued while the replay frame was in flight must
                # follow it, not overflow the outbox and close the viewer.
                for n in range(LIVE_DURING_REPLAY):
                    assert json.loads(await asyncio.wait_for(ws.recv(), 60)) == {
                        "type": "interim", "text": f"live {n}"}
                assert ws.close_code is None
                return time.monotonic() - started

        async def live_during_replay():
            while len(ui.clients) < 50:
                await asyncio.sleep(.01)
            for n in range(LIVE_DURING_REPLAY):  # ~7/s interim+draft rate, compressed
                await ui.emit({"type": "interim", "text": f"live {n}"})
                await asyncio.sleep(.005)

        *elapsed, _ = await asyncio.gather(*(viewer(n) for n in range(50)), live_during_replay())
        assert max(elapsed) < 60
        print(f"D17: 50 viewers, 200 KiB/s each, replay max {max(elapsed):.3f}s")
    finally:
        proxy.close()
        await proxy.wait_closed()
        if active_relays:
            await asyncio.gather(*list(active_relays), return_exceptions=True)
        ui.server.close()
        await ui.server.wait_closed()


async def tunnel_check():
    class Process:
        def __init__(self, index):
            self.index = index
            self.returncode = None
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

    children, order, shares, notices = [], [], [], []
    grace_failures = []
    probes_after_grace = {}
    started_at = {}
    virtual_time = 0

    def clock():
        return virtual_time

    async def spawn(port):
        child = Process(len(children))
        children.append(child)
        started_at[child.index] = clock()
        if child.index in (1, 3):  # interleave exit-triggered and probe-triggered restarts
            child.returncode = 1
            child.exited.set()
        order.append(("ready", child.index))
        return child, f"https://offline-{child.index}.trycloudflare.com"

    async def stop(child):
        order.append(("stop", child.index))
        child.returncode = 0
        child.exited.set()

    async def probe(url):
        nonlocal virtual_time
        virtual_time += 60
        index = int(url.split('offline-')[1].split('.')[0])
        if index == 0 and clock() - started_at[index] == 180:
            return True, "HTTP 200"  # success must reset the failure streak
        if clock() - started_at[index] < 90:
            grace_failures.append(url)
        else:
            probes_after_grace[index] = probes_after_grace.get(index, 0) + 1
        return False, "HTTP 530"

    class UI:
        async def set_share(self, lan, public):
            shares.append(public)
            order.append(("publish", public))

        async def emit_control(self, event):
            assert event["type"] == "tunnel_state"
            notices.append(event["text"])

    log = io.StringIO()
    with patch.object(babel, "maybe_tunnel", spawn), patch.object(babel, "stop_tunnel", stop), \
         patch.object(babel, "probe_public_url", probe), \
         patch.object(babel, "TUNNEL_PROBE_SECONDS", .003), \
         patch.object(babel, "time", SimpleNamespace(monotonic=clock)), redirect_stderr(log):
        await asyncio.wait_for(babel.maintain_tunnel(0, UI(), "http://lan/path", "/full-path"), 1)
    assert grace_failures and probes_after_grace == {0: 4, 2: 3}, probes_after_grace
    assert started_at[1] == 360  # not 240: an intervening success reset the streak
    assert len(children) == 4  # initial + only three automatic replacements
    for index in range(1, 4):
        assert order.index(("ready", index)) < order.index(("stop", index - 1))
        url = f"https://offline-{index}.trycloudflare.com/full-path"
        assert order.index(("publish", url)) < order.index(("stop", index - 1))
    assert shares[-1] is None and "check the network" in notices[-1]
    assert all(child.returncode is not None for child in children)
    assert "[tunnel-health] probe failed: HTTP 530; grace period" in log.getvalue()
    assert log.getvalue().count("[tunnel-health] replacing") == 3
    assert "[tunnel-health] giving up" in log.getvalue()

    # Cancel while publishing a replacement: both owned processes must stop.
    children.clear()
    gate = asyncio.Event()

    class CancellingUI(UI):
        async def set_share(self, lan, public):
            if public and "offline-1" in public:
                gate.set()
                await asyncio.Event().wait()

    with patch.object(babel, "maybe_tunnel", spawn), patch.object(babel, "stop_tunnel", stop), \
         patch.object(babel, "probe_public_url", probe), \
         patch.object(babel, "TUNNEL_PROBE_SECONDS", .001), \
         patch.object(babel, "TUNNEL_GRACE_SECONDS", 0), \
         patch.object(babel, "time", SimpleNamespace(monotonic=clock)), redirect_stderr(log):
        task = asyncio.create_task(babel.maintain_tunnel(0, CancellingUI(), "http://lan", "/path"))
        await asyncio.wait_for(gate.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(children) == 2 and all(child.returncode is not None for child in children)

    # The budget is rolling, not a three-restarts-for-the-whole-meeting cap.
    children.clear()
    gate.clear()

    async def hourly_probe(url):
        nonlocal virtual_time
        virtual_time += 3601
        return False, "HTTP 530"

    class HourlyUI(UI):
        async def set_share(self, lan, public):
            if public and "offline-4" in public:
                gate.set()
                await asyncio.Event().wait()

    with patch.object(babel, "maybe_tunnel", spawn), patch.object(babel, "stop_tunnel", stop), \
         patch.object(babel, "probe_public_url", hourly_probe), \
         patch.object(babel, "TUNNEL_PROBE_SECONDS", .001), \
         patch.object(babel, "time", SimpleNamespace(monotonic=clock)), redirect_stderr(log):
        task = asyncio.create_task(babel.maintain_tunnel(0, HourlyUI(), "http://lan", "/path"))
        await asyncio.wait_for(gate.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(children) == 5 and all(child.returncode is not None for child in children)


async def probe_and_leak_check():
    url = "https://offline.trycloudflare.com/random-caption-path"
    real_client = httpx.AsyncClient
    for status in (200, 530):
        def handler(request):
            assert str(request.url) == url and request.method == "GET"
            assert set(request.extensions["timeout"].values()) == {10}
            return httpx.Response(status)

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch.object(babel.httpx, "AsyncClient", client):
            assert await babel.probe_public_url(url) == (status == 200, f"HTTP {status}")
    cancelled = asyncio.Event()

    async def hanging(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with patch.object(babel.httpx, "AsyncClient", lambda **kw:
                      real_client(transport=httpx.MockTransport(hanging), **kw)), \
         patch.object(babel, "TUNNEL_PROBE_TIMEOUT", .02):
        started = time.monotonic()
        ok, detail = await babel.probe_public_url(url)
    assert not ok and "TimeoutError" in detail and cancelled.is_set()
    assert time.monotonic() - started < .2

    ark = ArkTranslator.__new__(ArkTranslator)
    ark.service_tier = "default"
    ark.system_prompt, ark.model, ark.usage = "offline", "offline", [0, 0, 0]
    rejected = "Wait, source text\n" + "x" * 210 + "DO-NOT-LOG-AFTER-200"
    results = iter([rejected, "正常译文"])

    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(results)))])

    ark.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    log = io.StringIO()
    with redirect_stderr(log):
        assert await ark.translate("Hello", "en", "zh") == "正常译文"
    lines = log.getvalue().splitlines()
    assert len(lines) == 1 and "rejected=" in lines[0]
    assert "Wait, source text " in lines[0] and "DO-NOT-LOG-AFTER-200" not in lines[0]


async def missing_cloudflared_check():
    import shutil

    class FakeUI:
        controls = []

        async def emit_control(self, event):
            self.controls.append(event)

    async def no_tunnel(port):
        return None, None

    ui, saved = FakeUI(), (babel.maybe_tunnel, shutil.which)
    babel.maybe_tunnel, shutil.which = no_tunnel, lambda name: None
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            await asyncio.wait_for(babel.maintain_tunnel(1, ui, "http://lan", "/t"), 1)
    finally:
        babel.maybe_tunnel, shutil.which = saved
    assert [ev["text"] for ev in ui.controls] == [
        "Public tunnel unavailable (cloudflared not installed); LAN only"]


async def main():
    await missing_cloudflared_check()
    await tunnel_check()
    await probe_and_leak_check()
    await replay_check()


if __name__ == "__main__":
    asyncio.run(main())
    print("D17: full-host/tail-viewer replay, replacement order/budget/grace, HTTP probe and leak log pass")
