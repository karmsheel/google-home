"""Settings resolution: what wins, what is clamped, and where state lives."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from googlehome import config as ghconfig


class FakeCtx:
    def __init__(self, values=None):
        self.values = values or {}

    def get_config(self, key, default=None):
        return self.values.get(key, default)


class ResolutionOrderTests(unittest.TestCase):
    def setUp(self):
        self._ctx = mock.patch.object(ghconfig, "_PLUGIN_CTX", None)
        self._ctx.start()
        self.addCleanup(self._ctx.stop)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in ("GOOGLEHOME_DEVICE", "GOOGLEHOME_PROFILE", "GOOGLEHOME_VOLUME"):
            os.environ.pop(key, None)
        # An empty HERMES_HOME: resolve_settings(None) now also reads
        # config.yaml, and the developer's real one must not leak into a test.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._home = mock.patch.dict(os.environ, {"HERMES_HOME": self._tmp.name}, clear=False)
        self._home.start()
        self.addCleanup(self._home.stop)

    def _platform(self, extra):
        return mock.Mock(extra=extra)

    def test_defaults(self):
        settings = ghconfig.resolve_settings(None)
        self.assertEqual(settings.device, "")
        self.assertEqual(settings.speech_backend, "auto")
        self.assertTrue(settings.speak_replies)
        self.assertEqual(settings.max_chars, ghconfig.DEFAULT_MAX_CHARS)
        self.assertEqual(settings.control_port, ghconfig.DEFAULT_CONTROL_PORT)

    def test_extra_is_used_without_plugin_context(self):
        settings = ghconfig.resolve_settings(self._platform({"device": "192.168.1.42", "volume": 40}))
        self.assertEqual(settings.device, "192.168.1.42")
        self.assertEqual(settings.volume, 40)

    def test_desktop_setting_beats_extra(self):
        ghconfig._PLUGIN_CTX = FakeCtx({"device": "living-room", "profile": "coder"})
        settings = ghconfig.resolve_settings(self._platform({"device": "192.168.1.42"}))
        self.assertEqual(settings.device, "living-room")
        self.assertEqual(settings.profile, "coder")

    def test_extra_beats_environment(self):
        os.environ["GOOGLEHOME_DEVICE"] = "10.0.0.9"
        settings = ghconfig.resolve_settings(self._platform({"device": "192.168.1.42"}))
        self.assertEqual(settings.device, "192.168.1.42")

    def test_environment_is_last(self):
        os.environ["GOOGLEHOME_PROFILE"] = "researcher"
        settings = ghconfig.resolve_settings(None)
        self.assertEqual(settings.profile, "researcher")

    def test_blank_desktop_setting_does_not_shadow_extra(self):
        ghconfig._PLUGIN_CTX = FakeCtx({"device": ""})
        settings = ghconfig.resolve_settings(self._platform({"device": "192.168.1.42"}))
        self.assertEqual(settings.device, "192.168.1.42")

    def test_volume_and_chars_are_clamped(self):
        settings = ghconfig.resolve_settings(self._platform({"volume": 900, "max_chars": 1}))
        self.assertEqual(settings.volume, 100)
        self.assertEqual(settings.max_chars, 120)

    def test_bools_parse_common_truthy_strings(self):
        settings = ghconfig.resolve_settings(self._platform({"speak_replies": "no", "restore_volume": "off"}))
        self.assertFalse(settings.speak_replies)
        self.assertFalse(settings.restore_volume)

    def test_backend_is_normalised(self):
        settings = ghconfig.resolve_settings(self._platform({"speech_backend": "  SAPI "}))
        self.assertEqual(settings.speech_backend, "sapi")

    def test_bad_int_falls_back(self):
        settings = ghconfig.resolve_settings(self._platform({"volume": "loud"}))
        self.assertEqual(settings.volume, 0)


class ConfigFileFallbackTests(unittest.TestCase):
    """CLI commands have no PlatformConfig, so they read config.yaml themselves."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": self._tmp.name}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in ("GOOGLEHOME_DEVICE", "GOOGLEHOME_PROFILE"):
            os.environ.pop(key, None)
        self.home = Path(self._tmp.name)

    def _write(self, body: str) -> None:
        (self.home / "config.yaml").write_text(body, encoding="utf-8")

    def test_extra_is_read_without_a_platform_config(self):
        self._write(
            "platforms:\n  googlehome:\n    extra:\n"
            "      device: 192.168.1.42\n      profile: coder\n"
        )
        settings = ghconfig.resolve_settings(None)
        self.assertEqual(settings.device, "192.168.1.42")
        self.assertEqual(settings.profile, "coder")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(ghconfig.config_file_extra(), {})
        self.assertEqual(ghconfig.resolve_settings(None).device, "")

    def test_environment_still_loses_to_the_file(self):
        os.environ["GOOGLEHOME_DEVICE"] = "10.0.0.9"
        self._write("platforms:\n  googlehome:\n    extra:\n      device: 192.168.1.42\n")
        self.assertEqual(ghconfig.resolve_settings(None).device, "192.168.1.42")


class ChatIdTests(unittest.TestCase):
    def test_one_session_per_device(self):
        self.assertEqual(ghconfig.Settings(device="192.168.1.42").chat_id, "speaker-192.168.1.42")

    def test_spaces_and_case_are_normalised(self):
        self.assertEqual(ghconfig.Settings(device="Living Room").chat_id, "speaker-living-room")

    def test_empty_device_has_a_stable_default(self):
        self.assertEqual(ghconfig.Settings().chat_id, "speaker-default")


class PathTests(unittest.TestCase):
    def test_state_and_media_dirs_live_under_hermes_home(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HERMES_HOME": tmp}, clear=False
        ):
            self.assertEqual(ghconfig.hermes_home(), Path(tmp))
            self.assertEqual(ghconfig.state_dir(), Path(tmp) / "googlehome")
            self.assertTrue(ghconfig.media_dir().is_dir())

    def test_token_is_created_once_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HERMES_HOME": tmp}, clear=False
        ):
            first = ghconfig.ensure_token()
            second = ghconfig.ensure_token()
            self.assertTrue(first)
            self.assertEqual(first, second)
            self.assertEqual(ghconfig.load_token(), first)


if __name__ == "__main__":
    unittest.main()
