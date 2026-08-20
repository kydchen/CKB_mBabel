"""Focused deterministic checks for the P1 completion batch."""

import asyncio
import os
import tempfile
from types import SimpleNamespace

from main import (discard_queued_audio, hotword_token_cost, load_hotwords_dir,
                  trim_hotwords)
from translate import (ArkTranslator, Glossary, _looks_like_reasoning,
                       _render_context, translation_rejection_reason)


assert hotword_token_cost("Cell model") == 2
assert hotword_token_cost("状态租金") == 4
assert hotword_token_cost("Fiber网络") == 3

kept, dropped, used = trim_hotwords([
    ("critical term", 2),
    ("normal", 1),
    ("low value phrase", 0),
], limit=3)
assert kept == ["critical term", "normal"]
assert dropped == ["low value phrase"]
assert used <= 3

queue = asyncio.Queue()
for chunk in (b"a", b"b", None):
    queue.put_nowait(chunk)
assert discard_queued_audio(queue) == 2
assert queue.get_nowait() is None
assert queue.empty()

with tempfile.TemporaryDirectory(prefix="mbabel-hotwords-") as temp_dir:
    with open(os.path.join(temp_dir, "terms.txt"), "w", encoding="utf-8") as f:
        f.write("#priority high\nCKB\n#priority low\ncommon phrase\n")
    assert load_hotwords_dir(temp_dir) == [("CKB", 2), ("common phrase", 0)]

context = [
    {"source_lang": "zh", "source": "状态租金保持不变。",
     "translation": "The state rent remains unchanged."},
    {"source_lang": "en", "source": "It applies to every Cell.",
     "translation": "它适用于每个 Cell。"},
]
assert _render_context(context) == (
    "- zh: 状态租金保持不变。 / en: The state rent remains unchanged.\n"
    "- en: It applies to every Cell. / zh: 它适用于每个 Cell。"
)
assert _looks_like_reasoning("The answer.\nExtra explanation.")
assert not _looks_like_reasoning("The state rent remains unchanged.")

identity = {"CKB", "CKCON", "RGB++", "Matt Quinn"}
assert translation_rejection_reason("Test, test.", "Test, test.", "zh", identity) == "same_as_source"
assert translation_rejection_reason("你能听到吗？", "你能听到吗？", "en", identity) == "same_as_source"
assert translation_rejection_reason("Can you hear me?", "Yes, I can.", "zh", identity) == "missing_target_zh"
assert translation_rejection_reason("你能听到吗？", "当然可以。", "en", identity) == "missing_target_en"
assert translation_rejection_reason("CKB", "CKB", "zh", identity) is None
assert translation_rejection_reason("CKCON", "CKCON", "zh", identity) is None
assert translation_rejection_reason("RGB++", "RGB++", "zh", identity) is None
assert translation_rejection_reason("Matt Quinn", "Matt Quinn", "zh", identity) is None
assert translation_rejection_reason("2026", "2026", "zh", identity) is None
assert translation_rejection_reason("短句", "A" * 81, "en", identity) == "excessive_length"


class TierError(Exception):
    status_code = 400


class StubArkCompletions:
    def __init__(self):
        self.calls = []
        self.reject_fast = False

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.reject_fast and kwargs["extra_body"].get("service_tier") == "fast":
            self.reject_fast = False
            raise TierError("service_tier fast is not enabled")
        return SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content="测试通过"))],
        )


async def ark_tier_check():
    original = os.environ.pop("ARK_SERVICE_TIER", None)
    try:
        translator = ArkTranslator(Glossary([]), "offline-model", api_key="offline")
        completions = StubArkCompletions()
        translator.client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions)
        )

        await translator.translate("It works.", "en", "zh")
        assert completions.calls[-1]["extra_body"] == {
            "thinking": {"type": "disabled"}
        }

        assert translator.set_service_tier("fast") == "fast"
        fast_meta = {}
        await translator.translate("It works.", "en", "zh",
                                   request_meta=fast_meta)
        assert completions.calls[-1]["extra_body"]["service_tier"] == "fast"
        assert fast_meta == {"tier_fallback": False, "service_tier": "fast"}

        completions.reject_fast = True
        meta = {}
        await translator.translate("It works.", "en", "zh", request_meta=meta)
        assert [call["extra_body"].get("service_tier")
                for call in completions.calls[-2:]] == ["fast", None]
        assert meta == {"tier_fallback": True, "service_tier": "default"}
        assert translator.service_tier == "default"
        assert translator.set_service_tier("fast") == "default"

        await translator.translate("It still works.", "en", "zh")
        assert "service_tier" not in completions.calls[-1]["extra_body"]

        os.environ["ARK_SERVICE_TIER"] = "fast"
        env_translator = ArkTranslator(Glossary([]), "offline-model", api_key="offline")
        assert env_translator.service_tier == "fast"
    finally:
        if original is None:
            os.environ.pop("ARK_SERVICE_TIER", None)
        else:
            os.environ["ARK_SERVICE_TIER"] = original


asyncio.run(ark_tier_check())

print("P1: context, translation gate, Ark tiers, and priority token budget pass")
