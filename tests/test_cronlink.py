"""Cron wiring: deliver-string rewriting, chat-id resolution, and job patching."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from googlehome import cronlink


class DeliverStringTests(unittest.TestCase):
    def test_parse_string_and_list(self):
        self.assertEqual(cronlink.parse_deliver("buzz, telegram"), ["buzz", "telegram"])
        self.assertEqual(cronlink.parse_deliver(["buzz", " ", "local"]), ["buzz", "local"])
        self.assertEqual(cronlink.parse_deliver(None), [])

    def test_attach_appends_target(self):
        value, changed = cronlink.attach("buzz", "speaker-1")
        self.assertTrue(changed)
        self.assertEqual(value, "buzz,googlehome:speaker-1")

    def test_attach_is_idempotent(self):
        value, changed = cronlink.attach("buzz,googlehome:speaker-1", "speaker-1")
        self.assertFalse(changed)
        self.assertEqual(value, "buzz,googlehome:speaker-1")

    def test_attach_keeps_an_existing_speaker_target(self):
        value, changed = cronlink.attach("buzz,googlehome:other", "speaker-1")
        self.assertFalse(changed)
        self.assertEqual(value, "buzz,googlehome:other")

    def test_attach_upgrades_a_bare_platform_token(self):
        value, changed = cronlink.attach("buzz,googlehome", "speaker-1")
        self.assertTrue(changed)
        self.assertEqual(value, "buzz,googlehome:speaker-1")

    def test_attach_replaces_local_only(self):
        value, changed = cronlink.attach("local", "speaker-1")
        self.assertTrue(changed)
        self.assertEqual(value, "googlehome:speaker-1")

    def test_attach_keeps_local_when_another_target_exists(self):
        value, changed = cronlink.attach("local,buzz", "speaker-1")
        self.assertTrue(changed)
        self.assertEqual(value, "local,buzz,googlehome:speaker-1")

    def test_attach_needs_a_chat_id(self):
        with self.assertRaises(ValueError):
            cronlink.attach("buzz", "")

    def test_detach_removes_every_speaker_token(self):
        value, changed = cronlink.detach("buzz,googlehome:a,googlehome:b")
        self.assertTrue(changed)
        self.assertEqual(value, "buzz")

    def test_detach_falls_back_to_local(self):
        value, changed = cronlink.detach("googlehome:speaker-1")
        self.assertTrue(changed)
        self.assertEqual(value, "local")

    def test_detach_is_idempotent(self):
        value, changed = cronlink.detach("buzz")
        self.assertFalse(changed)
        self.assertEqual(value, "buzz")


class ChatIdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _write_control(self, chat_id: str) -> None:
        state = self.home / "googlehome"
        state.mkdir(parents=True, exist_ok=True)
        (state / "control.json").write_text(
            json.dumps({"port": 8765, "chat_id": chat_id}), encoding="utf-8"
        )

    def test_override_wins(self):
        self._write_control("speaker-live")
        self.assertEqual(cronlink.resolve_chat_id("speaker-settings", "speaker-override"),
                         "speaker-override")

    def test_running_bridge_wins_over_settings(self):
        self._write_control("speaker-live")
        self.assertEqual(cronlink.resolve_chat_id("speaker-settings"), "speaker-live")

    def test_settings_are_the_fallback(self):
        self.assertEqual(cronlink.resolve_chat_id("speaker-settings"), "speaker-settings")

    def test_profile_home_finds_the_default_home(self):
        profile_home = self.home / "profiles" / "researcher"
        profile_home.mkdir(parents=True)
        state = self.home / "googlehome"
        state.mkdir(parents=True, exist_ok=True)
        (state / "control.json").write_text(
            json.dumps({"chat_id": "speaker-default-home"}), encoding="utf-8"
        )
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}, clear=False):
            self.assertEqual(cronlink.candidate_homes()[1], self.home)
            self.assertEqual(cronlink.resolve_chat_id("speaker-settings"),
                             "speaker-default-home")


SAMPLE_LIST = """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs (profile: default)               │
└─────────────────────────────────────────────────────────────────────────┘

  a1b2c3d4 [active]
    Name:      Digest
    Schedule:  35 21 * * *
    Deliver:   buzz
    Last run:  2026-11-11T21:36:32  ok

  e5f6a7b8 [paused]
    Name:      Only local
    Schedule:  0 9 * * *
    Deliver:   local,googlehome:speaker-1
"""


class ParseJobsTests(unittest.TestCase):
    """Reading `hermes cron list` instead of the cron store."""

    def test_reads_id_name_deliver_and_state(self):
        rows = cronlink.parse_jobs(SAMPLE_LIST)
        self.assertEqual([row["id"] for row in rows], ["a1b2c3d4", "e5f6a7b8"])
        self.assertEqual(rows[0]["name"], "Digest")
        self.assertEqual(rows[0]["deliver"], "buzz")
        self.assertFalse(rows[0]["attached"])
        self.assertTrue(rows[0]["enabled"])
        self.assertEqual(rows[1]["name"], "Only local")
        self.assertEqual(rows[1]["deliver"], "local,googlehome:speaker-1")
        self.assertTrue(rows[1]["attached"])
        self.assertFalse(rows[1]["enabled"], "a paused job is not enabled")

    def test_no_jobs_is_an_empty_list(self):
        self.assertEqual(
            cronlink.parse_jobs("No scheduled jobs in profile 'default'.\nCreate one ..."),
            [],
        )

    def test_an_unrecognised_listing_is_an_error_not_an_empty_store(self):
        with self.assertRaises(RuntimeError):
            cronlink.parse_jobs("some future output shape\n")

    def test_list_jobs_reads_every_job_through_the_cli(self):
        with mock.patch.object(cronlink, "_run_hermes", return_value=SAMPLE_LIST) as run:
            rows = cronlink.list_jobs()
        run.assert_called_once_with(["cron", "list", "--all"], home=None)
        self.assertEqual(len(rows), 2)


class ApplyJobsTests(unittest.TestCase):
    """Writes go through `hermes cron edit`, never the store."""

    def setUp(self):
        self.jobs = [
            {"id": "a", "name": "Digest", "enabled": True, "deliver": "buzz", "attached": False},
            {"id": "b", "name": "Only local", "enabled": False, "deliver": "local", "attached": False},
        ]
        self.edits: list[tuple[str, str]] = []

        def fake_edit(job_id, deliver, *, home=None):
            self.edits.append((job_id, deliver))
            return cronlink.edit_command(job_id, deliver)

        self._list = mock.patch.object(
            cronlink, "list_jobs", side_effect=lambda home=None: [dict(job) for job in self.jobs]
        )
        self._edit = mock.patch.object(cronlink, "edit_deliver", side_effect=fake_edit)
        self._list.start()
        self._edit.start()
        self.addCleanup(self._list.stop)
        self.addCleanup(self._edit.stop)

    def test_on_attaches_every_job(self):
        rows = cronlink.apply_jobs("on", "speaker-1")
        self.assertEqual([row["changed"] for row in rows], [True, True])
        self.assertEqual(self.edits, [
            ("a", "buzz,googlehome:speaker-1"),
            ("b", "googlehome:speaker-1"),
        ])
        self.assertEqual(
            rows[0]["command"], "hermes cron edit a --deliver buzz,googlehome:speaker-1"
        )

    def test_on_is_idempotent(self):
        self.jobs[0]["deliver"] = "buzz,googlehome:speaker-1"
        self.jobs[1]["deliver"] = "googlehome:speaker-1"
        rows = cronlink.apply_jobs("on", "speaker-1")
        self.assertFalse(any(row["changed"] for row in rows))
        self.assertEqual(self.edits, [], "no edit command when nothing changed")

    def test_off_restores_local_for_speaker_only_jobs(self):
        self.jobs[0]["deliver"] = "buzz,googlehome:speaker-1"
        self.jobs[1]["deliver"] = "googlehome:speaker-1"
        cronlink.apply_jobs("off", "speaker-1")
        self.assertEqual(self.edits, [("a", "buzz"), ("b", "local")])

    def test_dry_run_prints_instead_of_writing(self):
        rows = cronlink.apply_jobs("on", "speaker-1", dry_run=True)
        self.assertTrue(any(row["changed"] for row in rows))
        self.assertEqual(self.edits, [])
        self.assertEqual(
            rows[1]["command"], "hermes cron edit b --deliver googlehome:speaker-1"
        )


class EditCommandTests(unittest.TestCase):
    def test_home_routes_the_cli(self):
        home = Path("profiles") / "researcher"
        with mock.patch.object(cronlink, "_run_hermes", return_value="") as run:
            cronlink.edit_deliver("a", "buzz", home=home)
        run.assert_called_once_with(["cron", "edit", "a", "--deliver", "buzz"], home=home)

    def test_the_displayed_command_is_the_public_one(self):
        self.assertEqual(
            cronlink.edit_command("a1b2", "buzz,googlehome:speaker-1"),
            "hermes cron edit a1b2 --deliver buzz,googlehome:speaker-1",
        )

    def test_a_failed_edit_is_a_readable_error(self):
        failed = mock.Mock(returncode=1, stdout="", stderr="Job not found: a\n")
        with mock.patch.object(cronlink, "hermes_command", return_value=["hermes"]), \
             mock.patch.object(cronlink.subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as raised:
                cronlink.edit_deliver("a", "buzz")
        self.assertIn("Job not found", str(raised.exception))
        self.assertIn("cron edit", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
