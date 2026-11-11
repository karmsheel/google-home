"""Cron wiring: deliver-string rewriting, chat-id resolution, and job patching."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import types
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


class _FakeCronJobs:
    """Minimal cron.jobs stand-in: the real module needs the Hermes tree."""

    def __init__(self, jobs: list[dict]):
        self.jobs = jobs
        self.saved: list[dict] | None = None
        self.used_homes: list = []

    def load_jobs(self):
        return [dict(job) for job in self.jobs]

    def save_jobs(self, jobs):
        self.saved = [dict(job) for job in jobs]
        self.jobs = [dict(job) for job in jobs]

    @contextlib.contextmanager
    def use_cron_store(self, home):
        self.used_homes.append(home)
        yield

    @contextlib.contextmanager
    def _jobs_lock(self):
        yield


class JobPatchingTests(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeCronJobs([
            {"id": "a", "name": "Digest", "deliver": "buzz", "enabled": True},
            {"id": "b", "name": "Only local", "deliver": "local", "enabled": False},
        ])
        cron_pkg = types.ModuleType("cron")
        cron_pkg.jobs = self.fake
        self._modules = mock.patch.dict(
            sys.modules, {"cron": cron_pkg, "cron.jobs": self.fake}
        )
        self._modules.start()
        self.addCleanup(self._modules.stop)

    def test_on_attaches_every_job(self):
        rows = cronlink.apply_jobs("on", "speaker-1")
        self.assertEqual([row["changed"] for row in rows], [True, True])
        self.assertEqual(self.fake.jobs[0]["deliver"], "buzz,googlehome:speaker-1")
        self.assertEqual(self.fake.jobs[1]["deliver"], "googlehome:speaker-1")
        self.assertIsNotNone(self.fake.saved)

    def test_on_is_idempotent(self):
        cronlink.apply_jobs("on", "speaker-1")
        self.fake.saved = None
        rows = cronlink.apply_jobs("on", "speaker-1")
        self.assertFalse(any(row["changed"] for row in rows))
        self.assertIsNone(self.fake.saved, "no write when nothing changed")

    def test_off_restores_local_for_speaker_only_jobs(self):
        cronlink.apply_jobs("on", "speaker-1")
        rows = cronlink.apply_jobs("off", "speaker-1")
        self.assertEqual(self.fake.jobs[0]["deliver"], "buzz")
        self.assertEqual(self.fake.jobs[1]["deliver"], "local")
        self.assertTrue(all(row["changed"] for row in rows))

    def test_dry_run_does_not_write(self):
        rows = cronlink.apply_jobs("on", "speaker-1", dry_run=True)
        self.assertTrue(any(row["changed"] for row in rows))
        self.assertEqual(self.fake.jobs[0]["deliver"], "buzz")
        self.assertIsNone(self.fake.saved)

    def test_list_jobs_reports_attach_state(self):
        cronlink.apply_jobs("on", "speaker-1")
        rows = cronlink.list_jobs()
        self.assertTrue(all(row["attached"] for row in rows))
        self.assertEqual(rows[0]["deliver"], "buzz,googlehome:speaker-1")
        self.assertEqual(rows[0]["failure_deliver"], "",
                         "an unset failure lane follows deliver; it is not 'local'")

    def test_home_routes_the_store(self):
        home = Path("profiles") / "researcher"
        cronlink.apply_jobs("on", "speaker-1", home=home)
        self.assertEqual(self.fake.used_homes, [home])

    def test_default_store_is_not_redirected(self):
        cronlink.list_jobs()
        self.assertEqual(self.fake.used_homes, [])


if __name__ == "__main__":
    unittest.main()
