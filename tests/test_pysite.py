"""The import repair for launchers that strip ``site-packages`` from sys.path."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from googlehome import pysite


class CandidateTests(unittest.TestCase):
    def test_user_site_is_offered(self):
        with mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\tester\AppData\Roaming"}, clear=False):
            candidates = [str(path) for path in pysite.candidate_site_dirs()]
        version = f"Python{pysite.sys.version_info.major}{pysite.sys.version_info.minor}"
        self.assertTrue(
            any(path.endswith(str(Path("Python") / version / "site-packages")) for path in candidates),
            candidates,
        )

    def test_candidates_are_unique(self):
        paths = pysite.candidate_site_dirs()
        self.assertEqual(len(paths), len(set(paths)))


class EnsureImportTests(unittest.TestCase):
    def test_stdlib_imports_normally(self):
        self.assertIs(pysite.ensure_import("json"), json)

    def test_missing_package_still_raises(self):
        with self.assertRaises(ImportError):
            pysite.ensure_import("definitely_not_a_real_package_xyz")
