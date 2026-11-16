"""Regenerates tests/golden/finance_golden.json. Run ONLY after an owner-approved financial rule change."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.golden_finance_cases import compute_all  # noqa: E402

(ROOT / "tests" / "golden" / "finance_golden.json").write_text(
    json.dumps(compute_all(), ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
print("written")
