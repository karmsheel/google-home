"""Speech layer tests: markdown flattening, chunking, and backend fallback.

The real backends are never executed here — Hermes TTS, the Windows speech
engine and edge-tts are all replaced through ``speech._BACKEND_IMPL``, so these
tests are fast and platform independent. ``_synth_hermes`` is also exercised
against a fake ``tools.tts_tool`` module.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from googlehome import speech


def _reset_backend() -> None:
    speech._preferred = ""
    speech._last_backend = ""


class BackendSelectionTests(unittest.TestCase):
    def setUp(self):
        _reset_backend()
        self.addCleanup(_reset_backend)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.calls: list[str] = []
        self.behaviour: dict[str, str] = {}

    def _install(self):
        def make(name):
            def impl(text, provider, voice):
                self.calls.append(name)
                outcome = self.behaviour.get(name, "fail")
                if outcome == "ok":
                    path = self.tmp / f"{name}.wav"
                    path.write_bytes(b"RIFFfake")
                    return [str(path)]
                raise RuntimeError(f"{name} cannot speak")

            return impl

        patcher = mock.patch.dict(
            speech._BACKEND_IMPL,
            {name: make(name) for name in ("hermes", "sapi", "edge")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_prefers_hermes(self):
        self._install()
        self.behaviour["hermes"] = "ok"
        paths = speech.synthesize("hello")
        self.assertEqual(len(paths), 1)
        self.assertEqual(self.calls, ["hermes"])
        self.assertEqual(speech.last_backend(), "hermes")

    def test_auto_falls_back_to_sapi_when_hermes_fails(self):
        self._install()
        self.behaviour["sapi"] = "ok"
        speech.synthesize("hello")
        self.assertEqual(self.calls, ["hermes", "sapi"])

    def test_auto_falls_back_to_edge_last(self):
        self._install()
        self.behaviour["edge"] = "ok"
        speech.synthesize("hello")
        self.assertEqual(self.calls, ["hermes", "sapi", "edge"])

    def test_working_backend_is_remembered(self):
        self._install()
        self.behaviour["sapi"] = "ok"
        speech.synthesize("first")
        self.calls.clear()
        speech.synthesize("second")
        self.assertEqual(self.calls, ["sapi"], "a known-good backend should be tried first")

    def test_explicit_backend_is_the_only_one_tried(self):
        self._install()
        self.behaviour["edge"] = "ok"
        speech.synthesize("hello", backend="edge")
        self.assertEqual(self.calls, ["edge"])

    def test_explicit_backend_failure_does_not_fall_back(self):
        self._install()
        with self.assertRaises(speech.TtsUnavailable):
            speech.synthesize("hello", backend="hermes")
        self.assertEqual(self.calls, ["hermes"])

    def test_unknown_backend_is_rejected(self):
        with self.assertRaises(speech.TtsUnavailable) as raised:
            speech.synthesize("hello", backend="telepathy")
        self.assertIn("telepathy", str(raised.exception))

    def test_all_backends_failing_reports_each_reason(self):
        self._install()
        with self.assertRaises(speech.TtsUnavailable) as raised:
            speech.synthesize("hello")
        message = str(raised.exception)
        for name in ("hermes", "sapi", "edge"):
            self.assertIn(name, message)

    def test_empty_text_makes_no_calls(self):
        self._install()
        self.assertEqual(speech.synthesize("   "), [])
        self.assertEqual(self.calls, [])


class HermesBackendTests(unittest.TestCase):
    def setUp(self):
        _reset_backend()
        self.addCleanup(_reset_backend)
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)

    def _install_tool(self, payload_factory, calls):
        def text_to_speech_tool(text, output_path=None, speed=None, instructions=None, provider=None):
            calls.append(provider)
            return json.dumps(payload_factory())

        tools_pkg = types.ModuleType("tools")
        tts_module = types.ModuleType("tools.tts_tool")
        tts_module.text_to_speech_tool = text_to_speech_tool
        patcher = mock.patch.dict(sys.modules, {"tools": tools_pkg, "tools.tts_tool": tts_module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_returns_existing_paths(self):
        audio = self.tmp / "a.mp3"
        audio.write_bytes(b"ID3")
        calls: list = []
        self._install_tool(
            lambda: {"success": True, "file_path": str(audio), "file_paths": [str(audio)]}, calls
        )
        paths = speech.synthesize("hello", backend="hermes")
        self.assertEqual(paths, [str(audio)])

    def test_provider_is_forwarded(self):
        audio = self.tmp / "a.mp3"
        audio.write_bytes(b"ID3")
        calls: list = []
        self._install_tool(lambda: {"success": True, "file_path": str(audio)}, calls)
        speech.synthesize("hello", backend="hermes", provider="xai")
        self.assertEqual(calls, ["xai"])

    def test_reported_failure_raises(self):
        calls: list = []
        self._install_tool(lambda: {"success": False, "error": "no api key"}, calls)
        with self.assertRaises(speech.TtsUnavailable) as raised:
            speech.synthesize("hello", backend="hermes")
        self.assertIn("no api key", str(raised.exception))

    def test_success_without_a_file_raises(self):
        calls: list = []
        self._install_tool(
            lambda: {"success": True, "file_path": str(self.tmp / "missing.mp3")}, calls
        )
        with self.assertRaises(speech.TtsUnavailable):
            speech.synthesize("hello", backend="hermes")


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(speech.split_for_tts("Hello there.", 100), ["Hello there."])

    def test_long_text_splits_on_sentences(self):
        text = "One sentence here. " * 20
        chunks = speech.split_for_tts(text.strip(), 100)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 101)
        self.assertEqual(" ".join(chunks).count("sentence"), 20)

    def test_unbroken_text_still_splits(self):
        chunks = speech.split_for_tts("x" * 250, 100)
        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(chunks), "x" * 250)

    def test_empty_text(self):
        self.assertEqual(speech.split_for_tts("", 100), [])
        self.assertEqual(speech.split_for_tts("   ", 100), [])


class FlattenTests(unittest.TestCase):
    def test_strips_code_fences_and_headings(self):
        text = "# Title\n\n```python\nprint(1)\n```\n\nBody text."
        spoken = speech.flatten_for_speech(text)
        self.assertNotIn("```", spoken)
        self.assertNotIn("print(1)", spoken)
        self.assertNotIn("#", spoken)
        self.assertIn("Body text.", spoken)

    def test_keeps_link_text_drops_url(self):
        spoken = speech.flatten_for_speech("See [the docs](https://example.com/secret).")
        self.assertIn("the docs", spoken)
        self.assertNotIn("https://", spoken)

    def test_strips_lists_and_emphasis(self):
        spoken = speech.flatten_for_speech("- **bold** item\n- _italic_ item")
        self.assertNotIn("*", spoken)
        self.assertNotIn("_", spoken)
        self.assertIn("bold", spoken)

    def test_plain_text_passes_through(self):
        self.assertEqual(speech.flatten_for_speech("Just a sentence."), "Just a sentence.")

    def test_long_text_is_capped_at_a_sentence(self):
        text = ("This is a sentence. " * 200).strip()
        spoken = speech.flatten_for_speech(text, max_chars=200)
        self.assertLessEqual(len(spoken), 260)
        self.assertIn("more in the Hermes window", spoken)

    def test_empty_input(self):
        self.assertEqual(speech.flatten_for_speech(""), "")
        self.assertEqual(speech.flatten_for_speech(None), "")


class CronDigestTests(unittest.TestCase):
    WRAPPER = (
        "Cronjob Response: Daily Digest\n"
        "(job_id: abc123)\n"
        "-------------\n\n"
        "{body}\n\n"
        "To stop or manage this job, send me a new message "
        "(e.g. \"stop reminder Daily Digest\")."
    )

    def test_wrapper_is_stripped_and_name_is_kept(self):
        text = self.WRAPPER.format(body="The build is green.")
        spoken = speech.cron_digest(text)
        self.assertEqual(spoken, "Daily Digest. The build is green.")

    def test_footer_and_job_id_are_never_spoken(self):
        spoken = speech.cron_digest(self.WRAPPER.format(body="All systems nominal."))
        for fragment in ("Cronjob Response", "job_id", "To stop or manage", "abc123"):
            self.assertNotIn(fragment, spoken)

    def test_missing_wrapper_still_works(self):
        self.assertEqual(speech.cron_digest("Plain output."), "Plain output.")

    def test_long_output_is_capped_with_a_chat_tail(self):
        body = "This is a sentence about the daily result. " * 40
        spoken = speech.cron_digest(self.WRAPPER.format(body=body), max_chars=200)
        self.assertLessEqual(len(spoken), 260)
        self.assertIn("more in the chat", spoken)

    def test_speak_marker_wins(self):
        text = self.WRAPPER.format(
            body="Full report with lots of detail.\n\nSPEAK: The digest is ready, two new items."
        )
        spoken = speech.cron_digest(text)
        self.assertIn("The digest is ready", spoken)
        self.assertNotIn("Full report", spoken)

    def test_speak_marker_is_flattened(self):
        text = self.WRAPPER.format(body="SPEAK: **Build** _green_ for [main](https://x.test).")
        spoken = speech.cron_digest(text)
        self.assertEqual(spoken, "Daily Digest. Build green for main.")

    def test_empty_body(self):
        self.assertEqual(
            speech.cron_digest("Cronjob Response: X\n(job_id: 1)\n-------------\n\n"),
            "",
        )


class SapiAvailabilityTests(unittest.TestCase):
    def test_availability_follows_the_platform(self):
        self.assertEqual(speech.sapi_available(), sys.platform == "win32")

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_sapi_produces_a_wav(self):
        if not speech.sapi_available():
            self.skipTest("no Windows speech engine")
        paths = speech.synthesize("Testing one two three.", backend="sapi")
        self.assertTrue(paths)
        self.assertTrue(Path(paths[0]).is_file())
        self.assertGreater(Path(paths[0]).stat().st_size, 1024)
        self.assertTrue(paths[0].endswith(".wav"))


if __name__ == "__main__":
    unittest.main()
