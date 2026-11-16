"""F9 - release builds must use Python 3.12 only; the 3.14 fallback must be opt-in and never release-qualifying."""
from __future__ import annotations

import re
import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from tests._helpers import ROOT

SCRIPTS = ["build_windows.bat", "build_and_run.bat"]
LAUNCHERS = SCRIPTS + ["START_ACADEMY.bat", "scripts/make_lock.bat"]


class BuildScriptTests(unittest.TestCase):
    def test_python_314_fallback_is_opt_in(self):
        for name in SCRIPTS:
            text = (ROOT / name).read_text(encoding="utf-8")
            for m in re.finditer(r"py -3\.14", text):
                head = text[: m.start()]
                guard = head.rfind('if not defined PY if "%ALLOW_PROVISIONAL_BUILD%"=="1" (')
                self.assertGreater(guard, -1, f"{name}: py -3.14 reachable without ALLOW_PROVISIONAL_BUILD=1")
                self.assertLess(m.start() - guard, 400, f"{name}: guard too far from the 3.14 probe")

    def test_provisional_is_labelled_not_for_release(self):
        for name in SCRIPTS:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("NOT FOR RELEASE", text, name)

    def test_build_does_not_overwrite_committed_lock(self):
        text = (ROOT / "build_windows.bat").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"freeze\s*>\s*requirements\.lock")
        self.assertTrue((ROOT / "scripts" / "make_lock.bat").is_file())

    def test_launchers_are_ascii_with_crlf(self):
        """cmd.exe mis-parses LF-only batch files (goto/labels) and non-ASCII text without a matching code page."""
        for name in LAUNCHERS:
            raw = (ROOT / name).read_bytes()
            self.assertTrue(all(b < 128 for b in raw), f"{name}: contains non-ASCII bytes")
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), f"{name}: has LF-only line endings")

    def test_pywebview_is_optional_in_requirements(self):
        core = (ROOT / "requirements-core.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("pywebview", [ln.split(">")[0].strip() for ln in core.splitlines() if not ln.startswith("#")])
        build = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        self.assertIn("requirements-core.txt", build)


if __name__ == "__main__":
    unittest.main()
