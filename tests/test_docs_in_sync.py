"""F10 - documentation must not drift from the code: the documented route count equals the real one."""
from __future__ import annotations

import re
import subprocess
import sys
import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from tests._helpers import ROOT


class DocsInSyncTests(unittest.TestCase):
    def test_documented_route_count_matches_inventory(self):
        out = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "route_inventory.py")],
                             capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        m = re.search(r"Total API route registrations:\s*(\d+)", out.stdout + out.stderr)
        self.assertIsNotNone(m, out.stderr[-500:])
        real = int(m.group(1))
        for rel in ("docs/ARCHITECTURE.md",):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertRegex(text, rf"\b{real}\b[^\n]*route", f"{rel} does not state the real route count ({real})")


if __name__ == "__main__":
    unittest.main()
