"""Offline regression checks for P0 runtime behavior."""

import asyncio
import io
import json
import os
import signal
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stdout

import main as babel


SOLUTION = os.path.dirname(os.path.abspath(__file__))


class DummyTranslator:
    fail_streak = 0
    usage = {}
    calls = []

    async def translate(self, text, src, tgt, **kwargs):
        self.calls.append((text, kwargs))
        return "translated"


class DummyAsr:
    def __init__(self, config):
        self.config = config

    async def close(self):
        pass

    async def transcribe(self, chunks):
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "测试句。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(60)


class CorrectionAsr(DummyAsr):
    glossary_path = None

    async def transcribe(self, chunks):
        await DummyUI.instance.on_control(
            {"type": "correction", "wrong": "you double v", "right": "UW"}
        )
        with open(self.glossary_path, encoding="utf-8") as f:
            assert json.load(f)["corrections"]["you double v"] == "UW"
        assert not [name for name in os.listdir(os.path.dirname(self.glossary_path))
                    if name.startswith(os.path.basename(self.glossary_path) + ".")
                    and name.endswith(".tmp")]
        yield SimpleNamespace(text="", utterances=[], is_last=True)


class SignalCorrectionAsr(DummyAsr):
    async def transcribe(self, chunks):
        await DummyUI.instance.on_control(
            {"type": "correction", "wrong": "you double v", "right": "UW"}
        )
        async for event in super().transcribe(chunks):
            yield event


class DraftAsr(DummyAsr):
    async def transcribe(self, chunks):
        yield SimpleNamespace(text="这是第一句非常非常长的内容", utterances=[],
                              is_last=False)
        await asyncio.sleep(0.05)
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "这是第一句非常非常长的内容。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(0.7)
        yield SimpleNamespace(text="第二句开头六字", utterances=[], is_last=False)
        await asyncio.sleep(0.05)
        yield SimpleNamespace(text="", utterances=[], is_last=True)


class DraftCoverageAsr(DummyAsr):
    async def transcribe(self, chunks):
        yield SimpleNamespace(text="只有开头四字", utterances=[], is_last=False)
        await asyncio.sleep(0.05)
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "只有开头四字但最终句子增加了很多完全不同的内容。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(0.7)
        yield SimpleNamespace(
            text="这段草稿已经覆盖完整句子的绝大部分内容",
            utterances=[],
            is_last=False,
        )
        await asyncio.sleep(0.05)
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "这段草稿已经覆盖完整句子的绝大部分内容。",
                "start_time": 101,
                "end_time": 200,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=True,
        )


class ShortSentenceAsr(DummyAsr):
    async def transcribe(self, chunks):
        yield SimpleNamespace(text="你好朋友", utterances=[], is_last=False)
        await asyncio.sleep(0.05)
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "你好朋友。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(0.05)
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "收到。",
                "start_time": 101,
                "end_time": 200,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=True,
        )


class CommittedWithResidualAsr(DummyAsr):
    async def transcribe(self, chunks):
        yield SimpleNamespace(
            text="你能听到我说话吗？",
            utterances=[{
                "definite": True,
                "text": "你能听到我说话吗？",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "2", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(0.05)
        yield SimpleNamespace(text="", utterances=[], is_last=True)


class ContextAsr(DummyAsr):
    async def transcribe(self, chunks):
        utterances = []
        for index in range(1, 7):
            utterances.append({
                "definite": True,
                "text": f"第{index}句上下文。",
                "start_time": index * 100,
                "end_time": index * 100 + 99,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            })
        yield SimpleNamespace(text="", utterances=utterances, is_last=True)


class PauseAsr(DummyAsr):
    sessions = 0
    close_calls = 0

    async def close(self):
        type(self).close_calls += 1

    async def transcribe(self, chunks):
        type(self).sessions += 1
        if self.sessions == 1:
            # a sentence finished BEFORE the pause, still interim-only: it
            # must settle as a caption instead of vanishing with the session
            yield SimpleNamespace(text="暂停前已经说完的话", utterances=[],
                                  is_last=False)
            await DummyUI.instance.on_control({"type": "pause", "paused": True})

            async def resume():
                await asyncio.sleep(0.02)
                await DummyUI.instance.on_control({"type": "pause", "paused": False})

            asyncio.create_task(resume())
            return
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "恢复后的句子。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=True,
        )


class HotwordAsr(DummyAsr):
    direct = []

    def __init__(self, config):
        super().__init__(config)
        type(self).direct = list(config.hotwords)

    async def transcribe(self, chunks):
        yield SimpleNamespace(text="", utterances=[], is_last=True)


class ReconnectingAsr(DummyAsr):
    sessions = 0
    received = []

    async def transcribe(self, chunks):
        type(self).sessions += 1
        self.received.append(await anext(chunks))
        if self.sessions == 1:
            yield SimpleNamespace(
                text="",
                utterances=[{
                    "definite": True,
                    "text": "前一句已经完整落定。",
                    "start_time": 0,
                    "end_time": 100,
                    "additions": {"speaker_id": "1", "lid_lang": "speech_mand"},
                }],
                is_last=False,
            )
            yield SimpleNamespace(
                text="我们这周讨论一下 fiber 网络和 RGB++的进展，还有基于",
                utterances=[],
                is_last=False,
            )
            await asyncio.sleep(0)
            raise babel.AsrError(45000081, "test reconnect")
        yield SimpleNamespace(
            text="",
            utterances=[
                {
                    "definite": True,
                    "text": "已经完整落定。",
                    "start_time": 0,
                    "end_time": 100,
                    "additions": {"speaker_id": "1", "lid_lang": "speech_mand"},
                },
                {
                    "definite": True,
                    "text": "一、加加的进展，还有基于 UTXO 的状态通道设计。",
                    "start_time": 101,
                    "end_time": 200,
                    "additions": {"speaker_id": "1", "lid_lang": "speech_mand"},
                },
                {
                    "definite": True,
                    "text": "网络恢复后的正常新句子。",
                    "start_time": 201,
                    "end_time": 300,
                    "additions": {"speaker_id": "1", "lid_lang": "speech_mand"},
                },
            ],
            is_last=True,
        )


class DummyUI:
    events = []
    instance = None

    def __init__(self, host, port, token=None):
        type(self).instance = self
        self.port = port
        self.control_token = "offline"
        self.on_control = None

    async def start(self):
        pass

    async def emit(self, event):
        self.events.append(dict(event))

    async def emit_control(self, event):
        self.events.append(dict(event))

    async def set_share(self, lan, public):
        pass


class BindFailUI(DummyUI):
    async def start(self):
        raise OSError(48, "Address already in use")


def make_args(temp_dir, wav):
    return SimpleNamespace(
        wav=wav,
        audio_config=os.path.join(temp_dir, "audio_config.json"),
        glossary=os.path.join(SOLUTION, "glossary.json"),
        hotwords_dir=os.path.join(temp_dir, "hotwords"),
        translator="volc-mt",
        model=None,
        no_ui=True,
        share=False,
        port=8765,
        end_window=800,
    )


async def signal_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    args.glossary = os.path.join(temp_dir, "glossary-signal.json")
    with open(os.path.join(SOLUTION, "glossary.json"), encoding="utf-8") as src, \
         open(args.glossary, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    asyncio.get_running_loop().call_later(
        0.2, os.kill, os.getpid(), signal.SIGTERM
    )
    asyncio.get_running_loop().call_later(
        0.22, os.kill, os.getpid(), signal.SIGINT
    )
    class SlowPolisher(DummyTranslator):
        usage = (12, 4, 1)

        async def translate(self, text, src, tgt, **kwargs):
            await asyncio.sleep(0.3)
            return "translated"

    translator = DummyTranslator()
    translator.usage = {}
    with patch.object(babel, "VolcAsrClient", SignalCorrectionAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel, "build_translator", return_value=translator), \
         patch.object(babel, "ArkTranslator", return_value=SlowPolisher()), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    assert not os.path.exists(babel.session_file_path(args.port))
    transcript_dir = os.path.join(temp_dir, "transcripts")
    markdown = [name for name in os.listdir(transcript_dir) if name.endswith(".md")]
    assert len(markdown) == 1, markdown
    with open(os.path.join(transcript_dir, markdown[0]), encoding="utf-8") as f:
        assert "测试句。" in f.read()
    with open(os.path.join(transcript_dir, "usage.jsonl"), encoding="utf-8") as f:
        assert json.loads(f.readlines()[-1])["ark/polish"]["calls"] == 1
    with open(args.glossary, encoding="utf-8") as f:
        assert json.load(f)["corrections"]["you double v"] == "UW"


async def immediate_correction_check(temp_dir):
    glossary_path = os.path.join(temp_dir, "glossary-d4.json")
    with open(os.path.join(SOLUTION, "glossary.json"), encoding="utf-8") as src, \
         open(glossary_path, "w", encoding="utf-8") as dst:
        dst.write(src.read())
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    args.glossary = glossary_path
    CorrectionAsr.glossary_path = glossary_path
    with patch.object(babel, "VolcAsrClient", CorrectionAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True), \
         patch.object(babel.os, "replace", wraps=os.replace) as replaced:
        await babel.run(args)
    assert any(call.args[0].endswith(".tmp") and call.args[1] == glossary_path
               for call in replaced.call_args_list), replaced.call_args_list
    with open(glossary_path, encoding="utf-8") as f:
        assert json.load(f)["corrections"]["you double v"] == "UW"


def candidate_check(temp_dir):
    records = [
        {"source": "NovaChain met CKB and 新星链。"},
        {"source": "NovaChain met CKB and 新星链。"},
        {"source": "NovaChain met CKB and 新星链。"},
        {"source": "RareName appears once."},
    ]
    candidates = babel.extract_language_candidates(
        records, ["CKB"], {"you double v": "UW"}
    )
    found = {item["word"]: item["count"] for item in candidates}
    assert found == {"NovaChain": 3, "新星链": 3, "UW": 1}, found
    assert all(item["word"] != "CKB" for item in candidates)

    hotwords_path = os.path.join(temp_dir, "hotwords", "from-meetings.txt")
    with open(hotwords_path, "w", encoding="utf-8") as f:
        f.write("# meeting review\nCKB\n")
    babel.atomic_merge_hotwords(hotwords_path, ["NovaChain", "ckb"])
    with open(hotwords_path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert lines == ["# meeting review", "CKB", "NovaChain"], lines

    pending_dir = os.path.join(temp_dir, "candidate-pending")
    os.mkdir(pending_dir)
    older = os.path.join(pending_dir, "older-candidates.json")
    newer = os.path.join(pending_dir, "newer-candidates.json")
    babel.write_candidates(older, "older", candidates[:1])
    babel.write_candidates(newer, "newer", candidates[1:])
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    path, payload = babel.latest_candidates(pending_dir)
    assert path == newer and payload["stamp"] == "newer"
    assert not os.path.exists(older)


class EndNoReviewAsr(DummyAsr):
    async def transcribe(self, chunks):
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True,
                "text": "测试句。",
                "start_time": 0,
                "end_time": 100,
                "additions": {"speaker_id": "0", "lid_lang": "speech_mand"},
            }],
            is_last=False,
        )
        await asyncio.sleep(0)
        await DummyUI.instance.on_control({"type": "end"})
        await asyncio.sleep(60)


async def review_wait_signal_check(temp_dir):
    """A signal during the post-stop review wait must end the run, not hang."""
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    asyncio.get_running_loop().call_later(
        0.4, os.kill, os.getpid(), signal.SIGTERM
    )
    with patch.object(babel, "VolcAsrClient", EndNoReviewAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await asyncio.wait_for(babel.run(args), 10)
    assert any(e.get("type") == "candidates" and e.get("open")
               for e in DummyUI.events), "review modal was never offered"


def write_test_session(port, pid, url):
    os.makedirs(babel.SESSION_DIR, exist_ok=True)
    path = babel.session_file_path(port)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pid": pid, "port": port, "url": url,
                   "started_at": time.time()}, f)
    os.chmod(path, 0o600)
    return path


def session_registry_check():
    port = 18765
    url = f"http://127.0.0.1:{port}/secret"
    path = babel.register_session(port, url)
    with open(path, encoding="utf-8") as f:
        record = json.load(f)
    assert record["pid"] == os.getpid() and record["port"] == port
    assert record["url"] == url and isinstance(record["started_at"], float)
    assert os.stat(path).st_mode & 0o777 == 0o600
    with patch.object(babel, "process_alive", return_value=True), \
         patch.object(babel, "session_http_alive", return_value=True), \
         patch.object(babel.webbrowser, "open", return_value=True) as opened:
        assert babel.reopen_existing_session(port)
        opened.assert_called_once_with(url)
    assert not babel.reopen_existing_session(port + 1)
    assert os.path.exists(path)  # another port never touches this registration

    write_test_session(port, 99999999, url)
    with patch.object(babel, "process_alive", return_value=False), \
         patch.object(babel, "session_http_alive",
                      side_effect=AssertionError("dead pid must short-circuit HTTP")):
        assert babel.live_session(port) is None
    assert not os.path.exists(path)

    babel.register_session(port, url)
    with patch.object(babel, "process_alive", return_value=True), \
         patch.object(babel, "session_http_alive", return_value=False):
        assert babel.live_session(port) is None
    assert not os.path.exists(path)
    assert not babel.session_http_alive(f"http://example.com:{port}/secret", port)


async def session_reopen_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    url = f"http://127.0.0.1:{args.port}/existing-token"
    path = write_test_session(args.port, 4242, url)
    output = io.StringIO()
    with patch.object(babel, "process_alive", return_value=True), \
         patch.object(babel, "session_http_alive", return_value=True), \
         patch.object(babel.webbrowser, "open", return_value=True) as opened, \
         patch.object(babel.Glossary, "load",
                      side_effect=AssertionError("second pipeline started")), \
         redirect_stdout(output):
        await babel.run(args)
    opened.assert_called_once_with(url)
    assert "已有会话正在进行" in output.getvalue()
    assert os.path.exists(path)
    babel.clear_session_file(args.port)


async def no_ui_bypass_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    path = write_test_session(
        args.port, 4242, f"http://127.0.0.1:{args.port}/existing-token"
    )
    with patch.object(
        babel, "reopen_existing_session",
        side_effect=AssertionError("--no-ui consulted the UI session registry"),
    ), patch.object(
        babel, "VolcAsrClient", side_effect=RuntimeError("no-ui pipeline reached")
    ):
        try:
            await babel.run(args)
        except RuntimeError as e:
            assert str(e) == "no-ui pipeline reached"
        else:
            raise AssertionError("--no-ui did not continue to its own pipeline")
    assert os.path.exists(path)
    babel.clear_session_file(args.port)


async def bind_failure_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    args.port = 18767
    output = io.StringIO()
    with patch.object(babel, "CaptionUI", BindFailUI), redirect_stdout(output):
        try:
            await babel.run(args)
        except SystemExit as e:
            assert "mBabel did not start" in str(e) and str(args.port) in str(e)
        else:
            raise AssertionError("external port occupancy did not exit")
    assert "continuing without it" not in output.getvalue()


async def bind_race_reopen_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    args.port = 18768
    url = f"http://127.0.0.1:{args.port}/raced-token"

    class RacingUI(DummyUI):
        async def start(self):
            write_test_session(self.port, 4242, url)
            raise OSError(48, "Address already in use")

    with patch.object(babel, "CaptionUI", RacingUI), \
         patch.object(babel, "process_alive", return_value=True), \
         patch.object(babel, "session_http_alive", return_value=True), \
         patch.object(babel.webbrowser, "open", return_value=True) as opened, \
         patch.object(babel, "VolcAsrClient",
                      side_effect=AssertionError("second ASR pipeline started")):
        await babel.run(args)
    opened.assert_called_once_with(url)
    babel.clear_session_file(args.port)


async def different_port_lifecycle_check(temp_dir):
    existing_port = 18769
    existing_url = f"http://127.0.0.1:{existing_port}/first-token"
    existing_path = write_test_session(existing_port, 4242, existing_url)
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    args.port = existing_port + 1
    observed = {}

    def browser_open(url):
        with open(babel.session_file_path(args.port), encoding="utf-8") as f:
            observed.update(json.load(f))
        return True

    with patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel, "VolcAsrClient", HotwordAsr), \
         patch.object(babel.webbrowser, "open", side_effect=browser_open):
        await babel.run(args)
    assert observed["port"] == args.port and observed["url"].endswith(f":{args.port}")
    assert not os.path.exists(babel.session_file_path(args.port))
    assert os.path.exists(existing_path)
    babel.clear_session_file(existing_port)


async def audio_failure_check(temp_dir):
    async def failed_audio(_self):
        raise RuntimeError("no usable microphone detected")

    with patch.object(babel.AudioMixer, "run", failed_audio):
        try:
            await asyncio.wait_for(babel.run(make_args(temp_dir, None)), 2)
        except SystemExit as e:
            assert "no usable microphone detected" in str(e), e
        else:
            raise AssertionError("audio failure did not exit")


async def wav_failure_check(temp_dir):
    async def failed_wav(*_args):
        raise RuntimeError("broken wav")

    with patch.object(babel, "wav_chunks", failed_wav):
        try:
            await asyncio.wait_for(
                babel.run(make_args(temp_dir, os.path.join(temp_dir, "broken.wav"))), 2
            )
        except SystemExit as e:
            assert "broken wav" in str(e), e
        else:
            raise AssertionError("wav producer failure did not exit")


async def audio_mixer_check(temp_dir):
    import array

    config = os.path.join(temp_dir, "mixer.json")
    with open(config, "w", encoding="utf-8") as f:
        f.write('{"microphone":"Wireless Microphone","capture_system":true}')
    queue = asyncio.Queue()
    mixer = babel.AudioMixer(queue, config)
    restarts = []

    async def restarted(refresh):
        restarts.append(refresh)

    mixer.restart_streams = restarted
    present = {
        "microphones": ["Wireless Microphone", "MacBook Pro Microphone"],
        "outputs": ["MacBook Pro Speakers"],
        "default_input": "MacBook Pro Microphone",
        "default_output": "MacBook Pro Speakers",
        "blackhole": "BlackHole 2ch",
    }
    unplugged = {**present, "microphones": ["MacBook Pro Microphone"]}
    with patch.object(babel, "discover_audio_devices",
                      side_effect=[present, unplugged]):
        await mixer.scan(initial=True)
        await mixer.scan()
    assert mixer.microphone == "MacBook Pro Microphone"
    assert restarts == [True]
    assert "disappeared" in mixer.warning

    with patch.object(babel, "discover_audio_devices", return_value=present):
        await mixer.apply("Wireless Microphone", False, "MacBook Pro Speakers")
    assert mixer.microphone == "Wireless Microphone"
    assert restarts == [True, True]
    assert mixer.panel_seen

    class FakeRouter:
        def __init__(self):
            self.calls = []

        def enable(self, speaker, blackhole):
            self.calls.append(("enable", speaker, blackhole))

        def disable(self):
            self.calls.append(("disable",))

    router = FakeRouter()
    mixer.output_router = router
    with patch.object(babel, "discover_audio_devices", return_value=present):
        await mixer.apply("Wireless Microphone", True, "MacBook Pro Speakers")
        await mixer.apply("Wireless Microphone", False, "MacBook Pro Speakers")
    assert router.calls == [
        ("enable", "MacBook Pro Speakers", "BlackHole 2ch"),
        ("disable",),
    ], router.calls

    mixer.capture_system = True
    mixer.system_stream = object()
    pack = lambda value: array.array("h", [value]).tobytes()
    mixer._receive_system(pack(100))
    mixer._receive_system(pack(200))
    mixer._receive_system(pack(300))
    mixer._receive_mic(pack(1000))
    assert list(array.array("h", queue.get_nowait())) == [1200]
    assert mixer.stats["system_drop"] == 1
    assert list(array.array("h", babel.mix_pcm_s16(pack(30000), pack(10000)))) == [32767]
    mixer.system_chunks.clear()
    mixer.paused = True
    mixer._receive_system(pack(200))
    mixer._receive_mic(pack(1000))
    assert queue.empty() and mixer.system_chunks == []


def output_router_check():
    description = babel.multi_output_description("speaker-uid", "blackhole-uid")
    assert description["master"] == "speaker-uid"
    assert description["stacked"] is True
    assert description["subdevices"] == [
        {"uid": "speaker-uid", "drift": False},
        {"uid": "blackhole-uid", "drift": True},
    ]

    state = {"default": 3}
    devices = {1: "MacBook Pro Speakers", 2: "BlackHole 2ch",
               3: "Original Output", 9: "mBabel Multi-Output"}

    def set_default(device_id):
        state["default"] = device_id

    with patch.object(babel, "coreaudio_devices", return_value=devices), \
         patch.object(babel, "coreaudio_device_channels", return_value=2), \
         patch.object(babel, "coreaudio_default_device",
                      side_effect=lambda _selector: state["default"]), \
         patch.object(babel, "coreaudio_string_property", return_value=None), \
         patch.object(babel, "coreaudio_create_multi_output", return_value=9), \
         patch.object(babel, "coreaudio_set_default_output", side_effect=set_default):
        router = babel.CoreAudioOutputRouter()
        router.enable("MacBook Pro Speakers", "BlackHole 2ch")
        assert state["default"] == 9
        router.disable()
        assert state["default"] == 3

        router.enable("MacBook Pro Speakers", "BlackHole 2ch")
        state["default"] = 1  # explicit user change: do not overwrite it on exit
        router.disable()
        assert state["default"] == 1

        state["default"] = 9
        with patch.object(babel, "coreaudio_string_property",
                          return_value="com.mbabel.multi-output.stale"):
            router.enable("MacBook Pro Speakers", "BlackHole 2ch")
        router.disable()
        assert state["default"] == 1  # recover from a prior SIGKILL route


def audio_config_migration_check(temp_dir):
    # explicit legacy path: the default points at the real repo checkout,
    # which must not leak machine state into this check
    legacy = os.path.join(temp_dir, "legacy", "audio_config.json")
    target = os.path.join(temp_dir, "home", ".mbabel", "audio_config.json")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"speaker":"MacBook Pro Speakers"}')
    babel.migrate_audio_config(target, legacy)
    with open(target, encoding="utf-8") as f:
        assert json.load(f)["speaker"] == "MacBook Pro Speakers"
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"speaker":"Other Device"}')
    babel.migrate_audio_config(target, legacy)
    with open(target, encoding="utf-8") as f:
        assert json.load(f)["speaker"] == "MacBook Pro Speakers"  # one-shot copy


async def draft_reset_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyTranslator.calls.clear()
    with patch.object(babel, "VolcAsrClient", DraftAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    drafts = [text for text, kwargs in DummyTranslator.calls if kwargs.get("lite")]
    assert drafts == ["这是第一句非常非常长的内容", "第二句开头六字"], drafts


async def draft_promotion_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyUI.events.clear()
    with patch.object(babel, "VolcAsrClient", DraftCoverageAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    provisional_ids = [event["id"] for event in DummyUI.events
                       if event.get("provisional")]
    assert provisional_ids == [2], provisional_ids


async def short_sentence_refine_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyTranslator.calls.clear()
    DummyUI.events.clear()
    with patch.object(babel, "VolcAsrClient", ShortSentenceAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    refined = [text for text, kwargs in DummyTranslator.calls
               if not kwargs.get("lite")]
    assert refined == ["收到。"], refined
    provisional = [event["id"] for event in DummyUI.events
                   if event.get("provisional")]
    assert provisional == [1], provisional


async def translate_task_pruning_check():
    done = asyncio.create_task(asyncio.sleep(0))
    pending = asyncio.create_task(asyncio.Event().wait())
    await done
    tasks = [done, pending]
    babel.prune_done_tasks(tasks)
    assert tasks == [pending] and not pending.done(), tasks
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)


async def committed_draft_clear_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyTranslator.calls.clear()
    DummyUI.events.clear()
    with patch.object(babel, "VolcAsrClient", CommittedWithResidualAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    stale = [event for event in DummyUI.events if event["type"] == "draft"]
    assert stale == [], stale


async def context_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    DummyTranslator.calls.clear()
    with patch.object(babel, "VolcAsrClient", ContextAsr):
        await babel.run(args)
    sixth = next(kwargs for text, kwargs in DummyTranslator.calls
                 if text == "第6句上下文。")
    context = sixth["context"]
    assert [item["source"] for item in context] == [
        "第2句上下文。", "第3句上下文。", "第4句上下文。", "第5句上下文。"
    ], context
    assert [item["translation"] for item in context] == ["translated"] * 4


async def pause_check(temp_dir):
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyUI.events.clear()
    PauseAsr.sessions = 0
    PauseAsr.close_calls = 0
    with patch.object(babel, "VolcAsrClient", PauseAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    states = [event["paused"] for event in DummyUI.events
              if event.get("type") == "pause_state"]
    assert states == [True, False], states
    assert PauseAsr.sessions == 2
    assert PauseAsr.close_calls == 1
    sources = [event["source"] for event in DummyUI.events
               if event.get("type") == "committed"]
    assert sources == ["暂停前已经说完的话", "恢复后的句子。"], sources


async def hotword_budget_check(temp_dir):
    budget_dir = os.path.join(temp_dir, "budget-hotwords")
    os.mkdir(budget_dir)
    with open(os.path.join(budget_dir, "high.txt"), "w", encoding="utf-8") as f:
        f.write("#priority high\n" + "\n".join(f"Critical{i}" for i in range(98)))
    with open(os.path.join(budget_dir, "low.txt"), "w", encoding="utf-8") as f:
        f.write("#priority low\nlow one\nlow two\n")
    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.hotwords_dir = budget_dir
    output = io.StringIO()
    with patch.object(babel, "VolcAsrClient", HotwordAsr), redirect_stdout(output):
        await babel.run(args)
    assert sum(babel.hotword_token_cost(word) for word in HotwordAsr.direct) <= 100
    assert "Critical97" in HotwordAsr.direct
    assert "low two" not in HotwordAsr.direct
    assert "dropped" in output.getvalue() and "low two" in output.getvalue()


async def reconnect_check(temp_dir):
    queue = asyncio.Queue()
    for value in range(10, 20):
        queue.put_nowait(value)
    tail, dropped = babel.collect_reconnect_tail(range(10), queue)
    assert dropped == 5
    assert tail == list(range(5, 20))
    assert queue.empty()
    assert babel.is_replay_duplicate("已经完整落定。", "前一句已经完整落定。")
    assert not babel.is_replay_duplicate("网络恢复后的正常新句子。", "前一句已经完整落定。")
    assert not babel.is_replay_duplicate("好", "前一句说好")
    assert babel.merge_reconnect_partial(
        "我们这周讨论一下 fiber 网络和 RGB++的进展，还有基于",
        "一、加加的进展，还有基于 UTXO 的状态通道设计。",
    ) == "我们这周讨论一下 fiber 网络和 RGB++的进展，还有基于 UTXO 的状态通道设计。"

    args = make_args(temp_dir, os.path.join(SOLUTION, "test.wav"))
    args.no_ui = False
    DummyUI.events.clear()
    ReconnectingAsr.sessions = 0
    ReconnectingAsr.received.clear()
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    with patch.object(babel, "VolcAsrClient", ReconnectingAsr), \
         patch.object(babel, "CaptionUI", DummyUI), \
         patch.object(babel.webbrowser, "open", return_value=True), \
         patch.object(babel.asyncio, "sleep", fast_sleep):
        await babel.run(args)
    assert len(ReconnectingAsr.received) == 2
    assert ReconnectingAsr.received[0] == ReconnectingAsr.received[1]
    sources = [event["source"] for event in DummyUI.events
               if event["type"] == "committed"]
    assert sources == [
        "前一句已经完整落定。",
        "我们这周讨论一下 fiber 网络和 RGB++的进展，还有基于 UTXO 的状态通道设计。",
        "网络恢复后的正常新句子。",
    ], sources


with tempfile.TemporaryDirectory(prefix="mbabel-p0-") as temp_dir:
    os.mkdir(os.path.join(temp_dir, "hotwords"))
    babel.__file__ = os.path.join(temp_dir, "main.py")
    os.environ["VOLC_ASR_API_KEY"] = "offline-test"
    with patch.object(babel, "build_translator", return_value=DummyTranslator()), \
         patch.object(babel, "ArkTranslator", return_value=DummyTranslator()), \
         patch.object(babel, "VolcAsrClient", DummyAsr), \
         patch.object(babel, "SESSION_DIR", os.path.join(temp_dir, ".mbabel")):
        started = time.monotonic()
        session_registry_check()
        asyncio.run(session_reopen_check(temp_dir))
        asyncio.run(no_ui_bypass_check(temp_dir))
        asyncio.run(bind_failure_check(temp_dir))
        asyncio.run(bind_race_reopen_check(temp_dir))
        asyncio.run(different_port_lifecycle_check(temp_dir))
        asyncio.run(signal_check(temp_dir))
        asyncio.run(immediate_correction_check(temp_dir))
        candidate_check(temp_dir)
        asyncio.run(review_wait_signal_check(temp_dir))
        asyncio.run(audio_failure_check(temp_dir))
        asyncio.run(wav_failure_check(temp_dir))
        output_router_check()
        audio_config_migration_check(temp_dir)
        asyncio.run(audio_mixer_check(temp_dir))
        asyncio.run(draft_reset_check(temp_dir))
        asyncio.run(draft_promotion_check(temp_dir))
        asyncio.run(short_sentence_refine_check(temp_dir))
        asyncio.run(translate_task_pruning_check())
        asyncio.run(committed_draft_clear_check(temp_dir))
        asyncio.run(context_check(temp_dir))
        asyncio.run(pause_check(temp_dir))
        asyncio.run(hotword_budget_check(temp_dir))
        asyncio.run(reconnect_check(temp_dir))
        assert time.monotonic() - started < 5

print("runtime: sessions, D4 persistence/candidates, drafts, pause, mixer, and reconnect pass")
