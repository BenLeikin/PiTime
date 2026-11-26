"""Tests for tools/release_notes.py."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "tools" / "release_notes.py")
rn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rn)

SAMPLE = """# Changelog

Intro text.

## [2.1.0] - 2026-11-26

### Added

- History charts.

## [2.0.0] - 2026-11-25

- Merged.
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_one_version_without_its_heading(self):
        text = rn.notes(SAMPLE, "v2.0.0")
        self.assertTrue(text.startswith("- Merged."))
        self.assertNotIn("2.1.0", text)
        self.assertTrue(text.rstrip().endswith("[CHANGELOG.md](CHANGELOG.md)"))

    def test_latest(self):
        self.assertTrue(rn.notes(SAMPLE).startswith("### Added"))

    def test_unknown_version(self):
        self.assertIsNone(rn.notes(SAMPLE, "9.9.9"))

    def test_every_real_version_has_notes(self):
        text = rn.CHANGELOG.read_text()
        versions = [v for v, _, _ in rn.sections(text)]
        self.assertIn("1.0.0", versions)
        for version in versions:
            self.assertTrue(rn.notes(text, version).strip(), version)


if __name__ == "__main__":
    unittest.main()
