# CKB mBabel

[中文版](README_CN.md)

Real-time bilingual (zh↔en) meeting captions. Listens to a meeting, shows the original transcript and its translation side by side in a browser, with speaker labels, domain hotwords, and one-click Markdown export. Built for mixed Chinese/English teams where half the room cannot follow the other half.

No audio is stored locally; audio is processed by Volcengine cloud APIs.

**Live demo:** https://kydchen.github.io/CKB_mBabel/ — a simulated replay of a bilingual dev standup; click through every control, no setup needed.

![layout](docs/screenshot.png)

## Features

- **Both directions, one stream**: Chinese speech gets English captions, English speech gets Chinese captions, language detection and code-switching handled per sentence.
- **Three-stage translation, coverage-aware**: a rough draft (≈ Quick draft) follows the sentence as it is spoken. At commit it is promoted only when its source snapshot covers at least 60% of the final sentence; otherwise the context-aware refined pass lands directly instead of briefly showing a misleading fragment.
- **Speaker labels**: server-side speaker clustering; names sit in the script margin, and the names you assign at export time update the page live.
- **Domain adaptation without training**: ASR hotwords (direct + boosting table), a client-side corrections map for systematic mishearings, and a translation glossary enforced per sentence.
- **Live corrections (host only)**: fix a recurring mishearing mid-meeting from the Lab panel (`错词=正确词`); applies to all following sentences instantly and persists to the glossary on exit.
- **Meeting-style device controls (host only)**: pick or hot-switch the microphone and listening output in the browser, optionally mix in BlackHole system audio, and automatically fall back if a selected device disappears. mBabel builds the Multi-Output route for online meetings and restores the previous system output on exit.
- **Viewer preferences**: per-browser language view (bilingual / Chinese only / English only), four font sizes including a full-screen presentation mode; captions auto-follow only while you are at the bottom, with a "back to latest" button after scrolling up.
- **Shareable, read-only for viewers**: LAN link out of the box; a public `trycloudflare.com` link is one flag away (no account needed). Pipeline controls require a host token that is never sent to LAN or tunneled viewers.
- **Export**: Markdown or timestamped SRT — original, translation, or bilingual — with speaker names you type once and the browser remembers.
- **Robust for long meetings**: ASR auto-reconnects with backoff, rate limits are classified and backed off (failed lines quietly backfill), language-misidentified sentences get a "mishearing?" marker, and the full transcript auto-saves to disk (JSONL + Markdown) regardless of the browser.

## Architecture

```
selected mic + optional BlackHole → 200ms software mixer (mono s16le)
  → Volcengine Seed-ASR 2.0 (streaming, two-pass, hotwords, speaker info)
  → sentence accumulator (language-boundary split, silence watchdog)
  → live draft: Volcengine MT (matx_translate, native glossary_list)
  → at commit: sufficiently complete draft promoted, then replaced by a
    context-aware refined pass (Doubao Seed mini on Ark)
  → local web UI (one port: HTTP page + WebSocket events)
```

One Volcengine speech-console API key covers ASR and the live drafts; an optional Ark key enables the context-aware refined pass (recommended). Total latency: interim text <1s; a draft covering at least 60% is readable the instant a sentence closes, otherwise the refined caption lands a couple of seconds later.

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

Optional macOS app: double-click `install-app.command` once. It builds a local
`mBabel.app` for this clone in `/Applications` (or `~/Applications`).

### 3. Audio routing (macOS)

- In-person meetings: nothing to do, the MacBook mic works.
- Online meetings (Zoom/Teams/Meet): install BlackHole (`brew install blackhole-2ch`). In mBabel's audio panel, choose your listening output and enable **Capture meeting audio**.
- Keep the meeting app's speaker output on **System Default**. mBabel creates or reuses its own Multi-Output route while capture is enabled and restores the previous system output when capture is disabled or the app exits. No Audio MIDI Setup is required.

### 4. Run

```bash
cd solution
.venv/bin/python main.py
```

A browser window opens with the caption page. Useful flags:

- `--share` — print a LAN link, and a public `trycloudflare.com` link if `cloudflared` is installed (`brew install cloudflared`); the page is served under a random token path.
- `--translator {volc-mt,ark,qwen-mt}` — translation backend; default `volc-mt`.
- `--end-window 600` — ms of silence that closes an ASR fragment.
- `--port 8899` — caption UI port (default 8765), for running two instances side by side.
- `--wav test.wav` — replay a 16kHz mono WAV instead of the mic (a sample is included).
- `--no-ui` — terminal-only output.

Double-click `mBabel.app` (after the optional install above) or `Babel.command`. The host-only microphone chip opens the device panel; settings persist outside the synced repository in `~/.mbabel/audio_config.json`.

## Domain adaptation

- `hotwords/hotwords.txt`, `hotwords/hotwords_zh.txt` — ASR hotwords, one per line. `#priority high|normal|low` starts a priority section; low-priority terms are trimmed first at the 100-token direct cap. English proper nouns also become identity terms in the translation glossary (kept as-is).
- `solution/glossary.json` — `terms`: zh↔en pairs for translation enforcement (values should be mixed forms like "Fiber网络" for en→zh; matching is case-sensitive, variants are generated). `corrections`: ASR mishearing fixes applied before display and translation.
- `hotwords/boosting_table.txt` — regenerated on every run from the merged list (platform rules: <10 chars, no punctuation, digits spelled as Chinese characters). Upload it when the list changes.

The shipped lists are curated for the Nervos CKB blockchain ecosystem (mined from community Telegram chats and the Nervos Talk forum). Replace them with your own domain vocabulary.

## Repository layout

```
solution/     the app (ASR client, translators, sentence accumulator, UI)
hotwords/     domain hotword lists + generated boosting table
Babel.command macOS double-click launcher
install-app.command builds a local mBabel.app for this clone
.env.example  credentials template
```

Transcripts land in `solution/transcripts/` (created at runtime, gitignored).

## Notes and limits

- Speaker labels are cluster ids (说话人 1/2/…), stable within a session, not named identities.
- Cloud processing means meeting audio leaves the machine; for fully offline needs this stack is not the answer.
- The host-only Pause control closes ASR and discards audio until Resume; recovery uses the normal reconnect path.
- Tested on macOS (Apple Silicon). The audio-capture layer is cross-platform in principle; the routing instructions are macOS-specific.

## License

MIT
