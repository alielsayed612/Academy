"""Python <-> JavaScript finance parity.

The REAL `alahd_erp/domain/finance_engine.py` and the REAL `alahd_erp/ui/js/finance-engine.js` (executed by Node via
`tests/js/parity_runner.js`) are fed the *same* JSON inputs and must return the same numbers.

Nothing here changes a financial rule. Where the two engines genuinely disagree the test is marked
`expectedFailure` and carries the reason: the test then (a) keeps the suite green, (b) documents the divergence and
(c) turns into "unexpected success" the moment somebody makes the engines agree, so the marker must be removed.
Which side is *right* is a business decision and is deliberately NOT made here.

Skipped when `node` is not installed.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
import unittest
from typing import Any, Callable

from tests import _helpers  # noqa: F401  (sets sys.path)
from tests._helpers import ROOT, StoreTestCase
from alahd_erp.domain import finance_engine as fe

NODE = shutil.which("node")
RUNNER = ROOT / "tests" / "js" / "parity_runner.js"
UNDEF = "__undef__"          # JS `undefined` (JSON cannot express it)

REF = "2026-11-14"
MONTHS = ["2026-09", "2026-10", "2026-11"]


# --------------------------------------------------------------------------- plumbing
def run_js(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = subprocess.run([NODE, str(RUNNER)], input=json.dumps({"cases": cases}), capture_output=True,
                         text=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    res = json.loads(out.stdout)["results"]
    assert len(res) == len(cases)
    return res


def run_py(fn: Callable[..., Any], *a: Any, **kw: Any) -> dict[str, Any]:
    try:
        return {"ok": fn(*a, **kw)}
    except Exception as exc:  # noqa: BLE001 - parity only cares *that* it raised
        return {"err": type(exc).__name__}


def same(py: dict[str, Any], js: dict[str, Any]) -> bool:
    if "err" in py or "err" in js:
        return ("err" in py) and ("err" in js)          # both raise: messages differ by language, ignore
    return py["ok"] == js["ok"]


def diffs(items: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> list[str]:
    return [f"{label}: py={p} js={j}" for label, p, j in items if not same(p, j)]


# --------------------------------------------------------------------------- scalar helpers
NUMS: list[Any] = [0, 1, 0.5, 1.5, 2.5, -0.5, -1.5, 2.675, "2.675", "1234.5", "0.4999995", "0.5000005", 1e-7,
                   "1e-7", 99.5, 100, 1000, 333, "", None, True, False, "abc", " 7 ", -3, 7.0049999]


class Scalar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("node not available")

    def _compare(self, js_fn: str, py_fn: Callable[..., Any], arg_sets: list[list[Any]]) -> list[str]:
        js = run_js([{"fn": js_fn, "args": a} for a in arg_sets])
        py_args = [[None if x == UNDEF else x for x in a] for a in arg_sets]     # JS undefined == Python None
        return diffs([(f"{js_fn}{a}", run_py(py_fn, *pa), j) for a, pa, j in zip(arg_sets, py_args, js)])

    def test_round_half_up(self):
        bad = self._compare("roundHalfUp", fe.round_half_up, [[v] for v in NUMS])
        self.assertEqual(bad, [])

    def test_parse_pct_int(self):
        vals = [0, 1, 70, 100, 101, -1, 0.5, 0.7, 0.999, "70", "0.5", "100.4", "100.5", 99.5, 70.5, None, "", True,
                False, "abc"]
        sets = [[v, d] for v in vals for d in (0, 70)]
        self.assertEqual(self._compare("parsePctInt", fe.parse_pct_int, sets), [])

    def test_resolve_dates(self):
        entries: list[Any] = [None, {}, {"paymentDate": "2026-10-05"}, {"pay": "2026-10-06"},
                              {"paymentDate": None, "pay": "2026-10-07"}, {"paymentDate": ""}, {"startDate": "2026-10-01"},
                              {"actualStartDate": "2026-10-02", "startDate": "2026-10-01"},
                              {"actualStartDate": None, "startDate": "2026-10-01"},
                              {"endDate": "2026-11-01", "end": "x"}, {"endDate": "  ", "end": "2026-11-02"},
                              {"endDate": None, "end": None}]
        for js_fn, py_fn in (("resolveActualStartDate", fe.resolve_actual_start_date),
                             ("resolveEndDate", fe.resolve_end_date)):
            self.assertEqual(self._compare(js_fn, py_fn, [[e] for e in entries]), [], js_fn)

    def test_month_entry_amounts_valid_inputs(self):
        entries = [None, {}, {"value": 500}, {"value": 500, "discount": 100}, {"value": 100, "discount": 900},
                   {"value": "450.5", "discount": "0.5"}, {"value": -5}, {"value": 0, "discount": 50},
                   {"value": "", "discount": ""}, {"value": None}, {"value": 2.5, "discount": 1.5}]
        for js_fn, py_fn in (("effectiveMonthSubscription", fe.effective_month_subscription),
                             ("rawMonthDiscount", fe.raw_month_discount),
                             ("effectiveMonthDiscount", fe.effective_month_discount),
                             ("effectiveMonthNetSubscription", fe.effective_month_net_subscription)):
            self.assertEqual(self._compare(js_fn, py_fn, [[e] for e in entries]), [], js_fn)

    # ---- known divergence 1: non-numeric text in a money field
    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_non_numeric_money_text(self):
        """`value: "abc"`: Python raises ValueError, JS silently treats it as 0.

        The Store validates money fields before saving, so this only matters for data that bypassed validation
        (e.g. an old backup). Decision needed: should the UI also refuse, or should Python also return 0?"""
        entries = [{"value": "abc"}, {"value": 100, "discount": "xx"}]
        bad = []
        for js_fn, py_fn in (("effectiveMonthSubscription", fe.effective_month_subscription),
                             ("effectiveMonthNetSubscription", fe.effective_month_net_subscription)):
            bad += self._compare(js_fn, py_fn, [[e] for e in entries])
        bad += self._compare("effectiveMonthCollected", fe.effective_month_collected,
                             [[{"value": 100}, [{"amount": "x", "paymentDate": "2026-10-01"}], REF]])   # receipt amount text
        self.assertEqual(bad, [])

    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_arabic_indic_digits(self):
        """Python's Decimal accepts `٣٤٥` (= 345); the JS engine rejects it. The UI normalises digits before saving
        (`normalizeDigitsToAscii`), so this only matters for values that reach the engine without that step."""
        self.assertEqual(self._compare("roundHalfUp", fe.round_half_up, [["٣٤٥"]]), [])

    # ---- receipts / collection
    def test_collected_and_status(self):
        insts_a = [{"amount": 100, "paymentDate": "2026-10-01"}, {"amount": 150, "date": "2026-10-20"}]
        future = [{"amount": 300, "paymentDate": "2027-01-01"}, {"amount": 50, "paymentDate": "2026-10-10"}]
        entries = [{"value": 500, "discount": 100}, {"value": 500, "discount": 500}, {"value": 0},
                   {"value": 400, "paymentDate": "2026-10-05"}, {"value": 400, "pay": "2026-10-05"},
                   {"value": 400, "paymentDate": "2027-05-05"}, {"value": 400, "installments": future}]
        sets = []
        for e in entries:
            for insts in ([], insts_a, future, [{"amount": -5, "paymentDate": "2026-10-01"}]):
                for ref in (REF, "2026-10-05", "2026-09-01"):
                    sets.append([e, insts, ref])
        for js_fn, py_fn in (("effectiveMonthCollected", fe.effective_month_collected),
                             ("deriveMonthPaymentStatus", fe.derive_month_payment_status)):
            self.assertEqual(self._compare(js_fn, py_fn, sets), [], js_fn)

    # ---- known divergence 2: null vs undefined receipts
    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_null_receipts_argument(self):
        """JS distinguishes `undefined` (-> use month.installments / paymentDate fallback) from `null` (-> ignore).
        Python's `None` means "not provided". app.js passes `undefined`, so the UI is unaffected; any other JS caller
        passing `null` gets a different number than Python for the same month."""
        e = {"value": 100, "installments": [{"amount": 50, "paymentDate": "2026-01-01"}]}
        self.assertEqual(self._compare("effectiveMonthCollected", fe.effective_month_collected,
                                       [[e, None, REF]]), [])

    def test_undefined_receipts_argument_matches_python_none(self):
        e = {"value": 100, "installments": [{"amount": 50, "paymentDate": "2026-01-01"}]}
        e2 = {"value": 100, "paymentDate": "2026-01-01"}
        self.assertEqual(self._compare("effectiveMonthCollected", fe.effective_month_collected,
                                       [[e, UNDEF, REF], [e2, UNDEF, REF], [e2, UNDEF, "2025-01-01"]]), [])

    # ---- known divergence 3: reference date handling
    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_invalid_reference_date(self):
        """JS ignores a reference date that is not a real calendar date (treats it as "no cut-off"); Python compares
        the raw string. With reference date `2026-02-30` a receipt dated `2026-03-01` is excluded by Python (0) and
        counted by JS (100). Verified: py=0 js=100. (A non-date like "garbage" happens to agree.)"""
        e = {"value": 100}
        insts = [{"amount": 100, "paymentDate": "2026-03-01"}]
        self.assertEqual(self._compare("effectiveMonthCollected", fe.effective_month_collected,
                                       [[e, insts, "2026-02-30"], [e, insts, "garbage"]]), [])


# --------------------------------------------------------------------------- aggregates
def make_state(rng: random.Random, n_students: int = 7, with_receipts_for_all: bool = False) -> dict[str, Any]:
    teachers = ["أ. سارة", "أ. محمد", "أ. نور"]
    students, installments = [], []
    for i in range(n_students):
        sid = f"s{i}"
        months: dict[str, Any] = {}
        for mk in MONTHS:
            if rng.random() < 0.15:
                continue
            e: dict[str, Any] = {"value": rng.choice([0, 300, 450, "500", 999.5, None, "", 1200]),
                                 "discount": rng.choice([0, 0, 50, None, 600, "10", ""])}
            if rng.random() < 0.15:
                e["customWage"] = rng.choice([0, 100, 250])
            if rng.random() < 0.3:
                e["paymentDate"] = rng.choice(["2026-10-05", "2026-12-30", "", None])
            if rng.random() < 0.4:
                e["actualStartDate"] = rng.choice(["2026-10-01", "2026-11-10", "2026-09-25"])
            months[mk] = e
            if with_receipts_for_all or rng.random() < 0.6:
                for _ in range(rng.choice([0, 1, 1, 2, 3])):
                    installments.append({"studentId": sid, "month": mk,
                                         "amount": rng.choice([100, 150, 200, "75", 333.5, 0]),
                                         "paymentDate": rng.choice(["2026-09-15", "2026-10-05", "2026-11-14", "2027-01-10"])})
        students.append({"id": sid, "name": f"طالب {i}", "teacher": rng.choice(teachers),
                         "pct": rng.choice([None, "", 60, 70, 0.5, "75", 100, 0]), "months": months})
    return {
        "students": students,
        "installments": installments,
        "teacherMeta": {t: {"pct": rng.choice([None, 50, 65, 80])} for t in teachers[:2]},
        "payouts": [{"month": rng.choice(MONTHS), "amount": rng.choice([100, 250, "40.5"])} for _ in range(4)],
        "transactions": [{"type": rng.choice(["revenue", "expense"]), "month": rng.choice(MONTHS + [None]),
                          "date": rng.choice(["2026-09-03", "2026-10-09", "2026-11-01"]),
                          "amount": rng.choice([50, 120, "60.5"])} for _ in range(6)],
        "settings": {"defaultTeacherPct": rng.choice([70, 55, 0, 100])},
    }


COMBOS = [(ps, wb, wm) for ps in ("student", "teacher") for wb in ("net", "gross") for wm in ("accrued", "collected")]
AGG_MAP = {"n": "count", "grossSubs": "grossValue", "discounts": "discounts", "subs": "value",
           "collected": "collected", "uncollected": "uncollected", "wages": "wages"}


class Aggregates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("node not available")

    @staticmethod
    def _py_month_total(state, mk, ps, wb, wm, ref, default_pct):
        py = run_py(lambda: fe.compute_month_stats(state["students"], mk, installments=state["installments"],
                                                   teacher_meta=state["teacherMeta"], pct_source=ps, wage_basis=wb,
                                                   wage_accrual_mode=wm, reference_date=ref, default_pct=default_pct))
        if "ok" in py:
            py = {"ok": {js_k: py["ok"]["total"][py_k] for py_k, js_k in AGG_MAP.items()}}
        return py

    @staticmethod
    def _js_month_total_case(state, mk, ps, wb, wm, ref, default_pct):
        opts = {"pctSource": ps, "wageBasis": wb, "wageAccrualMode": wm, "referenceDate": ref, "defaultPct": default_pct}
        return {"fn": "computeMonthAggregate",
                "args": [state["students"], mk, state["installments"], state["teacherMeta"], opts]}

    @staticmethod
    def _pick(js, keys):
        return {"ok": {k: js["ok"][k] for k in keys}} if "ok" in js else js

    def _month_total(self, state, mk, ps, wb, wm, ref, default_pct):
        py = self._py_month_total(state, mk, ps, wb, wm, ref, default_pct)
        js = self._pick(run_js([self._js_month_total_case(state, mk, ps, wb, wm, ref, default_pct)])[0],
                        list(AGG_MAP.values()))
        return py, js

    def test_month_totals_match_when_every_student_month_has_receipts_or_no_receipts_at_all(self):
        """Production-shaped inputs: a receipts array exists and every student-month that has money also has receipts
        (legacy payments are migrated to receipts on load). 40 random states x 8 policy combos x 3 months."""
        rng = random.Random(20261114)
        labels, pys, cases = [], [], []
        for n in range(40):
            state = make_state(rng, with_receipts_for_all=True)
            for s in state["students"]:
                for m in s["months"].values():
                    m.pop("paymentDate", None)      # with receipts present the month-level date is not the source
            dp = state["settings"]["defaultTeacherPct"]
            for ps, wb, wm in COMBOS:
                for mk in MONTHS:
                    labels.append(f"state#{n} {ps}/{wb}/{wm}/{mk}")
                    pys.append(self._py_month_total(state, mk, ps, wb, wm, REF, dp))
                    cases.append(self._js_month_total_case(state, mk, ps, wb, wm, REF, dp))
        js = [self._pick(r, list(AGG_MAP.values())) for r in run_js(cases)]
        bad = diffs(list(zip(labels, pys, js)))
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(cases)} mismatches")

    # ---- known divergence 4 (the important one)
    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_student_without_receipt_but_with_month_paymentDate(self):
        """A receipts ARRAY exists, but this student has no receipt for the month, and the month record carries a
        `paymentDate`.
          Python: index lookup returns None -> falls back to the legacy rule -> counts the whole net fee as collected.
          JS    : `instMap[key] || []` -> empty array -> receipts are authoritative -> collected = 0.
        Same data, two different "collected" figures (and different wages in `collected` accrual mode)."""
        state = {"students": [{"id": "s1", "name": "أ", "teacher": "t", "pct": 70,
                               "months": {"2026-10": {"value": 500, "paymentDate": "2026-10-05"}}}],
                 "installments": [{"studentId": "other", "month": "2026-10", "amount": 10, "paymentDate": "2026-10-05"}],
                 "teacherMeta": {}}
        py, js = self._month_total(state, "2026-10", "student", "net", "collected", REF, 70)
        self.assertTrue(same(py, js), f"py={py} js={js}")

    # ---- known divergence 5: production Python ignores settings.pctSource / defaultTeacherPct
    def test_production_python_report_path_vs_ui_options(self):
        """`Store.stats/accounting_summary` call the engine WITHOUT pct_source / default_pct (defaults 'student', 70),
        while `alerts_engine` and `statistics_engine` DO read them from settings, and the UI's `getFinanceOptions()`
        passes neither. This test only records the facts as data so the divergence is visible, it asserts nothing
        about which side is right."""
        import inspect
        from alahd_erp.infrastructure import database as db
        src_stats = inspect.getsource(db.Store.stats) + inspect.getsource(db.Store.accounting_summary)
        self.assertNotIn("pct_source", src_stats)
        self.assertNotIn("default_pct", src_stats)

    def test_accounting_summary_matches_with_valid_reference_date(self):
        rng = random.Random(77)
        keys = ["grossSubscriptions", "discounts", "subscriptions", "collected", "uncollected", "cashReceiptsInPeriod",
                "wages", "salaryPaid", "salaryRemaining", "extraRevenue", "operatingExpense", "totalCashIn",
                "totalCashOut", "netCashBalance", "netOperatingProfit"]
        labels, pys, cases = [], [], []
        for n in range(25):
            state = make_state(rng, with_receipts_for_all=True)
            for s in state["students"]:
                for m in s["months"].values():
                    m.pop("paymentDate", None)
            dp = state["settings"]["defaultTeacherPct"]
            for ps, wb, wm in COMBOS:
                for mk in [None, *MONTHS]:
                    py = run_py(lambda: fe.compute_accounting_summary(
                        state["students"], payouts=state["payouts"], transactions=state["transactions"],
                        installments=state["installments"], month_key=mk, teacher_meta=state["teacherMeta"],
                        pct_source=ps, wage_basis=wb, wage_accrual_mode=wm, reference_date=REF, default_pct=dp))
                    pys.append({"ok": {k: py["ok"][k] for k in keys}} if "ok" in py else py)
                    labels.append(f"state#{n} {ps}/{wb}/{wm}/{mk}")
                    opts = {"pctSource": ps, "wageBasis": wb, "wageAccrualMode": wm, "referenceDate": REF}
                    cases.append({"fn": "computeAccountingSummary", "args": [state, mk or "__all__", opts]})
        js = [self._pick(r, keys) for r in run_js(cases)]
        bad = diffs(list(zip(labels, pys, js)))
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(cases)} mismatches")

    # ---- known divergence 6: student with an empty name
    @unittest.expectedFailure
    def test_KNOWN_DIVERGENCE_student_with_blank_name(self):
        """JS skips students whose name is blank; Python counts them. Store validation requires a name, so only
        imported/legacy rows can trigger it."""
        state = {"students": [{"id": "s1", "name": "  ", "teacher": "t", "pct": 70,
                               "months": {"2026-10": {"value": 500}}}], "installments": [], "teacherMeta": {}}
        py, js = self._month_total(state, "2026-10", "student", "net", "accrued", REF, 70)
        self.assertTrue(same(py, js), f"py={py} js={js}")


# --------------------------------------------------------------------------- PARITY-7 (server reports vs settings)
# PARITY-7 (measured in session 7): Store report methods ignore `settings.pctSource` / `settings.defaultTeacherPct`.
# 
# `Store.stats` / `accrual_report` / `accounting_summary` never pass `pct_source` / `default_pct` to the engine, while the
# UI (finance-engine.js) and `alerts_engine` read them from settings. Measured: pctSource='teacher', defaultTeacherPct=60,
# T1 meta pct 80, T2 no meta → server wages 800, UI/engine wages 960; netOperatingProfit 900 vs 740.
# Left unfixed on purpose (financial; needs the user's approval) -> expectedFailure, so the suite goes red-for-the-right-
# reason the moment someone fixes it (remove the decorator then).
MK = "2026-11"  # PARITY-7 month


def _p7_student(i, teacher, pct, value, pay):
    s = {"id": f"s{i}", "code": f"C{i}", "name": f"n{i}", "phone": "", "teacher": teacher, "type": "x",
         "months": {MK: {"value": value, "paymentDate": pay, "pay": pay}}, "adminStatus": "active", "transferHistory": []}
    if pct is not None:
        s["pct"] = pct
    return s


class Parity7(StoreTestCase):
    def setUp(self):
        super().setUp()
        st = self.store.export_state()
        st["students"] = [_p7_student(1, "T1", 70, 1000, "2026-11-02"), _p7_student(2, "T2", None, 500, "2026-11-03"),
                          _p7_student(3, "T1", 50, 200, "2026-11-04")]
        st["teachers"] = ["T1", "T2"]
        st["teacherMeta"] = {"T1": {"pct": 80, "defaultPct": 80, "active": True}, "T2": {"active": True}}
        st["settings"] = dict(st.get("settings") or {}, pctSource="teacher", defaultTeacherPct=60)
        self.store.replace_all(st, actor="t")
        self.state = self.store.export_state()

    def _engine_month(self):
        s, sett = self.state, self.state["settings"]
        return fe.compute_month_stats(
            s["students"], MK, installments=s["installments"], teacher_meta=s["teacherMeta"],
            pct_source=sett["pctSource"], wage_basis=str(sett.get("wageBasis") or "net"),
            wage_accrual_mode=str(sett.get("wageAccrualMode") or "accrued"), reference_date=REF,
            default_pct=int(sett["defaultTeacherPct"]))

    def test_measured_values(self):
        """Pins what is true today (so the defect can't silently change)."""
        self.assertEqual(self.store.stats(MK, reference_date=REF)["total"]["wages"], 800)
        self.assertEqual(self._engine_month()["total"]["wages"], 960)

    @unittest.expectedFailure
    def test_DEFECT_store_stats_should_follow_settings(self):
        self.assertEqual(self.store.stats(MK, reference_date=REF)["total"], self._engine_month()["total"])


if __name__ == "__main__":
    unittest.main()
