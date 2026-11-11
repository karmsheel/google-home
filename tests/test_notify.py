"""One-line task notifications: wording, speaker matching, and dispatch."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from googlehome import notify
from googlehome import config as ghconfig


LIVING = {
    "name": "Living Room speaker",
    "model": "Google Home Mini",
    "address": "192.168.1.145",
    "host": "uuid-living.local",
    "port": 8009,
    "audio_only": True,
}
KITCHEN = {
    "name": "Kitchen speaker",
    "model": "Google Home Mini",
    "address": "192.168.1.146",
    "host": "uuid-kitchen.local",
    "port": 8009,
    "audio_only": True,
}
BEDROOM = {
    "name": "Bedroom speaker",
    "model": "Nest Mini",
    "address": "192.168.1.147",
    "host": "uuid-bedroom.local",
    "port": 8009,
    "audio_only": True,
}


class NotifyTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ctx = mock.patch.object(ghconfig, "_PLUGIN_CTX", None)
        self.ctx.start()
        self.addCleanup(self.ctx.stop)


class ComposeTests(NotifyTestBase):
    def test_done_is_the_line_the_user_asked_for(self):
        self.assertEqual(
            notify.compose("done", "the report"),
            "Task complete: the report. Waiting for your review.",
        )

    def test_blocked_says_what_it_needs(self):
        self.assertEqual(
            notify.compose("blocked", "the deploy", "API credentials"),
            "Task blocked: the deploy. API credentials. Needs your input.",
        )

    def test_failed_can_be_generic(self):
        self.assertEqual(notify.compose("failed"), "Task failed. Needs your attention.")

    def test_aliases_are_accepted(self):
        for word in ("completed", "Finished", "waiting-review", "success"):
            self.assertEqual(notify.normalize_status(word), "done")
        for word in ("needs_input", "stuck", "question"):
            self.assertEqual(notify.normalize_status(word), "blocked")
        for word in ("error", "failure", "broken"):
            self.assertEqual(notify.normalize_status(word), "failed")

    def test_unknown_status_is_refused(self):
        self.assertEqual(notify.normalize_status("almost"), "")
        with self.assertRaises(ValueError):
            notify.compose("almost")

    def test_markdown_and_newlines_are_flattened(self):
        line = notify.compose("done", "**the build**\n\nfor `main`")
        self.assertNotIn("\n", line)
        self.assertNotIn("*", line)
        self.assertIn("the build for main", line)

    def test_long_label_and_detail_are_clipped(self):
        line = notify.compose("done", "x" * 200, "y" * 300)
        self.assertLessEqual(len(line), notify.MAX_LINE_CHARS)
        self.assertTrue(line.startswith("Task complete: "))
        self.assertTrue(line.endswith("Waiting for your review."))

    def test_task_punctuation_is_not_doubled(self):
        self.assertEqual(
            notify.compose("done", "the report."),
            "Task complete: the report. Waiting for your review.",
        )


class MatchingTests(NotifyTestBase):
    def test_friendly_name_finds_the_speaker(self):
        matches = notify.match_devices([LIVING, KITCHEN, BEDROOM], "living room speaker")
        self.assertEqual([device["address"] for device in matches], ["192.168.1.145"])

    def test_room_alone_is_enough(self):
        matches = notify.match_devices([LIVING, KITCHEN], "kitchen")
        self.assertEqual([device["address"] for device in matches], ["192.168.1.146"])

    def test_unknown_room_matches_nothing_instead_of_the_first_speaker(self):
        self.assertEqual(notify.match_devices([LIVING, KITCHEN], "attic"), [])

    def test_two_rooms_with_the_same_words_are_ambiguous(self):
        twin = {**LIVING, "address": "192.168.1.200", "host": "uuid-twin.local"}
        twin = {**twin, "name": "Living Room speaker 2"}
        matches = notify.match_devices([LIVING, twin], "living room")
        self.assertEqual(len(matches), 2)

    def test_saying_speaker_prefers_the_speaker_over_a_same_room_tv(self):
        tv = {
            "name": "Living Room TV",
            "model": "Chromecast",
            "address": "192.168.1.150",
            "host": "uuid-tv.local",
            "audio_only": False,
        }
        matches = notify.match_devices([LIVING, tv], "living room speaker")
        self.assertEqual([device["name"] for device in matches], ["Living Room speaker"])

    def test_a_room_name_alone_can_still_be_ambiguous(self):
        tv = {
            "name": "Living Room TV",
            "model": "Chromecast",
            "address": "192.168.1.150",
            "host": "uuid-tv.local",
            "audio_only": False,
        }
        self.assertEqual(len(notify.match_devices([LIVING, tv], "living room")), 2)

    def test_a_room_named_in_another_script_still_matches(self):
        room = {**LIVING, "name": "Гостиная", "address": "192.168.1.148"}
        matches = notify.match_devices([room, KITCHEN], "гостиная")
        self.assertEqual([device["address"] for device in matches], ["192.168.1.148"])

    def test_address_is_matchable(self):
        matches = notify.match_devices([LIVING, KITCHEN], "192.168.1.146")
        self.assertEqual([device["name"] for device in matches], ["Kitchen speaker"])


class ResolveTargetTests(NotifyTestBase):
    def _devices(self, *_args, **_kwargs):
        return [dict(LIVING), dict(KITCHEN)]

    def test_named_speaker_wins(self):
        device = notify.resolve_target(
            "living room speaker",
            settings=ghconfig.Settings(),
            discover_fn=self._devices,
        )
        self.assertEqual(device["address"], "192.168.1.145")

    def test_unknown_name_raises_with_the_available_speakers(self):
        with self.assertRaises(notify.SpeakerLookupError) as caught:
            notify.resolve_target(
                "attic", settings=ghconfig.Settings(), discover_fn=self._devices
            )
        message = str(caught.exception)
        self.assertIn("Living Room speaker", message)
        self.assertIn("Kitchen speaker", message)
        self.assertEqual(len(caught.exception.available), 2)

    def test_ambiguous_name_raises(self):
        twin = {**LIVING, "name": "Living Room speaker 2", "address": "192.168.1.200"}
        with self.assertRaises(notify.SpeakerLookupError) as caught:
            notify.resolve_target(
                "living room",
                settings=ghconfig.Settings(),
                discover_fn=lambda *a, **k: [dict(LIVING), dict(twin)],
            )
        self.assertIn("more than one", str(caught.exception))

    def test_empty_spec_uses_the_configured_speaker(self):
        with mock.patch.object(notify, "_direct_device", return_value=dict(KITCHEN)):
            device = notify.resolve_target(
                "",
                settings=ghconfig.Settings(device="192.168.1.146"),
                discover_fn=self._devices,
            )
        self.assertEqual(device["address"], "192.168.1.146")

    def test_empty_spec_without_configuration_prefers_audio_only(self):
        video = {
            "name": "TV",
            "model": "Chromecast",
            "address": "192.168.1.150",
            "host": "uuid-tv.local",
            "audio_only": False,
        }
        device = notify.resolve_target(
            "",
            settings=ghconfig.Settings(),
            discover_fn=lambda *a, **k: [dict(video), dict(KITCHEN)],
        )
        self.assertEqual(device["name"], "Kitchen speaker")

    def test_a_generic_phrase_means_the_default_speaker(self):
        with mock.patch.object(notify, "_direct_device", return_value=dict(KITCHEN)):
            device = notify.resolve_target(
                "the speaker",
                settings=ghconfig.Settings(device="192.168.1.146"),
                discover_fn=self._devices,
            )
        self.assertEqual(device["address"], "192.168.1.146")

    def test_no_speaker_at_all_is_an_error(self):
        with self.assertRaises(notify.SpeakerLookupError):
            notify.resolve_target(
                "", settings=ghconfig.Settings(), discover_fn=lambda *a, **k: []
            )

    def test_ip_is_dialled_directly(self):
        with mock.patch.object(
            notify, "_direct_device", return_value={"host": "10.0.0.9", "address": "10.0.0.9"}
        ) as direct:
            device = notify.resolve_target(
                "10.0.0.9", settings=ghconfig.Settings(), discover_fn=self._devices
            )
        self.assertEqual(device["address"], "10.0.0.9")
        direct.assert_called_once()

    def test_an_address_skips_the_discovery_window(self):
        """The composer stores addresses; notifying must not cost 3 seconds."""
        calls = []

        def discover(*_args, **_kwargs):
            calls.append(True)
            return []

        with mock.patch.object(
            notify, "_direct_device", return_value={"host": "10.0.0.9", "address": "10.0.0.9"}
        ):
            notify.resolve_target(
                "10.0.0.9", settings=ghconfig.Settings(), discover_fn=discover
            )
            notify.resolve_target(
                "",
                settings=ghconfig.Settings(device="10.0.0.9"),
                discover_fn=discover,
            )
        self.assertEqual(calls, [])


class AnnounceTests(NotifyTestBase):
    def test_speaks_the_composed_line_to_the_named_speaker(self):
        calls = []

        def fake_cast(line, device, settings):
            calls.append((line, device, settings))
            return {"ok": True, "device": "Living Room speaker", "via": "direct"}

        result = notify.announce(
            "done",
            task="the report",
            speaker="living room",
            settings=ghconfig.Settings(),
            discover_fn=lambda *a, **k: [dict(LIVING), dict(KITCHEN)],
            cast=fake_cast,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["spoken"], "Task complete: the report. Waiting for your review.")
        self.assertEqual(result["device"], "Living Room speaker")
        self.assertEqual(result["address"], "192.168.1.145")
        self.assertEqual(len(calls), 1)
        line, device, settings = calls[0]
        self.assertEqual(line, result["spoken"])
        self.assertEqual(device["address"], "192.168.1.145")
        # The cast path gets a device spec it can dial, not a room name.
        self.assertEqual(settings.device, "192.168.1.145")

    def test_cast_failure_is_reported_not_raised(self):
        result = notify.announce(
            "blocked",
            task="the deploy",
            settings=ghconfig.Settings(),
            discover_fn=lambda *a, **k: [dict(LIVING)],
            cast=lambda line, device, settings: {"ok": False, "error": "cast failed"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "cast failed")
        self.assertEqual(result["status"], "blocked")

    def test_unknown_speaker_never_casts(self):
        def must_not_run(*_args):
            raise AssertionError("cast should not run without a target")

        with self.assertRaises(notify.SpeakerLookupError):
            notify.announce(
                "done",
                speaker="attic",
                settings=ghconfig.Settings(),
                discover_fn=lambda *a, **k: [dict(LIVING)],
                cast=must_not_run,
            )


class AdapterTargetTests(NotifyTestBase):
    def test_same_host_reuses_the_live_adapter(self):
        adapter = mock.Mock()
        adapter._device = {"host": "192.168.1.145"}
        self.assertTrue(notify._adapter_targets(adapter, "192.168.1.145"))

    def test_another_host_uses_the_direct_path(self):
        adapter = mock.Mock()
        adapter._device = {"host": "192.168.1.146"}
        self.assertFalse(notify._adapter_targets(adapter, "192.168.1.145"))

    def test_unresolved_adapter_falls_back_to_direct(self):
        adapter = mock.Mock()
        adapter._device = {}
        adapter.settings = ghconfig.Settings(device="living room")
        with mock.patch.object(notify.discovery, "resolve_device", return_value={}):
            self.assertFalse(notify._adapter_targets(adapter, "192.168.1.145"))


if __name__ == "__main__":
    unittest.main()
