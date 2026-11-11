"""The Desktop page's view of a turn.

The speaker is the only output channel, so the page used to fire a turn and
never learn its answer. These tests pin the two halves of the fix:

* the adapter records the reply (or the reason there is none) under a token;
* the control API hands that token out and serves it from ``GET /reply``.

Stdlib only where possible; the adapter classes need Hermes core and are
skipped without it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # the adapter subclasses gateway.platforms.base
    from gateway.config import PlatformConfig  # noqa: F401

    from googlehome.adapter import GoogleHomeAdapter

    CORE_AVAILABLE = True
except Exception:  # noqa: BLE001
    CORE_AVAILABLE = False

from googlehome import control as control_module  # noqa: E402
from googlehome import speech  # noqa: E402


def _register_platform() -> None:
    """``Platform("googlehome")`` only resolves once the registry knows it."""
    from gateway.platform_registry import PlatformEntry, platform_registry

    if platform_registry.is_registered("googlehome"):
        return
    platform_registry.register(
        PlatformEntry(
            name="googlehome",
            label="Google Home",
            adapter_factory=lambda _config: None,
            check_fn=lambda: True,
        )
    )


@unittest.skipUnless(CORE_AVAILABLE, "Hermes core (gateway.*) not importable")
class CaptureLifecycleTests(unittest.TestCase):
    """begin_capture / send / capture_snapshot without a gateway or a speaker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env_patch = mock.patch.dict(
            os.environ, {"HERMES_HOME": str(Path(self.tmp.name))}, clear=False
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        _register_platform()

        from googlehome import playback

        playback.reset()
        self.addCleanup(playback.reset)

        self.adapter = GoogleHomeAdapter(
            PlatformConfig(enabled=True, extra={"device": "127.0.0.1", "profile": "coder"})
        )
        self.chat = self.adapter.settings.chat_id

    def _played(self):
        """Patch synthesis + casting so a send succeeds without a device."""
        patcher = mock.patch.multiple(
            GoogleHomeAdapter,
            _synthesize=mock.Mock(return_value=["reply.wav"]),
            _play=mock.AsyncMock(return_value=None),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_successful_send_records_the_whole_reply(self):
        self._played()
        token = self.adapter.begin_capture()
        self.assertFalse(self.adapter.capture_snapshot(token)["done"])

        import asyncio

        asyncio.run(self.adapter.send(self.chat, "**Hello** there.\n\nSecond line."))

        state = self.adapter.capture_snapshot(token)
        self.assertTrue(state["done"])
        # The page shows what the agent wrote, not the clipped TTS script.
        self.assertEqual(state["reply"], "**Hello** there.\n\nSecond line.")
        self.assertEqual(state["error"], "")

    def test_a_tts_failure_is_reported_and_a_retry_replaces_it(self):
        # Keep the real ``_synthesize`` so the patched backend raises inside the
        # adapter's own error path; only casting is stubbed.
        patcher = mock.patch.multiple(
            GoogleHomeAdapter, _play=mock.AsyncMock(return_value=None)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        token = self.adapter.begin_capture()

        import asyncio

        with mock.patch.object(
            speech, "synthesize", side_effect=speech.TtsUnavailable("no backend")
        ):
            asyncio.run(self.adapter.send(self.chat, "The answer."))

        failed = self.adapter.capture_snapshot(token)
        self.assertTrue(failed["done"])
        self.assertIn("no backend", failed["error"])
        self.assertEqual(failed["reply"], "")

        # The gateway retries once with a plain-text fallback; that success must
        # replace the error the page might already have seen.
        with mock.patch.object(speech, "synthesize", return_value=["reply.wav"]):
            asyncio.run(self.adapter.send(self.chat, "The answer."))
        recovered = self.adapter.capture_snapshot(token)
        self.assertTrue(recovered["done"])
        self.assertEqual(recovered["error"], "")
        self.assertEqual(recovered["reply"], "The answer.")

    def test_a_muted_reply_still_reaches_the_page(self):
        self.adapter.settings.speak_replies = False
        token = self.adapter.begin_capture()

        import asyncio

        asyncio.run(self.adapter.send(self.chat, "Said quietly."))

        state = self.adapter.capture_snapshot(token)
        self.assertTrue(state["done"])
        self.assertEqual(state["reply"], "Said quietly.")

    def test_mute_silences_the_speaker_but_keeps_the_page_reply(self):
        import asyncio

        result = asyncio.run(self.adapter.set_muted(True))

        self.assertTrue(result["ok"])
        self.assertTrue(result["muted"])
        self.assertTrue(self.adapter.settings.muted)
        token = self.adapter.begin_capture()
        with mock.patch.object(GoogleHomeAdapter, "_synthesize") as synthesize:
            sent = asyncio.run(self.adapter.send(self.chat, "Said quietly."))
        self.assertTrue(sent.success)
        synthesize.assert_not_called()

        state = self.adapter.capture_snapshot(token)
        self.assertTrue(state["done"])
        self.assertEqual(state["reply"], "Said quietly.")
        self.assertTrue(asyncio.run(self.adapter.status())["settings"]["muted"])

    def test_unmute_restores_spoken_replies(self):
        import asyncio

        asyncio.run(self.adapter.set_muted(True))
        result = asyncio.run(self.adapter.set_muted(False))

        self.assertFalse(result["muted"])
        self.assertFalse(self.adapter.settings.muted)
        self.assertFalse(asyncio.run(self.adapter.status())["settings"]["muted"])

    def test_hermes_synthesized_audio_ends_the_wait_without_text(self):
        self._played()
        token = self.adapter.begin_capture()

        import asyncio

        asyncio.run(self.adapter.play_tts(self.chat, "reply.mp3"))

        state = self.adapter.capture_snapshot(token)
        self.assertTrue(state["done"])
        self.assertEqual(state["reply"], "")
        self.assertEqual(state["error"], "")

    def test_auto_tts_audio_is_not_a_second_speaking_of_the_same_reply(self):
        """``voice.auto_tts`` hands the reply to play_tts; ``send`` speaks it too."""
        self._played()
        token = self.adapter.begin_capture()
        auto = Path(tempfile.gettempdir()) / "hermes_voice" / "tts_reply_abc123.mp3"

        import asyncio

        result = asyncio.run(self.adapter.play_tts(self.chat, str(auto)))

        self.assertTrue(result.success, "the redundant clip is acknowledged, not failed")
        self.adapter._play.assert_not_awaited()
        # It must not close the capture: the real reply is still on its way.
        self.assertFalse(self.adapter.capture_snapshot(token)["done"])

    def test_rendered_reasoning_is_neither_spoken_nor_shown(self):
        self._played()
        token = self.adapter.begin_capture()
        reply = (
            "💭 **Reasoning:**\n"
            "```\n"
            "The user wants the single word pong, so keep it short.\n"
            "```\n\n"
            "pong"
        )

        import asyncio

        asyncio.run(self.adapter.send(self.chat, reply))

        state = self.adapter.capture_snapshot(token)
        self.assertEqual(state["reply"], "pong")
        self.assertEqual(GoogleHomeAdapter._synthesize.call_args[0][0], "pong")

    def test_a_reply_that_mentions_reasoning_is_left_alone(self):
        from googlehome.adapter import strip_rendered_reasoning

        for text in (
            "Reasoning helps here, which is why I keep it.",
            "Answer first.\n\n💭 **Reasoning:** only at the start is stripped.",
            "Résumé of the plan: ship it.",
        ):
            self.assertEqual(strip_rendered_reasoning(text), text)

    def test_a_localized_reasoning_header_is_still_stripped(self):
        from googlehome.adapter import strip_rendered_reasoning

        self.assertEqual(
            strip_rendered_reasoning("💭 **Raisonnement :**\n```\nétape\n```\n\nRéponse."),
            "Réponse.",
        )

    def test_tool_speech_is_not_the_answer(self):
        self._played()
        token = self.adapter.begin_capture()

        import asyncio

        asyncio.run(self.adapter.speak_text("spoken on command"))
        self.assertFalse(self.adapter.capture_snapshot(token)["done"])

        asyncio.run(self.adapter.send(self.chat, "The real answer."))
        self.assertEqual(self.adapter.capture_snapshot(token)["reply"], "The real answer.")

    def test_an_unknown_token_is_an_error_not_a_crash(self):
        state = self.adapter.capture_snapshot("nope")
        self.assertFalse(state["ok"])
        self.assertIn("expired", state["error"])

    def test_captures_expire(self):
        token = self.adapter.begin_capture()
        with mock.patch("googlehome.adapter._CAPTURE_TTL_SECONDS", -1):
            state = self.adapter.capture_snapshot(token)
        self.assertFalse(state["ok"])


@unittest.skipUnless(CORE_AVAILABLE, "Hermes core (gateway.*) not importable")
class RoutedProfileTests(unittest.TestCase):
    """The page's profile chip must say who answers, not who was selected."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env_patch = mock.patch.dict(
            os.environ, {"HERMES_HOME": str(Path(self.tmp.name))}, clear=False
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)
        _register_platform()

        self.adapter = GoogleHomeAdapter(
            PlatformConfig(enabled=True, extra={"device": "192.168.1.145", "profile": "default"})
        )

    def test_a_gateway_route_overrides_the_selected_profile(self):
        self.adapter.build_source = lambda **_kwargs: SimpleNamespace(profile="coder")

        import asyncio

        status = asyncio.run(self.adapter.status())

        self.assertEqual(status["profile"], "default")
        self.assertEqual(status["routed_profile"], "coder")
        self.assertTrue(status["profile_overridden"])

    def test_without_a_route_the_selected_profile_is_the_answer(self):
        self.adapter.build_source = lambda **_kwargs: SimpleNamespace(profile="")

        import asyncio

        status = asyncio.run(self.adapter.status())

        self.assertEqual(status["routed_profile"], "default")
        self.assertFalse(status["profile_overridden"])

    def test_routing_failure_never_breaks_status(self):
        def boom(**_kwargs):
            raise RuntimeError("routing exploded")

        self.adapter.build_source = boom

        import asyncio

        status = asyncio.run(self.adapter.status())

        self.assertEqual(status["routed_profile"], "default")
        self.assertFalse(status["profile_overridden"])


class _LoopAdapter:
    """Just enough adapter for the control server: a loop and a turn."""

    def __init__(self) -> None:
        import asyncio

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self.seen: dict = {}
        self.snapshot = {"ok": True, "done": True, "reply": "the answer", "error": ""}

    def begin_capture(self) -> str:
        return "tok-123"

    def capture_snapshot(self, token: str) -> dict:
        self.seen["token"] = token
        return {**self.snapshot, "token": token}

    async def submit_utterance(self, text: str, *, origin: str = "text", audio_path: str = "", capture: str = "") -> bool:
        self.seen.update(text=text, origin=origin, capture=capture)
        return True

    async def set_muted(self, muted: bool) -> dict:
        self.seen["muted"] = muted
        return {"ok": True, "muted": muted}

    def profile_availability(self) -> dict:
        return {
            "ok": True,
            "bridge_profile": "default",
            "plugin_profiles": ["default", "coder"],
            "profiles": [],
        }

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)


class ControlReplyEndpointTests(unittest.TestCase):
    """``POST /ask`` hands out the token and ``GET /reply`` serves it."""

    def setUp(self):
        self.adapter = _LoopAdapter()
        self.addCleanup(self.adapter.close)
        self.server = control_module.ControlServer(self.adapter, port=0, token="")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_ask_returns_a_token_and_reply_serves_the_text(self):
        request = urllib.request.Request(
            self.base + "/ask",
            data=json.dumps({"text": "hello"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            asked = json.loads(response.read().decode("utf-8"))

        self.assertTrue(asked["ok"])
        self.assertTrue(asked["submitted"])
        self.assertEqual(asked["token"], "tok-123")
        self.assertEqual(self.adapter.seen["text"], "hello")
        self.assertEqual(self.adapter.seen["capture"], "tok-123")

        replied = self._get("/reply?token=tok-123")
        self.assertTrue(replied["ok"])
        self.assertTrue(replied["done"])
        self.assertEqual(replied["reply"], "the answer")
        self.assertEqual(self.adapter.seen["token"], "tok-123")

    def test_reply_without_a_token_reports_an_expired_turn(self):
        replied = self._get("/reply")
        self.assertTrue(replied["ok"])
        self.assertEqual(self.adapter.seen["token"], "")

    def test_profiles_endpoint_names_the_bridge_and_plugin_profiles(self):
        info = self._get("/profiles")
        self.assertTrue(info["ok"])
        self.assertEqual(info["bridge_profile"], "default")
        self.assertEqual(info["plugin_profiles"], ["default", "coder"])


class ControlMuteEndpointTests(unittest.TestCase):
    """``POST /mute`` is the chip's switch: boolean in, adapter flag out."""

    def setUp(self):
        self.adapter = _LoopAdapter()
        self.addCleanup(self.adapter.close)
        self.server = control_module.ControlServer(self.adapter, port=0, token="")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_mute_and_unmute(self):
        muted = self._post("/mute", {"muted": True})
        self.assertTrue(muted["ok"])
        self.assertTrue(muted["muted"])
        self.assertTrue(self.adapter.seen["muted"])

        unmuted = self._post("/mute", {"muted": "false"})
        self.assertTrue(unmuted["ok"])
        self.assertFalse(unmuted["muted"])
        self.assertFalse(self.adapter.seen["muted"])

    def test_mute_rejects_a_missing_flag(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/mute", {})
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

