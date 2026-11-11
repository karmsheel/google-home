"""Tests for the `hermes googlehome` CLI surface (stdlib only)."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from googlehome import cli
from googlehome import config as ghconfig


class ParserTests(unittest.TestCase):
    def _parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="hermes googlehome")
        cli.setup_cli(parser)
        return parser

    def test_check_defaults_to_check(self):
        args = self._parser().parse_args([])
        self.assertIsNone(args.googlehome_action)
        self.assertEqual(args.func, cli.run_action)

    def test_check_parses_json_and_speak(self):
        args = self._parser().parse_args(["check", "--json", "--speak", "hello"])
        self.assertEqual(args.googlehome_action, "check")
        self.assertTrue(args.json)
        self.assertEqual(args.speak, "hello")

    def test_devices_timeout(self):
        args = self._parser().parse_args(["devices", "--timeout", "2.5"])
        self.assertEqual(args.timeout, 2.5)

    def test_speak_joins_words(self):
        args = self._parser().parse_args(["speak", "good", "morning"])
        self.assertEqual(args.text, ["good", "morning"])

    def test_firewall_port_override(self):
        args = self._parser().parse_args(["firewall", "--port", "8766"])
        self.assertEqual(args.port, 8766)

    def test_cron_defaults_to_status(self):
        args = self._parser().parse_args(["cron"])
        self.assertEqual(args.googlehome_action, "cron")
        self.assertEqual(args.state, "status")
        self.assertEqual(args.chat_id, "")
        self.assertFalse(args.dry_run)

    def test_cron_on_parses_flags(self):
        args = self._parser().parse_args(
            ["cron", "on", "--dry-run", "--chat-id", "speaker-1", "--json",
             "--cron-profile", "researcher"]
        )
        self.assertEqual(args.state, "on")
        self.assertTrue(args.dry_run)
        self.assertEqual(args.chat_id, "speaker-1")
        self.assertEqual(args.cron_profile, "researcher")
        self.assertTrue(args.json)

    def test_unknown_action_exits_nonzero(self):
        parser = argparse.ArgumentParser(prog="hermes googlehome")
        cli.setup_cli(parser)
        args = parser.parse_args([])
        args.googlehome_action = "nope"
        self.assertEqual(cli.run_action(args), 2)


class ToneTests(unittest.TestCase):
    def test_tone_is_a_valid_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = cli._write_tone(Path(tmp) / "tone.wav")
            self.assertTrue(path.exists())
            with wave.open(str(path), "rb") as handle:
                self.assertEqual(handle.getnchannels(), 1)
                self.assertEqual(handle.getsampwidth(), 2)
                self.assertEqual(handle.getframerate(), 16000)
                self.assertGreater(handle.getnframes(), 1000)


class ControlFileTests(unittest.TestCase):
    def test_missing_control_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HERMES_HOME": tmp}, clear=False
        ):
            self.assertEqual(cli._read_control(), {})

    def test_control_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HERMES_HOME": tmp}, clear=False
        ):
            state = Path(tmp) / "googlehome"
            state.mkdir(parents=True)
            (state / "control.json").write_text(json.dumps({"port": 8765, "token": "t"}))
            self.assertEqual(cli._read_control()["port"], 8765)

    def test_status_without_bridge_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"HERMES_HOME": tmp}, clear=False
        ):
            self.assertEqual(cli._status(argparse.Namespace()), 1)


class CronCommandTests(unittest.TestCase):
    def _run(self, args: argparse.Namespace) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli._cron(args)
        return code, buffer.getvalue()

    def test_on_reports_the_change(self):
        rows = [{
            "id": "a", "name": "Digest", "enabled": True,
            "before": "buzz", "after": "buzz,googlehome:speaker-1", "changed": True,
        }]
        with mock.patch.object(cli, "_settings", return_value=ghconfig.Settings()), \
             mock.patch.object(cli.cronlink, "resolve_chat_id", return_value="speaker-1"), \
             mock.patch.object(cli.cronlink, "profile_label", return_value="researcher"), \
             mock.patch.object(cli.cronlink, "apply_jobs", return_value=rows) as apply:
            code, output = self._run(
                argparse.Namespace(state="on", chat_id="", dry_run=False, json=False)
            )

        self.assertEqual(code, 0)
        self.assertIn("Attached the speaker on profile 'researcher'", output)
        self.assertIn("buzz  →  buzz,googlehome:speaker-1", output)
        apply.assert_called_once_with("on", "speaker-1", dry_run=False, home=None)

    def test_profile_option_targets_another_store(self):
        rows = [{
            "id": "a", "name": "Digest", "enabled": True,
            "before": "buzz", "after": "buzz,googlehome:speaker-1", "changed": True,
        }]
        home = Path("profiles") / "researcher"
        with mock.patch.object(cli, "_settings", return_value=ghconfig.Settings()), \
             mock.patch.object(cli.cronlink, "resolve_chat_id", return_value="speaker-1"), \
             mock.patch.object(cli, "_cron_profile_home", return_value=home), \
             mock.patch.object(cli.cronlink, "profile_label", return_value="researcher"), \
             mock.patch.object(cli.cronlink, "apply_jobs", return_value=rows) as apply:
            code, output = self._run(
                argparse.Namespace(
                    state="on", chat_id="", cron_profile="researcher",
                    dry_run=False, json=False
                )
            )

        self.assertEqual(code, 0)
        self.assertIn("profile 'researcher'", output)
        apply.assert_called_once_with("on", "speaker-1", dry_run=False, home=home)

    def test_status_shows_attached_jobs(self):
        jobs = [{
            "id": "a", "name": "Digest", "enabled": True,
            "deliver": "buzz,googlehome:speaker-1", "failure_deliver": "",
            "attached": True,
        }]
        with mock.patch.object(cli, "_settings", return_value=ghconfig.Settings()), \
             mock.patch.object(cli.cronlink, "resolve_chat_id", return_value="speaker-1"), \
             mock.patch.object(cli.cronlink, "profile_label", return_value="researcher"), \
             mock.patch.object(cli.cronlink, "platform_ready", return_value=True), \
             mock.patch.object(cli.cronlink, "list_jobs", return_value=jobs):
            code, output = self._run(
                argparse.Namespace(state="status", chat_id="", dry_run=False, json=False)
            )

        self.assertEqual(code, 0)
        self.assertIn("registered for cron delivery", output)
        self.assertIn("1 attached", output)
        self.assertIn("googlehome:speaker-1", output)

    def test_status_without_jobs(self):
        with mock.patch.object(cli, "_settings", return_value=ghconfig.Settings()), \
             mock.patch.object(cli.cronlink, "resolve_chat_id", return_value="speaker-1"), \
             mock.patch.object(cli.cronlink, "profile_label", return_value="default"), \
             mock.patch.object(cli.cronlink, "platform_ready", return_value=False), \
             mock.patch.object(cli.cronlink, "list_jobs", return_value=[]):
            code, output = self._run(
                argparse.Namespace(state="status", chat_id="", dry_run=False, json=False)
            )

        self.assertEqual(code, 0)
        self.assertIn("none in this profile", output)
        self.assertIn("NOT registered", output)

    def test_off_warns_when_nothing_is_left(self):
        rows = [{
            "id": "a", "name": "Digest", "enabled": True,
            "before": "googlehome:speaker-1", "after": "local", "changed": True,
        }]
        with mock.patch.object(cli, "_settings", return_value=ghconfig.Settings()), \
             mock.patch.object(cli.cronlink, "resolve_chat_id", return_value="speaker-1"), \
             mock.patch.object(cli.cronlink, "profile_label", return_value="researcher"), \
             mock.patch.object(cli.cronlink, "apply_jobs", return_value=rows):
            code, output = self._run(
                argparse.Namespace(state="off", chat_id="", dry_run=False, json=False)
            )

        self.assertEqual(code, 0)
        self.assertIn("no delivery target", output)


if __name__ == "__main__":
    unittest.main()
