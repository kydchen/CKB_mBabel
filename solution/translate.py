"""Translation backends with glossary support.

Default backend: Volcengine Ark (Doubao LLM) via the OpenAI-compatible API,
so ASR and translation stay on one vendor and one bill. The glossary is
injected into the system prompt; the model is instructed to output the
translation only.

Alternative backend: Alibaba Qwen-MT (qwen-mt-plus), which has a native
terms API. Select with --translator qwen-mt.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from openai import AsyncOpenAI

try:
    from volcenginesdkarkruntime import AsyncArk
except ImportError:  # optional: only needed for AK/SK auth
    AsyncArk = None

ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@dataclass
class Glossary:
    terms: list[dict]  # [{"source": "...", "target": "..."}]
    domains: str = ""
    corrections: dict = field(default_factory=dict)  # ASR mishearing -> correct form

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            terms=data.get("terms", []),
            domains=data.get("domains", ""),
            corrections=data.get("corrections", {}),
        )

    @property
    def hotwords(self) -> list[str]:
        """Source terms, fed to ASR as hotwords."""
        return [t["source"] for t in self.terms]

    def render_table(self) -> str:
        lines = [f"{t['source']} = {t['target']}" for t in self.terms]
        return "\n".join(lines)


class RateLimitError(RuntimeError):
    """volc-mt reports quota exhaustion as HTTP 500 with body
    {"code": 55000000, "message": "quota exceeded for types: qpm"}.
    Distinct type so callers can back off longer."""


class VolcMtTranslator:
    """Volcengine speech-product machine translation (matx_translate).

    LLM-based text translation on the same product line and the SAME
    X-Api-Key as Seed-ASR, with a native glossary_list ({"原词": "译词"}).
    Measured ~0.7s for a sentence, faster than an Ark chat call.
    """

    URL = "https://openspeech.bytedance.com/api/v3/machine_translation/matx_translate"

    def __init__(self, glossary: Glossary, api_key: str | None = None):
        import httpx

        def with_case_variants(table: dict) -> dict:
            # the API matches glossary keys case-sensitively; ASR output may
            # be "Fiber Network", "fiber network" or "Fiber network"
            out = dict(table)
            for key, value in table.items():
                if key.isascii():
                    out.setdefault(key.lower(), value)
                    out.setdefault(key.lower().capitalize(), value)
            return out

        self.api_key = api_key or os.environ["VOLC_ASR_API_KEY"]
        forward = {t["source"]: t["target"] for t in glossary.terms}
        backward: dict = {}
        for t in glossary.terms:
            # first term wins on key collisions in the flipped table
            backward.setdefault(t["target"], t["source"])
        self.forward = with_case_variants(forward)
        self.backward = with_case_variants(backward)
        # lite tables for drafts: identity terms (proper nouns) only, much
        # smaller prompts; cost control for the per-keystroke draft calls
        self.forward_lite = with_case_variants(
            {s: t for s, t in forward.items() if s == t})
        self.backward_lite = with_case_variants(
            {t: s for s, t in forward.items() if s == t})
        # token accounting per call type: {"draft"|"refined": [prompt, completion, calls]}
        self.usage = {"draft": [0, 0, 0], "refined": [0, 0, 0]}
        self.fail_streak = 0  # consecutive failures; the service flaps in bursts
        self.client = httpx.AsyncClient(timeout=15.0, trust_env=False)

    async def translate(self, text: str, source_lang: str, target_lang: str,
                        lite: bool = False) -> str:
        import uuid

        if lite:
            table = self.forward_lite if target_lang == "en" else self.backward_lite
        else:
            table = self.forward if target_lang == "en" else self.backward

        body = {
            "source_language": source_lang,
            "target_language": target_lang,
            "text_list": [text],
            "corpus": {
                # the API maps 原词 -> 译词, so flip the table by direction
                "glossary_list": table
            },
        }
        headers = {
            "X-Api-Key": self.api_key,
            "X-Api-Resource-Id": "volc.speech.mt",
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }
        resp = await self.client.post(self.URL, json=body, headers=headers)
        if resp.status_code != 200:
            self.fail_streak += 1
            snippet = resp.text[:300]
            if "quota exceeded" in snippet:
                raise RateLimitError(
                    f"volc-mt rate limit (HTTP {resp.status_code}): {snippet}")
            resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 20000000:
            self.fail_streak += 1
            msg = data.get("message") or ""
            if "quota exceeded" in msg:
                raise RateLimitError(
                    f"volc-mt rate limit: {msg} "
                    f"(logid {resp.headers.get('X-Tt-Logid', '')})")
            raise RuntimeError(f"volc-mt error: {data.get('code')} {msg}")
        self.fail_streak = 0
        item = data["data"]["translation_list"][0]
        usage = item.get("usage") or {}
        bucket = self.usage["draft" if lite else "refined"]
        bucket[0] += usage.get("prompt_tokens", 0)
        bucket[1] += usage.get("completion_tokens", 0)
        bucket[2] += 1
        return item["translation"].strip()


# reasoning-leak markers: meta-commentary English words that should never
# appear in a zh translation (observed once: mini deliberating in output)
_LEAK_MARKERS = ("Wait,", "wait no", "source text", "target language",
                 "the user's", "Let's translate", "let's translate",
                 "looking again")


def _looks_like_reasoning(out: str) -> bool:
    hits = sum(1 for m in _LEAK_MARKERS if m in out)
    return hits >= 2


class ArkTranslator:
    """Doubao LLM on Volcengine Ark, glossary via system prompt.

    Auth: AK/SK pair (VOLC_ACCESSKEY / VOLC_SECRETKEY or AccessKeyID /
    SecretAccessKey) via the Ark SDK, or a plain Ark API key (ARK_API_KEY)
    via the OpenAI-compatible endpoint.
    """

    def __init__(self, glossary: Glossary, model: str, api_key: str | None = None):
        bearer = api_key or os.environ.get("ARK_API_KEY") or os.environ.get("apikey")
        ak = os.environ.get("VOLC_ACCESSKEY") or os.environ.get("AccessKeyID")
        sk = os.environ.get("VOLC_SECRETKEY") or os.environ.get("SecretAccessKey")
        if bearer:
            self.client = AsyncOpenAI(api_key=bearer, base_url=ARK_BASE_URL)
        elif ak and sk:
            if AsyncArk is None:
                raise ImportError("pip install 'volcengine-python-sdk[ark]' for AK/SK auth")
            self.client = AsyncArk(ak=ak, sk=sk)
        else:
            raise ValueError("Ark auth missing: set ARK_API_KEY, or AccessKeyID + SecretAccessKey")
        self.usage = [0, 0, 0]  # [prompt_tokens, completion_tokens, calls]
        self.model = model
        domain_note = f" Domain: {glossary.domains}." if glossary.domains else ""
        self.system_prompt = (
            "You are a professional Chinese-English interpreter for tech meetings."
            f"{domain_note}\n"
            "Translate the user's text between Chinese and English. "
            "The target language is given in square brackets. "
            "Output ONLY the translation: no quotes, no explanation, and NEVER "
            "any acknowledgment or conversational prefix such as '好的' or '收到'.\n"
            "Use these terminology mappings exactly:\n"
            f"{glossary.render_table()}"
        )

    async def translate(self, text: str, source_lang: str, target_lang: str,
                        lite: bool = False,
                        context: list[str] | None = None) -> str:
        if context:
            ctx = "\n".join(f"- {c}" for c in context)
            user = (
                f"Recent meeting context for reference only, do NOT translate it:\n"
                f"{ctx}\n\n"
                f"Now translate this sentence into {target_lang}, output ONLY the translation:\n{text}"
            )
        else:
            user = f"[{target_lang}] {text}"

        for attempt in range(2):  # second try only on reasoning-leak output
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=512,
                # Doubao Seed 2.x defaults to thinking mode: it burns hundreds of
                # reasoning tokens and several seconds before answering. Useless
                # for translation, so disable it (17s -> ~2s on a sentence).
                extra_body={"thinking": {"type": "disabled"}},
                extra_headers={"X-Project-Name": "default"},
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.usage[0] += getattr(usage, "prompt_tokens", 0) or 0
                self.usage[1] += getattr(usage, "completion_tokens", 0) or 0
                self.usage[2] += 1
            out = (resp.choices[0].message.content or "").strip()
            if not _looks_like_reasoning(out):
                return out
            print("[translate] reasoning-leak detected, retrying once",
                  file=sys.stderr)
        return out  # rare: return as-is rather than drop the line


class QwenMtTranslator:
    """Alibaba qwen-mt-plus, glossary via the native terms API."""

    def __init__(self, glossary: Glossary, model: str = "qwen-mt-plus", api_key: str | None = None):
        self.client = AsyncOpenAI(
            api_key=api_key or os.environ["DASHSCOPE_API_KEY"],
            base_url=DASHSCOPE_BASE_URL,
        )
        self.model = model
        self.glossary = glossary

    async def translate(self, text: str, source_lang: str, target_lang: str,
                        lite: bool = False) -> str:
        options = {
            "source_lang": source_lang,
            "target_lang": target_lang,
            "terms": self.glossary.terms,
        }
        if self.glossary.domains:
            options["domains"] = self.glossary.domains
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": text}],
            extra_body={"translation_options": options},
        )
        return (resp.choices[0].message.content or "").strip()


def build_translator(backend: str, glossary: Glossary, model: str | None = None):
    if backend == "volc-mt":
        return VolcMtTranslator(glossary)
    if backend == "ark":
        return ArkTranslator(
            glossary,
            model=model or os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215"),
        )
    if backend == "qwen-mt":
        return QwenMtTranslator(glossary, model=model or "qwen-mt-plus")
    raise ValueError(f"unknown translator backend: {backend}")
