"""Characterization (golden) tests: current finance outputs on synthetic data are pinned.

A diff here means a financial number changed. That is only acceptable after an explicit owner decision, in which case
regenerate with `python scripts/qa/gen_finance_golden.py` and explain the change in DECISIONS_LOG.md.
"""
from __future__ import annotations

import json
import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from tests._helpers import ROOT
from tests.golden_finance_cases import compute_all


class FinanceGolden(unittest.TestCase):
    def test_outputs_match_golden(self):
        golden = json.loads((ROOT / "tests" / "golden" / "finance_golden.json").read_text(encoding="utf-8"))
        current = json.loads(json.dumps(compute_all(), ensure_ascii=False, sort_keys=True, default=str))
        self.assertEqual(sorted(golden), sorted(current))
        for k in sorted(golden):
            self.assertEqual(golden[k], current[k], f"golden drift in {k}")


if __name__ == "__main__":
    unittest.main()
