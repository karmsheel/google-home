"""The adapter must install its own profile secret scope.

A multiplexing gateway refuses secret reads from unscoped tasks — that is the
`UnscopedSecretError` whose user-facing sentence is "Hermes could not read this
profile's XAI_BASE_URL (an internal profile-scoping bug on the multiplexed
gateway…)". Turns run inside a scope; the control API, cron links and ambient
speech call in from outside one, so the adapter wraps those calls itself.

Regression guard: without `_profile_scope`, Hermes TTS fails and the bridge
silently drops to the offline Windows engine.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:  # the adapter subclasses gateway.platforms.base
    from gateway.config import PlatformConfig  # noqa: F401

    from googlehome.adapter import GoogleHomeAdapter

    CORE_AVAILABLE = True
except Exception:  # noqa: BLE001
    CORE_AVAILABLE = False

try:  # stdlib-only, but it belongs to Hermes core
    from agent.secret_scope import (
        UnscopedSecretError,
        get_secret,
        reset_multiplex_context,
        set_multiplex_context,
    )

    SECRET_SCOPE_AVAILABLE = True
except Exception:  # noqa: BLE001
    SECRET_SCOPE_AVAILABLE = False

requires_scope = unittest.skipUnless(
    SECRET_SCOPE_AVAILABLE, "agent.secret_scope not importable"
)
requires_core = unittest.skipUnless(
    CORE_AVAILABLE and SECRET_SCOPE_AVAILABLE, "Hermes core not importable"
)


class _Host:
    """Just enough adapter state for the scope helper (hermetic HERMES_HOME)."""

    def __init__(self, profile: str = "") -> None:
        import tempfile

        from googlehome import config as ghconfig

        self.settings = ghconfig.resolve_settings(None)
        self.settings.profile = profile
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self._tmp.name)
        # The real helper resolves a profile's home from settings; pin it to a temp dir.
        self._profile_home = lambda: self.home
        if CORE_AVAILABLE:
            import types

            self._profile_scope = types.MethodType(GoogleHomeAdapter._profile_scope, self)

    def cleanup(self) -> None:
        self._tmp.cleanup()


@requires_scope
class UnscopedReadTests(unittest.TestCase):
    """The failure mode the adapter has to defend against."""

    def setUp(self):
        token = set_multiplex_context(True)
        self.addCleanup(reset_multiplex_context, token)

    def test_unscoped_read_raises(self):
        with self.assertRaises(UnscopedSecretError) as raised:
            get_secret("XAI_BASE_URL")
        self.assertIn("XAI_BASE_URL", str(raised.exception))


@requires_core
class AdapterProfileScopeTests(unittest.TestCase):
    def setUp(self):
        token = set_multiplex_context(True)
        self.addCleanup(reset_multiplex_context, token)
        self.host = _Host()
        self.addCleanup(self.host.cleanup)

    def test_scope_makes_a_secret_read_succeed(self):
        with GoogleHomeAdapter._profile_scope(self.host):
            # Must not raise: this is the call that failed inside adapter.send().
            value = get_secret("XAI_BASE_URL")
        self.assertTrue(value is None or isinstance(value, str))

    def test_scope_is_removed_on_exit(self):
        with GoogleHomeAdapter._profile_scope(self.host):
            get_secret("XAI_BASE_URL")
        with self.assertRaises(UnscopedSecretError):
            get_secret("XAI_BASE_URL")

    def test_scope_is_removed_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with GoogleHomeAdapter._profile_scope(self.host):
                raise RuntimeError("boom")
        with self.assertRaises(UnscopedSecretError):
            get_secret("XAI_BASE_URL")

    def test_scopes_nest_and_unwind(self):
        with GoogleHomeAdapter._profile_scope(self.host):
            with GoogleHomeAdapter._profile_scope(self.host):
                get_secret("XAI_BASE_URL")
            get_secret("XAI_BASE_URL")
        with self.assertRaises(UnscopedSecretError):
            get_secret("XAI_BASE_URL")

    def test_unknown_profile_falls_back_instead_of_raising(self):
        self.host.settings.profile = "definitely-not-a-profile"
        with GoogleHomeAdapter._profile_scope(self.host):
            get_secret("XAI_BASE_URL")

    def test_profile_home_follows_the_route_not_the_old_selection(self):
        """TTS credentials must use the profile that actually answers."""
        from unittest import mock

        import hermes_constants

        adapter = GoogleHomeAdapter.__new__(GoogleHomeAdapter)
        adapter.settings = type("Settings", (), {"profile": "coder"})()
        adapter.effective_profile = lambda: "researcher"
        routed = Path(self.host.home) / "profiles" / "researcher"
        with mock.patch.object(hermes_constants, "get_default_hermes_root",
                               return_value=self.host.home), \
             mock.patch.object(hermes_constants, "named_profile_home", return_value=routed), \
             mock.patch.object(hermes_constants, "named_profile_is_live", return_value=True):
            self.assertEqual(adapter._profile_home(), routed)

    def test_default_route_uses_the_root_not_the_active_profile(self):
        """A routed "default" means the root home, even from a -p process."""
        from unittest import mock

        import hermes_constants

        adapter = GoogleHomeAdapter.__new__(GoogleHomeAdapter)
        adapter.settings = type("Settings", (), {"profile": ""})()
        adapter.effective_profile = lambda: "default"
        with mock.patch.object(hermes_constants, "get_default_hermes_root",
                               return_value=self.host.home):
            self.assertEqual(adapter._profile_home(), self.host.home)

    def test_synthesize_runs_inside_the_scope(self):
        """`_synthesize` is what `send()` actually calls on a worker thread."""
        from unittest import mock

        from googlehome import speech

        seen: dict = {}

        def fake_synthesize(text, provider="", backend="auto", voice=""):
            seen["value"] = get_secret("XAI_BASE_URL")  # raises if unscoped
            seen["text"] = text
            return ["/tmp/fake.mp3"]

        with mock.patch.object(speech, "synthesize", side_effect=fake_synthesize):
            paths = GoogleHomeAdapter._synthesize(self.host, "hello")

        self.assertEqual(paths, ["/tmp/fake.mp3"])
        self.assertEqual(seen["text"], "hello")

    def test_transcription_runs_inside_the_scope(self):
        """Push-to-talk transcribes outside any turn, so it needs the scope too.

        `_transcribe_blocking` is what `/listen` calls on a worker thread; a
        keyed STT provider would fail closed without this.
        """
        from unittest import mock

        seen: dict = {}

        def fake_transcribe(path):
            seen["value"] = get_secret("XAI_BASE_URL")  # raises if unscoped
            seen["path"] = path
            return {"success": True, "transcript": "hello"}

        with mock.patch("tools.voice_mode.transcribe_recording", side_effect=fake_transcribe):
            result = GoogleHomeAdapter._transcribe_blocking(self.host, "/tmp/clip.webm")

        self.assertTrue(result["success"])
        self.assertEqual(seen["path"], "/tmp/clip.webm")
        self.assertTrue(seen["value"] is None or isinstance(seen["value"], str))


if __name__ == "__main__":
    unittest.main()
