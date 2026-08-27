# Babel Solution: Live Bilingual Meeting Captions

[中文版](README_CN.md)

Pipeline: mic/system audio → Volcengine Seed-ASR (streaming, hotwords) → Volcengine machine-translation LLM (`matx_translate`, native glossary) → browser caption UI. **One API key covers everything**: both services live in the speech product line (openspeech.bytedance.com) and share `VOLC_ASR_API_KEY`.

- English speech → committed caption shows Chinese translation
- Chinese speech (including code-switched English terms) → committed caption shows English translation
- Interim ASR text and a live draft translation stream on screen before the sentence closes

## How it works

`asr_client.py` implements the Seed-ASR WebSocket binary protocol on the optimized bidirectional endpoint (`bigmodel_async`), with two-pass recognition enabled (`enable_nonstream=true`): interim results arrive fast, and each VAD-closed fragment is re-recognized by the non-streaming model for accuracy, marked `definite: true`. Glossary source terms are pushed to the ASR as hotwords (streaming cap: 100 tokens). A client-side corrections map (`glossary.json -> corrections`) fixes systematic mishearings (MATE→Matt, CKC→CKCon, RGB 加加→RGB++) before display and translation.

`translate.py` default backend is `volc-mt`: the speech product's LLM-based text translation (doc 6561/2306735), which takes a native `glossary_list` and answers in ~0.2–0.9s per sentence. Direction handling: any CJK character means the speaker used Chinese, target English. The glossary table is flipped per direction; measured quirks: enforcement is case-sensitive (we send lowercase and Capitalized variants) and en→zh values work best as mixed strings ("Fiber网络", "Cell模型"), pure-latin values get overridden by entrenched translations (光纤网络). Fallback backends: `ark` (Doubao LLM, needs `ARK_API_KEY`; remember `"thinking": {"type": "disabled"}` or every call wastes ~15s reasoning) and `qwen-mt` (Alibaba, needs `DASHSCOPE_API_KEY`).

## Setup

1. Volcengine speech console (console.volcengine.com/speech): activate 豆包流式语音识别模型 2.0 (hourly billing) AND 机器翻译大模型 (resource `volc.speech.mt`), then create an API key.
2. Environment (the app auto-loads `../.env`):

```bash
VOLC_ASR_API_KEY=...   # the only required key: streaming ASR + translation
# optional fallback backend:
ARK_API_KEY=...
ARK_MODEL=doubao-seed-2-0-lite-260215
```

Verified end-to-end on 2026-08-08. Translation is three-stage since W1: volc-mt drafts (whole QPM budget, 0.6s debounce) -> a draft covering at least 60% of the final source is promoted as the provisional caption at commit (italic ≈) -> an ark refined pass with the previous four source/translation pairs as context replaces it in place (falls back to volc-mt without ark credentials).

3. Install (use a venv):

```bash
pip install -r requirements.txt
```

## Run

```bash
cd CKBA/Babel/solution
.venv/bin/python main.py                      # mic input (default device)
.venv/bin/python main.py --end-window 600     # override VAD silence window (min 300; vi default 400)
.venv/bin/python main.py --wav test.wav       # test with a recording
.venv/bin/python main.py --list-devices       # diagnostic device list
```

Credentials (`../.env`), `glossary.json`, and `../hotwords/*.txt` all load automatically. Hotword files are merged with the glossary terms and fitted to the 100-token streaming limit; `#priority high|normal|low` sections decide which terms survive, and every dropped term is logged.

## Daily meeting usage

One-time audio setup for online meetings (Zoom / Teams / Google Meet / 腾讯会议):

1. Install BlackHole 2ch (`brew install blackhole-2ch`).
2. Keep the meeting app's speaker output on **System Default**. In mBabel's audio panel, choose your listening output and enable **Capture meeting audio**. mBabel creates the Multi-Output route and restores the previous system output on exit.

For every meeting:

```bash
cd CKBA/Babel/solution
.venv/bin/python main.py
```

On startup a browser window opens with the caption UI. The host-only microphone chip opens the device panel: choose a mic and enable “Capture meeting audio” for BlackHole. The choice persists in `~/.mbabel/audio_config.json`; a missing mic falls back to the system default without reconnecting ASR. If BlackHole is absent, in-person mic capture still works and the switch shows the install command. Stop with Ctrl+C.

Test wav must be 16kHz mono 16-bit in a WAV container; convert with:

```bash
ffmpeg -i meeting.wav -ar 16000 -ac 1 -c:a pcm_s16le test.wav
```

## Glossary and hotwords

Edit `glossary.json`: `terms` drives both ASR hotwords (source side) and the translation terminology table; `corrections` maps systematic ASR mishearings to the right form. The `../hotwords/*.txt` files (mined from Telegram and Nervos Talk, see `../hotwords/README.md`) are merged in automatically at startup: English entries and proper nouns also become identity translation terms (kept as-is, e.g. "Nervape"), Chinese entries only feed ASR.

Hotwords travel in two channels. Direct transmission is capped at 100 estimated tokens (CJK characters and whitespace-delimited words), so it carries only table-incompatible terms (anything with punctuation or length >= 10: RGB++, CKB-VM, RISC-V…) plus English proper nouns. Higher `#priority` sections are fitted first and dropped terms print at startup. Everything else — including all Chinese hotwords — is written at startup to `../hotwords/boosting_table.txt` (digits converted to Chinese characters per platform rules). Upload that file once in the speech console 自学习平台, then set `VOLC_BOOSTING_TABLE_ID` in `../.env`; the app passes it as `corpus.boosting_table_id` and both channels stay active together.

## Latency design

- Interim ASR text streams in well under 1s (bidirectional streaming, 200ms packets).
- **Sentence accumulator**: definite ASR fragments (VAD closes them after `--end-window` ms of silence, default 800 for zh-en and 400 for vi pairs) are buffered into a live line. The line commits on sentence-final punctuation (。！？.!?…), at the first comma/semicolon clause boundary after 80 characters, on a 200-char cap, after 2s of ASR quiet time, or on a **language boundary**: a long latin stretch (>=15 letters) counts as English, anything with CJK counts as Chinese, and when the next piece's language differs from the line's, the line commits first and the piece starts a new line. This works inside single ASR fragments too, so a speaker switching languages mid-sentence still yields one line per language with the right translation direction. Short embedded terms (UTXO, Fiber) never trigger splits. Pure fillers (嗯/哦/呃…) are dropped.
- **Live line = repair mechanism**: the growing original and its draft translation update in place in the UI (both 21px, wrapping); nothing is frozen on screen until commit, and the commit carries the two-pass re-recognized, corrections-applied text with one final translation.
- **Speaker labels**: Seed-ASR 2.0 does speaker clustering server-side (`enable_speaker_info=true`, `ssd_version="200"`, no extra latency or cost); each definite fragment carries `additions.speaker_id`. A speaker change flushes the current line (natural turn boundary), and the UI shows a colored 说话人 chip only when the speaker changes.
- **Draft translation**: while a sentence is still growing, the whole accumulated line (fragments + current interim) is translated on a 0.6s debounce and shown as a dashed "≈" draft inside the translations pane. Drafts emit whenever a result newer than the screen arrives; at commit time all in-flight drafts are cancelled so the final translation gets priority.
- **Thinking mode must be disabled** on Doubao Seed 2.x: by default the model burns hundreds of reasoning tokens before answering (measured 17s vs 2s for one sentence). `translate.py` sends `"thinking": {"type": "disabled"}` on every call. This was the single largest latency win.
- Translation model: doubao-seed-2-0-lite, max_tokens 512, temperature 0.1.
- **Corrections map**: `glossary.json` -> `corrections` fixes systematic ASR mishearings client-side before display and translation (MATE -> Matt, CKC -> CKCon), case-insensitive with CJK-safe word boundaries (\b fails between CJK and latin, lookarounds are used instead). Hotword priority sections fit high-value terms first within the direct 100-token budget.

## One-click launch

Double-click `../install-app.command` once to build a clone-local `mBabel.app` in Applications, then launch from Finder, Spotlight, or the Dock. `../Babel.command` remains the direct launcher; both run `main.py --share`, with microphone and system-audio choices in the host device panel.

## Robustness notes (post-audit, 2026-08-07)

- The ASR connection auto-reconnects with capped backoff (2s→10s) on network errors or silent server closes. It retains the newest three seconds of audio and the last uncommitted interim text to complete speech across the break; the re-recognized tail is overlap-merged into that prefix. For ten seconds after recovery, a bounded similarity guard filters replay of the prior committed sentence tail. The UI header shows "ASR reconnecting…" and flips back to "connected" on recovery. Errors before the first result (bad key, service not activated, handshake 401/403) exit with a clear message instead of looping.
- A failed translation is retried once, then shown as "⚠ 翻译失败 translation unavailable" — a sentence can never sit pending forever; failures are not cached.
- Every committed line is appended to `transcripts/babel-<timestamp>.jsonl` (via to_thread, so OneDrive stalls never block the loop) and rendered to `.md` on exit, including on Ctrl-C.
- Browser reconnects replay history idempotently (client dedupes by segment id); history replay uses direct awaited sends, so late joiners work past any history length.
- With `--share`, the page is served only under a random token path (LAN and tunnel URLs both include it); slow or stalled viewers get dropped rather than slowing the pipeline. The cloudflared tunnel starts in the background — startup never waits for it, and the public link appears in the Share dialog when ready.
- Draft translations use a fixed 0.6s debounce and a reduced identity-terms-only glossary (proper nouns), keeping draft cost far below the refined pass without visible lag.
- Audio devices refresh every three seconds. A selected microphone disappearing triggers a visible warning and an in-place fallback to the default microphone; the ASR session and buffered captions stay alive.
- The host-only Pause control actively closes ASR, drains queued audio while paused, and resumes through the existing reconnect loop without replaying the paused interval.


## Experiments and UX (2026-08-07 evening)

- Header controls: language view (bilingual / zh-only / English-only, per viewer), font size presets (S/M/L/投影 XL with header hidden), both persisted per browser.
- Scroll-follow: auto-scroll only while you are at the bottom; a "back to latest" button appears when you read back.
- Lab modal (实验 Lab): disfluency smoothing (DDC) live toggle restarts the ASR session via the reconnect loop. The default ark refined pass carries the previous four source/translation pairs; there is no separate polish toggle.
- Live correction (会中纠错): `wrong=right` in the Lab modal takes effect immediately and is persisted to `glossary.json` corrections on exit. Localhost-only; remote viewers cannot steer the pipeline.
- Timestamps: every committed line carries a session-relative `ts` into the UI and the transcript; export offers SRT (.srt) alongside Markdown.
- Silence flush lowered from 3.0s to 2.0s. ASR reconnect now keeps the latest fifteen 200ms chunks instead of draining the whole queue and overlap-merges the frozen interim prefix; a time-bounded text guard handles replay of the prior committed sentence.


## UX iteration (2026-08-08, from real-meeting feedback)

- Slow/stalled viewers are force-closed with WS code 1013 so the browser's reconnect+replay path catches them up (a cancelled sender alone left frozen-but-green pages).
- Language view selector means "your reading language": single-language mode collapses originals and merges into one reading stream (your language shows the source, the rest shows the translation). Draft events carry a language tag and are filtered accordingly.
- Layout: translations get 2/3 of the height and the bright text color (they are the reading face; originals are gray reference); sticky pane labels; permanent draft slot pinned to the pane bottom (no layout shift); divider button collapses originals to just the live line; latest settled sentence carries a subtle left-edge marker.
- Translation robustness: drafts capped by a semaphore so they never starve refined commits; refined pass retries twice (1s/3s) with failure stats in stderr; a failed line auto-retries once more after 15s and updates in place.
- Live correction relabeled "识别纠错 ASR correction" with the 陈红周=陈泓舟 example.


## W1: draft promotion + context refined (2026-08-08 night)

Root cause of the translation failures: volc-mt enforces a per-ACCOUNT QPM quota (misreported as HTTP 500, body code 55000000 "quota exceeded for types: qpm", effective ~55/min sustained). Dual-key pooling was disproven empirically (quota is per account). Current split:

- Drafts stay on volc-mt (fast, lite identity glossary), cadence 0.6s with a <6-char-in-2s skip threshold; QPM budget is basically all theirs. 2.5s back-off while rate-limited.
- On commit, the sentence's last draft is promoted immediately only when its source snapshot covers at least 60% of the final sentence. Shorter drafts yield to the refined pass instead of showing a misleading fragment.
- The refined pass runs on the ark model with the previous four source/translation pairs as context (glossary in system prompt); it replaces the provisional in place. The old separate "context polish" fourth pass is gone — context is the default. volc-mt remains the fallback engine when ark credentials are absent.
- Backfills (every 15s, up to ~3 min) use the same ark engine. RateLimitError is classified separately with 5s/15s retry waits.
- Lab modal shows 60s request counts per channel (draft/refined/rate-limited). Draft promotion removed the old polish toggle.
