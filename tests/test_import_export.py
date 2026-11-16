"""Import / export end-to-end tests.

Every expectation here was first observed against the real code (exploration scripts), then pinned.
Workbooks are built in memory with openpyxl; no files, no network.
"""
from __future__ import annotations

import io
import os
import unittest

from openpyxl import Workbook, load_workbook

from tests._helpers import StoreTestCase
from alahd_erp.domain.finance_engine import compute_accounting_summary
from alahd_erp.infrastructure.database import MonthLockedError
from alahd_erp.services import excel_service as X
from alahd_erp.services import import_service as I

EVIL = '=HYPERLINK("http://evil.example","x")'
INDIC = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def xlsx(rows, sheet="ورقة1") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def cells(data: bytes):
    wb = load_workbook(io.BytesIO(data))
    return [(ws.title, c) for ws in wb.worksheets for row in ws.iter_rows() for c in row]


STU_HEAD = ["كود الطالبة", "اسم الطالبة", "رقم الهاتف", "المعلمة", "نوع الاشتراك", "الشهر", "قيمة الاشتراك",
            "الخصم", "المبلغ المسدد", "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية"]


def stu_row(code="S1", name="طالبة أولى", month="2026-11", value="1000", discount="0", paid="500",
            pay="2026-11-05", start="2026-11-02", end="2026-11-28", phone="01000000001", teacher="معلمة أ"):
    return [code, name, phone, teacher, "8 حلق", month, value, discount, paid, pay, start, end]


class ImportTestCase(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.bk = self.home / "backups"
        self.bk.mkdir()

    def commit(self, data, dataset, name="f.xlsx", **kw):
        return I.commit_import(self.store, data, name, dataset, backup_folder=self.bk, **kw)

    def state(self):
        return self.store.export_state()


class Templates(ImportTestCase):
    def test_every_template_parses_and_previews(self):
        for ds in I.SUPPORTED_DATASETS:
            p = I.preview_import_file(I.generate_import_template_xlsx(ds), "t.xlsx", ds, self.state())
            self.assertEqual(p["dataset"], ds)
            self.assertGreater(p["summary"]["totalRows"], 0, ds)
            self.assertIn("rows", p)
            self.assertIn("errors", p)

    def test_non_receipt_templates_import_cleanly(self):
        for ds in ("teachers", "packages", "students", "transactions", "salaries"):
            p = I.preview_import_file(I.generate_import_template_xlsx(ds), "t.xlsx", ds, self.state())
            self.assertEqual(p["errors"], [], ds)
            self.assertEqual(p["summary"]["invalidCount"], 0, ds)
            self.assertEqual(p["summary"]["newCount"], p["summary"]["totalRows"], ds)

    def test_receipts_template_needs_existing_students(self):
        p = I.preview_import_file(I.generate_import_template_xlsx("receipts"), "t.xlsx", "receipts", self.state())
        self.assertEqual(p["summary"]["invalidCount"], p["summary"]["totalRows"])
        self.assertTrue(all("غير موجودة" in e["reason"] for e in p["errors"]))

    def test_instructions_sheet_is_not_the_data_sheet(self):
        p = I.preview_import_file(I.generate_import_template_xlsx("students"), "t.xlsx", "students", self.state())
        self.assertNotIn("تعليمات الاستيراد", p["detectedSheet"])


class StudentImport(ImportTestCase):
    def test_arabic_headers_indic_digits_and_title_rows(self):
        phone = "01000000001".translate(INDIC)
        data = xlsx([["تقرير الطالبات"], [""], STU_HEAD,
                     stu_row(phone=phone, value="١٬٠٠٠", discount="١٠٠", paid="٩٠٠")])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state())
        self.assertEqual(p["errors"], [])
        self.assertEqual(p["rows"][0]["row"], 4)  # real spreadsheet row number, after the 2 title rows
        r = self.commit(data, "students")
        self.assertEqual((r["ok"], r["committed"]), (True, 1))
        st = self.state()
        stu = st["students"][0]
        self.assertEqual((stu["code"], stu["name"], stu["phone"], stu["teacher"]),
                         ("S1", "طالبة أولى", "01000000001", "معلمة أ"))
        self.assertEqual(st["teachers"], ["معلمة أ"])
        self.assertEqual(st["subTypes"], {"8 حلق": 1000})
        m = stu["months"]["2026-11"]
        self.assertEqual((m["value"], m["discount"]), (1000, 100))

    def test_receipt_comes_from_paid_amount_and_dates_stay_independent(self):
        self.commit(xlsx([STU_HEAD, stu_row(paid="900", pay="2026-11-05", start="2026-11-02")]), "students")
        st = self.state()
        self.assertEqual([(i["month"], i["amount"], i["paymentDate"]) for i in st["installments"]],
                         [("2026-11", 900, "2026-11-05")])
        m = st["students"][0]["months"]["2026-11"]
        self.assertEqual((m["paymentDate"], m["actualStartDate"], m["endDate"]),
                         ("2026-11-05", "2026-11-02", "2026-11-28"))

    def test_start_date_is_never_copied_into_payment_date(self):
        data = xlsx([STU_HEAD, stu_row(paid="", pay="", start="2026-11-02")])
        self.commit(data, "students")
        m = self.state()["students"][0]["months"]["2026-11"]
        self.assertEqual(m["actualStartDate"], "2026-11-02")
        self.assertFalse(m.get("paymentDate"))
        self.assertEqual(self.state()["installments"], [])

    def test_invalid_values_are_row_errors_not_commits(self):
        data = xlsx([STU_HEAD,
                     stu_row(code="A", name="سليمة"),
                     stu_row(code="B", name="خصم كبير", value="100", discount="500", paid=""),
                     stu_row(code="C", name="تاريخ خاطئ", pay="31/02/2026"),
                     stu_row(code="D", name="", phone="01000000004")])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state())
        self.assertEqual(p["summary"]["newCount"], 1)
        self.assertEqual(p["summary"]["invalidCount"], 3)
        self.assertEqual([r["status"] for r in p["rows"]], ["new", "invalid", "invalid", "invalid"])
        self.commit(data, "students")
        self.assertEqual([s["name"] for s in self.state()["students"]], ["سليمة"])

    def test_duplicate_rows_inside_one_file(self):
        data = xlsx([STU_HEAD, stu_row(), stu_row()])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state())
        self.assertEqual((p["summary"]["newCount"], p["summary"]["duplicateCount"]), (1, 1))

    def test_formula_cell_makes_the_row_invalid_and_is_never_committed(self):
        bad = stu_row(code="F", name="=1+1")
        data = xlsx([STU_HEAD, bad, stu_row(code="G", name="ثانية", phone="01000000002")])
        p = I.preview_import_file(data, "f.xlsx", "students", self.state())
        self.assertEqual([(r["row"], r["status"]) for r in p["rows"]], [(2, "invalid"), (3, "new")])
        self.assertTrue(any(e["row"] == 2 and "معادل" in e["reason"] for e in p["errors"]))
        r = self.commit(data, "students")
        self.assertEqual(r["committed"], 1)
        self.assertEqual([s["name"] for s in self.state()["students"]], ["ثانية"])

    def test_missing_required_column_reports_and_commits_nothing(self):
        data = xlsx([["كود الطالبة", "الشهر"], ["S1", "2026-11"]])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state())
        self.assertEqual(p["summary"]["invalidCount"], 1)
        self.assertTrue(any("غير موجود" in e["reason"] for e in p["errors"]))
        self.assertEqual(self.commit(data, "students")["committed"], 0)


class BadFiles(unittest.TestCase):
    def assertRejected(self, data, filename, fragment):
        with self.assertRaises(ValueError) as cm:
            I.preview_import_file(data, filename, "students", {})
        self.assertIn(fragment, str(cm.exception))

    def test_empty(self):
        self.assertRejected(b"", "a.xlsx", "فارغ")

    def test_corrupt_xlsx(self):
        self.assertRejected(b"this is not a zip", "a.xlsx", "غير صالح")

    def test_legacy_xls_and_unknown_extension(self):
        self.assertRejected(b"x", "a.xls", "غير مدعومة")
        self.assertRejected(b"a,b", "a", "غير مدعومة")

    def test_oversize(self):
        self.assertRejected(b"x" * (I.MAX_IMPORT_BYTES + 1), "a.csv", "الحد الأقصى")

    def test_csv_encodings_and_header_only(self):
        for enc in ("utf-8-sig", "utf-8", "cp1256"):
            p = I.preview_import_file("الاسم,النسبة\nمعلمة,70\n".encode(enc), "t.csv", "teachers", {})
            self.assertEqual(p["summary"]["newCount"], 1, enc)
        p = I.preview_import_file("الاسم,النسبة\n".encode(), "t.csv", "teachers", {})
        self.assertEqual(p["summary"]["totalRows"], 0)

    def test_unknown_dataset_is_rejected_with_validation_error(self):
        with self.assertRaises(ValueError) as cm:
            I.preview_import_file("الاسم\nأ\n".encode(), "t.csv", "zzz", {})
        self.assertIn("zzz", str(cm.exception))


class Modes(ImportTestCase):
    DATA = None

    def setUp(self):
        super().setUp()
        self.data = xlsx([STU_HEAD, stu_row(paid="900")])
        self.commit(self.data, "students")

    def test_add_only_and_skip_duplicates_do_not_touch_existing(self):
        for mode in ("add_only", "skip_duplicates"):
            rev = self.store.rev
            r = self.commit(self.data, "students", mode=mode)
            self.assertEqual(r["committed"], 0, mode)
            self.assertEqual(r["summary"]["duplicateCount"], 1, mode)
            self.assertEqual(self.store.rev, rev, mode)
            self.assertEqual(len(self.state()["students"]), 1, mode)
            self.assertEqual(len(self.state()["installments"]), 1, mode)

    def test_update_existing_updates_in_place_without_duplicating(self):
        data = xlsx([STU_HEAD, stu_row(paid="400")])
        r = self.commit(data, "students", mode="update_existing")
        self.assertEqual((r["committed"], r["summary"]["updateCount"]), (1, 1))
        st = self.state()
        self.assertEqual(len(st["students"]), 1)
        self.assertEqual([i["amount"] for i in st["installments"]], [400])

    def test_commit_records_batch_and_undo_restores(self):
        self.assertTrue(self.store.get_last_import_batch())
        u = I.undo_last_import(self.store, self.bk, force=True)
        self.assertTrue(u["ok"])
        self.assertEqual(self.state()["students"], [])


class ClosedMonths(ImportTestCase):
    def test_mixed_batch_with_a_closed_month_commits_nothing(self):
        self.store.close_month("2026-10")
        data = xlsx([STU_HEAD, stu_row(code="A", name="أولى", month="2026-11"),
                     stu_row(code="B", name="ثانية", month="2026-10", pay="2026-10-05")])
        rev = self.store.rev
        with self.assertRaises(MonthLockedError):
            self.commit(data, "students")
        st = self.state()
        self.assertEqual((st["students"], st["installments"]), ([], []))
        self.assertEqual(self.store.rev, rev)
        self.assertEqual(os.listdir(self.bk), [])  # the check runs before the safety snapshot
        self.assertFalse(self.store.get_last_import_batch())

    def test_student_without_month_payload_is_allowed_in_a_closed_month(self):
        self.store.close_month("2026-10")
        r = self.commit(xlsx([["كود الطالبة", "اسم الطالبة", "الشهر"], ["C", "ثالثة", "2026-10"]]), "students")
        self.assertEqual(r["committed"], 1)

    def test_import_cannot_move_a_receipt_out_of_a_closed_month(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 70})
        self.store.upsert_student({"id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": "معلمة أ",
                                   "type": "8 حلق", "pct": 70,
                                   "months": {"2026-10": {"value": 1000}, "2026-11": {"value": 1000}}})
        head = ["رقم الإيصال", "كود الطالبة", "اسم الطالبة", "الشهر", "المبلغ", "تاريخ الدفع"]
        self.commit(xlsx([head, ["R1", "", "طالبة ١", "2026-10", "500", "2026-10-05"]]), "receipts")
        self.store.close_month("2026-10")
        with self.assertRaises(MonthLockedError):
            self.commit(xlsx([head, ["R1", "", "طالبة ١", "2026-11", "700", "2026-11-05"]]), "receipts")
        self.assertEqual([(i["receiptNo"], i["month"], i["amount"]) for i in self.state()["installments"]],
                         [("R1", "2026-10", 500)])

    def test_transactions_and_salaries_respect_closed_months(self):
        self.store.close_month("2026-11")
        tx = xlsx([["النوع", "التصنيف", "المبلغ", "التاريخ"], ["مصروف", "ماء", "10", "2026-11-09"]])
        sal = xlsx([["اسم المعلمة", "الشهر", "المبلغ"], ["معلمة أ", "2026-11", "10"]])
        for ds, data in (("transactions", tx), ("salaries", sal)):
            with self.assertRaises(MonthLockedError, msg=ds):
                self.commit(data, ds)
        st = self.state()
        self.assertEqual((st["transactions"], st["payouts"]), ([], []))


class ReceiptsTransactionsSalaries(ImportTestCase):
    def test_receipt_without_number_gets_a_stable_generated_one(self):
        self.store.upsert_student({"id": "s1", "name": "طالبة ١", "phone": "01000000001",
                                   "months": {"2026-11": {"value": 1000}}})
        head = ["رقم الإيصال", "كود الطالبة", "اسم الطالبة", "الشهر", "المبلغ", "تاريخ الدفع"]
        row = ["", "", "طالبة ١", "2026-11", "500", "2026-11-05"]
        p = I.preview_import_file(xlsx([head, row, row]), "r.xlsx", "receipts", self.state())
        self.assertEqual((p["summary"]["newCount"], p["summary"]["duplicateCount"]), (1, 1))
        numbers = {r["data"]["receiptNo"] for r in p["rows"]}
        self.assertEqual(len(numbers), 1)
        self.assertTrue(next(iter(numbers)).startswith("RCP-IMP-"))

    def test_transactions_validate_dedupe_and_classify(self):
        head = ["النوع", "التصنيف", "المبلغ", "التاريخ", "البيان"]
        data = xlsx([head, ["إيراد", "منحة", "100", "2026-11-03", "x"],
                     ["مصروف", "كهرباء", "50", "2026-11-04", "y"],
                     ["مصروف", "كهرباء", "-5", "2026-11-04", "bad"]])
        p = I.preview_import_file(data, "t.xlsx", "transactions", self.state())
        self.assertEqual((p["summary"]["newCount"], p["summary"]["invalidCount"]), (2, 1))
        self.assertEqual(self.commit(data, "transactions")["committed"], 2)
        txs = self.state()["transactions"]
        self.assertEqual(sorted((t["type"], t["amount"], t["month"]) for t in txs),
                         [("expense", 50, "2026-11"), ("revenue", 100, "2026-11")])
        self.assertEqual(self.commit(data, "transactions")["committed"], 2)  # default update_existing: same ids
        self.assertEqual(len(self.state()["transactions"]), 2)
        self.assertEqual(self.commit(data, "transactions", mode="add_only")["committed"], 0)

    def test_salaries_create_missing_teacher_and_are_idempotent(self):
        data = xlsx([["اسم المعلمة", "الشهر", "المبلغ", "تاريخ الصرف"], ["معلمة جديدة", "2026-11", "300", "2026-11-30"]])
        self.assertEqual(self.commit(data, "salaries")["committed"], 1)
        st = self.state()
        self.assertEqual(st["teachers"], ["معلمة جديدة"])
        self.assertEqual([(p["teacher"], p["amount"]) for p in st["payouts"]], [("معلمة جديدة", 300)])
        self.commit(data, "salaries")
        self.assertEqual(len(self.state()["payouts"]), 1)


class ErrorReport(unittest.TestCase):
    def test_valid_workbook_with_rows(self):
        errs = [{"row": 3, "column": "الشهر", "reason": "صيغة الشهر غير صالحة"}]
        wb = load_workbook(io.BytesIO(I.generate_import_error_report_xlsx(errs, "s.xlsx")))
        rows = [[c.value for c in r] for r in wb.active.iter_rows()]
        self.assertEqual(rows[1], [3, "الشهر", "صيغة الشهر غير صالحة", "s.xlsx"])
        self.assertTrue(wb.active.sheet_view.rightToLeft)

    def test_empty_report_is_header_only(self):
        wb = load_workbook(io.BytesIO(I.generate_import_error_report_xlsx([])))
        self.assertEqual(wb.active.max_row, 1)

    def test_text_is_sanitised_no_live_formulas(self):
        # Regression: `=...` in a reason or in the uploaded file name used to be stored as a live formula.
        errs = [{"row": 3, "column": "ب", "reason": EVIL}, {"row": 4, "column": "+cmd", "reason": "@SUM(1)"}]
        data = I.generate_import_error_report_xlsx(errs, filename="=evil.xlsx")
        formulas = [(t, c.coordinate) for t, c in cells(data) if c.data_type == "f"]
        self.assertEqual(formulas, [])
        values = [c.value for _, c in cells(data)]
        self.assertIn(EVIL, values)          # text preserved, just not executable
        self.assertIn("=evil.xlsx", values)


class ExportSafety(StoreTestCase):
    def build(self, teacher="معلمة أ", category="كهرباء", note="", method="نقدي"):
        st = self.store
        st.upsert_teacher(teacher, {"pct": 70})
        st.upsert_student({"id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": teacher,
                           "type": "8 حلق", "pct": 70,
                           "months": {"2026-11": {"value": 1000, "paymentDate": "2026-11-05"}}})
        st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 900,
                               "paymentDate": "2026-11-05", "receiptNo": "R1", "note": note, "method": method})
        st.upsert_transaction({"id": "t1", "type": "expense", "category": category, "amount": 120,
                               "date": "2026-11-06", "note": note, "method": method})
        st.upsert_payout({"id": "p1", "teacher": teacher, "month": "2026-11", "amount": 400,
                          "date": "2026-11-20", "note": note})
        return st.export_state()

    SCOPES = ("full", "teacher", "teachers_stats", "students", "payroll", "attendance",
              "memorization", "madrasa", "accounting")

    def export(self, state, scope, teacher="معلمة أ"):
        return X.export_bytes(state, scope=scope, teacher=teacher if scope == "teacher" else None)

    def test_formula_text_is_plain_in_every_scope_and_every_field(self):
        state = self.build(teacher=EVIL, category=EVIL, note=EVIL, method=EVIL)
        seen = 0
        for scope in self.SCOPES:
            data = self.export(state, scope, teacher=EVIL)
            for title, c in cells(data):
                self.assertNotEqual(c.data_type, "f", f"{scope}:{title}!{c.coordinate} is a live formula")
                seen += isinstance(c.value, str) and "HYPERLINK" in c.value
        self.assertGreater(seen, 10)  # the hostile text really was exported (teacher, category, note, method...)

    def test_teacher_names_with_sheet_forbidden_characters_do_not_break_export(self):
        # Regression: «أ/ فاطمة» (very common) raised ValueError: Invalid character / found in sheet title.
        for name in ("أ/ فاطمة", "معلمة: نور", "معلمة [أ]", "أ?*", "'نور'", "x" * 40):
            with self.subTest(name=name):
                store_dir = self.home / f"d{abs(hash(name))}"
                store_dir.mkdir()
                from alahd_erp.infrastructure.database import Store
                st = Store(store_dir / "t.sqlite3")
                self.addCleanup(st.close)
                st.upsert_teacher(name, {"pct": 70})
                st.upsert_student({"id": "s1", "name": "ط", "teacher": name, "type": "8 حلق", "pct": 70,
                                   "months": {"2026-11": {"value": 1000}}})
                state = st.export_state()
                for scope in ("full", "teacher"):
                    data = X.export_bytes(state, scope=scope, teacher=name if scope == "teacher" else None)
                    wb = load_workbook(io.BytesIO(data))
                    for ws in wb.worksheets:
                        self.assertLessEqual(len(ws.title), 31)
                        self.assertFalse(set(ws.title) & set("\\/?*[]:"), ws.title)
                        self.assertFalse(ws.title.startswith("'") or ws.title.endswith("'"), ws.title)

    def test_valid_teacher_names_keep_their_exact_sheet_title(self):
        state = self.build(teacher="معلمة أ")
        self.assertIn("معلمة أ", load_workbook(io.BytesIO(self.export(state, "full"))).sheetnames)

    def test_safe_sheet_title_rules(self):
        f = X._safe_sheet_title
        self.assertEqual(f("أ/ فاطمة"), "أ- فاطمة")
        self.assertEqual(f("معلمة  أ"), "معلمة  أ")   # valid names are untouched, even odd spacing
        self.assertEqual(f(""), "المعلمة")
        self.assertEqual(f("   "), "المعلمة")
        self.assertEqual(len(f("x" * 40)), 31)


class ExportMatchesEngine(StoreTestCase):
    """The workbook must show the same numbers as `compute_accounting_summary` for the same state."""

    def setUp(self):
        super().setUp()
        st = self.store
        st.upsert_teacher("معلمة أ", {"pct": 70})
        st.upsert_teacher("معلمة ب", {"pct": 60})
        st.upsert_student({"id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": "معلمة أ",
                           "type": "8 حلق", "pct": 70,
                           "months": {"2026-11": {"value": 1000, "discount": 100, "paymentDate": "2026-11-05"}}})
        st.upsert_student({"id": "s2", "name": "طالبة ٢", "teacher": "معلمة ب", "type": "8 حلق", "pct": 60,
                           "months": {"2026-11": {"value": 800}}})
        st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 900,
                               "paymentDate": "2026-11-05", "receiptNo": "R1"})
        st.upsert_transaction({"id": "t1", "type": "expense", "category": "كهرباء", "amount": 120, "date": "2026-11-06"})
        st.upsert_payout({"id": "p1", "teacher": "معلمة أ", "month": "2026-11", "amount": 400, "date": "2026-11-20"})
        self.state = st.export_state()
        s = self.state["settings"]
        self.summary = compute_accounting_summary(
            self.state["students"], self.state["payouts"], self.state["transactions"], self.state["installments"],
            month_key="2026-11", teacher_meta=self.state["teacherMeta"], pct_source=s["pctSource"],
            wage_basis=s["wageBasis"], wage_accrual_mode=s["wageAccrualMode"], default_pct=s["defaultTeacherPct"])

    @staticmethod
    def labelled(ws):
        """label -> value, pairing each text cell with the next non-empty cell (merged cells leave gaps)."""
        out = {}
        for row in ws.iter_rows(values_only=True):
            vals = [v for v in row if v is not None]
            for label, value in zip(vals, vals[1:]):
                if isinstance(label, str) and isinstance(value, (int, float)):
                    out.setdefault(label, value)
        return out

    def test_summary_sheet_equals_engine(self):
        wb = load_workbook(io.BytesIO(X.export_bytes(self.state, scope="full")))
        got = self.labelled(wb["ملخص"])
        s = self.summary
        self.assertEqual(got["إجمالي الاشتراكات المسجلة"], s["subscriptions"])
        self.assertEqual(got["المحصّل (حسب تاريخ الدفع)"], s["collected"])
        self.assertEqual(got["غير المحصّل"], s["uncollected"])
        self.assertEqual(got["إجمالي أجور المعلمات"], s["wages"])
        self.assertEqual(got["مصروف مرتبات المعلمات"], s["salaryPaid"])
        self.assertEqual(got["متبقي مرتبات المعلمات"], s["salaryRemaining"])

    def test_payroll_rows_add_up_to_engine_totals(self):
        wb = load_workbook(io.BytesIO(X.export_bytes(self.state, scope="payroll")))
        rows = [r for r in wb["مرتبات المعلمات"].iter_rows(values_only=True) if r[1] in ("معلمة أ", "معلمة ب")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r[5] for r in rows), self.summary["wages"])        # المستحق
        self.assertEqual(sum(r[6] for r in rows), self.summary["salaryPaid"])    # المصروف
        self.assertEqual(sum(r[7] for r in rows), self.summary["salaryRemaining"])  # المتبقي

    def test_receipts_sheet_lists_every_receipt_with_its_amount(self):
        wb = load_workbook(io.BytesIO(X.export_bytes(self.state, scope="accounting")))
        rows = [r for r in wb["إيصالات الدفعات الجزئية"].iter_rows(values_only=True) if r[0] == "R1"]
        self.assertEqual([(r[1], r[2], r[5]) for r in rows], [("2026-11-05", "طالبة ١", 900)])


# --------------------------------------------------------------------------- IMPORT-2 regression
# A NEW student on several rows (one per month) must keep every month. Before the fix `commit_import` created a second
# student object with the same id per later row, so only the last month survived (silent data loss).
MULTI_HEAD = ["كود الطالبة", "اسم الطالبة", "رقم الهاتف", "المعلمة", "نوع الاشتراك", "الشهر", "قيمة الاشتراك",
        "المبلغ المسدد", "تاريخ الدفع", "تاريخ النهاية"]


def multi_book(rows) -> bytes:
    return xlsx([MULTI_HEAD, *rows])


class MultiMonthNewStudent(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.bk = self.home / "backups"
        self.bk.mkdir()

    def test_new_student_with_three_month_rows_keeps_all_months(self):
        data = multi_book([
            ["S1", "طالبة أولى", "01000000001", "معلمة أ", "باقة", "2026-10", 100, 100, "2026-10-02", "2026-10-30"],
            ["S1", "طالبة أولى", "01000000001", "معلمة أ", "باقة", "2026-11", 150, 150, "2026-11-02", "2026-11-30"],
            ["S1", "طالبة أولى", "01000000001", "معلمة أ", "باقة", "2026-12", 200, None, "2026-12-02", None],
            ["S2", "طالبة ثانية", "01000000002", "معلمة أ", "باقة", "2026-11", 50, None, None, None],
        ])
        out = I.commit_import(self.store, data, "f.xlsx", "students", backup_folder=self.bk)
        self.assertEqual(out["committed"], 4)
        st = self.store.export_state()
        self.assertEqual(len(st["students"]), 2)
        s1 = next(s for s in st["students"] if s["code"] == "S1")
        self.assertEqual({k: m["value"] for k, m in s1["months"].items()},
                         {"2026-10": 100, "2026-11": 150, "2026-12": 200})
        # Observed behaviour: the Store's legacy migration also turns the dated Dec month (fee 200, no paid amount)
        # into a receipt; S2 (no payment date) gets none.
        self.assertEqual(sorted((i["month"], i["amount"]) for i in st["installments"]),
                         [("2026-10", 100), ("2026-11", 150), ("2026-12", 200)])




class Phase1ContractAndPreview(ImportTestCase):
    def test_preview_and_commit_return_unified_ui_and_backend_keys(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 70})
        self.store.upsert_student({
            "id": "s_existing",
            "code": "S1",
            "name": "طالبة أولى",
            "phone": "01000000001",
            "teacher": "معلمة أ",
            "type": "8 حلق",
            "pct": 70,
            "months": {"2026-11": {"value": 1000}},
        })
        data = xlsx([
            STU_HEAD,
            stu_row(code="S1", name="طالبة أولى", phone="01000000001", value="1000", paid="500"),
            stu_row(code="S2", name="طالبة ثانية", phone="01000000002", teacher="معلمة جديدة", value="800", paid="800"),
            stu_row(code="S2", name="طالبة ثانية", phone="01000000002", teacher="معلمة جديدة", value="800", paid="800"),
            stu_row(code="S3", name="", phone="01000000003"),
        ])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state(), mode="update_existing")

        # Worksheet keys
        self.assertEqual(p["detectedSheet"], "ورقة1")
        self.assertEqual(p["selectedSheet"], "ورقة1")
        self.assertEqual(p["sheets"], ["ورقة1"])
        self.assertEqual(p["sheetNames"], ["ورقة1"])
        self.assertTrue(p["fileHash"])

        # Summary dual keys
        s = p["summary"]
        self.assertEqual((s["totalRows"], s["total"]), (4, 4))
        self.assertEqual((s["validCount"], s["valid"]), (2, 2))
        self.assertEqual((s["newCount"], s["new"]), (1, 1))
        self.assertEqual((s["updateCount"], s["will_update"]), (1, 1))
        self.assertEqual((s["duplicateCount"], s["duplicate"]), (1, 1))
        self.assertEqual((s["invalidCount"], s["invalid"]), (1, 1))

        # Row dual keys
        for r in p["rows"]:
            self.assertEqual(r["row"], r["rowIndex"])
            self.assertEqual(r["data"], r["normalized"])
            self.assertIsInstance(r["errors"], list)
            self.assertIsInstance(r["warnings"], list)

        # Auto-create warning recorded for new teacher
        self.assertIn("معلمة جديدة", p["newEntities"]["teachers"])
        self.assertTrue(any("معلمة جديدة" in w for w in p["rows"][1]["warnings"]))

        # Commit response keys
        res = self.commit(data, "students", mode="update_existing", expected_file_hash=p["fileHash"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["committed"], 2)
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["updated"], 1)
        self.assertEqual(res["skipped"], 2)
        self.assertEqual(res["duplicate"], 1)
        self.assertEqual(res["invalid"], 1)
        self.assertIn("معلمة جديدة", res["createdTeachers"])

    def test_stale_file_hash_is_rejected_on_commit(self):
        data1 = xlsx([STU_HEAD, stu_row(code="S1", name="طالبة أولى")])
        data2 = xlsx([STU_HEAD, stu_row(code="S1", name="طالبة معدلة")])
        p1 = I.preview_import_file(data1, "s.xlsx", "students", self.state())
        with self.assertRaises(ValueError) as cm:
            self.commit(data2, "students", expected_file_hash=p1["fileHash"])
        self.assertIn("تغير محتوى الملف", str(cm.exception))
        self.assertEqual(self.state()["students"], [])


class Phase1Worksheets(ImportTestCase):
    def _multi_sheet_wb(self) -> bytes:
        wb = Workbook()
        ws1 = wb.active
        ws1.title = "الفرع الأول"
        ws1.append(["اسم المعلمة", "نسبة المعلمة"])
        ws1.append(["معلمة فرع ١", 70])

        ws2 = wb.create_sheet("الفرع الثاني")
        ws2.append(["اسم المعلمة", "نسبة المعلمة"])
        ws2.append(["معلمة فرع ٢ أ", 65])
        ws2.append(["معلمة فرع ٢ ب", 75])

        ws_inst = wb.create_sheet("تعليمات الاستيراد")
        ws_inst.append(["ملاحظة", "لا تستورد"])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_multi_sheet_detection_and_explicit_selection(self):
        data = self._multi_sheet_wb()
        p_default = I.preview_import_file(data, "m.xlsx", "teachers", self.state())
        self.assertEqual(p_default["sheetNames"], ["الفرع الأول", "الفرع الثاني"])
        self.assertEqual(p_default["selectedSheet"], "الفرع الأول")
        self.assertEqual(p_default["summary"]["total"], 1)

        p_second = I.preview_import_file(data, "m.xlsx", "teachers", self.state(), sheet_name="الفرع الثاني")
        self.assertEqual(p_second["selectedSheet"], "الفرع الثاني")
        self.assertEqual(p_second["summary"]["total"], 2)

        res = self.commit(data, "teachers", sheet_name="الفرع الثاني")
        self.assertEqual(res["added"], 2)
        self.assertEqual(self.state()["teachers"], ["معلمة فرع ٢ أ", "معلمة فرع ٢ ب"])

    def test_non_existent_sheet_name_raises_validation_error(self):
        data = self._multi_sheet_wb()
        with self.assertRaises(ValueError) as cm:
            I.preview_import_file(data, "m.xlsx", "teachers", self.state(), sheet_name="ورقة غير موجودة")
        self.assertIn("غير موجودة", str(cm.exception))
        with self.assertRaises(ValueError):
            self.commit(data, "teachers", sheet_name="ورقة غير موجودة")
        with self.assertRaises(ValueError):
            I.preview_import_file("الاسم,النسبة\nأ,70\n".encode("utf-8"), "t.csv", "teachers", self.state(), sheet_name="ورقة1")


class Phase1ColumnMapping(ImportTestCase):
    def test_custom_mapping_unmapping_persistence_and_validation(self):
        custom_xlsx = xlsx([
            ["الرمز", "الاسم الكامل للطالبة", "تليفون ولي الأمر", "قيمة الرسوم"],
            ["C-10", "مريم خالد", "01012345678", "450"],
        ])
        # Without custom mapping, "الاسم الكامل للطالبة" matches via substring "الاسم", but "الرمز" is unmapped
        p_unmapped = I.preview_import_file(custom_xlsx, "c.xlsx", "students", self.state())
        self.assertNotIn("code", p_unmapped["columnMapping"])

        # Apply explicit column mapping
        overrides = {
            "code": "الرمز",
            "name": "الاسم الكامل للطالبة",
            "phone": "تليفون ولي الأمر",
            "value": "قيمة الرسوم",
        }
        p_mapped = I.preview_import_file(
            custom_xlsx, "c.xlsx", "students", self.state(), column_mapping=overrides, default_month="2026-11"
        )
        self.assertEqual(p_mapped["mappingErrors"], [])
        self.assertEqual(p_mapped["rows"][0]["normalized"]["code"], "C-10")
        self.assertEqual(p_mapped["rows"][0]["normalized"]["phone"], "01012345678")
        self.assertEqual(p_mapped["rows"][0]["normalized"]["value"], 450)

        # Commit saves mapping to state["columnMappings"]["students"]
        self.commit(custom_xlsx, "students", column_mapping=overrides, default_month="2026-11")
        st = self.state()
        self.assertEqual(st["columnMappings"]["students"]["code"], "الرمز")
        self.assertEqual(st["students"][0]["code"], "C-10")

        # Next preview reuses saved mapping automatically even without passing overrides
        p_reused = I.preview_import_file(custom_xlsx, "c.xlsx", "students", st)
        self.assertEqual(p_reused["columnMapping"]["code"], "الرمز")

        # Unmapping required field "name" via "__none__" reports missingRequiredFields and invalidates rows
        p_unmap_req = I.preview_import_file(
            custom_xlsx, "c.xlsx", "students", st, column_mapping={"name": "__none__"}
        )
        self.assertTrue(any(m["field"] == "name" for m in p_unmap_req["missingRequiredFields"]))
        self.assertEqual(p_unmap_req["summary"]["invalid"], 1)

        # Invalid column name and duplicate column assignment in column_mapping are flagged
        p_bad_col = I.preview_import_file(
            custom_xlsx, "c.xlsx", "students", st, column_mapping={"phone": "عمود وهمي"}
        )
        self.assertTrue(len(p_bad_col["mappingErrors"]) > 0)
        self.assertEqual(p_bad_col["summary"]["invalid"], 1)

        p_dup_col = I.preview_import_file(
            custom_xlsx, "c.xlsx", "students", st, column_mapping={"code": "الرمز", "phone": "الرمز"}
        )
        self.assertTrue(any("تعارض" in e for e in p_dup_col["mappingErrors"]))
        self.assertEqual(p_dup_col["summary"]["invalid"], 1)


class Phase1ImportOptionsAndAutoCreate(ImportTestCase):
    def test_granular_auto_create_teachers_and_packages_flags(self):
        data = xlsx([STU_HEAD, stu_row(code="S1", name="طالبة ١", teacher="معلمة جديدة", value="500")])

        # Disable teacher auto-create only
        p_no_t = I.preview_import_file(
            data, "s.xlsx", "students", self.state(), auto_create_teachers=False, auto_create_packages=True
        )
        self.assertEqual(p_no_t["summary"]["invalid"], 1)
        self.assertTrue(any("المعلمة" in e["reason"] for e in p_no_t["errors"]))

        # Enable teacher auto-create, disable package auto-create
        p_no_p = I.preview_import_file(
            data, "s.xlsx", "students", self.state(), auto_create_teachers=True, auto_create_packages=False
        )
        self.assertEqual(p_no_p["summary"]["invalid"], 1)
        self.assertTrue(any("الباقة" in e["reason"] for e in p_no_p["errors"]))

        # Enable both -> valid with explicit newEntities warnings
        p_both = I.preview_import_file(
            data, "s.xlsx", "students", self.state(), auto_create_teachers=True, auto_create_packages=True
        )
        self.assertEqual(p_both["summary"]["invalid"], 0)
        self.assertEqual(p_both["newEntities"]["teachers"], ["معلمة جديدة"])
        self.assertEqual(p_both["newEntities"]["packages"], ["8 حلق"])

    def test_arabic_spelling_variants_do_not_create_duplicate_references(self):
        self.store.upsert_teacher("فاطمة عبد الرحمن", {"pct": 70})
        data = xlsx([
            STU_HEAD,
            stu_row(code="S1", name="طالبة ١", phone="01000000001", teacher="أ/ فاطمه عبدالرحمن"),
            stu_row(code="S2", name="طالبة ٢", phone="01000000002", teacher="خديجة أحمد"),
            stu_row(code="S3", name="طالبة ٣", phone="01000000003", teacher="أ. خديجه احمد"),
        ])
        res = self.commit(data, "students", auto_create_refs=True)
        self.assertEqual(res["committed"], 3)
        st = self.state()
        # "أ/ فاطمه عبدالرحمن" matched existing "فاطمة عبد الرحمن", and "خديجة أحمد" + "أ. خديجه احمد" merged into one!
        self.assertEqual(st["teachers"], ["فاطمة عبد الرحمن", "خديجة أحمد"])
        self.assertEqual(res["createdTeachers"], ["خديجة أحمد"])

    def test_import_works_when_state_subtypes_is_a_list_from_demo_data(self):
        self.store.load_demo_data()
        st = self.state()
        self.assertIsInstance(st["subTypes"], list)
        data = xlsx([STU_HEAD, stu_row(code="STU-NEW-99", name="طالبة فوق الديمو", phone="01098765432")])
        p = I.preview_import_file(data, "s.xlsx", "students", st)
        self.assertEqual(p["summary"]["invalid"], 0)
        res = self.commit(data, "students")
        self.assertEqual(res["added"], 1)


class Phase1UnsafeFallbacksRegression(ImportTestCase):
    def test_invalid_dataset_rejected_everywhere(self):
        data = xlsx([STU_HEAD, stu_row()])
        for bad_ds in ("unknown", "users", "", "   "):
            with self.assertRaises(ValueError):
                I.generate_import_template_xlsx(bad_ds)
            with self.assertRaises(ValueError):
                I.preview_import_file(data, "s.xlsx", bad_ds, self.state())
            with self.assertRaises(ValueError):
                self.commit(data, bad_ds)

    def test_invalid_mode_rejected_everywhere(self):
        data = xlsx([STU_HEAD, stu_row()])
        for bad_mode in ("overwrite_all", "merge", "invalid_mode", ""):
            with self.assertRaises(ValueError):
                I.preview_import_file(data, "s.xlsx", "students", self.state(), mode=bad_mode)
            with self.assertRaises(ValueError):
                self.commit(data, "students", mode=bad_mode)

    def test_invalid_worksheet_rejected_everywhere(self):
        data = xlsx([STU_HEAD, stu_row()], sheet="البيانات")
        with self.assertRaises(ValueError):
            I.preview_import_file(data, "s.xlsx", "students", self.state(), sheet_name="غير_موجودة")
        with self.assertRaises(ValueError):
            self.commit(data, "students", sheet_name="غير_موجودة")


class Phase1StudentMatchingAndAmbiguity(ImportTestCase):
    def setUp(self):
        super().setUp()
        self.store.upsert_teacher("معلمة أ", {"pct": 70})

    def test_ambiguous_name_only_match_is_rejected_and_never_updates_wrong_student(self):
        # Two existing students share the same name with different phones
        self.store.upsert_student({
            "id": "s1", "code": "STU-01", "name": "مريم أحمد", "phone": "01000000001",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 300}},
        })
        self.store.upsert_student({
            "id": "s2", "code": "STU-02", "name": "مريم احمد", "phone": "01000000002",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 400}},
        })
        # Imported row has no code and no phone -> ambiguous between s1 and s2!
        data = xlsx([STU_HEAD, stu_row(code="", name="مريم أحمد", phone="", value="999")])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state(), mode="update_existing")
        self.assertEqual(p["summary"]["invalid"], 1)
        self.assertEqual(p["summary"]["will_update"], 0)
        self.assertTrue(any("مطابقة غامضة" in e for e in p["rows"][0]["errors"]))
        res = self.commit(data, "students", mode="update_existing")
        self.assertEqual(res["committed"], 0)
        # Neither student's subscription was modified
        st = self.state()
        self.assertEqual(st["students"][0]["months"]["2026-11"]["value"], 300)
        self.assertEqual(st["students"][1]["months"]["2026-11"]["value"], 400)

    def test_conflicting_code_and_name_is_rejected(self):
        self.store.upsert_student({
            "id": "s1", "code": "STU-01", "name": "سارة محمود", "phone": "01000000001",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 300}},
        })
        # Imported row uses STU-01 but with a completely different student name
        data = xlsx([STU_HEAD, stu_row(code="STU-01", name="فاطمة علي", phone="01000000009", value="700")])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state(), mode="update_existing")
        self.assertEqual(p["summary"]["invalid"], 1)
        self.assertTrue(any("تعارض في بيانات الهوية" in e for e in p["rows"][0]["errors"]))
        self.assertEqual(self.commit(data, "students")["committed"], 0)

    def test_conflicting_code_on_name_phone_match_is_rejected(self):
        self.store.upsert_student({
            "id": "s1", "code": "STU-01", "name": "سارة محمود", "phone": "01000000001",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 300}},
        })
        # Same name and phone as s1, but specifies a different student code STU-99
        data = xlsx([STU_HEAD, stu_row(code="STU-99", name="سارة محمود", phone="01000000001", value="700")])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state(), mode="update_existing")
        self.assertEqual(p["summary"]["invalid"], 1)
        self.assertTrue(any("تعارض في بيانات الهوية" in e for e in p["rows"][0]["errors"]))

    def test_duplicate_code_for_different_students_in_same_file_is_rejected(self):
        data = xlsx([
            STU_HEAD,
            stu_row(code="STU-10", name="طالبة أ", phone="01000000001", month="2026-10"),
            stu_row(code="STU-10", name="طالبة ب", phone="01000000002", month="2026-11"),
        ])
        p = I.preview_import_file(data, "s.xlsx", "students", self.state())
        self.assertEqual(p["summary"]["new"], 1)
        self.assertEqual(p["summary"]["invalid"], 1)
        self.assertTrue(any("مستخدم لطالبة أخرى" in e for e in p["rows"][1]["errors"]))

    def test_receipt_with_conflicting_phone_is_rejected(self):
        self.store.upsert_student({
            "id": "s1", "code": "STU-01", "name": "طالبة إيصال", "phone": "01000000001",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 500}},
        })
        head = ["رقم الإيصال", "اسم الطالبة", "رقم الهاتف", "الشهر", "المبلغ", "تاريخ الدفع"]
        data = xlsx([head, ["R-100", "طالبة إيصال", "01099999999", "2026-11", "250", "2026-11-05"]])
        p = I.preview_import_file(data, "r.xlsx", "receipts", self.state())
        self.assertEqual(p["summary"]["invalid"], 1)
        self.assertTrue(any("تعارض" in e for e in p["rows"][0]["errors"]))


class Phase1FinancialReceiptSafety(ImportTestCase):
    def test_two_legitimate_same_day_same_amount_receipts_with_different_notes_or_methods_do_not_collide(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 70})
        self.store.upsert_student({
            "id": "s1", "code": "STU-01", "name": "طالبة قسطين", "phone": "01000000001",
            "teacher": "معلمة أ", "pct": 70, "months": {"2026-11": {"value": 400}},
        })
        head = ["رقم الإيصال", "كود الطالبة", "اسم الطالبة", "الشهر", "المبلغ", "تاريخ الدفع", "طريقة الدفع", "ملاحظة"]
        data = xlsx([
            head,
            ["", "STU-01", "طالبة قسطين", "2026-11", "200", "2026-11-05", "فودافون كاش", "دفعة أولى صباحا"],
            ["", "STU-01", "طالبة قسطين", "2026-11", "200", "2026-11-05", "فودافون كاش", "دفعة ثانية مساء"],
        ])
        p = I.preview_import_file(data, "r.xlsx", "receipts", self.state())
        self.assertEqual(p["summary"]["new"], 2)
        self.assertEqual(p["summary"]["duplicate"], 0)
        res = self.commit(data, "receipts")
        self.assertEqual(res["added"], 2)
        insts = self.state()["installments"]
        self.assertEqual(len(insts), 2)
        self.assertEqual(sum(i["amount"] for i in insts), 400)
        self.assertEqual(len({i["receiptNo"] for i in insts}), 2)
        self.assertEqual(len({i["id"] for i in insts}), 2)

    def test_student_import_receipt_numbers_do_not_collide_when_ids_share_suffix(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 70})
        self.store.upsert_student({
            "id": "stu_a_0001", "code": "A1", "name": "طالبة أ", "phone": "01000000001", "teacher": "معلمة أ", "pct": 70,
        })
        self.store.upsert_student({
            "id": "stu_b_0001", "code": "B1", "name": "طالبة ب", "phone": "01000000002", "teacher": "معلمة أ", "pct": 70,
        })
        data = xlsx([
            STU_HEAD,
            stu_row(code="A1", name="طالبة أ", phone="01000000001", paid="300"),
            stu_row(code="B1", name="طالبة ب", phone="01000000002", paid="300"),
        ])
        self.commit(data, "students")
        insts = self.state()["installments"]
        self.assertEqual(len(insts), 2)
        self.assertEqual(len({i["receiptNo"] for i in insts}), 2)


class Phase1ExportImportCompatibility(ImportTestCase):
    def test_exported_students_xlsx_reimports_into_fresh_store(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 75})
        self.store.upsert_student({
            "id": "s1",
            "code": "STU-001",
            "name": "طالبة التصدير ١",
            "phone": "01011112222",
            "teacher": "معلمة أ",
            "type": "8 حلقات",
            "pct": 75,
            "months": {
                "2026-10": {"value": 300, "paymentDate": "2026-10-03", "actualStartDate": "2026-10-05", "endDate": "2026-11-04"},
                "2026-11": {"value": 350, "paymentDate": "2026-11-01", "actualStartDate": "2026-11-05", "endDate": "2026-12-04"},
            },
        })
        exported_xlsx = X.export_bytes(self.state(), scope="students")

        # Re-import into a completely fresh Store
        from alahd_erp.infrastructure.database import Store
        fresh_dir = self.home / "fresh"
        fresh_dir.mkdir()
        fresh_store = Store(fresh_dir / "fresh.sqlite3")
        self.addCleanup(fresh_store.close)

        p = I.preview_import_file(exported_xlsx, "exported_students.xlsx", "students", fresh_store.export_state())
        self.assertEqual(p["summary"]["invalid"], 0, p["errors"])
        self.assertEqual(p["summary"]["new"], 2)  # 2 student-month rows unpivoted from the 2-month export sheet

        res = I.commit_import(fresh_store, exported_xlsx, "exported_students.xlsx", "students", backup_folder=fresh_dir)
        self.assertEqual(res["committed"], 2)
        st2 = fresh_store.export_state()
        self.assertEqual(len(st2["students"]), 1)
        stu2 = st2["students"][0]
        self.assertEqual(stu2["name"], "طالبة التصدير ١")
        self.assertEqual(stu2["pct"], 75)
        self.assertEqual(stu2["months"]["2026-10"]["value"], 300)
        self.assertEqual(stu2["months"]["2026-10"]["paymentDate"], "2026-10-03")
        self.assertEqual(stu2["months"]["2026-10"]["actualStartDate"], "2026-10-05")
        self.assertEqual(stu2["months"]["2026-11"]["value"], 350)
        self.assertEqual(stu2["months"]["2026-11"]["paymentDate"], "2026-11-01")
        self.assertEqual(stu2["months"]["2026-11"]["actualStartDate"], "2026-11-05")

    def test_exported_teachers_and_accounting_sheets_reimport_cleanly(self):
        self.store.upsert_teacher("معلمة أ", {"pct": 65, "notes": "متميزة"})
        self.store.upsert_student({
            "id": "s1", "code": "S1", "name": "طالبة ١", "phone": "01000000001", "teacher": "معلمة أ", "pct": 65,
            "months": {"2026-11": {"value": 500}},
        })
        self.store.upsert_installment({
            "id": "i1", "studentId": "s1", "month": "2026-11", "amount": 500, "paymentDate": "2026-11-05", "receiptNo": "RCP-55",
        })
        self.store.upsert_transaction({
            "id": "t1", "type": "expense", "category": "إنترنت", "amount": 150, "date": "2026-11-06", "month": "2026-11",
        })
        full_xlsx = X.export_bytes(self.state(), scope="full")

        from alahd_erp.infrastructure.database import Store
        fresh_dir = self.home / "fresh2"
        fresh_dir.mkdir()
        fresh_store = Store(fresh_dir / "fresh2.sqlite3")
        self.addCleanup(fresh_store.close)

        # 1. Import teachers from "المعلمات" sheet
        r_t = I.commit_import(fresh_store, full_xlsx, "full.xlsx", "teachers", sheet_name="المعلمات", backup_folder=fresh_dir)
        self.assertEqual(r_t["added"], 1)
        self.assertEqual(fresh_store.export_state()["teacherMeta"]["معلمة أ"]["pct"], 65)

        # 2. Import students from "الطلاب" sheet
        r_s = I.commit_import(fresh_store, full_xlsx, "full.xlsx", "students", sheet_name="الطلاب", backup_folder=fresh_dir)
        self.assertEqual(r_s["committed"], 1)

        # 3. Import receipts from "إيصالات الدفعات الجزئية" sheet
        r_rcp = I.commit_import(fresh_store, full_xlsx, "full.xlsx", "receipts", sheet_name="إيصالات الدفعات الجزئية", backup_folder=fresh_dir)
        self.assertEqual(r_rcp["added"], 1)

        # 4. Import transactions from "المحاسبة والمصروفات" sheet
        r_tx = I.commit_import(fresh_store, full_xlsx, "full.xlsx", "transactions", sheet_name="المحاسبة والمصروفات", backup_folder=fresh_dir)
        self.assertEqual(r_tx["added"], 1)


if __name__ == "__main__":
    unittest.main()
