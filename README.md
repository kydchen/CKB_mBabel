# CKB mBabel

[中文版](README_CN.md)

Real-time bilingual (zh↔en) meeting captions. Listens to a meeting, shows the original transcript and its translation side by side in a browser, with speaker labels, domain hotwords, and one-click Markdown export. Built for mixed Chinese/English teams where half the room cannot follow the other half.

No audio is stored locally; audio is processed by Volcengine cloud APIs.

**Live demo:** https://kydchen.github.io/CKB_mBabel/ — a simulated replay of a bilingual dev standup; click through every control, no setup needed.

![layout](docs/screenshot.png)

## Features

- **Both directions, one stream**: Chinese speech gets English captions, English speech gets Chinese captions, language detection and code-switching handled per sentence.
- **Three-stage translation, no waiting**: a rough draft (≈ Quick draft) follows the sentence as it is spoken; the moment the sentence ends the draft is promoted as the provisional caption, then a context-aware refined pass (previous sentences as context — pronouns and ellipsis-heavy Chinese resolved) silently replaces it. You never see a "translating…" placeholder.
- **Speaker labels**: server-side speaker clustering; names sit in the script margin, and the names you assign at export time update the page live.
- **Domain adaptation without training**: ASR hotwords (direct + boosting table), a client-side corrections map for systematic mishearings, and a translation glossary enforced per sentence.
- **Live corrections (host only)**: fix a recurring mishearing mid-meeting from the Lab panel (`错词=正确词`); applies to all following sentences instantly and persists to the glossary on exit.
- **Viewer preferences**: per-browser language view (bilingual / Chinese only / English only), four font sizes including a full-screen presentation mode; captions auto-follow only while you are at the bottom, with a "back to latest" button after scrolling up.
- **Shareable, read-only for viewers**: LAN link out of the box; a public `trycloudflare.com` link is one flag away (no account needed). Pipeline controls require a host token that is never sent to LAN or tunneled viewers.
- **Export**: Markdown or timestamped SRT — original, translation, or bilingual — with speaker names you type once and the browser remembers.
- **Robust for long meetings**: ASR auto-reconnects with backoff, rate limits are classified and backed off (failed lines quietly backfill), language-misidentified sentences get a "mishearing?" marker, and the full transcript auto-saves to disk (JSONL + Markdown) regardless of the browser.

## Architecture

```
mic / system audio (BlackHole + Aggregate Device on macOS)
  → Volcengine Seed-ASR 2.0 (streaming, two-pass, hotwords, speaker info)
  → sentence accumulator (language-boundary split, silence watchdog)
  → live draft: Volcengine MT (matx_translate, native glossary_list)
  → at commit: draft promoted to the caption, then replaced by a
    context-aware refined pass (Doubao Seed mini on Ark)
  → local web UI (one port: HTTP page + WebSocket events)
```

One Volcengine speech-console API key covers ASR and the live drafts; an optional Ark key enables the context-aware refined pass (recommended). Total latency: interim text <1s, a readable caption the instant a sentence closes, refined replacement a couple of seconds later — invisibly.

## Cost

Prices as of 2026-08, check the consoles before publishing numbers derived from these.

| Solution | Recognition | Translation | Total per hour |
|---|---|---|---|
| **Babel (Volcengine)** | Seed-ASR 2.0: ¥3.5/h ($0.49) pay-as-you-go; ¥0.93/h ($0.13) with the ¥28/30h ($3.9) pack | MT LLM: ¥1.62/M tokens ($0.23, pack); a meeting hour is well under 100k tokens | **≈ ¥3.5/h ($0.49); ≈ ¥0.5/h ($0.07) on packs** |
| iFlytek 同传 LLM tier | — | — | ¥40.8/h ($5.7), sold as ¥4080 ($570) / 100h |
| Google stack | Cloud STT Chirp $0.016/min ≈ $0.96/h (¥6.8) | Cloud Translation $20/M chars ≈ $0.2/h (¥1.4) | ≈ $1.2/h (¥8.5), plus mainland-China network workarounds |

Babel is ~12x cheaper than iFlytek's LLM-tier simultaneous interpretation while running newer models, and ~2.5x cheaper than a self-assembled Google stack.

## Setup

### 1. Volcengine (one API key)

1. Open the speech console: `console.volcengine.com/speech`.
2. Activate **豆包流式语音识别模型 2.0** (hourly billing, resource `volc.seedasr.sauc.duration`) and **机器翻译大模型** (resource `volc.speech.mt`).
3. Create an API key in the speech console (API Key 管理).
4. Copy `.env.example` to `.env` and fill in `VOLC_ASR_API_KEY`. Optionally add `ARK_API_KEY` (console.volcengine.com/ark) to enable the context-aware refined pass — without it, refined falls back to the same MT as the drafts.

Optional: for hotwords beyond the 100-token direct cap, upload `hotwords/boosting_table.txt` in 自学习平台 → 热词管理 and set `VOLC_BOOSTING_TABLE_ID` in `.env`.

### 2. Install

```bash
cd solution
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # use a mirror if PyPI is slow
```

### 3. Audio routing (macOS)

- In-person meetings: nothing to do, the MacBook mic works.
- Online meetings (Zoom/Teams/Meet): install BlackHole (`brew install blackhole-2ch`), then in Audio MIDI Setup:
  - a **Multi-Output Device** with your speakers + BlackHole 2ch (you hear the meeting, Babel gets a copy);
  - an **Aggregate Device** with BlackHole 2ch + your microphone (remote voices + your voice), if your own speech should be captioned too.
- Set the meeting app's output to the Multi-Output Device.

### 4. Run

```bash
cd solution
.venv/bin/python main.py --list-devices                       # find device indexes
.venv/bin/python main.py --device <aggregate idx> --channels 4
# or simply, for the default mic:
.venv/bin/python main.py
```

A browser window opens with the caption page. Useful flags:

- `--share` — print a LAN link, and a public `trycloudflare.com` link if `cloudflared` is installed (`brew install cloudflared`); the page is served under a random token path.
- `--translator {volc-mt,ark,qwen-mt}` — translation backend; default `volc-mt`.
- `--end-window 600` — ms of silence that closes an ASR fragment.
- `--port 8899` — caption UI port (default 8765), for running two instances side by side.
- `--wav test.wav` — replay a 16kHz mono WAV instead of the mic (a sample is included).
- `--no-ui` — terminal-only output.

Double-clickable launcher: edit the device index in `Babel.command`, then double-click it from Finder.

## Domain adaptation

- `hotwords/hotwords.txt`, `hotwords/hotwords_zh.txt` — ASR hotwords, one per line. English proper nouns also become identity terms in the translation glossary (kept as-is).
- `solution/glossary.json` — `terms`: zh↔en pairs for translation enforcement (values should be mixed forms like "Fiber网络" for en→zh; matching is case-sensitive, variants are generated). `corrections`: ASR mishearing fixes applied before display and translation.
- `hotwords/boosting_table.txt` — regenerated on every run from the merged list (platform rules: <10 chars, no punctuation, digits spelled as Chinese characters). Upload it when the list changes.

The shipped lists are curated for the Nervos CKB blockchain ecosystem (mined from community Telegram chats and the Nervos Talk forum). Replace them with your own domain vocabulary.

## Repository layout

```
solution/     the app (ASR client, translators, sentence accumulator, UI)
hotwords/     domain hotword lists + generated boosting table
Babel.command macOS double-click launcher
.env.example  credentials template
```

Transcripts land in `solution/transcripts/` (created at runtime, gitignored).

## Notes and limits

- Speaker labels are cluster ids (说话人 1/2/…), stable within a session, not named identities.
- Cloud processing means meeting audio leaves the machine; for fully offline needs this stack is not the answer.
- No pause button yet: the ASR server closes idle sessions after ~8s, so a real pause needs coordinated sender/reconnect logic (planned).
- Tested on macOS (Apple Silicon). The audio-capture layer is cross-platform in principle; the routing instructions are macOS-specific.

## License

MIT
