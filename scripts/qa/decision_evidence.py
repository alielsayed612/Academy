"""Prints numeric evidence for the open owner decisions (read-only; synthetic data in a temp DB). No rule is changed."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from alahd_erp.domain import finance_engine as fe  # noqa: E402
from alahd_erp.infrastructure.database import Store  # noqa: E402
from tests.golden_finance_cases import INSTALLMENTS, REF, STUDENTS, TEACHER_META  # noqa: E402

ev: dict = {}

# ---- D2: receipt numbers ------------------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    st = Store(Path(d) / "t.sqlite3")
    st.upsert_teacher("معلمة أ", {"pct": 70})
    for i in (1, 2):
        st.upsert_student({"id": f"s{i}", "name": f"طالبة {i}", "teacher": "معلمة أ", "pct": 70,
                           "months": {"2026-11": {"value": 500, "discount": 0}}})
    base = 1_800_000_000.0
    nums = []
    for k, t in enumerate((base, base + 1000.0)):          # 1000 s == 1,000,000 ms apart
        with mock.patch("time.time", return_value=t):
            st.upsert_installment({"id": f"i{k}", "studentId": f"s{k+1}", "month": "2026-11", "amount": 100,
                                   "paymentDate": "2026-11-05"})
    rows = st.db.execute("SELECT id, receipt_no FROM installments ORDER BY id").fetchall()
    ev["D2_time_based_numbers_16min_apart"] = [r["receipt_no"] for r in rows]
    st.upsert_installment({"id": "dupA", "receiptNo": "RCP-MANUAL-1", "studentId": "s1", "month": "2026-11", "amount": 50,
                           "paymentDate": "2026-11-06"})
    st.upsert_installment({"id": "dupB", "receiptNo": "RCP-MANUAL-1", "studentId": "s2", "month": "2026-11", "amount": 70,
                           "paymentDate": "2026-11-06"})
    ev["D2_rows_sharing_one_receipt_number_in_db"] = st.db.execute(
        "SELECT COUNT(*) FROM installments WHERE receipt_no='RCP-MANUAL-1'").fetchone()[0]
    ev["D2_unique_index_present"] = any(
        r["unique"] for r in st.db.execute("PRAGMA index_list(installments)").fetchall()
        if any(c["name"] == "receipt_no" for c in st.db.execute(f"PRAGMA index_info({r['name']})").fetchall()))
    st.close()

# ---- D1: closed month is not frozen ------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    st = Store(Path(d) / "t.sqlite3")
    st.upsert_teacher("معلمة أ", {"pct": 70})
    st.upsert_student({"id": "s1", "name": "طالبة 1", "teacher": "معلمة أ", "pct": 70,
                       "months": {"2026-10": {"value": 500, "discount": 0}}})
    st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-10", "amount": 500, "paymentDate": "2026-10-03"})
    before = st.accrual_report("2026-10", reference_date="2026-11-14")
    st.close_month("2026-10")
    st.update_settings({"wageBasis": "gross"})
    st.upsert_student({"id": "s1", "name": "طالبة 1", "teacher": "معلمة أ", "pct": 40})
    after = st.accrual_report("2026-10", reference_date="2026-11-14")
    pick = lambda r: {k: r.get(k) for k in ("totals", "total", "wages", "teachers") if k in r}  # noqa: E731
    ev["D1_closed_month_report_before_close"] = pick(before)
    ev["D1_closed_month_report_after_settings_and_pct_change"] = pick(after)
    ev["D1_identical"] = pick(before) == pick(after)
    st.close()

# ---- D3: PARITY-7 (server ignores pctSource) -----------------------------------------------------------------
def stats(src):
    return fe.compute_month_stats(STUDENTS, "2026-11", INSTALLMENTS, TEACHER_META, pct_source=src, reference_date=REF)["total"]
ev["D3_month_2026-11_total_server_today(student)"] = stats("student")
ev["D3_month_2026-11_total_ui_when_pctSource=teacher"] = stats("teacher")

# ---- D4: PARITY-1 --------------------------------------------------------------------------------------------
me = {"value": 500, "discount": 0, "paymentDate": "2026-11-01"}
py = {"installments_None": fe.effective_month_collected(me, None, REF), "installments_[]": fe.effective_month_collected(me, [], REF)}
cases = [{"fn": "effectiveMonthCollected", "args": [me, "__undef__", REF]},
         {"fn": "effectiveMonthCollected", "args": [me, [], REF]}]
js = json.loads(subprocess.run(["node", str(ROOT / "tests/js/parity_runner.js")], input=json.dumps({"cases": cases}),
                               capture_output=True, text=True, timeout=60).stdout)["results"]
ev["D4_month_value500_paymentDate_set_no_receipts"] = {"python": py, "js_undefined": js[0], "js_empty_list": js[1]}

print(json.dumps(ev, ensure_ascii=False, indent=2, default=str))
