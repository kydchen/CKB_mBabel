"""Focused deterministic checks for the P1 completion batch."""

import asyncio
import os
import tempfile

from main import (discard_queued_audio, hotword_token_cost, load_hotwords_dir,
                  trim_hotwords)
from translate import _looks_like_reasoning, _render_context


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

print("P1: paired context, newline guard, and priority token budget pass")
