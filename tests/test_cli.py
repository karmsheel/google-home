"""Tests for the `hermes googlehome` CLI surface (stdlib only)."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from googlehome import cli


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


if __name__ == "__main__":
    unittest.main()
