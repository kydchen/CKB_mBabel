"""D15 offline fault injection. No microphones, keys or cloud calls.

Run: .venv/bin/python test_d15.py
Use --wall-clock to also exercise the real 20s / 30s deadlines.
"""

import asyncio
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import main as babel
import translate

REAL_SLEEP = asyncio.sleep
REAL_SPAWN = asyncio.create_subprocess_exec


def event(text="", *, last=False, definite=True):
    return SimpleNamespace(text="" if definite else text, is_last=last,
                           utterances=[{"text": text, "definite": True,
                                        "start_time": 0, "end_time": len(text),
                                        "additions": {"speaker_id": "0"}}]
                           if text and definite else [])


class UI:
    instance = None

    def __init__(self, host, port, token=None):
        UI.instance = self
        self.port, self.control_token = port, "offline"
        self.events = []

    async def start(self):
        pass

    async def emit(self, value):
        self.events.append(value.copy())

    emit_control = emit

    async def set_pair(self, pair):
        pass

    async def set_share(self, lan, public):
        await self.emit({"type": "share", "lan": lan, "public": public})


class Translator:
    usage = {}

    async def translate(self, text, src, tgt, **kwargs):
        if tgt == "vi":
            return "Đây là bản dịch hợp lệ."
        return "A valid translation." if tgt == "en" else "有效译文。"


async def idle_producer(*_args):
    await asyncio.Event().wait()


@contextmanager
def pipeline(root, asr, ark=None, pair="zh-en", corrections=None):
    root = Path(root)
    hotwords = root / "hotwords"
    hotwords.mkdir()
    glossary = root / "glossary.json"
    glossary.write_text(json.dumps({"terms": [], "corrections": corrections or {}}))
    args = SimpleNamespace(no_ui=False, port=18765, share=False, pair=pair,
                           glossary=str(glossary), hotwords_dir=str(hotwords),
                           wav="offline.wav", translator="volc-mt", model=None,
                           end_window=None, audio_config=str(root / "audio.json"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(babel, "__file__", str(root / "main.py")))
        stack.enter_context(patch.object(babel, "SESSION_DIR", str(root / ".mbabel")))
        stack.enter_context(patch.dict(os.environ, {"VOLC_ASR_API_KEY": "offline"}))
        stack.enter_context(patch.object(babel, "VolcAsrClient", asr))
        stack.enter_context(patch.object(babel, "CaptionUI", UI))
        stack.enter_context(patch.object(babel, "build_translator", return_value=Translator()))
        stack.enter_context(patch.object(babel, "ArkTranslator", return_value=ark or Translator()))
        stack.enter_context(patch.object(babel, "wav_chunks", idle_producer))
        stack.enter_context(patch.object(babel.webbrowser, "open", return_value=True))
        yield args


async def handshake_check(root):
    class ASR:
        attempts = 0

        def __init__(self, config):
            self.config = config

        async def transcribe(self, chunks):
            type(self).attempts += 1
            attempt = self.attempts
            if 2 <= attempt < 20:
                status = 503 if attempt == 2 else (403 if attempt % 2 else 401)
                raise babel.InvalidStatus(SimpleNamespace(status_code=status))
            print("[asr] connected, logid=offline-handshake")
            yield event("第一场连接成功。" if attempt == 1 else "已经恢复连接。",
                        last=attempt == 21)
            if attempt < 21:
                raise babel.AsrError(1, "offline disconnect")

    delays = []

    async def fast_sleep(delay):
        delays.append(delay)
        await REAL_SLEEP(0)

    with pipeline(root, ASR) as args, patch.object(babel.asyncio, "sleep", fast_sleep):
        await babel.run(args)
    statuses = [v["text"] for v in UI.instance.events if v["type"] == "status"]
    assert ASR.attempts == 21
    assert any("403" in v and "quota" in v for v in statuses), statuses
    assert any("401" in v and "quota" in v for v in statuses), statuses
    backoffs = [v for v in delays if v >= 2]
    assert backoffs == [min(2 * n, 10 if n < 3 else 30) for n in range(1, 20)] + [2], backoffs
    data = next((Path(root) / "transcripts").glob("babel-*.log")).read_text()
    assert "logid=offline-handshake" in data and "HTTP 503" in data and "reconnecting" in data


async def initial_rejection_check(root):
    class ASR:
        def __init__(self, config):
            pass

        async def transcribe(self, chunks):
            raise babel.InvalidStatus(SimpleNamespace(status_code=403))
            yield

    with pipeline(root, ASR) as args:
        try:
            await babel.run(args)
        except SystemExit as error:
            assert str(error) == ("[asr] handshake rejected (HTTP 403); "
                                  "check VOLC_ASR_API_KEY and service activation")
        else:
            raise AssertionError("Initial authentication failure did not exit")


async def tunnel_check(root):
    children = []
    payload_bytes = 2 * 1024 * 1024

    async def spawn(*args, **kwargs):
        index = len(children)
        code = ("import sys; "
                f"print('https://offline-{index}.trycloudflare.com',flush=True); "
                f"sys.stdout.write('x'*{payload_bytes}); sys.stdout.flush()")
        proc = await REAL_SPAWN(sys.executable, "-u", "-c", code, **kwargs)
        children.append(proc)
        return proc

    ui = UI("offline", 0)
    terminal = io.StringIO()
    path = str(Path(root) / "tunnel.log")
    with babel.session_log(path), redirect_stdout(terminal), \
         patch("shutil.which", return_value=sys.executable), \
         patch.object(babel.asyncio, "create_subprocess_exec", spawn):
        # stderr remains tee'd to disk; subprocesses are local Python only.
        await asyncio.wait_for(babel.maintain_tunnel(0, ui, "http://lan/caption", "/caption"), 10)
    data = Path(path).read_text()
    assert data.count("x") >= payload_bytes * 4 and "[tunnel]" in data
    assert len(children) == 4 and all(p.returncode == 0 for p in children)
    shares = [v["public"] for v in ui.events if v["type"] == "share"]
    assert shares == [url for n in range(4) for url in
                      (f"https://offline-{n}.trycloudflare.com/caption", None)] + [None], shares
    notices = [v["text"] for v in ui.events if v["type"] == "tunnel_state"]
    assert notices[-1] == ("公网链接持续不可用，请检查网络 / "
                           "Public link keeps failing, check the network")
    statuses = [v["text"] for v in ui.events if v["type"] == "status"]
    assert not any("tunnel" in v.lower() for v in statuses), statuses  # LIVE pill stays ASR-only


async def timeout_check(root, wall_clock=False):
    class ASR:
        def __init__(self, config):
            pass

        async def transcribe(self, chunks):
            yield event("这是一个挂起时也必须保留下来的长句子。", last=True)

    class HangingArk(Translator):
        calls = 0

        def __init__(self):
            self.durations = []

        async def translate(self, *args, **kwargs):
            self.calls += 1
            attempt_started = time.monotonic()
            try:
                await asyncio.Event().wait()
            finally:
                self.durations.append(time.monotonic() - attempt_started)

    ark = HangingArk()
    timeout = 30.0 if wall_clock else 0.12
    started = time.monotonic()
    with pipeline(root, ASR, ark) as args, \
         patch.object(babel, "SHUTDOWN_TRANSLATION_SECONDS", timeout):
        await babel.run(args)
    elapsed = time.monotonic() - started
    assert elapsed < timeout + 2, elapsed
    if wall_clock:
        assert ark.calls >= 2, "20s deadline never advanced to the application retry"
        assert 19.8 <= ark.durations[0] < 21, ark.durations
        assert elapsed >= 29
    path = next((Path(root) / "transcripts").glob("babel-*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["translation_outcome"] == "incomplete", rows
    assert rows[0]["source"] == "这是一个挂起时也必须保留下来的长句子。"
    assert "挂起时" in next(path.parent.glob("babel-*.md")).read_text()
    return {"wall_clock": wall_clock, "exit_seconds": round(elapsed, 3),
            "ark_attempts": ark.calls, "first_attempt_seconds": round(ark.durations[0], 3)}


async def tunnel_cancel_check(root):
    for with_url in (False, True):
        spawned, published = asyncio.Event(), asyncio.Event()
        children = []

        class CancelUI(UI):
            async def set_share(self, lan, public):
                await super().set_share(lan, public)
                if public:
                    published.set()

        async def spawn(*args, **kwargs):
            code = "import time; "
            if with_url:
                code += "print('https://offline-cancel.trycloudflare.com',flush=True); "
            code += "time.sleep(60)"
            child = await REAL_SPAWN(sys.executable, "-u", "-c", code, **kwargs)
            children.append(child)
            spawned.set()
            return child

        with patch("shutil.which", return_value=sys.executable), \
             patch.object(babel.asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(babel.maintain_tunnel(0, CancelUI("offline", 0), "http://lan", "/caption"))
            await asyncio.wait_for((published if with_url else spawned).wait(), 3)
            task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4)
        assert len(children) == 1 and children[0].returncode is not None
        assert children[0]._babel_reader.done()


async def correction_check(root):
    class ASR:
        def __init__(self, config):
            self.calls = 0

        async def transcribe(self, chunks):
            self.calls += 1
            if self.calls > 1:
                yield event("Welcome to CKCon and vote for leaf.", last=True)
                return
            # Both sides of a language boundary need their own raw evidence.
            yield event("我们讨论 wiki。We sell tokens on the market.")
            yield event("Unchanged source.")
            yield event("GM everyone welcome to CKCon", definite=False)
            raise babel.AsrError(1, "trace reconnect")

    async def fast_sleep(_delay):
        await REAL_SLEEP(0)

    with pipeline(root, ASR, corrections={"wiki": "Vicky", "sell": "cell",
                                         "GM": "GA", "leaf": "lift"}) as args, \
         patch.object(babel.asyncio, "sleep", fast_sleep):
        await babel.run(args)
    path = next((Path(root) / "transcripts").glob("babel-*.jsonl"))
    rows = sorted((json.loads(line) for line in path.read_text().splitlines()),
                  key=lambda row: row["seq"])
    assert len(rows) == 4, rows
    assert rows[0]["raw_source"] == "我们讨论 wiki。" and rows[0]["corrections_hit"] == ["wiki"]
    assert rows[1]["raw_source"] == "We sell tokens on the market." and rows[1]["corrections_hit"] == ["sell"]
    assert "raw_source" not in rows[2] and "corrections_hit" not in rows[2]
    assert rows[3]["raw_source"] == "GM everyone welcome to CKCon and vote for leaf.", rows[3]
    assert rows[3]["source"] == "GA everyone welcome to CKCon and vote for lift."
    assert rows[3]["corrections_hit"] == ["GM", "leaf"]


async def pause_trace_check(root):
    class ASR:
        def __init__(self, config):
            pass

        async def close(self):
            pass

        async def transcribe(self, chunks):
            yield event("刚才张老师说这个提案", definite=False)
            await UI.instance.on_control({"type": "pause", "paused": True})
            await UI.instance.on_control({"type": "pause", "paused": False})
            yield event(last=True)

    with pipeline(root, ASR, corrections={"老师说": "老实说"}) as args:
        await babel.run(args)
    path = next((Path(root) / "transcripts").glob("babel-*.jsonl"))
    row = json.loads(path.read_text())
    assert row["raw_source"] == "刚才张老师说这个提案" and row["corrections_hit"] == ["老师说"]


async def vi_trace_check(root):
    class ASR:
        def __init__(self, config):
            pass

        async def transcribe(self, chunks):
            yield event("Wiki is speaking at CKCon.", last=True)

    with pipeline(root, ASR, pair="en-vi", corrections={"wiki": "Vicky"}) as args:
        await babel.run(args)
    path = next((Path(root) / "transcripts").glob("babel-*.jsonl"))
    row = json.loads(path.read_text())
    assert row["raw_source"] == "Wiki is speaking at CKCon." and row["corrections_hit"] == ["wiki"]


def log_check(root):
    class DeadTTY(io.StringIO):
        def write(self, value):
            raise OSError("dead pty")

        def flush(self):
            raise OSError("dead pty")

    path = Path(root) / "dead-tty.log"
    with redirect_stdout(DeadTTY()), redirect_stderr(DeadTTY()), babel.session_log(str(path)):
        print("stdout stays on disk")
        print("stderr stays on disk", file=sys.stderr)
    assert path.read_text() == "stdout stays on disk\nstderr stays on disk\n"
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o077 == 0
    terminal = io.StringIO()
    tee = babel.LogTee(terminal, DeadTTY())
    tee.write("still running\n")
    tee.write("still draining\n")
    assert terminal.getvalue().count("disk logging disabled") == 1
    assert "still running\nstill draining\n" in terminal.getvalue()


def sdk_check():
    for env, name in [({"ARK_API_KEY": "offline"}, "AsyncOpenAI"),
                      ({"VOLC_ACCESSKEY": "offline", "VOLC_SECRETKEY": "offline"}, "AsyncArk")]:
        with patch.dict(os.environ, env, clear=True), patch.object(translate, name) as constructor:
            translate.ArkTranslator(translate.Glossary([]), "offline-model")
        assert constructor.call_args.kwargs["timeout"] == 20
        assert constructor.call_args.kwargs["max_retries"] == 0


def stats_check():
    from collections import deque
    counters = {"ark": deque([0, 41, 100])}
    latencies = deque([(0, 800), (40, 700), (99, 900)])
    babel.trim_stats(counters, latencies, 100)
    assert list(counters["ark"]) == [41, 100] and list(latencies) == [(99, 900)]


async def checks(root, wall_clock):
    for name, check in [("handshake", handshake_check), ("startup", initial_rejection_check),
                        ("tunnel", tunnel_check), ("correction", correction_check),
                        ("pause", pause_trace_check), ("vi", vi_trace_check),
                        ("cancel", tunnel_cancel_check)]:
        folder = Path(root) / name
        folder.mkdir()
        await check(folder)
    folder = Path(root) / "timeout"
    folder.mkdir()
    return await timeout_check(folder, wall_clock)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="mbabel-d15-") as root, \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        metrics = asyncio.run(checks(root, "--wall-clock" in sys.argv))
        sdk_check()
        stats_check()
        log_check(root)
        launcher = Path(__file__).resolve().parents[1] / "Babel.command"
        assert 'exec caffeinate -ims .venv/bin/python main.py --share "$@"' in launcher.read_text()
    print("D15: offline handshake, tunnel drain/restart, shutdown, corrections, log and stats pass")
    print(json.dumps(metrics))
