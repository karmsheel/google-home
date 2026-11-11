"""The in-process playback registry that the Desktop stop control talks to."""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from googlehome import playback  # noqa: E402


class PlaybackRegistryTests(unittest.TestCase):
    def setUp(self):
        playback.reset()
        self.addCleanup(playback.reset)

    def test_idle_snapshot(self):
        state = playback.snapshot()
        self.assertFalse(state["speaking"])
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["sources"], [])

    def test_begin_marks_speaking_and_stop_calls_the_hook(self):
        calls = []
        handle = playback.begin(lambda: calls.append("stop") or True,
                                source="reply", device="Living Room")
        self.addCleanup(playback.end, handle)

        state = playback.snapshot()
        self.assertTrue(state["speaking"])
        self.assertEqual(state["count"], 1)
        self.assertEqual(state["sources"], ["reply"])
        self.assertEqual(state["devices"], ["Living Room"])

        result = playback.stop()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 1)
        self.assertEqual(calls, ["stop"])
        # A stopped clip is no longer advertised, even before its player
        # thread unwinds and unregisters it.
        self.assertFalse(playback.snapshot()["speaking"])

    def test_stop_stops_every_active_clip(self):
        first = playback.begin(lambda: True, source="reply", device="Kitchen")
        second = playback.begin(lambda: True, source="ambient", device="Bedroom")
        self.addCleanup(playback.end, first)
        self.addCleanup(playback.end, second)

        result = playback.stop()
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["stopped"], 2)
        self.assertEqual(playback.snapshot()["count"], 0)

    def test_stop_reports_a_hook_that_refuses(self):
        handle = playback.begin(lambda: False, source="reply")
        self.addCleanup(playback.end, handle)

        result = playback.stop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["stopped"], 0)
        self.assertIn("did not report a stop", result["errors"][0])
        self.assertTrue(playback.snapshot()["speaking"],
                        "a failed stop must keep the control visible for a retry")

    def test_stop_reports_an_exception_and_keeps_the_clip(self):
        def boom():
            raise RuntimeError("cast connection dropped")

        handle = playback.begin(boom, source="ambient")
        self.addCleanup(playback.end, handle)

        result = playback.stop()
        self.assertFalse(result["ok"])
        self.assertIn("cast connection dropped", result["errors"][0])
        self.assertTrue(playback.is_speaking())

    def test_end_is_idempotent(self):
        handle = playback.begin(lambda: True)
        playback.end(handle)
        playback.end(handle)
        self.assertFalse(playback.is_speaking())

    def test_stop_with_nothing_active_is_a_no_op(self):
        result = playback.stop()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stopped"], 0)
        self.assertEqual(result["count"], 0)

    def test_concurrent_stop_calls_are_safe(self):
        calls = []
        lock = threading.Lock()

        def hook():
            with lock:
                calls.append(1)
            return True

        handle = playback.begin(hook)
        self.addCleanup(playback.end, handle)

        threads = [threading.Thread(target=playback.stop) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        # The first caller wins; later ones find nothing and send no extra STOP.
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main()
