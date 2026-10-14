"""D21 offline checks: no microphone, cloud requests, or real transcript writes."""

import contextlib
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import httpx
import websockets

import main as babel
from translate import Glossary, translation_rejection_reason
import translate
from ui_server import CaptionUI
from test_d15 import pipeline, event, UI as PipelineUI


class TranscriptRecoveryTests(unittest.TestCase):
    def test_recovery_shares_shutdown_format_and_merges_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = [
                {"seq": 2, "source": "Second", "translation": "pending", "lang": "en", "speaker": "1", "pair": "en-vi"},
                {"seq": 1, "source": "第一句", "translation": "First", "lang": "zh", "speaker": "0", "time": "09:00:00"},
                {"seq": 2, "backfill": "Thứ hai"},
            ]
            old = root / "babel-20260101-0900.jsonl"
            old.write_text("\n".join(json.dumps(r) for r in records) + '\n{"seq":3', encoding="utf-8")
            current = root / "babel-20260101-1000.jsonl"
            current.write_text(json.dumps(records[0]), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                babel.rebuild_missing_markdown(directory, str(current))
            expected = [records[1], {**records[0], "translation": "Thứ hai"}]
            markdown = old.with_suffix(".md")
            self.assertEqual(markdown.read_text(), babel.transcript_markdown(expected, "20260101-0900"))
            self.assertIn("[transcript] rebuilt", output.getvalue())
            self.assertFalse(current.with_suffix(".md").exists())
            markdown.write_text("User-edited Markdown", encoding="utf-8")
            babel.rebuild_missing_markdown(directory, str(current))
            self.assertEqual(markdown.read_text(), "User-edited Markdown")

    def test_truncated_utf8_does_not_hide_completed_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "babel-20200101-1000.jsonl")
            row = {"seq": 1, "source": "你好", "translation": "Hello", "lang": "zh"}
            path.write_bytes(json.dumps(row, ensure_ascii=False).encode() + b'\n{"source":"\xe4')
            babel.rebuild_missing_markdown(directory, str(Path(directory, "current.jsonl")))
            self.assertIn("你好", path.with_suffix(".md").read_text())


class HotwordRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(babel.__file__).parent
        cls.terms = [word for word, _ in babel.load_hotwords_dir(str(root.parent / "hotwords"))]
        cls.terms += Glossary.load(root / "glossary.json").hotwords

    def test_observed_hallucinations(self):
        for text in (
            "zk-Lock, bitlight, Cipher, SealX, SEAL, Ren, reorg, Lightning, Erlay, Bitcoin Light…",
            "dotbitOmnilock去中心化ScoobyproposalGodwokenCheckoutDinowalletDiscord",
            "挑战期。Godwoken，Jodi，SealX，knwang。挑战期，Godwoken，Jodi，SealX，knwang。",
        ):
            with self.subTest(text=text):
                self.assertTrue(babel.is_hotword_run(text, self.terms))

    def test_real_speech_and_distinct_term_threshold(self):
        self.assertFalse(babel.is_hotword_run(
            "Jacky will walk us through the opportunity map for the Fiber and AI payment", self.terms))
        self.assertFalse(babel.is_hotword_run(
            "We will compare CKB with Bitcoin and explain how Fiber and RGB++ enable these applications.", self.terms))
        self.assertFalse(babel.is_hotword_run("CKB CKB CKB CKB", self.terms))
        self.assertFalse(babel.is_hotword_run("", self.terms))
        self.assertFalse(babel.is_hotword_run("CKB Fiber Bitcoin", self.terms))


async def receive(ws):
    return json.loads(await asyncio.wait_for(ws.recv(), 2))


class TransientErrorTests(unittest.TestCase):
    def test_throttling_and_5xx_retry_but_auth_does_not(self):
        def status(code):
            request = httpx.Request("POST", "https://example.invalid")
            return httpx.HTTPStatusError("x", request=request,
                                         response=httpx.Response(code, request=request))
        self.assertTrue(babel.retryable_translation_error(status(503)))
        self.assertTrue(babel.retryable_translation_error(translate.RateLimitError("qpm")))
        self.assertFalse(babel.retryable_translation_error(status(401)))
        self.assertFalse(babel.retryable_translation_error(ValueError("bad output")))


class TranslationGateTests(unittest.TestCase):
    def test_numeric_output_requires_numeric_source(self):
        for source, output in (("四点十五。", "4:15."), ("两千零二十四", "2024"),
                               ("１２：３０。", "12:30."), ("〇点五", "0.5")):
            for target in ("zh", "en", "vi"):
                with self.subTest(source=source, target=target):
                    self.assertIsNone(translation_rejection_reason(source, output, target))
        for source in ("你好", "hello world", "第十五", "四点十五见"):
            for target in ("zh", "en", "vi"):
                self.assertEqual(translation_rejection_reason(source, "123", target),
                                 "missing_target_" + target)
        self.assertEqual(translation_rejection_reason("Test, test.", "Test, test.", "zh"), "same_as_source")
        self.assertEqual(translation_rejection_reason("hello world", "hello world", "vi"), "same_as_source")
        self.assertEqual(translation_rejection_reason("remotion。", "remotion。", "zh"), "same_as_source")
        self.assertEqual(translation_rejection_reason("四", "1" * 81, "en"), "excessive_length")
        self.assertEqual(translation_rejection_reason("四点十五", "4:15🔑", "en"), "missing_target_en")

    def test_source_fallback_is_short_latin_only(self):
        for source in ("remotion。", "vm ckb vm。", "hello world", "Freshelle", "café"):
            self.assertTrue(translate.short_latin_source(source), source)
        for source in ("", "...", "123", "Can you hear me?", "你好 remotion", "カメラ",
                       "안녕 remotion", "𠮷 remotion", "Привет remotion"):
            self.assertFalse(translate.short_latin_source(source), source)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_kept_only_after_all_content_rejections(self):
        for source, mode, expected in (
            ("remotion。", "copy", "source_kept"),
            ("vm ckb vm。", "copy", "source_kept"),
            ("hello world", "copy", "source_kept"),
            ("Test, test.", "copy", "source_kept"),
            ("remotion。", "fallback", "volc-mt_fallback"),
            ("hello world", "fallback", "volc-mt_fallback"),
            ("Can you hear me?", "copy", "unavailable"),
            ("remotion。", "backend_error", "unavailable"),
            ("remotion。", "mixed_error", "unavailable"),
            ("remotion。", "network_error", "unavailable"),
            ("remotion。", "empty", "unavailable"),
            ("remotion。", "reasoning_leak", "unavailable"),
            ("四点十五。", "numeric", "ark"),
        ):
            with self.subTest(source=source, mode=mode):
                calls, sleeps = [], []
                real_sleep = asyncio.sleep

                class ASR:
                    def __init__(self, config):
                        pass

                    async def transcribe(self, chunks):
                        yield event(source, last=True)

                class Engine:
                    usage = {}

                    def __init__(self, label):
                        self.label = label

                    async def translate(self, text, *args, **kwargs):
                        calls.append((self.label, kwargs.get("context") is None))
                        if mode == "backend_error":
                            raise RuntimeError("synthetic quota rejection")
                        if mode == "mixed_error" and len(calls) == 1:
                            raise RuntimeError("synthetic initial rejection")
                        if mode == "network_error":
                            if len(calls) <= 5:
                                raise ConnectionError("synthetic disconnect")
                            return "网络恢复后的有效译文。"
                        if mode == "empty":
                            return ""
                        if mode == "reasoning_leak":
                            return "Wait, the source text is remotion."
                        if mode == "numeric":
                            return "4:15."
                        if mode == "fallback" and self.label == "fallback":
                            return "后备渠道给出的有效译文。"
                        return text

                async def fast_sleep(delay):
                    sleeps.append(delay)
                    await real_sleep(.02 if delay == 15 else 0)

                with tempfile.TemporaryDirectory() as directory, pipeline(directory, ASR, Engine("ark")) as args, \
                        patch.object(babel, "build_translator", return_value=Engine("fallback")), \
                        patch.object(babel.asyncio, "sleep", fast_sleep), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    await babel.run(args)
                    records = [json.loads(line) for line in next(Path(directory, "transcripts").glob("babel-*.jsonl")).read_text().splitlines()]
                record = next(row for row in records if "source" in row)
                self.assertEqual(record["translation_outcome"], expected)
                emitted = [ev["text"] for ev in PipelineUI.instance.events if ev["type"] == "translation"]
                if expected == "source_kept":
                    self.assertEqual(record["translation"], source)
                    self.assertEqual(emitted, [source])
                elif expected == "volc-mt_fallback":
                    self.assertEqual(emitted, ["后备渠道给出的有效译文。"])
                elif expected == "ark":
                    self.assertEqual(emitted, ["4:15."])
                else:
                    self.assertTrue(record["translation"].startswith("⚠ "))
                if mode == "network_error":
                    self.assertIn(15, sleeps)
                    self.assertTrue(any(row.get("backfill") == "网络恢复后的有效译文。" for row in records))
                else:
                    self.assertNotIn(15, sleeps)
                    self.assertFalse(any("backfill" in row for row in records))
                    if mode == "numeric":
                        self.assertEqual(calls, [("ark", False)])
                    elif mode == "mixed_error":
                        self.assertEqual(calls, [("ark", False), ("ark", False),
                                                ("ark", True), ("fallback", True)])
                    elif mode != "backend_error":
                        self.assertEqual(calls, [("ark", False), ("ark", True), ("fallback", True)])

    async def test_startup_recovery_and_hotword_runs_never_reach_translation(self):
        words = ["zk-Lock", "bitlight", "Cipher", "SealX", "SEAL", "Ren", "reorg",
                 "Lightning", "Erlay", "Bitcoin Light", "dotbit", "Omnilock", "去中心化",
                 "Scooby", "proposal", "Godwoken", "Checkout", "Dino", "wallet", "Discord",
                 "挑战期", "Jodi", "knwang"]
        bad = [
            "zk-Lock, bitlight, Cipher, SealX, SEAL, Ren, reorg, Lightning, Erlay, Bitcoin Light…",
            "dotbitOmnilock去中心化ScoobyproposalGodwokenCheckoutDinowalletDiscord",
            "挑战期。Godwoken，Jodi，SealX，knwang。挑战期，Godwoken，Jodi，SealX，knwang。",
        ]
        real = "Jacky will walk us through the opportunity map for the Fiber and AI payment."
        calls = []

        class ASR:
            def __init__(self, config):
                pass

            async def transcribe(self, chunks):
                for text in bad:
                    yield event(text, definite=False)
                    yield event(text)
                yield event(real, last=True)

        class Translator:
            usage = {}

            async def translate(self, text, *args, **kwargs):
                calls.append(text)
                return "Jacky将介绍相关的应用机会。"

        with tempfile.TemporaryDirectory() as directory, pipeline(directory, ASR, Translator()) as args:
            Path(args.hotwords_dir, "hotwords.txt").write_text("\n".join(words), encoding="utf-8")
            root = Path(directory, "transcripts")
            root.mkdir()
            orphan = root / "babel-20200101-0900.jsonl"
            orphan.write_text(json.dumps({"seq": 1, "source": "old", "translation": "旧", "lang": "en"}))
            log = io.StringIO()
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                await babel.run(args)
            self.assertEqual(calls, [real])
            self.assertTrue(orphan.with_suffix(".md").exists())
            self.assertEqual(log.getvalue().count("[asr] dropped hotword run:"), 3)
            self.assertEqual([ev["source"] for ev in PipelineUI.instance.events if ev["type"] == "committed"], [real])
            self.assertFalse(any(ev.get("text") in bad for ev in PipelineUI.instance.events))

    async def test_content_failure_has_no_backfill_but_transport_can_recover(self):
        for failure, invalid in ((None, None), (None, "不同的中文。"), (None, "x" * 1000),
                                 (ConnectionError, None), (TimeoutError, None),
                                 (translate.RateLimitError, None)):
            with self.subTest(failure=failure, invalid=invalid):
                calls, delays = [], []
                source = "这是一句需要翻译的中文。"
                real_sleep = asyncio.sleep

                class ASR:
                    def __init__(self, config):
                        pass

                    async def transcribe(self, chunks):
                        yield event(source, last=True)

                class Translator:
                    usage = {}

                    async def translate(self, text, *args, **kwargs):
                        calls.append(kwargs)
                        if failure and len(calls) <= 5:
                            raise failure("synthetic transport failure")
                        if failure:
                            return "A recovered translation."
                        return invalid or text

                async def fast_sleep(delay):
                    delays.append(delay)
                    await real_sleep(.02 if delay == 15 else 0)

                engine = Translator()
                with tempfile.TemporaryDirectory() as directory, pipeline(directory, ASR, engine) as args, \
                        patch.object(babel, "build_translator", return_value=engine), \
                        patch.object(babel.asyncio, "sleep", fast_sleep), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    await babel.run(args)
                    records = [json.loads(line) for line in next(Path(directory, "transcripts").glob("babel-*.jsonl")).read_text().splitlines()]
                if failure is None:
                    # Same object as both engines: primary + context-free only.
                    self.assertEqual(len(calls), 2)
                    self.assertNotIn(15, delays)
                    self.assertFalse(any("backfill" in row for row in records))
                else:
                    self.assertIn(15, delays)
                    self.assertTrue(any(row.get("backfill") == "A recovered translation." for row in records))


class SharingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ui = CaptionUI(port=0, token="old-link")
        await self.ui.emit({"type": "committed", "id": 1, "source": "Old private content", "lang": "en"})
        await self.ui.emit({"type": "translation", "id": 1, "text": "旧内容"})
        await self.ui.start()
        self.ui.port = self.ui.server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{self.ui.port}"
        self.wsbase = self.base.replace("http", "ws", 1)
        self.connections = []

    async def asyncTearDown(self):
        for ws in self.connections:
            await ws.close()
        self.ui.server.close()
        await self.ui.server.wait_closed()

    async def connect(self, host=False, token="old-link"):
        # Simulate a LAN viewer behind a local relay, without claiming to be CF.
        ws = await websockets.connect(self.wsbase + "/" + token,
            additional_headers={} if host else {"X-Forwarded-For": "192.0.2.1"})
        self.connections.append(ws)
        replay = await receive(ws)
        if host:
            self.assertEqual((await receive(ws))["type"], "control_token")
        return ws, replay

    async def test_private_replay_late_translation_and_host_authority(self):
        host, replay = await self.connect(host=True)
        self.assertFalse(replay["live_only"])
        viewer, _ = await self.connect()
        await self.ui.set_private(True)
        await asyncio.wait_for(viewer.wait_closed(), 2)
        self.assertEqual(viewer.close_code, 1012)
        self.assertIsNone(host.close_code)
        viewer, replay = await self.connect()
        self.assertTrue(replay["live_only"])
        self.assertFalse(any("id" in ev for ev in replay["events"]))
        new_host, replay = await self.connect(host=True)
        self.assertEqual([ev["id"] for ev in replay["events"] if ev["type"] == "committed"], [1])
        called = []

        async def control(message):
            called.append(message)
            await self.ui.emit({"type": "status", "text": "host acknowledged"})

        self.ui.on_control = control
        await viewer.send(json.dumps({"type": "reset_link", "token": self.ui.control_token}))
        await host.send(json.dumps({"type": "sharing_get", "token": self.ui.control_token}))
        self.assertEqual((await receive(viewer))["text"], "host acknowledged")
        self.assertEqual([msg["type"] for msg in called], ["sharing_get"])
        await self.ui.emit({"type": "translation", "id": 1, "text": "Late old secret"})
        await self.ui.emit({"type": "committed", "id": 2, "source": "After joining", "lang": "en"})
        await self.ui.emit({"type": "translation", "id": 2, "text": "新内容"})
        self.assertEqual((await receive(viewer))["id"], 2)
        self.assertEqual((await receive(viewer))["id"], 2)
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.get(self.base + "/old-link")).status_code, 200)
            response = await client.get(self.base + "/old-link", headers={"Cf-Ray": "synthetic"})
            self.assertEqual(response.status_code, 404)
        await self.ui.set_private(False)
        _, replay = await self.connect()
        self.assertFalse(replay["live_only"])
        self.assertEqual([ev["id"] for ev in replay["events"] if ev["type"] == "committed"], [1, 2])

    async def test_inflight_handshake_cannot_bypass_privacy_or_rotation(self):
        ui = CaptionUI(token="before")
        handlers = []

        async def serve(handler, *args, **kwargs):
            handlers.append(handler)

        with patch("ui_server.websockets.serve", serve):
            await ui.start()
        for private, token, expected in ((True, "before", 1008), (False, "after", 4001)):
            ui.private, ui.token = private, token
            socket = SimpleNamespace(remote_address=("127.0.0.1", 1),
                request=SimpleNamespace(path="/before", headers={"Cf-Ray": "synthetic"}),
                close=AsyncMock())
            await handlers[0](socket)
            self.assertEqual(socket.close.call_args.kwargs["code"], expected)
            self.assertFalse(ui.clients)

    async def test_reset_invalidates_old_http_and_sockets_not_host(self):
        host, _ = await self.connect(host=True)
        viewer, _ = await self.connect()
        await self.ui.reset_link("new-link")
        await asyncio.wait_for(viewer.wait_closed(), 2)
        self.assertEqual(viewer.close_code, 4001)
        self.assertIsNone(host.close_code)
        await self.ui.emit({"type": "status", "text": "still alive"})
        self.assertEqual((await receive(host))["text"], "still alive")
        async with httpx.AsyncClient(trust_env=False) as client:
            self.assertEqual((await client.get(self.base + "/old-link")).status_code, 404)
            self.assertEqual((await client.get(self.base + "/new-link")).status_code, 200)
        with self.assertRaises(websockets.exceptions.InvalidStatus):
            await self.connect()
        _, replay = await self.connect(token="new-link")
        self.assertEqual(replay["type"], "replay")

    async def test_tunnel_lifetime_rotation_registration_and_shutdown(self):
        started, stopped = [], []

        async def tunnel(port, ui, lan, suffix):
            started.append(suffix)
            try:
                await ui.set_share(lan, "https://synthetic.invalid" + suffix)
                await asyncio.Event().wait()
            finally:
                stopped.append(suffix)

        sharing = babel.SharingSession(self.ui, True, self.base)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(babel, "SESSION_DIR", directory), \
                patch.object(babel, "maintain_tunnel", tunnel):
            await sharing.start()
            await asyncio.sleep(0)
            self.assertEqual(started, ["/old-link"])
            await sharing.control({"type": "private", "enabled": True})
            self.assertEqual(stopped, started)
            self.assertNotIn("public", self.ui.share)
            self.assertEqual(self.ui.share["lan"], self.base + "/old-link")
            await sharing.control({"type": "private", "enabled": False})
            await asyncio.sleep(0)
            self.assertEqual(len(started), 2)
            with patch.object(babel, "register_session", side_effect=OSError("offline disk failure")):
                await sharing.control({"type": "reset_link"})
            self.assertEqual(self.ui.token, "old-link")
            await sharing.control({"type": "reset_link"})
            await asyncio.sleep(0)
            self.assertNotEqual(self.ui.token, "old-link")
            registered = json.loads(next(Path(directory).glob("*.json")).read_text())
            self.assertEqual(registered["url"], self.base + "/" + self.ui.token)
            self.assertEqual(started[-1], "/" + self.ui.token)
            await sharing.close()
            await sharing.control({"type": "private", "enabled": False})
            self.assertEqual(stopped, started)
            self.assertIsNone(sharing.task)


if __name__ == "__main__":
    unittest.main()
