"""Offline D9 checks: profiles, routing, VI gate, and sentence pipeline."""

import asyncio
import gzip
import json
import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import asr_client
import main as babel
from translate import translation_rejection_reason


def request_payload(pair):
    packet = asr_client._build_full_request(asr_client.AsrConfig(pair=pair))
    return json.loads(gzip.decompress(packet[8:]))


zh_packet = asr_client._build_full_request(asr_client.AsrConfig(pair="zh-en"))
zh = json.loads(gzip.decompress(zh_packet[8:]))
assert asr_client.AsrConfig(pair="zh-en").endpoint == asr_client.ENDPOINT
assert zh["request"] == {
    "model_name": "bigmodel", "enable_punc": True, "enable_itn": True,
    "enable_nonstream": True, "enable_speaker_info": True,
    "enable_lid": True, "ssd_version": "200",
    "end_window_size": 800, "show_utterances": True,
    "result_type": "single", "enable_ddc": False,
}
assert gzip.decompress(zh_packet[8:]) == json.dumps(
    {"user": {"uid": "babel"}, "audio": {
        "format": "pcm", "codec": "raw", "rate": 16000,
        "bits": 16, "channel": 1,
    }, "request": zh["request"]}, ensure_ascii=False
).encode("utf-8")
config = asr_client.AsrConfig(pair="zh-en", hotwords=["CKB"],
                              boosting_table_id="table-id")
zh_corpus = json.loads(gzip.decompress(asr_client._build_full_request(config)[8:]))
assert zh_corpus["request"]["corpus"] == {
    "context": json.dumps({"hotwords": [{"word": "CKB"}]}, ensure_ascii=False),
    "boosting_table_id": "table-id",
}
vi_expected = {
    "model_name": "bigmodel", "enable_punc": True,
    "enable_speaker_info": True, "enable_auto_lang": True,
    "end_window_size": 800, "show_utterances": True,
    "result_type": "single",
}
for pair in ("en-vi", "zh-vi"):
    config = asr_client.AsrConfig(pair=pair, hotwords=["CKB"],
                                  boosting_table_id="must-not-leak")
    assert config.endpoint == asr_client.MULTILINGUAL_ENDPOINT
    payload = json.loads(gzip.decompress(asr_client._build_full_request(config)[8:]))
    assert payload["request"] == vi_expected
    assert "corpus" not in payload["request"]
assert request_payload("en-vi") == request_payload("zh-vi")


cases = {
    "zh": ("zh-CN", "今天讨论方案。"),
    "en": ("en-US", "We will discuss the proposal."),
    "vi": ("vi-VN", "Chúng ta thảo luận hôm nay."),
    "ja": ("ja-JP", "今日は提案を話します。"),
    "missing": (None, "Tiếng Việt rất rõ ràng."),
}
for pair in babel.LANGUAGE_PAIRS:
    for key, (tag, text) in cases.items():
        src, tgt, detected, source = babel.resolve_direction(text, pair, tag)
        if pair == "zh-en":
            assert (src, tgt) == babel.detect_direction(text)
            assert source == "fallback"
        else:
            local = babel.PAIR_LANGS[pair][0]
            expected_src = "vi" if key in ("vi", "missing") else key
            expected_tgt = local if expected_src == "vi" else "vi"
            assert (src, tgt, detected) == (expected_src, expected_tgt, expected_src)
            assert source == ("fallback" if key == "missing" else "asr")
assert babel.resolve_direction("plain ASCII", "zh-vi", None) == (
    "zh", "vi", "zh", "fallback"
)


assert translation_rejection_reason("你好", "Xin chào", "vi") is None
assert translation_rejection_reason("hello world", "hello world", "vi") == "same_as_source"
assert translation_rejection_reason("你好", "123", "vi") == "missing_target_vi"
assert translation_rejection_reason("CKB", "CKB", "vi", {"CKB"}) is None
assert translation_rejection_reason("你好", "hello", "en") is None
assert translation_rejection_reason("hello", "你好", "zh") is None


class UI:
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

    async def set_pair(self, pair):
        self.events.append({"type": "pair_state", "pair": pair})


class SentenceAsr:
    instance = None

    def __init__(self, config):
        type(self).instance = self
        self.config = config
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1

    async def transcribe(self, chunks):
        await UI.instance.on_control({"type": "pair", "pair": "en-vi"})
        assert self.config.endpoint == asr_client.MULTILINGUAL_ENDPOINT
        assert request_payload(self.config.pair)["request"] == vi_expected
        yield SimpleNamespace(
            text="",
            utterances=[{
                "definite": True, "text": "Please vote on the proposal.",
                "start_time": 0, "end_time": 100,
                "additions": {"language": "en-US"},
            }],
            is_last=True,
        )


class FastTranslator:
    fail_streak = 0
    usage = {}
    calls = []

    async def translate(self, text, src, tgt, **kwargs):
        type(self).calls.append((src, tgt, dict(kwargs)))
        assert (src, tgt) == ("en", "vi")
        return "Vui lòng bỏ phiếu cho đề xuất."


class RefinedTranslator:
    usage = [0, 0, 0]
    service_tier = "default"
    calls = []

    async def translate(self, text, src, tgt, **kwargs):
        type(self).calls.append((src, tgt, kwargs.get("context")))
        return "Vui lòng bỏ phiếu cho đề xuất này."


async def pipeline_check(root):
    hotwords = os.path.join(root, "hotwords")
    os.mkdir(hotwords)
    glossary = os.path.join(root, "glossary.json")
    with open(glossary, "w", encoding="utf-8") as handle:
        json.dump({"domains": "", "terms": [], "corrections": {}}, handle)
    args = SimpleNamespace(
        wav=os.path.join(os.path.dirname(__file__), "test.wav"),
        audio_config=os.path.join(root, "audio.json"), glossary=glossary,
        hotwords_dir=hotwords, translator="volc-mt", model=None,
        no_ui=False, share=False, port=8765, end_window=800, pair="zh-en",
    )
    UI.events.clear(); FastTranslator.calls.clear(); RefinedTranslator.calls.clear()
    fast = FastTranslator(); refined = RefinedTranslator()
    with patch.object(babel, "__file__", os.path.join(root, "main.py")), \
         patch.object(babel, "SESSION_DIR", os.path.join(root, ".mbabel")), \
         patch.object(babel, "CaptionUI", UI), \
         patch.object(babel, "VolcAsrClient", SentenceAsr), \
         patch.object(babel, "build_translator", return_value=fast), \
         patch.object(babel, "ArkTranslator", return_value=refined), \
         patch.object(babel.webbrowser, "open", return_value=True):
        await babel.run(args)
    assert SentenceAsr.instance.close_calls == 1
    assert FastTranslator.calls == [("en", "vi", {"lite": True})]
    assert len(RefinedTranslator.calls) == 1
    captions = [event for event in UI.events
                if event["type"] in ("committed", "translation")]
    assert [event["type"] for event in captions] == [
        "committed", "translation", "translation"
    ]
    assert captions[1].get("provisional") is True
    assert not [event for event in UI.events
                if event["type"] in ("draft", "interim")]
    transcript_dir = os.path.join(root, "transcripts")
    path = next(os.path.join(transcript_dir, name) for name in os.listdir(transcript_dir)
                if name.endswith(".jsonl") and name != "usage.jsonl")
    with open(path, encoding="utf-8") as handle:
        row = next(json.loads(line) for line in handle if '"source"' in line)
    assert row["pair"] == "en-vi"
    assert row["detected_lang"] == "en" and row["lang_source"] == "asr"
    assert row["speaker"] == "0" and row["refined_latency_ms"] >= 1


os.environ["VOLC_ASR_API_KEY"] = "offline-test"
with tempfile.TemporaryDirectory(prefix="mbabel-d9-") as root:
    asyncio.run(pipeline_check(root))

print("D9: payloads, routing, VI gate, switch, and sentence pipeline pass")
