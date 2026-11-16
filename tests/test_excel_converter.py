"""Dashboard → import converter. Workbooks are built in memory; no network.

The synthetic dashboard mirrors the real file's layout: 2 title rows, merged month headers over 4 sub-columns
(قيمة/تاريخ الدفع/تاريخ النهاية/أجر المعلمة), a wage *formula* with no cached value (openpyxl never caches),
and a totals row of formulas. An optional end-to-end test runs on the real file when ALAHD_DASHBOARD_XLSX is set.
"""
from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from tests._helpers import StoreTestCase
from alahd_erp.services import import_service as I
from alahd_erp.tools import excel_converter as C

MONTHS = ["أكتوبر 2026", "نوفمبر 2026", "ديسمبر 2026"]
SUB = ["قيمة الاشتراك", "تاريخ الدفع", "تاريخ النهاية", "أجر المعلمة"]


def dashboard(students, teachers=None, summary=None, extra=None) -> bytes:
    """students: list of (name, phone, teacher, type, pct, {month_idx: (value, pay, end)})."""
    wb = Workbook()
    ws = wb.active
    ws.title = "الطلاب"
    ws["A1"] = "لوحة تحكم"
    ws["A2"] = "سطر وصف"
    heads = ["م", "اسم الطالب", "رقم التواصل", "اسم المعلمة", "نوع الاشتراك", "نسبة المعلمة"]
    for i, h in enumerate(heads, 1):
        ws.cell(row=3, column=i, value=h)
        ws.merge_cells(start_row=3, start_column=i, end_row=4, end_column=i)
    for mi, m in enumerate(MONTHS):
        c0 = 7 + mi * 4
        ws.cell(row=3, column=c0, value=m)
        ws.merge_cells(start_row=3, start_column=c0, end_row=3, end_column=c0 + 3)
        for j, s in enumerate(SUB):
            ws.cell(row=4, column=c0 + j, value=s)
    r = 5
    for n, (name, phone, teacher, ptype, pct, months) in enumerate(students, 1):
        ws.cell(row=r, column=1, value=n)
        ws.cell(row=r, column=2, value=name)
        ws.cell(row=r, column=3, value=phone)
        ws.cell(row=r, column=4, value=teacher)
        ws.cell(row=r, column=5, value=ptype)
        ws.cell(row=r, column=6, value=pct)
        for mi, (val, pay, end) in months.items():
            c0 = 7 + mi * 4
            ws.cell(row=r, column=c0, value=val)
            ws.cell(row=r, column=c0 + 1, value=pay)
            ws.cell(row=r, column=c0 + 2, value=end)
            ws.cell(row=r, column=c0 + 3, value=f"={ws.cell(row=r, column=c0).coordinate}*F{r}")  # wage formula
        r += 1
    ws.cell(row=r, column=2, value=f"الإجمالي ({len(students)} طالب)")
    for mi in range(len(MONTHS)):
        c = 7 + mi * 4
        ws.cell(row=r, column=c, value=f"=SUM({ws.cell(row=5, column=c).coordinate}:{ws.cell(row=r - 1, column=c).coordinate})")
    if teachers is not None:
        wt = wb.create_sheet("المعلمات")
        wt.append(["بيانات"])
        wt.append(["وصف"])
        wt.append(["م", "اسم المعلمة", "نسبة المعلمة", "ملاحظات", "عدد الطالبات المسجلات"])
        for i, (tn, tp) in enumerate(teachers, 1):
            wt.append([i, tn, tp, None, 0])
    if summary is not None:
        wsum = wb.create_sheet("ملخص")
        wsum.append(["عنوان"])
        wsum.append(["وصف"])
        wsum.append(["المؤشر", "القيمة", "ملاحظات", "تفصيل أكتوبر 2026", "تفصيل نوفمبر 2026"])
        wsum.append(["إجمالي الاشتراكات المسجلة", sum(summary), "", summary[0], summary[1]])
    if extra:
        extra(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


STUDENTS = [
    # name, phone, teacher, type, pct(fraction), {month_idx: (value, pay, end)}
    ("طالبة أولى", "01000000001", "معلمة أ", "باقة 8", 0.7, {0: (300, "2026-10-02", "2026-10-30"), 1: (350, "2026-11-02", "2026-11-30")}),
    ("طالبة ثانية", "01000000002", "معلمة ب", "باقة 4", 0.75, {1: (200, None, None)}),
    ("طالبة ثالثة", "01000000003", "معلمة أ", "باقة 8", 0.7, {0: (None, "2026-10-05", "2026-11-05"), 1: (250, "2026-11-05", "2026-12-05")}),
]


def rows_of(data: bytes):
    ws = load_workbook(io.BytesIO(data)).active
    return [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]


class LayoutAndShape(unittest.TestCase):
    def setUp(self):
        self.res = C.convert_dashboard(dashboard(STUDENTS, teachers=[("معلمة أ", 0.7), ("معلمة ب", 0.75)], summary=[300, 800]))

    def test_months_and_columns_detected_from_sheet(self):
        self.assertEqual(self.res.stats["months_detected"], ["2026-10", "2026-11", "2026-12"])
        self.assertEqual(self.res.stats["layout"]["base_columns"], {"name": 2, "phone": 3, "teacher": 4, "type": 5, "pct": 6})

    def test_wide_to_long(self):
        rows = rows_of(self.res.students_xlsx)
        self.assertEqual(len(rows), 4)  # s1×2 months, s2×1, s3×1 (Oct has dates but no fee → dropped)
        got = {(r[1], r[6]): r[7] for r in rows}
        self.assertEqual(got, {("طالبة أولى", "2026-10"): 300, ("طالبة أولى", "2026-11"): 350,
                               ("طالبة ثانية", "2026-11"): 200, ("طالبة ثالثة", "2026-11"): 250})
        first = rows[0]
        self.assertEqual(first[0], "STU-001")
        self.assertEqual(first[5], 70)            # 0.7 → 70
        self.assertEqual(first[10], "2026-10-02")  # payment date kept
        self.assertEqual(first[12], "2026-10-30")  # end date kept
        self.assertIsNone(first[11])               # actual start never invented

    def test_totals_row_and_wage_columns_are_not_imported(self):
        names = [r[1] for r in rows_of(self.res.students_xlsx)]
        self.assertFalse(any("الإجمالي" in n for n in names))
        self.assertEqual(self.res.count("error"), 0)  # totals-row formulas must not be reported as errors
        info = [i for i in self.res.issues if i.code == "FORMULAS_IGNORED"]
        self.assertEqual(len(info), 1)

    def test_output_has_no_formula_cells(self):
        for data in (self.res.students_xlsx, self.res.teachers_xlsx, self.res.issues_xlsx):
            wb = load_workbook(io.BytesIO(data))
            self.assertFalse([c.coordinate for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"])

    def test_dates_without_fee_dropped_with_warning(self):
        w = [i for i in self.res.issues if i.code == "DATE_WITHOUT_FEE"]
        self.assertEqual([(i.student, i.month) for i in w], [("طالبة ثالثة", "2026-10")])

    def test_teachers_file(self):
        self.assertEqual(rows_of(self.res.teachers_xlsx),
                         [["معلمة أ", None, None, 70, None], ["معلمة ب", None, None, 75, None]])

    def test_reconcile_with_summary_sheet(self):
        rec = self.res.stats["reconcile"]
        self.assertTrue(rec["checked"])
        self.assertEqual(rec["months"]["2026-10"], {"summary_sheet": 300, "converted": 300, "match": True})
        self.assertEqual(rec["months"]["2026-11"], {"summary_sheet": 800, "converted": 800, "match": True})

    def test_reconcile_flags_mismatch(self):
        res = C.convert_dashboard(dashboard(STUDENTS, summary=[999, 800]))
        self.assertEqual([i.code for i in res.issues if i.code == "TOTAL_MISMATCH"], ["TOTAL_MISMATCH"])


class DataProblemsAreReported(unittest.TestCase):
    def test_flags(self):
        st = [
            ("س1", "01000000001", "م1", "ب", 0.7, {0: (100, "2026-10-09", "2026-10-01")}),   # end < pay
            ("س2", "01000000001", None, None, 0.7, {0: (100, "2026-10-01", None)}),            # dup phone, no teacher/type
            ("س3", "+967782442922", "م1", "ب", 0.7, {}),                                        # foreign phone, no months
            ("س4", None, "م1", "ب", 1.5, {0: (100, None, None)}),                              # bad pct, no phone
        ]
        res = C.convert_dashboard(dashboard(st))
        codes = {i.code for i in res.issues}
        self.assertTrue({"END_BEFORE_PAYMENT", "DUP_PHONE", "NO_TEACHER", "NO_TYPE", "NON_EGYPT_PHONE", "NO_MONTH_DATA",
                         "NO_PHONE", "BAD_PCT"} <= codes, codes)
        self.assertEqual(res.count("error"), 1)  # BAD_PCT only
        rows = rows_of(res.students_xlsx)
        self.assertEqual(len(rows), 4)  # nobody is dropped; س3 is kept as a student-only row
        self.assertEqual(rows[2][2], "+967782442922")  # '+' phone not mangled

    def test_formula_in_a_used_column_without_cached_value_is_an_error(self):
        def extra(wb):
            wb["الطلاب"]["G5"] = "=100+200"  # fee as a formula; openpyxl stores no cached value
        res = C.convert_dashboard(dashboard(STUDENTS, extra=extra))
        errs = [i for i in res.issues if i.code == "FORMULA_NO_VALUE"]
        self.assertEqual([i.column for i in errs], ["G5"])

    def test_error_value_and_bad_date(self):
        def extra(wb):
            wb["الطلاب"]["K5"] = "#REF!"
            wb["الطلاب"]["H5"] = "مش تاريخ"
        res = C.convert_dashboard(dashboard(STUDENTS, extra=extra))
        by = {i.code for i in res.issues if i.severity == "error"}
        self.assertEqual(by, {"BAD_VALUE", "BAD_DATE"})
        # the broken month is skipped, not guessed
        self.assertNotIn(("طالبة أولى", "2026-11"), {(r[1], r[6]) for r in rows_of(res.students_xlsx)})

    def test_suspicious_default_date_cluster(self):
        st = [(f"س{i}", f"0100000000{i}", "م", "ب", 0.7, {0: (100, "2026-11-11", None)}) for i in range(6)]
        res = C.convert_dashboard(dashboard(st))
        self.assertIn("SUSPECT_DEFAULT_DATE", {i.code for i in res.issues})
        self.assertIn("AUTO_RECEIPTS", {i.code for i in res.issues})

    def test_injection_text_never_becomes_formula(self):
        st = [('=HYPERLINK("http://evil.example","x")', "01000000001", "م", "ب", 0.7, {0: (100, None, None)})]
        res = C.convert_dashboard(dashboard(st))
        wb = load_workbook(io.BytesIO(res.students_xlsx))
        self.assertFalse([c for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"])


class BadInputs(unittest.TestCase):
    def test_rejects(self):
        with self.assertRaises(C.ConversionError):
            C.convert_dashboard(b"")
        with self.assertRaises(C.ConversionError):
            C.convert_dashboard(b"not an xlsx")
        with self.assertRaises(C.ConversionError):
            C.convert_dashboard(dashboard(STUDENTS), student_sheet="مش موجود")
        wb = Workbook()
        wb.active.title = "الطلاب"
        wb.active.append(["a", "b"])
        buf = io.BytesIO()
        wb.save(buf)
        with self.assertRaises(C.ConversionError):
            C.convert_dashboard(buf.getvalue())
        with self.assertRaises(ValueError):
            C.convert_dashboard(dashboard(STUDENTS), paid_mode="bogus")


class CliDoesNotTouchSource(unittest.TestCase):
    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src.xlsx"
            src.write_bytes(dashboard(STUDENTS, teachers=[("معلمة أ", 0.7)]))
            before = hashlib.sha256(src.read_bytes()).hexdigest()
            out = Path(d) / "out"
            self.assertEqual(C.main([str(src), "-o", str(out)]), 0)
            self.assertEqual(sorted(p.name for p in out.iterdir()),
                             ["conversion_issues.xlsx", "import_students.xlsx", "import_teachers.xlsx"])
            self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(), before)


class RealImporterAcceptsOutput(StoreTestCase):
    """The decisive check: the app's own importer, not our reading of it."""

    def setUp(self):
        super().setUp()
        self.bk = self.home / "backups"
        self.bk.mkdir()

    def _run(self, paid_mode):
        res = C.convert_dashboard(dashboard(STUDENTS, teachers=[("معلمة أ", 0.7), ("معلمة ب", 0.75)]), paid_mode=paid_mode)
        pt = I.preview_import_file(res.teachers_xlsx, "t.xlsx", "teachers", self.store.export_state())
        self.assertEqual(pt["summary"]["invalidCount"], 0)
        self.assertEqual(pt["errors"], [])
        ps = I.preview_import_file(res.students_xlsx, "s.xlsx", "students", self.store.export_state(), auto_create_refs=True)
        self.assertEqual(ps["errors"], [])  # in particular: zero formula errors
        self.assertEqual(ps["summary"]["invalidCount"], 0)
        self.assertEqual(ps["summary"]["newCount"], 4)
        I.commit_import(self.store, res.teachers_xlsx, "t.xlsx", "teachers", backup_folder=self.bk)
        I.commit_import(self.store, res.students_xlsx, "s.xlsx", "students", backup_folder=self.bk, auto_create_refs=True)
        return self.store.export_state()

    def _monthly(self, st):
        tot = {}
        for s in st["students"]:
            for mk, m in s["months"].items():
                tot[mk] = tot.get(mk, 0) + (m.get("value") or 0)
        return tot

    def test_none_mode(self):
        st = self._run("none")
        self.assertEqual(len(st["students"]), 3)
        self.assertEqual(self._monthly(st), {"2026-10": 300, "2026-11": 800})  # == the source's month totals
        self.assertEqual(st["teacherMeta"]["معلمة ب"]["pct"], 75)
        # no explicit paid amounts, yet the Store's legacy migration receipts the 3 dated months
        self.assertEqual(sorted((i["month"], i["amount"]) for i in st["installments"]),
                         [("2026-10", 300), ("2026-11", 250), ("2026-11", 350)])

    def test_full_mode_same_money_as_none(self):
        a = self._run("full")
        self.assertEqual(self._monthly(a), {"2026-10": 300, "2026-11": 800})
        self.assertEqual(sorted((i["month"], i["amount"]) for i in a["installments"]),
                         [("2026-10", 300), ("2026-11", 250), ("2026-11", 350)])  # not doubled
        self.assertTrue(all(i["receiptNo"].startswith("RCP-IMP-") for i in a["installments"]))


_DEFAULT_DASH_XLSX = Path(__file__).resolve().parent.parent / "data" / "Academy_Management_Dashboard_2026-11-11.xlsx"


@unittest.skipUnless(os.environ.get("ALAHD_DASHBOARD_XLSX") or _DEFAULT_DASH_XLSX.is_file(), "set ALAHD_DASHBOARD_XLSX to the real dashboard file")
class RealDashboardFile(StoreTestCase):
    def test_real_file_end_to_end(self):
        res = C.convert_dashboard(os.environ.get("ALAHD_DASHBOARD_XLSX") or _DEFAULT_DASH_XLSX)
        self.assertEqual(res.count("error"), 0)
        self.assertEqual(res.stats["students"], 63)
        self.assertTrue(all(v["match"] for v in res.stats["reconcile"]["months"].values()))
        bk = self.home / "b"
        bk.mkdir()
        I.commit_import(self.store, res.teachers_xlsx, "t.xlsx", "teachers", backup_folder=bk)
        I.commit_import(self.store, res.students_xlsx, "s.xlsx", "students", backup_folder=bk, auto_create_refs=True)
        st = self.store.export_state()
        tot = {}
        for s in st["students"]:
            for mk, m in s["months"].items():
                tot[mk] = tot.get(mk, 0) + (m.get("value") or 0)
        self.assertEqual(len(st["students"]), 63)
        self.assertEqual(tot, {"2026-10": 9445, "2026-11": 6125})


if __name__ == "__main__":
    unittest.main()
