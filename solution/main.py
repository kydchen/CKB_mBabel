"""Babel: live bilingual captions for zh/en meetings.

Pipeline: mic or wav file -> Volcengine Seed-ASR (streaming, hotwords)
        -> translation API (glossary-injected) -> terminal captions.

Interim ASR text is shown live; when the ASR marks an utterance as definite
(VAD-closed, two-pass re-recognized), it is translated and committed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import sys
import time
import wave
import webbrowser

from websockets.exceptions import ConnectionClosedError, InvalidStatus

from asr_client import AsrConfig, AsrError, VolcAsrClient
from translate import ArkTranslator, Glossary, build_translator
from ui_server import CaptionUI


def load_dotenv(path: str) -> None:
    """Minimal .env loader: KEY=value lines, '#' comments. Later duplicate
    keys override earlier ones — deliberate: the file may append a 主账号
    section reusing the same key names, and the later (root) pair must win."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def load_hotwords_dir(path: str) -> list[str]:
    """Read *.txt hotword files (one entry per line, '#' comments).
    Skips boosting_table.txt: it is a generated derivative, not a source."""
    words: list[str] = []
    if not os.path.isdir(path):
        return words
    for name in sorted(os.listdir(path)):
        if not name.endswith(".txt") or name == "boosting_table.txt":
            continue
        for line in open(os.path.join(path, name), encoding="utf-8"):
            word = line.strip()
            if word and not word.startswith("#"):
                words.append(word)
    return words

SAMPLE_RATE = 16000
CHUNK_MS = 200  # 200ms per packet is optimal for the bidirectional endpoint
CHUNK_BYTES = SAMPLE_RATE * CHUNK_MS // 1000 * 2  # s16le mono

def print_safe(*args, **kwargs) -> None:
    """print() that survives a dead pty: closing the terminal window delivers
    SIGHUP with the tty already gone, so status prints during cleanup raise
    EIO — and must never abort the transcript/glossary writes that follow."""
    try:
        print(*args, **kwargs)
    except OSError:
        pass


# ANSI styles
DIM = "\033[2m"
BOLD = "\033[1m"
CLEAR_LINE = "\033[2K\r"
RESET = "\033[0m"

CJK_RE = re.compile(r"[一-鿿]")
LATIN_RE = re.compile(r"[A-Za-z]")


def detect_direction(text: str) -> tuple[str, str]:
    """Return (source_lang, target_lang). Any CJK char means the speaker
    used Chinese (possibly code-switched), so translate to English."""
    if CJK_RE.search(text):
        return "zh", "en"
    return "en", "zh"


def dominant_lang(text: str) -> str:
    """Rough majority vote by character class, for language-boundary splits."""
    cjk = len(CJK_RE.findall(text))
    latin = len(LATIN_RE.findall(text))
    return "zh" if cjk >= latin else "en"


# Python's \w matches CJK, which would let a latin run swallow following
# Chinese text; keep every character class strictly ASCII.
LONG_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'’&+\-]*(?:\s+[A-Za-z0-9'’&+\-.,?!…]+)*")


def split_lang_runs(text: str) -> list[tuple[str, str]]:
    """Split text into (lang, chunk) pieces. A latin stretch counts as
    English speech only when it is at least 15 letters AND 4 words — short
    runs and multi-letter terms (Lightning Network, Cell model) embedded in
    Chinese stay with the Chinese chunk. Anything containing a CJK
    character counts as Chinese."""
    pieces: list[tuple[str, str]] = []
    pos = 0
    for m in LONG_LATIN.finditer(text):
        letters = sum(1 for c in m.group(0) if c.isascii() and c.isalpha())
        words = len(m.group(0).split())
        if letters < 15 or words < 4 or CJK_RE.search(m.group(0)):
            continue  # term or too short, or (defensively) contains CJK
        pre = text[pos : m.start()]
        if pre:
            pieces.append(("zh" if CJK_RE.search(pre) else "en", pre))
        pieces.append(("en", m.group(0)))
        pos = m.end()
    rest = text[pos:]
    if rest:
        pieces.append(("zh" if CJK_RE.search(rest) else "en", rest))
    return pieces


def downmix_to_mono(data: bytes, channels: int) -> bytes:
    """Average N interleaved s16le channels into mono."""
    import array

    samples = array.array("h")
    samples.frombytes(data)
    out = array.array("h", bytes(len(samples) // channels * 2))
    for i in range(0, len(samples), channels):
        out[i // channels] = int(sum(samples[i : i + channels]) / channels)
    return out.tobytes()


async def mic_chunks(queue: asyncio.Queue, device: int | str | None = None,
                     channels: int = 1) -> None:
    """Capture mic audio with sounddevice; None signals end.

    channels > 1 is for an Aggregate Device that merges several sources
    (e.g. BlackHole for remote participants + a microphone for yourself);
    all channels are downmixed to mono.
    """
    import sounddevice as sd

    loop = asyncio.get_running_loop()

    dropped = {"n": 0}

    def callback(indata, frames, time_info, status):
        data = bytes(indata)
        if channels > 1:
            data = downmix_to_mono(data, channels)

        def _offer() -> None:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # drop the OLDEST chunk, not the new one, and make it loud;
                # this is the buffer that carries us through ASR reconnects
                try:
                    queue.get_nowait()
                    queue.put_nowait(data)
                except Exception:
                    pass
                dropped["n"] += 1
                if dropped["n"] % 50 == 1:
                    print(f"[audio] queue full, dropped oldest chunks: "
                          f"{dropped['n']}", file=sys.stderr)

        loop.call_soon_threadsafe(_offer)

    with sd.RawInputStream(
        device=device,
        samplerate=SAMPLE_RATE,
        blocksize=SAMPLE_RATE * CHUNK_MS // 1000,
        dtype="int16",
        channels=channels,
        callback=callback,
    ) as stream:
        while True:
            await asyncio.sleep(0.5)
            if not stream.active:
                raise RuntimeError("audio input stream stopped")


async def wav_chunks(path: str, queue: asyncio.Queue) -> None:
    """Feed a 16kHz mono s16le wav file in real time, then send None."""
    with wave.open(path, "rb") as wf:
        if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (
            SAMPLE_RATE,
            1,
            2,
        ):
            raise ValueError("wav must be 16kHz mono 16-bit; convert with: "
                             "ffmpeg -i in.wav -ar 16000 -ac 1 -c:a pcm_s16le out.wav")
        while True:
            data = wf.readframes(CHUNK_BYTES // 2)
            if not data:
                break
            await queue.put(data)
            await asyncio.sleep(CHUNK_MS / 1000)  # real-time pacing
    await queue.put(None)


async def chunk_iterator(queue: asyncio.Queue):
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        yield chunk


def _append_jsonl(path: str, record: dict) -> None:
    """Synchronous file append, meant for asyncio.to_thread so a stalling
    File Provider (OneDrive) never blocks the event loop."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def lan_ip() -> str:
    import socket
    import subprocess

    for iface in ("en0", "en1"):  # macOS: prefer the real Wi-Fi interface
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=2)
            ip = out.stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def maybe_tunnel(port: int):
    """Start a cloudflared quick tunnel if the binary exists; return
    (proc, public_url). Quick tunnels need no account or config."""
    import shutil

    exe = shutil.which("cloudflared")
    if not exe:
        return None, None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            for _ in range(60):
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
                if not line:
                    break
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              line.decode(errors="ignore"))
                if m:
                    return proc, m.group(0)
        except asyncio.CancelledError:
            proc.terminate()  # Ctrl-C during tunnel setup: no orphan
            raise
        except Exception:
            pass
        proc.terminate()  # no URL found in time: never leave an orphan tunnel
    except Exception:
        return None, None
    return None, None


async def run(args) -> None:
    device = args.device
    if isinstance(device, str):
        try:
            device = int(device)
        except ValueError:
            pass  # sounddevice accepts a device name substring
    if not args.wav:
        import sounddevice as sd

        try:
            info = sd.query_devices(device, "input")
            max_channels = int(info["max_input_channels"])
            if not 1 <= args.channels <= max_channels:
                raise ValueError(
                    f"requested {args.channels} channels, device supports {max_channels}"
                )
        except Exception as e:
            print(f"[audio] cannot use input device {device!r}: {e}", file=sys.stderr)
            print("[audio] available devices:", file=sys.stderr)
            devices = sd.query_devices()
            print(devices if len(devices) else "(none detected)", file=sys.stderr)
            raise SystemExit(2)

    glossary = Glossary.load(args.glossary)
    # Merge hotword files: English/proper nouns become identity translation
    # terms (kept as-is, e.g. "Meepo"), Chinese entries only feed ASR.
    # ASR hotword priority: curated files first, glossary terms after, so the
    # 100-token streaming cap truncates low-value entries, not curated ones.
    file_words = load_hotwords_dir(args.hotwords_dir)
    sources = {t["source"] for t in glossary.terms}
    for word in file_words:
        if word not in sources:
            if not CJK_RE.search(word):
                glossary.terms.append({"source": word, "target": word})
            sources.add(word)
    hotwords_all = list(dict.fromkeys(file_words + [t["source"] for t in glossary.terms]))

    # Split hotwords into two channels: direct transmission (cap 100 tokens,
    # for table-incompatible entries and English proper nouns) and a
    # 自学习平台 boosting table (thousands of entries, but strict format:
    # <10 chars, no punctuation, digits written as Chinese characters).
    DIGITS = str.maketrans("0123456789", "零一二三四五六七八九")

    def table_form(word: str) -> str | None:
        w = word.translate(DIGITS)
        if re.search(r"[^\w一-鿿]", w):  # punctuation/symbols not allowed
            return None
        if len(w) >= 10 or len(w.encode("utf-8")) > 30:
            return None
        return w

    table_words: list[str] = []
    incompatible: list[str] = []
    for w in hotwords_all:
        tf = table_form(w)
        if tf is None:
            incompatible.append(w)
        else:
            table_words.append(tf)
    direct = list(dict.fromkeys(
        incompatible + [w for w in hotwords_all
                        if table_form(w) is not None and not CJK_RE.search(w)]
    ))
    if len(direct) > 100:
        print(f"[hotwords] direct list has {len(direct)} entries; "
              f"sending the first 100, the rest ride the boosting table")
        direct = direct[:100]
    table_words = list(dict.fromkeys(table_words))
    table_path = os.path.join(args.hotwords_dir, "boosting_table.txt")
    with open(table_path, "w", encoding="utf-8") as f:
        f.write("\n".join(table_words) + "\n")
    boosting_table_id = os.environ.get("VOLC_BOOSTING_TABLE_ID")
    if not boosting_table_id:
        print(f"[hotwords] {len(table_words)} table-format words at "
              f"{os.path.normpath(table_path)}; upload in speech console 自学习平台 "
              f"and set VOLC_BOOSTING_TABLE_ID to activate zh hotwords")

    # ASR correction map: fix systematic mishearings (e.g. MATE -> Matt,
    # CKC -> CKCon) client-side before display and translation. Rebuildable
    # at runtime: the UI can push new corrections mid-meeting.
    corr_map = {k.lower(): v for k, v in glossary.corrections.items()}
    new_corrections: dict = {}  # pushed via UI this session, persisted on exit

    def build_corr_re():
        if not corr_map:
            return None
        patterns = []
        for wrong in sorted(corr_map, key=len, reverse=True):
            pat = re.escape(wrong)
            if wrong.isascii():
                # \b fails at CJK<->latin boundaries (Python \w matches CJK),
                # so bound by non-alphanumeric lookarounds instead
                pat = rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"
            patterns.append(pat)
        return re.compile("|".join(patterns), re.IGNORECASE)

    corr = {"re": build_corr_re()}

    def correct(text: str) -> str:
        if not corr["re"]:
            return text
        return corr["re"].sub(lambda m: corr_map[m.group(0).lower()], text)

    if args.translator == "volc-mt" and not os.environ.get("VOLC_ASR_API_KEY"):
        sys.exit(
            "volc-mt 后端需要 VOLC_ASR_API_KEY(语音控制台 API Key)。\n"
            "The volc-mt backend requires VOLC_ASR_API_KEY from the speech "
            "console. With old-console app credentials, use --translator ark."
        )
    translator = build_translator(args.translator, glossary, model=args.model)

    ui: CaptionUI | None = None
    tunnel_proc = None
    share_task: asyncio.Task | None = None
    if not args.no_ui:
        # with --share the page is served only under a random token path
        share_token = secrets.token_urlsafe(6) if args.share else None
        suffix = f"/{share_token}" if share_token else ""
        ui = CaptionUI(host="0.0.0.0" if args.share else "127.0.0.1",
                       port=args.port, token=share_token)
        try:
            await ui.start()
            url = f"http://127.0.0.1:{ui.port}{suffix}"
            print(f"[ui] captions at {url}")
            print(f"[control] Lab token: {ui.control_token} "
                  f"(the local page receives it automatically; never shared)")
            if args.share:
                lan_url = f"http://{lan_ip()}:{ui.port}{suffix}"
                print(f"[share] LAN link: {lan_url}")
                await ui.set_share(lan_url, None)

                async def _tunnel_later() -> None:
                    # cloudflared can take >10s to hand out a URL; never block
                    # startup or the ASR connection on it
                    nonlocal tunnel_proc
                    proc, public_url = await maybe_tunnel(ui.port)
                    tunnel_proc = proc
                    if public_url:
                        public_url = public_url + suffix
                        print(f"[share] public link: {public_url}  (cloudflared quick tunnel)")
                        if ui:
                            await ui.set_share(lan_url, public_url)
                    else:
                        print("[share] no public link (cloudflared unavailable or timed out)")

                share_task = asyncio.create_task(_tunnel_later())
            webbrowser.open(url)
        except OSError as e:
            print(f"[ui] cannot start caption UI ({e}); continuing without it")
            ui = None
    asr_key = os.environ.get("VOLC_ASR_API_KEY")
    app_key = os.environ.get("VOLC_ASR_APP_KEY")
    access_key = os.environ.get("VOLC_ASR_ACCESS_KEY")
    if not asr_key and not (app_key and access_key):
        sys.exit(
            "ASR credentials are not set. Either create an access-control API Key "
            "whose scope includes 语音技术 and set VOLC_ASR_API_KEY, or create an app "
            "in the speech console (old console) and set VOLC_ASR_APP_KEY + "
            "VOLC_ASR_ACCESS_KEY (APP ID + Access Token)."
        )
    asr = VolcAsrClient(
        AsrConfig(
            api_key=asr_key,
            app_key=app_key,
            access_key=access_key,
            hotwords=direct,
            boosting_table_id=boosting_table_id,
            end_window_size=args.end_window,
        )
    )

    # Refined translation engine (ark with sentence context since W1); the
    # volc-mt translator stays as fallback when ark credentials are absent.
    ark_polisher = None
    try:
        ark_polisher = ArkTranslator(
            glossary, model=os.environ.get("ARK_MODEL", "doubao-seed-2-0-mini-260215")
        )
    except Exception:
        print("[translate] ark backend unavailable; refined falls back to volc-mt")

    def stats_snapshot() -> dict:
        now = time.time()
        return {k: sum(1 for t in dq if now - t < 60)
                for k, dq in req_stats.items()}

    async def on_control(msg: dict) -> None:
        """Control messages from the UI (token-guarded in ui_server)."""
        kind = msg.get("type")
        if kind == "correction":
            wrong = (msg.get("wrong") or "").strip()
            right = (msg.get("right") or "").strip()
            if wrong and right and wrong.lower() != right.lower():
                corr_map[wrong.lower()] = right
                new_corrections[wrong] = right
                corr["re"] = build_corr_re()
                print(f"[corrections] {wrong} -> {right}")
                if ui:
                    await ui.emit({"type": "status", "text": f"correction: {wrong} → {right}"})
        elif kind == "ddc":
            enabled = bool(msg.get("enabled"))
            if asr.config.enable_ddc != enabled:
                asr.config.enable_ddc = enabled
                print(f"[asr] enable_ddc={enabled}; restarting session")
                if ui:
                    await ui.emit({"type": "status",
                                   "text": f"ddc {'on' if enabled else 'off'}, ASR restarting…"})
                await asr.close()
        elif kind == "stats":
            if ui:
                await ui.emit({"type": "stats", **stats_snapshot()})

    if ui:
        ui.on_control = on_control

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    run_task = asyncio.current_task()
    assert run_task is not None
    audio_failure = {"error": None}
    producer = (
        asyncio.create_task(wav_chunks(args.wav, queue))
        if args.wav
        else asyncio.create_task(mic_chunks(queue, device=device, channels=args.channels))
    )
    if not args.wav:
        def audio_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            error = task.exception()
            if error is not None:
                audio_failure["error"] = error
                print(f"[audio] input failed: {error}", file=sys.stderr)
                run_task.cancel()

        producer.add_done_callback(audio_done)

    committed: set[tuple[int, int, str]] = set()
    translate_tasks: list[asyncio.Task] = []
    draft_tasks: list[asyncio.Task] = []  # cancelled on flush: finals have priority
    cache: dict[str, str] = {}
    recent_sources: list[str] = []  # last committed originals, context for polish
    session_t0 = time.time()
    seg_seq = 0
    draft_seq = 0
    last_draft = {"text": "", "time": 0.0}
    last_draft_emit = {"seq": 0}
    # Sentence accumulator: definite ASR fragments are buffered until a
    # sentence-final punctuation or a length cap, then committed as ONE
    # line and translated ONCE. This avoids fragmenting natural speech
    # (mid-sentence pauses) into tiny cards and flooding the translator.
    line_parts: list[str] = []
    line_speaker: dict = {"id": None}  # speaker_id of the current live line
    line_misrec = {"v": False}  # lid said English but the text came out Chinese
    FINAL_PUNCT = ("。", "！", "？", ".", "!", "?", "…")
    FILLER_RE = re.compile(r"^[嗯哦呃啊唉哼哈唔呐吧嘛呀哎\s,.，。！？!?…~～]+$")
    MAX_LINE_CHARS = 200
    FLUSH_SILENCE_S = 2.0  # flush the live line after this much ASR quiet time
    last_activity = {"t": 0.0}

    draft_sem = asyncio.Semaphore(3)  # drafts never starve the refined pass
    fail_stats: dict = {}
    from collections import deque
    # request counters for the Lab stats line (60s sliding window per channel)
    req_stats = {"volc_draft": deque(), "volc_refined": deque(), "ark": deque(),
                 "ratelimit": deque()}
    last_draft_result = {"text": "", "src": "", "time": 0.0}

    async def draft_translate(text: str, seq: int) -> None:
        """Translate the still-growing sentence for a live draft. Any draft
        newer than what is on screen is shown, even if a newer one is
        already in flight; this keeps the draft cadence at translation
        speed instead of collapsing to sentence end."""
        try:
            src, tgt = detect_direction(text)
            req_stats["volc_draft"].append(time.time())
            async with draft_sem:
                translated = await translator.translate(text, src, tgt, lite=True)
        except Exception:
            return
        last_draft_result.update(text=translated, src=src, time=time.time())
        if ui and seq > last_draft_emit["seq"]:
            last_draft_emit["seq"] = seq
            await ui.emit({"type": "draft", "text": translated, "lang": src})

    async def commit(text: str, seg_id: int, speaker: str | None,
                     misrec: bool = False, draft_snap: dict | None = None) -> None:
        src, tgt = detect_direction(text)
        arrow = "中→英" if src == "zh" else "EN→中"
        ts = round(time.time() - session_t0, 1)
        if ui:
            await ui.emit({"type": "committed", "id": seg_id, "lang": src,
                           "source": text, "speaker": speaker, "ts": ts,
                           "misrec": misrec})

        # W1 draft promotion: the last draft of THIS sentence becomes the
        # provisional translation the moment the sentence commits — the line
        # is immediately readable, marked ≈, replaced by the refined pass.
        provisional = None
        ld = draft_snap or {"text": "", "src": "", "time": 0.0}
        if ld["text"] and ld["src"] == src and time.time() - ld["time"] < 3.0:
            provisional = ld["text"]
            if ui:
                await ui.emit({"type": "translation", "id": seg_id,
                               "text": provisional, "provisional": True})

        ctx = recent_sources[-2:]
        recent_sources.append(text)
        # refined pass: ark with the previous sentences as context (default
        # since W1); volc-mt stays as fallback when ark credentials are absent
        translated = None
        if text in cache:
            translated = cache[text]
        else:
            engine = ark_polisher if ark_polisher is not None else translator
            waits = [1, 3]
            for attempt in range(3):
                if attempt:
                    await asyncio.sleep(waits[attempt - 1])
                try:
                    req_stats["ark" if engine is ark_polisher else "volc_refined"].append(time.time())
                    if engine is ark_polisher:
                        translated = await engine.translate(text, src, tgt, context=ctx)
                    else:
                        translated = await engine.translate(text, src, tgt)
                    break
                except Exception as e:
                    key = type(e).__name__
                    fail_stats[key] = fail_stats.get(key, 0) + 1
                    if key == "RateLimitError":
                        req_stats["ratelimit"].append(time.time())
                    print(f"[translate] attempt {attempt + 1} failed: {e} "
                          f"(totals: {fail_stats})", file=sys.stderr)
                    if key == "RateLimitError":
                        waits = [5, 15]
            if translated is None:
                # both engines failed: the provisional draft is still a
                # readable line; schedule quiet backfills
                translated = provisional or "⚠ 翻译失败 translation unavailable"
                translate_tasks.append(
                    asyncio.create_task(backfill(text, seg_id, src, tgt, ctx))
                )
            else:
                cache[text] = translated  # never cache the failure placeholder
        if ui and translated != provisional:
            await ui.emit({"type": "translation", "id": seg_id, "text": translated})
        elif ui and provisional is None:
            await ui.emit({"type": "translation", "id": seg_id, "text": translated})
        record = {"seq": seg_id, "speaker": speaker, "lang": src,
                  "source": text, "translation": translated,
                  "ts": ts, "time": time.strftime("%H:%M:%S")}
        transcript_records.append(record)
        await asyncio.to_thread(_append_jsonl, jsonl_path, record)
        sys.stdout.write(
            f"{CLEAR_LINE}{DIM}[{arrow}] {text}{RESET}\n{BOLD}{translated}{RESET}\n"
        )
        sys.stdout.flush()

    async def backfill(text: str, seg_id: int, src: str, tgt: str,
                     ctx: list[str]) -> None:
        """A failed refined translation is retried quietly every 15s for up
        to ~3 minutes, same engine as the refined pass (ark with context
        when available); on success the line updates in place."""
        engine = ark_polisher if ark_polisher is not None else translator
        for _ in range(12):
            await asyncio.sleep(15)
            try:
                if engine is ark_polisher:
                    fixed = await engine.translate(text, src, tgt, context=ctx)
                else:
                    fixed = await engine.translate(text, src, tgt)
            except Exception:
                continue
            cache[text] = fixed
            if ui:
                await ui.emit({"type": "translation", "id": seg_id, "text": fixed})
            for r in transcript_records:
                if r["seq"] == seg_id:
                    r["translation"] = fixed
            await asyncio.to_thread(_append_jsonl, jsonl_path,
                                    {"seq": seg_id, "backfill": fixed})
            return

    async def flush_line() -> None:
        nonlocal seg_seq
        text = "".join(line_parts).strip()
        speaker = line_speaker["id"]
        misrec = line_misrec["v"]
        line_parts.clear()
        line_speaker["id"] = None
        line_misrec["v"] = False
        for task in draft_tasks:  # stale drafts are obsolete once a line commits
            task.cancel()
        draft_tasks.clear()
        # snapshot the draft that belongs to THIS line and clear the slot:
        # a short next sentence committing within 3s must never promote the
        # previous sentence's draft as its own provisional translation
        draft_snap = dict(last_draft_result)
        last_draft_result.update(text="", src="", time=0.0)
        last_draft.update(text="")
        if not text or FILLER_RE.match(text):
            return  # drop pure fillers (嗯/哦/呃…) entirely
        seg_seq += 1
        translate_tasks.append(
            asyncio.create_task(commit(text, seg_seq, speaker, misrec, draft_snap)))

    async def silence_watchdog() -> None:
        """Flush the live line when the ASR has been quiet for a while; real
        speech rarely ends with clean sentence-final punctuation."""
        while True:
            await asyncio.sleep(0.5)
            if line_parts and asyncio.get_running_loop().time() - last_activity["t"] > FLUSH_SILENCE_S:
                await flush_line()

    watchdog = asyncio.create_task(silence_watchdog())

    # Transcript auto-save: every committed line is appended as JSONL, and a
    # Markdown rendering is written on exit. Export from the UI is now a
    # convenience, not the only copy.
    transcript_records: list[dict] = []
    transcript_dir = os.path.join(os.path.dirname(__file__), "transcripts")
    os.makedirs(transcript_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    jsonl_path = os.path.join(transcript_dir, f"babel-{stamp}.jsonl")

    reconnects = 0
    ever_connected = False
    announced_connected = True  # flips False while disconnected
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, run_task.cancel)
    try:
        while True:  # ASR reconnect loop: survive network blips mid-meeting
            got_last = False
            try:
                async for event in asr.transcribe(chunk_iterator(queue)):
                    if not ever_connected:
                        ever_connected = True
                    if not announced_connected:
                        announced_connected = True
                        if ui:
                            await ui.emit({"type": "status", "text": "connected"})
                    if event.text or event.utterances:
                        last_activity["t"] = asyncio.get_running_loop().time()
                    for utt in event.utterances:
                        key = (utt.get("start_time", 0), utt.get("end_time", 0),
                               (utt.get("text") or "")[:16])
                        if utt.get("definite") and key not in committed and utt.get("text"):
                            committed.add(key)
                            frag = correct(utt["text"])
                            speaker = (utt.get("additions") or {}).get("speaker_id")
                            # a speaker change is a natural turn boundary
                            if (speaker is not None and line_speaker["id"] is not None
                                    and speaker != line_speaker["id"] and line_parts):
                                await flush_line()
                            # language-boundary split, also inside a single fragment:
                            # a speaker can switch languages mid-utterance
                            lid = (utt.get("additions") or {}).get("lid_lang")
                            for piece_lang, piece in split_lang_runs(frag):
                                # lid says English speech but the text came out
                                # Chinese: LLM-ASR translated instead of
                                # transcribing. Mark the line as suspect.
                                if lid == "speech_en" and piece_lang == "zh":
                                    line_misrec["v"] = True
                                if (line_parts and len(piece.strip()) >= 6
                                        and piece_lang != dominant_lang("".join(line_parts))):
                                    await flush_line()
                                if not line_parts:
                                    line_speaker["id"] = speaker
                                line_parts.append(piece)
                                current = "".join(line_parts).strip()
                                if current.endswith(FINAL_PUNCT) or len(current) >= MAX_LINE_CHARS:
                                    await flush_line()
                    if event.text or line_parts:
                        line = "".join(line_parts) + correct(event.text)
                        text = line.strip()
                        now = asyncio.get_running_loop().time()
                        # Draft budget: QPM is per-account and reserved for
                        # drafts. Fire at most every 0.6s, and skip when the
                        # line grew by <6 chars in <2s (nothing new to say).
                        # While rate-limited, slow further to 2.5s.
                        streak = getattr(translator, "fail_streak", 0)
                        debounce = 2.5 if streak >= 2 else 0.6
                        grown = len(text) - len(last_draft["text"])
                        due = now - last_draft["time"]
                        if (ui and len(text) >= 4 and text != last_draft["text"]
                                and (grown >= 6 or due >= 2.0)
                                and due >= debounce):
                            last_draft.update(text=text, time=now)
                            draft_seq += 1
                            draft_tasks.append(
                                asyncio.create_task(draft_translate(text, draft_seq))
                            )
                        if ui:
                            await ui.emit({"type": "interim", "text": line})
                        if args.no_ui and event.text:
                            sys.stdout.write(f"{CLEAR_LINE}{DIM}… {line}{RESET}")
                            sys.stdout.flush()
                    if event.is_last:
                        got_last = True
                        break
            except InvalidStatus as e:
                # handshake rejected (401/403): bad key or service not
                # activated — reconnecting would loop forever, so exit
                sys.exit(f"[asr] handshake rejected (HTTP {e.response.status_code}); "
                         f"check VOLC_ASR_API_KEY and service activation")
            except (AsrError, ConnectionClosedError, OSError) as e:
                if not ever_connected:
                    # failing before the first event (auth, params) is fatal,
                    # not a reconnect case
                    sys.exit(f"[asr] failed before any result: {e}")
                print(f"\n[asr] connection problem: {e}", file=sys.stderr)
            if got_last and args.wav:
                break
            reconnects += 1
            announced_connected = False
            # drain the audio queue: buffered chunks belong to the dead
            # session, and sending them to the new one re-recognizes speech
            # that may already be committed. Lose a few seconds of audio
            # rather than duplicate captions.
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            committed.clear()
            delay = min(2 * reconnects, 10)
            print(f"[asr] reconnecting in {delay}s (attempt {reconnects})", file=sys.stderr)
            if ui:
                await ui.emit({"type": "status", "text": f"ASR reconnecting… ({reconnects})"})
            await asyncio.sleep(delay)
        await flush_line()
    except KeyboardInterrupt:
        pass
    except asyncio.CancelledError:
        if audio_failure["error"] is not None and ui:
            await ui.emit({"type": "status", "text":
                           f"audio input failed: {audio_failure['error']}"})
            await asyncio.sleep(0)
    finally:
        await flush_line()  # Ctrl-C must not drop the line still accumulating
        producer.cancel()
        watchdog.cancel()
        cleanup_tasks = [producer, watchdog]
        if share_task:
            share_task.cancel()  # terminates an in-flight cloudflared too
            cleanup_tasks.append(share_task)
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        if tunnel_proc:
            tunnel_proc.terminate()
        for task in draft_tasks:
            task.cancel()
        if translate_tasks:
            await asyncio.gather(*translate_tasks, return_exceptions=True)
        if new_corrections:
            # merge into the on-disk glossary without dumping runtime-added
            # identity terms back into the curated file
            try:
                with open(args.glossary, encoding="utf-8") as f:
                    disk = json.load(f)
                disk.setdefault("corrections", {}).update(new_corrections)
                with open(args.glossary, "w", encoding="utf-8") as f:
                    json.dump(disk, f, ensure_ascii=False, indent=2)
                print_safe(f"[corrections] persisted {len(new_corrections)} to glossary.json")
            except OSError as e:
                print_safe(f"[corrections] cannot persist: {e}", file=sys.stderr)
        if transcript_records:
            md_path = jsonl_path.replace(".jsonl", ".md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(f"# Babel transcript {stamp}\n\n")
                for r in transcript_records:
                    who = (f"Speaker {int(r['speaker']) + 1}"
                           if r["speaker"] is not None and str(r["speaker"]).isdigit()
                           else r["speaker"] or "")
                    when = r.get("time", "")
                    f.write(f"**{who}** ({r['lang']}) [{when}]: {r['source']}\n\n"
                            f"> {r['translation']}\n\n")
            print_safe(f"[transcript] saved {jsonl_path} and {md_path}")
        # per-feature token accounting for the session
        usage_report = {}
        if hasattr(translator, "usage"):
            u = translator.usage
            if isinstance(u, dict):
                for k, (p, c, n) in u.items():
                    usage_report[f"volc-mt/{k}"] = {"prompt": p, "completion": c, "calls": n}
        if ark_polisher is not None and getattr(ark_polisher, "usage", None):
            p, c, n = ark_polisher.usage
            usage_report["ark/polish"] = {"prompt": p, "completion": c, "calls": n}
        if usage_report:
            print_safe(f"[usage] {json.dumps(usage_report, ensure_ascii=False)}")
            await asyncio.to_thread(
                _append_jsonl,
                os.path.join(transcript_dir, "usage.jsonl"),
                {"stamp": stamp, **usage_report},
            )
        print_safe()
    if audio_failure["error"] is not None:
        raise SystemExit(f"[audio] input failed: {audio_failure['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live bilingual meeting captions")
    parser.add_argument("--wav", help="test with a 16kHz mono s16le wav file instead of the mic")
    parser.add_argument("--glossary", default=os.path.join(os.path.dirname(__file__), "glossary.json"))
    parser.add_argument("--translator", choices=["volc-mt", "ark", "qwen-mt"], default="volc-mt",
                        help="volc-mt: speech-product MT, same key as ASR (default); "
                             "ark: Doubao LLM on Ark; qwen-mt: Alibaba")
    parser.add_argument("--model", help="override the translation model id")
    parser.add_argument("--end-window", type=int, default=800,
                        help="ms of silence that closes an ASR fragment; sentences are "
                             "reassembled by the accumulator, so keep this moderate")
    parser.add_argument("--env", default=os.path.join(os.path.dirname(__file__), "..", ".env"),
                        help="path to the .env file with API credentials")
    parser.add_argument("--hotwords-dir", default=os.path.join(os.path.dirname(__file__), "..", "hotwords"),
                        help="directory with *.txt hotword files (merged with the glossary)")
    parser.add_argument("--device", default=None,
                        help="input device index (see --list-devices) or a device name "
                             "substring, e.g. --device Aggregate")
    parser.add_argument("--channels", type=int, default=1,
                        help="input channels to downmix; use 4 for an Aggregate Device "
                             "combining BlackHole (2ch) + a stereo mic (2ch)")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio input devices and exit")
    parser.add_argument("--port", type=int, default=8765,
                        help="caption UI port (default 8765); pick another to run "
                             "two instances side by side")
    parser.add_argument("--no-ui", action="store_true",
                        help="disable the browser caption UI (terminal output only)")
    parser.add_argument("--share", action="store_true",
                        help="share the caption page: print a LAN link, and a public "
                             "cloudflared quick-tunnel link when cloudflared is installed")
    args = parser.parse_args()
    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return
    load_dotenv(args.env)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
