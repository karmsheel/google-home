"""Per-conversation destinations and the turn-end hook."""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

from googlehome import ambient


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
        text, entry = speak.call_args.args
        self.assertEqual(text, "hello")
        self.assertEqual(entry["device"], "192.168.1.145")

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


class SpeakTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _speak_with_fake_tools(self, entry):
        """``googlehome.tools`` reaches gateway core, which these tests lack.

        In a core-less run the module is replaced wholesale. When the rest of
        the suite already imported the real module (core available), patching
        its attribute is what ``from . import tools`` actually resolves.
        """
        try:
            tools = importlib.import_module("googlehome.tools")
        except Exception:  # noqa: BLE001 - gateway.* is genuinely absent
            tools = None
        if tools is not None:
            with mock.patch.object(tools, "speak_to", return_value={"ok": True}) as speak_to:
                ambient.speak("hello", entry)
            return speak_to
        fake = types.ModuleType("googlehome.tools")
        fake.speak_to = mock.Mock(return_value={"ok": True})
        with mock.patch.dict(sys.modules, {"googlehome.tools": fake}):
            ambient.speak("hello", entry)
        return fake.speak_to

    def test_speak_targets_the_conversations_device(self):
        speak_to = self._speak_with_fake_tools({"mode": "speaker", "device": "192.168.1.145"})
        settings = speak_to.call_args.args[1]
        self.assertEqual(settings.device, "192.168.1.145")

    def test_speak_without_a_device_falls_back_to_settings(self):
        speak_to = self._speak_with_fake_tools({"mode": "speaker", "device": ""})
        settings = speak_to.call_args.args[1]
        self.assertEqual(settings.device, "")


if __name__ == "__main__":
    unittest.main()
