import io
import unittest

from openpyxl import load_workbook

from tests import _helpers  # noqa: F401
from alahd_erp.services.excel_service import (
    _school_year_label,
    build_workbook,
    export_bytes,
)


def state_with_months(months, name="طالبة"):
    return {
        "teachers": ["معلمة أ"],
        "teacherMeta": {"معلمة أ": {"pct": 70}},
        "settings": {"wageBasis": "net", "wageAccrualMode": "accrued"},
        "students": [{
            "id": "s1", "name": name, "teacher": "معلمة أ", "pct": 70,
            "months": {m: {"value": 1000, "discount": 0} for m in months},
        }],
        "installments": [], "payouts": [], "transactions": [],
    }


def sheet_values(xlsx_bytes, title):
    wb = load_workbook(io.BytesIO(xlsx_bytes))
    ws = wb[title]
    return [[c.value for c in row] for row in ws.iter_rows()]


class ComparativePayroll(unittest.TestCase):
    TITLE = "رواتب المعلمات الشهرية"

    def test_all_months_included_not_just_oct_nov_2026(self):
        months = ["2026-10", "2026-11", "2026-12", "2027-01"]
        rows = sheet_values(export_bytes(state_with_months(months), scope="teachers_stats"), self.TITLE)
        header_months = [v for v in rows[2] if isinstance(v, str) and "20" in v]
        self.assertEqual(len(header_months), 4)
        teacher_row = next(r for r in rows if r[1] == "معلمة أ")
        # 4 months x 1000 subscription x 70 % = 2800 total wages, shown in the 2nd-to-last column
        self.assertEqual(teacher_row[-2], 2800)
        self.assertEqual(teacher_row[-1], 4000 - 2800)

    def test_grand_total_row_matches(self):
        months = ["2026-12", "2027-01"]
        rows = sheet_values(export_bytes(state_with_months(months), scope="teachers_stats"), self.TITLE)
        total = next(r for r in rows if r[0] == "الإجمالي العام")
        self.assertEqual(total[-2], 1400)

    def test_wage_follows_settings_basis(self):
        st = state_with_months(["2026-11"])
        st["students"][0]["months"]["2026-11"]["discount"] = 100
        st["settings"]["wageBasis"] = "gross"
        rows = sheet_values(export_bytes(st, scope="teachers_stats"), self.TITLE)
        self.assertEqual(next(r for r in rows if r[1] == "معلمة أ")[-2], 700)   # 70 % of gross 1000
        st["settings"]["wageBasis"] = "net"
        rows = sheet_values(export_bytes(st, scope="teachers_stats"), self.TITLE)
        self.assertEqual(next(r for r in rows if r[1] == "معلمة أ")[-2], 630)   # 70 % of net 900


class Safety(unittest.TestCase):
    def test_formula_like_names_are_plain_text(self):
        evil = '=HYPERLINK("http://evil.example","click")'
        data = export_bytes(state_with_months(["2026-11"], name=evil), scope="students")
        wb = load_workbook(io.BytesIO(data))
        found = False
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and "HYPERLINK" in c.value:
                        found = True
                        self.assertNotEqual(c.data_type, "f", f"{ws.title}!{c.coordinate} is a live formula")
        self.assertTrue(found)

    def test_school_year_label_derived_from_data(self):
        self.assertIn("2026", _school_year_label(["2026-11"]))
        self.assertIn("2027", _school_year_label(["2026-11"]))
        self.assertEqual(_school_year_label(["2027-02"]), "السنة الدراسية: أكتوبر 2026 – سبتمبر 2027")
        self.assertEqual(_school_year_label(["2027-10"]), "السنة الدراسية: أكتوبر 2027 – سبتمبر 2028")

    def test_every_scope_builds(self):
        st = state_with_months(["2026-11"])
        for scope in ("full", "teacher", "teachers_stats", "students", "payroll", "attendance",
                      "memorization", "madrasa", "accounting"):
            self.assertGreater(len(export_bytes(st, scope=scope)), 1000, scope)


if __name__ == "__main__":
    unittest.main()
