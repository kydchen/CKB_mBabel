"""Offline regression checks for P0 runtime behavior."""

import asyncio
import os
import signal
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

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


class DummyUI:
    events = []

    def __init__(self, host, port, token=None):
        self.port = port
        self.control_token = "offline"
        self.on_control = None

    async def start(self):
        pass

    async def emit(self, event):
        self.events.append(dict(event))

    async def set_share(self, lan, public):
        pass


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
    asyncio.get_running_loop().call_later(
        0.2, os.kill, os.getpid(), signal.SIGTERM
    )
    await babel.run(make_args(temp_dir, os.path.join(SOLUTION, "test.wav")))
    transcript_dir = os.path.join(temp_dir, "transcripts")
    markdown = [name for name in os.listdir(transcript_dir) if name.endswith(".md")]
    assert len(markdown) == 1, markdown
    with open(os.path.join(transcript_dir, markdown[0]), encoding="utf-8") as f:
        assert "测试句。" in f.read()


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


with tempfile.TemporaryDirectory(prefix="mbabel-p0-") as temp_dir:
    os.mkdir(os.path.join(temp_dir, "hotwords"))
    babel.__file__ = os.path.join(temp_dir, "main.py")
    os.environ["VOLC_ASR_API_KEY"] = "offline-test"
    with patch.object(babel, "build_translator", return_value=DummyTranslator()), \
         patch.object(babel, "ArkTranslator", return_value=DummyTranslator()), \
         patch.object(babel, "VolcAsrClient", DummyAsr):
        started = time.monotonic()
        asyncio.run(signal_check(temp_dir))
        asyncio.run(audio_failure_check(temp_dir))
        asyncio.run(wav_failure_check(temp_dir))
        asyncio.run(audio_mixer_check(temp_dir))
        asyncio.run(draft_reset_check(temp_dir))
        asyncio.run(committed_draft_clear_check(temp_dir))
        assert time.monotonic() - started < 5

print("runtime: draft reset/clear, SIGTERM save, producer failures, and mixer fallback pass")
