"""The Desktop popover's Mute switch must close every door to the speaker.

``speak_replies`` only covers platform replies; tools, the composer's ambient
speech and out-of-process cron all cast directly. A mute that left those open
would be a switch that appears to work and then does not.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googlehome import standalone, tools  # noqa: E402


class MuteGateTests(unittest.TestCase):
    def setUp(self):
        self.muted = SimpleNamespace(muted=True, max_chars=1400, cron_max_chars=400)

    def test_tool_speech_is_skipped_while_muted(self):
        self.assertEqual(
            tools._speak("hello", settings=self.muted),
            {"ok": True, "muted": True, "skipped": True},
        )

    def test_ambient_and_notify_speech_is_skipped_while_muted(self):
        self.assertEqual(
            tools.speak_to("hello", self.muted),
            {"ok": True, "muted": True, "skipped": True},
        )

    def test_out_of_process_cron_is_silent_while_muted(self):
        self.assertEqual(standalone._spoken_text("Cronjob Response: build\nok", self.muted), "")
        self.assertEqual(standalone._spoken_text("a plain line", self.muted), "")


if __name__ == "__main__":
    unittest.main()
