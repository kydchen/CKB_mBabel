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


class DummyUI:
    def __init__(self, host, port, token=None):
        self.port = port
        self.control_token = "offline"
        self.on_control = None

    async def start(self):
        pass

    async def emit(self, event):
        pass

    async def set_share(self, lan, public):
        pass


def make_args(temp_dir, wav):
    return SimpleNamespace(
        wav=wav,
        device=None,
        channels=1,
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
    async def failed_mic(*_args, **_kwargs):
        raise RuntimeError("simulated device disconnect")

    import sounddevice

    with patch.object(sounddevice, "query_devices",
                      return_value={"max_input_channels": 2}), \
         patch.object(babel, "mic_chunks", failed_mic):
        try:
            await babel.run(make_args(temp_dir, None))
        except SystemExit as e:
            assert "simulated device disconnect" in str(e), e
        else:
            raise AssertionError("audio failure did not exit")


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
        asyncio.run(draft_reset_check(temp_dir))
        assert time.monotonic() - started < 5

print("runtime: draft reset, SIGTERM save, and audio-failure exit pass")
