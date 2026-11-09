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

        self.adapter_module = adapter_module
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

    class RecordingCtx:
        def __init__(self) -> None:
            self.platforms: list[dict] = []
            self.tools: list[dict] = []
            self.cli: list[dict] = []
            self.config: dict = {}

        def register_platform(self, **kwargs):
            self.platforms.append(kwargs)

        def register_tool(self, **kwargs):
            self.tools.append(kwargs)

        def register_cli_command(self, **kwargs):
            self.cli.append(kwargs)

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
