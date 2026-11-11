"""Per-conversation destinations and the turn-end hook."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from unittest import mock

from googlehome import ambient

try:  # stdlib-only, but it belongs to Hermes core
    from agent.secret_scope import (
        get_secret,
        reset_multiplex_context,
        reset_secret_scope,
        set_multiplex_context,
        set_secret_scope,
    )

    SECRET_SCOPE_AVAILABLE = True
except Exception:  # noqa: BLE001 - the rest of the suite runs without core
    SECRET_SCOPE_AVAILABLE = False

requires_scope = unittest.skipUnless(SECRET_SCOPE_AVAILABLE, "agent.secret_scope not importable")


class DestinationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        self.path = os.path.join(self.tmp.name, "googlehome", "destinations.json")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _write_raw(self, entries):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)

    def test_round_trip(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145", name="Living Room")
        entry = ambient.destination_for("s1")
        self.assertEqual(entry["mode"], "speaker")
        self.assertEqual(entry["device"], "192.168.1.145")
        self.assertEqual(entry["name"], "Living Room")

    def test_unknown_session_is_none(self):
        self.assertIsNone(ambient.destination_for("nope"))
        self.assertIsNone(ambient.destination_for(""))

    def test_off_mode_is_kept(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145")
        ambient.set_destination("s1", "off")
        self.assertEqual(ambient.destination_for("s1")["mode"], "off")

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            ambient.set_destination("s1", "computer")

    def test_missing_session_raises(self):
        with self.assertRaises(ValueError):
            ambient.set_destination("", "speaker")

    def test_stale_entry_is_ignored(self):
        self._write_raw({"s1": {"mode": "speaker", "device": "x", "at": int(time.time() * 1000) - ambient.STALE_MS - 1}})
        self.assertIsNone(ambient.destination_for("s1"))

    def test_clear_removes_entry(self):
        ambient.set_destination("s1", "off")
        ambient.clear_destination("s1")
        self.assertIsNone(ambient.destination_for("s1"))

    def test_malformed_file_is_empty(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertIsNone(ambient.destination_for("s1"))


class NoticeStoreTests(unittest.TestCase):
    """A reply that never reached the room is recorded for the Desktop's poll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        self.path = os.path.join(self.tmp.name, "googlehome", "notices.json")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _write_raw(self, payload):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_failure_round_trip(self):
        ambient.record_failure(
            "s1", {"name": "Living Room speaker", "device": "192.168.1.145"}, "hermes: TTS failed"
        )
        rows = ambient.notices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "s1")
        self.assertEqual(rows[0]["name"], "Living Room speaker")
        self.assertEqual(rows[0]["error"], "hermes: TTS failed")
        self.assertGreater(rows[0]["seq"], 0)

    def test_seq_increases_so_the_page_announces_each_once(self):
        ambient.record_failure("s1", {}, "one")
        ambient.record_failure("s1", {}, "two")
        self.assertEqual([row["seq"] for row in ambient.notices()], [1, 2])

    def test_stale_notice_is_ignored(self):
        self._write_raw({
            "seq": 1,
            "entries": [{
                "seq": 1,
                "session": "s1",
                "at": int(time.time() * 1000) - ambient.NOTICE_TTL_MS - 1,
                "error": "old",
            }],
        })
        self.assertEqual(ambient.notices(), [])

    def test_malformed_file_is_empty(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(ambient.notices(), [])

    def test_entries_are_bounded(self):
        for index in range(ambient.MAX_NOTICES + 5):
            ambient.record_failure("s1", {}, f"failure {index}")
        rows = ambient.notices()
        self.assertEqual(len(rows), ambient.MAX_NOTICES)
        self.assertEqual(rows[-1]["error"], f"failure {ambient.MAX_NOTICES + 4}")

    def test_missing_session_or_error_records_nothing(self):
        ambient.record_failure("", {}, "boom")
        ambient.record_failure("s1", {}, "")
        self.assertEqual(ambient.notices(), [])


class PlaybackNoticeEndpointTests(unittest.TestCase):
    """``GET /playback`` is also where the composer learns a reply was not spoken."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        from googlehome import control as control_module

        self.server = control_module.ControlServer(mock.Mock(), port=0, token="")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_playback_poll_carries_unspoken_replies(self):
        ambient.record_failure(
            "local-1", {"name": "Living Room speaker", "device": "192.168.1.145"}, "hermes: TTS failed"
        )
        payload = self._get("/playback")
        self.assertIn("speaking", payload)
        self.assertEqual(payload["notices"][0]["session"], "local-1")
        self.assertEqual(payload["notices"][0]["name"], "Living Room speaker")
        self.assertEqual(payload["notices"][0]["error"], "hermes: TTS failed")

    def test_playback_poll_has_an_empty_notice_list_without_failures(self):
        self.assertEqual(self._get("/playback")["notices"], [])


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        ambient._spoken_turns.clear()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()
        ambient._spoken_turns.clear()

    def test_silent_without_a_destination(self):
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="hello", session_id="s1", platform="desktop")
        speak.assert_not_called()

    def test_silent_for_off(self):
        ambient.set_destination("s1", "off")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="hello", session_id="s1", platform="desktop")
        speak.assert_not_called()

    def test_speaks_for_the_chosen_speaker(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145", name="Living Room")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="hello", session_id="s1", platform="desktop")
        speak.assert_called_once()
        text, entry, session = speak.call_args.args
        self.assertEqual(text, "hello")
        self.assertEqual(entry["device"], "192.168.1.145")
        self.assertEqual(session, "s1")

    def test_never_repeats_the_googlehome_platform(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="hello", session_id="s1", platform="googlehome")
        speak.assert_not_called()

    def test_a_tool_that_spoke_marks_the_turn(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145")
        ambient.on_post_tool_call(tool_name="googlehome_speak", session_id="s1", turn_id="t1")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="hello", session_id="s1", turn_id="t1", platform="desktop")
        speak.assert_not_called()

    def test_a_notification_replaces_the_full_reply(self):
        """The room hears the one-line outcome, not the whole answer again."""
        ambient.set_destination("s1", "speaker", device="192.168.1.145")
        ambient.on_post_tool_call(tool_name="googlehome_notify", session_id="s1", turn_id="t2")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="the whole long answer", session_id="s1", turn_id="t2", platform="desktop")
        speak.assert_not_called()

    def test_empty_reply_is_silent(self):
        ambient.set_destination("s1", "speaker", device="192.168.1.145")
        with mock.patch.object(ambient, "_speak_in_background") as speak:
            ambient.on_post_llm_call(assistant_response="   ", session_id="s1", platform="desktop")
        speak.assert_not_called()


@requires_scope
class BackgroundScopeTests(unittest.TestCase):
    """Ambient speech must run with the turn's profile scope.

    The hook fires inside the gateway's per-profile scope (HERMES_HOME override +
    secret mapping, both contextvars). The clip is synthesized and cast on a
    worker thread, and a new thread starts with an empty context: without an
    explicit copy, Hermes TTS fails closed on a multiplexed gateway with
    ``UnscopedSecretError`` ("Hermes could not read this profile's XAI_BASE_URL")
    and the room hears nothing. That is the bug this guards.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        ambient._spoken_turns.clear()
        # The turn's scope as the gateway installs it: a home override plus the
        # profile's secret mapping, both contextvars.
        from hermes_constants import set_hermes_home_override

        self.home = set_hermes_home_override(self.tmp.name)
        self.scope = set_secret_scope(
            {"XAI_BASE_URL": "https://api.x.ai/v1"}, profile_home=self.tmp.name
        )
        self.multiplex = set_multiplex_context(True)

    def tearDown(self):
        from hermes_constants import reset_hermes_home_override

        reset_secret_scope(self.scope)
        reset_hermes_home_override(self.home)
        reset_multiplex_context(self.multiplex)
        ambient._spoken_turns.clear()
        self.env.stop()
        self.tmp.cleanup()

    def _speak_with_a_probe(self):
        """Patch the real cast so only the credential read remains to check."""
        done = threading.Event()
        seen = {}

        def probe(text, entry, session_id=""):
            try:
                seen["value"] = get_secret("XAI_BASE_URL")
            except Exception as exc:  # noqa: BLE001 - the failure is the assertion
                seen["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                done.set()

        with mock.patch.object(ambient, "speak", side_effect=probe):
            ambient.set_destination("s1", "speaker", device="192.168.1.145", name="Living Room")
            ambient.on_post_llm_call(
                assistant_response="hello", session_id="s1", turn_id="t1", platform="local"
            )
            self.assertTrue(done.wait(10), "the speaking worker never ran")
        return seen

    def test_the_worker_reads_the_turns_secret_scope(self):
        seen = self._speak_with_a_probe()
        self.assertNotIn("error", seen, seen.get("error", ""))
        self.assertEqual(seen.get("value"), "https://api.x.ai/v1")

    def test_the_worker_sees_the_turns_hermes_home(self):
        """The home override travels with the scope, so config.yaml is the turn's."""
        from hermes_constants import get_hermes_home_override

        done = threading.Event()
        seen = {}

        def probe(text, entry, session_id=""):
            seen["home"] = get_hermes_home_override()
            done.set()

        with mock.patch.object(ambient, "speak", side_effect=probe):
            ambient._speak_in_background("hello", {"mode": "speaker", "device": "x"})
            self.assertTrue(done.wait(10), "the speaking worker never ran")
        self.assertEqual(seen.get("home"), self.tmp.name)


class SpeakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _speak_with_fake_tools(self, entry, result=None, session="s1"):
        """``googlehome.tools`` reaches gateway core, which these tests lack.

        In a core-less run the module is replaced wholesale. When the rest of
        the suite already imported the real module (core available), patching
        its attribute is what ``from . import tools`` actually resolves.
        """
        outcome = {"ok": True} if result is None else result
        try:
            tools = importlib.import_module("googlehome.tools")
        except Exception:  # noqa: BLE001 - gateway.* is genuinely absent
            tools = None
        if tools is not None:
            with mock.patch.object(tools, "speak_to", return_value=outcome) as speak_to:
                ambient.speak("hello", entry, session)
            return speak_to
        fake = types.ModuleType("googlehome.tools")
        fake.speak_to = mock.Mock(return_value=outcome)
        with mock.patch.dict(sys.modules, {"googlehome.tools": fake}):
            ambient.speak("hello", entry, session)
        return fake.speak_to

    def test_speak_targets_the_conversations_device(self):
        speak_to = self._speak_with_fake_tools({"mode": "speaker", "device": "192.168.1.145"})
        settings = speak_to.call_args.args[1]
        self.assertEqual(settings.device, "192.168.1.145")

    def test_speak_without_a_device_falls_back_to_settings(self):
        speak_to = self._speak_with_fake_tools({"mode": "speaker", "device": ""})
        settings = speak_to.call_args.args[1]
        self.assertEqual(settings.device, "")

    def test_a_failed_speak_is_recorded_for_the_desktop(self):
        entry = {"mode": "speaker", "device": "192.168.1.145", "name": "Living Room speaker"}
        self._speak_with_fake_tools(entry, result={"ok": False, "error": "hermes: TTS failed"})
        rows = ambient.notices()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "s1")
        self.assertEqual(rows[0]["name"], "Living Room speaker")
        self.assertIn("TTS failed", rows[0]["error"])

    def test_a_spoken_reply_records_nothing(self):
        self._speak_with_fake_tools({"mode": "speaker", "device": "192.168.1.145"})
        self.assertEqual(ambient.notices(), [])


if __name__ == "__main__":
    unittest.main()
