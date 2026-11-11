"""Adapter integration: reply -> TTS file -> media server -> Cast LOAD -> speaker.

Runs against the fake Cast device, with the speech engine and discovery stubbed.
The gateway core must be importable; the suite is skipped otherwise (so the
stdlib-only tests still run anywhere).
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # the adapter subclasses gateway.platforms.base
    from gateway.config import PlatformConfig  # noqa: F401
    from gateway.platforms.event import MessageType  # noqa: F401

    CORE_AVAILABLE = True
except Exception:  # noqa: BLE001
    CORE_AVAILABLE = False

from tests.mock_cast_device import MockCastDevice  # noqa: E402


def _write_wav(path: Path, seconds: float = 0.05, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


@unittest.skipUnless(CORE_AVAILABLE, "Hermes core (gateway.*) not importable")
class AdapterOutboundTests(unittest.TestCase):
    def setUp(self):
        # Hermes core opens a state file under HERMES_HOME, which Windows keeps
        # locked, so tolerate leftovers at teardown.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self._env_patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        self._ensure_platform_registered()

        self.device = MockCastDevice(finish_after=0.05).start()
        self.addCleanup(self.device.stop)

        from googlehome import adapter as adapter_module
        from googlehome import playback as playback_module

        self.adapter_module = adapter_module
        self.playback = playback_module
        playback_module.reset()
        self.addCleanup(playback_module.reset)
        self.wav = _write_wav(self.home / "reply.wav")

        self.events: list = []
        test_class = self

        class RecordingAdapter(adapter_module.GoogleHomeAdapter):
            async def handle_message(self, event):  # noqa: D401 - test seam
                test_class.events.append(event)

        self.adapter = RecordingAdapter(
            PlatformConfig(
                enabled=True,
                extra={
                    "device": "127.0.0.1",
                    "control_port": 0,
                    "volume": 0,
                    "profile": "coder",
                },
            )
        )

    def _ensure_platform_registered(self) -> None:
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

    def _patch_discovery(self):
        return mock.patch.multiple(
            "googlehome.discovery",
            resolve_device=mock.Mock(
                return_value={"host": "127.0.0.1", "port": self.device.port,
                              "name": "Test Speaker", "model": "google home mini",
                              "address": "127.0.0.1", "audio_only": True}
            ),
            local_ip_for=mock.Mock(return_value="127.0.0.1"),
        )

    def test_reply_is_synthesized_served_and_cast(self):
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", return_value=[str(self.wav)]
        ):
            async def scenario():
                self.assertTrue(await self.adapter.connect())
                result = await self.adapter.send("chat-1", "**Hello** there, human.")
                await self.adapter.disconnect()
                return result

            result = asyncio.run(scenario())

        self.assertTrue(result.success, result.error)
        self.assertEqual(len(self.device.loaded_urls), 1)
        url = self.device.loaded_urls[0]
        self.assertTrue(url.startswith("http://127.0.0.1:"), url)
        self.assertTrue(url.endswith(".wav"), url)
        self.assertEqual(self.device.loaded_content_types[0], "audio/wav")
        self.assertEqual(self.adapter.last_spoken, "Hello there, human.")

    def test_markdown_is_flattened_before_synthesis(self):
        captured: dict = {}

        def fake_synthesize(text, provider="", **_kw):
            captured["text"] = text
            return [str(self.wav)]

        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", side_effect=fake_synthesize
        ):
            async def scenario():
                await self.adapter.connect()
                await self.adapter.send("chat-1", "# Title\n\n- one\n- two\n\n```py\nx=1\n```\nDone.")
                await self.adapter.disconnect()

            asyncio.run(scenario())

        spoken = captured["text"]
        self.assertNotIn("```", spoken)
        self.assertNotIn("#", spoken)
        self.assertNotIn("- ", spoken)
        self.assertIn("Done.", spoken)

    def _cron_text(self, body: str) -> str:
        return (
            "Cronjob Response: Daily Digest\n"
            "(job_id: abc123)\n"
            "-------------\n\n"
            f"{body}\n\n"
            "To stop or manage this job, send me a new message "
            "(e.g. \"stop reminder Daily Digest\")."
        )

    def test_cron_delivery_speaks_the_digest_only(self):
        captured: dict = {}

        def fake_synthesize(text, provider="", **_kw):
            captured["text"] = text
            return [str(self.wav)]

        body = "Lead sentence about the result. " + ("Detail sentence. " * 80)
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", side_effect=fake_synthesize
        ):
            async def scenario():
                await self.adapter.connect()
                result = await self.adapter.send(
                    "chat-1", self._cron_text(body), metadata={"job_id": "abc123"}
                )
                await self.adapter.disconnect()
                return result

            result = asyncio.run(scenario())

        self.assertTrue(result.success, result.error)
        spoken = captured["text"]
        self.assertTrue(spoken.startswith("Daily Digest. Lead sentence"), spoken)
        self.assertNotIn("Cronjob Response", spoken)
        self.assertNotIn("job_id", spoken)
        self.assertNotIn("To stop or manage", spoken)
        self.assertLessEqual(len(spoken), self.adapter.settings.cron_max_chars + 60)

    def test_cron_speech_marker_controls_the_spoken_sentence(self):
        captured: dict = {}

        def fake_synthesize(text, provider="", **_kw):
            captured["text"] = text
            return [str(self.wav)]

        body = "A very long report that should not be read aloud.\n\nSPEAK: Two new items today."
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", side_effect=fake_synthesize
        ):
            async def scenario():
                await self.adapter.connect()
                result = await self.adapter.send(
                    "chat-1", self._cron_text(body), metadata={"job_id": "abc123"}
                )
                await self.adapter.disconnect()
                return result

            result = asyncio.run(scenario())

        self.assertTrue(result.success, result.error)
        self.assertEqual(captured["text"], "Daily Digest. Two new items today.")

    def test_cron_delivery_can_be_muted(self):
        self.adapter.settings.speak_cron = False
        synthesizer = mock.Mock(return_value=[str(self.wav)])
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", synthesizer
        ):
            async def scenario():
                await self.adapter.connect()
                result = await self.adapter.send(
                    "chat-1", self._cron_text("Anything."), metadata={"job_id": "abc123"}
                )
                await self.adapter.disconnect()
                return result

            result = asyncio.run(scenario())

        self.assertTrue(result.success, result.error)
        synthesizer.assert_not_called()
        self.assertEqual(self.device.loaded_urls, [])

    def test_inbound_turn_carries_profile_and_text(self):
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", return_value=[str(self.wav)]
        ):
            async def scenario():
                await self.adapter.connect()
                await self.adapter.submit_utterance("what is the weather", origin="mic")
                await self.adapter.disconnect()

            asyncio.run(scenario())

        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        self.assertEqual(event.text, "what is the weather")
        self.assertEqual(event.source.profile, "coder")
        self.assertEqual(event.source.chat_id, "speaker-127.0.0.1")
    def test_control_file_is_written_and_removed(self):
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", return_value=[str(self.wav)]
        ):
            async def scenario():
                await self.adapter.connect()
                return (self.home / "googlehome" / "control.json").exists()

            exists = asyncio.run(scenario())
            self.assertTrue(exists)
            asyncio.run(self.adapter.disconnect())
            self.assertFalse((self.home / "googlehome" / "control.json").exists())

    def test_tts_failure_is_reported_not_raised(self):
        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize",
            side_effect=self.adapter_module.speech.TtsUnavailable("no provider"),
        ):
            async def scenario():
                await self.adapter.connect()
                result = await self.adapter.send("chat-1", "hello")
                await self.adapter.disconnect()
                return result

            result = asyncio.run(scenario())

        self.assertFalse(result.success)
        self.assertEqual(result.error_kind, "tts_unavailable")
        self.assertIn("no provider", result.error)

    def test_status_answers_and_playback_can_be_stopped_mid_clip(self):
        """A live poll must not queue behind the clip, and STOP must cut it short.

        Before the registry, ``/status`` took the speak lock for its live volume
        read, so the Desktop could not see ``speaking`` until the reply had
        already finished — and there was no stop path at all.
        """
        self.device.finish_after = None  # only STOP can end this clip

        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", return_value=[str(self.wav)]
        ):
            async def scenario():
                await self.adapter.connect()
                sending = asyncio.create_task(
                    self.adapter.send("chat-1", "This reply is deliberately long.")
                )
                for _ in range(200):
                    if self.playback.is_speaking() and self.device.current_state == "PLAYING":
                        break
                    await asyncio.sleep(0.02)
                self.assertEqual(self.device.current_state, "PLAYING")

                # The old implementation blocked here for the length of the
                # clip (its live-volume read waited on the playback lock).
                status = await asyncio.wait_for(self.adapter.status(), timeout=2.0)
                self.assertTrue(status["speaking"])
                self.assertTrue(status["playback"]["speaking"])

                stopped = self.playback.stop()
                send_result = await asyncio.wait_for(sending, timeout=5.0)
                await self.adapter.disconnect()
                return status, stopped, send_result

            status, stopped, send_result = asyncio.run(scenario())

        self.assertTrue(status["speaking"])
        self.assertEqual(stopped["stopped"], 1)
        self.assertTrue(stopped["ok"], stopped["errors"])
        self.assertTrue(send_result.success, send_result.error)
        self.assertEqual(self.device.current_state, "IDLE")
        self.assertFalse(self.playback.is_speaking())

    def _control(self, path: str, payload: dict | None = None) -> dict:
        import json
        import urllib.request

        info = self.adapter.control_info()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{path}",
            data=body,
            method="POST" if body is not None else "GET",
        )
        request.add_header("X-Hermes-Token", info["token"])
        if body is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return json.loads(response.read())

    def test_control_api_reports_and_stops_playback(self):
        """The Desktop's two calls: GET /playback, then POST /stop."""
        self.device.finish_after = None

        with self._patch_discovery(), mock.patch(
            "googlehome.speech.synthesize", return_value=[str(self.wav)]
        ):
            async def scenario():
                await self.adapter.connect()
                sending = asyncio.create_task(self.adapter.send("chat-1", "A long reply."))
                for _ in range(200):
                    if self.playback.is_speaking():
                        break
                    await asyncio.sleep(0.02)

                before = await asyncio.to_thread(self._control, "/playback")
                stopped = await asyncio.to_thread(self._control, "/stop", {})
                send_result = await asyncio.wait_for(sending, timeout=5.0)
                after = await asyncio.to_thread(self._control, "/playback")
                await self.adapter.disconnect()
                return before, stopped, after, send_result

            before, stopped, after, send_result = asyncio.run(scenario())

        self.assertTrue(before["speaking"])
        self.assertIn("reply", before["sources"])
        self.assertEqual(stopped["stopped"], 1)
        self.assertTrue(stopped["ok"], stopped.get("errors"))
        self.assertFalse(stopped["playback"]["speaking"])
        self.assertFalse(after["speaking"])
        self.assertTrue(send_result.success, send_result.error)
        self.assertEqual(self.device.current_state, "IDLE")

    def test_direct_playback_registers_for_stop_too(self):
        """A conversation's Speaker picker bypasses the adapter; it must still stop."""
        from googlehome import tools

        self.device.finish_after = None
        outcome: dict = {}

        def run():
            with self._patch_discovery():
                outcome.update(
                    tools._direct_play([str(self.wav)], self.adapter.settings, source="ambient")
                )

        thread = threading.Thread(target=run, name="gh-direct-play-test", daemon=True)
        thread.start()
        for _ in range(200):
            if self.playback.is_speaking():
                break
            time.sleep(0.02)

        state = self.playback.snapshot()
        self.assertTrue(state["speaking"], "the direct path was not registered")
        self.assertEqual(state["sources"], ["ambient"])

        stopped = self.playback.stop()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "direct playback kept waiting after stop")
        self.assertEqual(stopped["stopped"], 1)
        self.assertTrue(stopped["ok"], stopped["errors"])
        self.assertEqual(self.device.current_state, "IDLE")
        self.assertTrue(outcome.get("ok"), outcome.get("error"))

    def test_status_labels_the_speaker_by_model_and_address(self):
        with self._patch_discovery():
            async def scenario():
                await self.adapter.connect()
                status = await self.adapter.status()
                await self.adapter.disconnect()
                return status

            status = asyncio.run(scenario())

        self.assertEqual(status["device"]["label"], "google home mini (127.0.0.1)")
        self.assertEqual(status["device"]["model"], "google home mini")

    def test_unreachable_speaker_keeps_platform_up(self):
        with mock.patch.multiple(
            "googlehome.discovery",
            resolve_device=mock.Mock(return_value={}),
            local_ip_for=mock.Mock(return_value="127.0.0.1"),
        ):
            async def scenario():
                connected = await self.adapter.connect()
                status = await self.adapter.status()
                await self.adapter.disconnect()
                return connected, status

            connected, status = asyncio.run(scenario())

        self.assertTrue(connected, "the platform should stay up without a device")
        self.assertFalse(status["device"]["reachable"])
        self.assertIn("no Cast device", status["last_error"])


@unittest.skipUnless(CORE_AVAILABLE, "Hermes core (gateway.*) not importable")
class RegistrationTests(unittest.TestCase):
    """What the plugin loader actually receives from ``register(ctx)``."""

    def setUp(self):
        # register() stashes the context for later persistence; put the previous
        # one back so a full-suite run does not leak this test's settings.
        from googlehome import config as ghconfig

        previous = ghconfig.plugin_ctx()
        self.addCleanup(ghconfig.remember_ctx, previous)

    class RecordingCtx:
        def __init__(self) -> None:
            self.platforms: list[dict] = []
            self.tools: list[dict] = []
            self.cli: list[dict] = []
            self.hooks: list[tuple[str, object]] = []
            self.config: dict = {}

        def register_platform(self, **kwargs):
            self.platforms.append(kwargs)

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_cli_command(self, **kwargs):
            self.cli.append(kwargs)

        def register_hook(self, name, hook):
            self.hooks.append((name, hook))

        def get_config(self, key, default=None):
            return self.config.get(key, default)

        def set_config(self, key, value):
            self.config[key] = value

    def test_register_claims_platform_tools_and_cli(self):
        import googlehome

        ctx = self.RecordingCtx()
        googlehome.register(ctx)

        self.assertEqual(len(ctx.platforms), 1)
        platform = ctx.platforms[0]
        self.assertEqual(platform["name"], "googlehome")
        self.assertEqual(platform["label"], "Google Home")
        self.assertTrue(platform["platform_hint"])
        # every extra kwarg must be a real PlatformEntry field
        from gateway.platform_registry import PlatformEntry

        allowed = {field.name for field in __import__("dataclasses").fields(PlatformEntry)}
        for key in platform:
            self.assertIn(key, allowed, f"{key} is not a PlatformEntry field")
        if "cron_deliver_env_var" in allowed:
            self.assertEqual(platform["cron_deliver_env_var"], "GOOGLEHOME_HOME_CHANNEL")

        names = {tool["name"] for tool in ctx.tools}
        self.assertEqual(
            names,
            {"googlehome_speak", "googlehome_status", "googlehome_volume", "googlehome_devices"},
        )
        for tool in ctx.tools:
            self.assertEqual(tool["toolset"], "googlehome")
            self.assertEqual(tool["schema"]["name"], tool["name"])
            self.assertTrue(callable(tool["handler"]))

        self.assertEqual(len(ctx.cli), 1)
        self.assertEqual(ctx.cli[0]["name"], "googlehome")
        self.assertTrue(callable(ctx.cli[0]["setup_fn"]))
        self.assertTrue(callable(ctx.cli[0]["handler_fn"]))

    def test_register_stashes_the_context_for_later_writes(self):
        """The adapter can only persist UI choices if register() kept the context."""
        import googlehome
        from googlehome import config as ghconfig

        ctx = self.RecordingCtx()
        googlehome.register(ctx)
        self.assertIs(ghconfig.plugin_ctx(), ctx)
        ctx.set_config("device", "192.168.1.42")
        self.assertEqual(ctx.config["device"], "192.168.1.42")

    def test_manifest_tool_list_matches_registration(self):
        from pathlib import Path

        import yaml  # Hermes vendors PyYAML

        manifest = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "googlehome" / "plugin.yaml").read_text(encoding="utf-8")
        )
        ctx = self.RecordingCtx()
        import googlehome

        googlehome.register(ctx)
        self.assertEqual(set(manifest["provides_tools"]), {tool["name"] for tool in ctx.tools})


if __name__ == "__main__":
    unittest.main()
