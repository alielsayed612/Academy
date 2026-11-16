import datetime
import unittest

from tests._helpers import StoreTestCase
from alahd_erp.services.import_service import (
    auto_map_headers,
    parse_flexible_date,
    parse_flexible_money,
    parse_flexible_month,
)


class DateParsing(unittest.TestCase):
    def test_iso_and_slash_formats(self):
        self.assertEqual(parse_flexible_date("2026-11-05"), "2026-11-05")
        self.assertEqual(parse_flexible_date("5/11/2026"), "2026-11-05")
        self.assertEqual(parse_flexible_date("2026-11-05 00:00:00"), "2026-11-05")

    def test_arabic_indic_digits(self):
        self.assertEqual(parse_flexible_date("٥/١١/٢٠٢٦"), "2026-11-05")

    def test_month_first_is_swapped_when_day_over_12(self):
        self.assertEqual(parse_flexible_date("11/13/2026"), "2026-11-13")

    def test_arabic_month_name(self):
        self.assertEqual(parse_flexible_date("5 نوفمبر 2026"), "2026-11-05")
        self.assertEqual(parse_flexible_date("نوفمبر 2026"), "2026-11-01")

    def test_excel_serial_and_date_objects(self):
        self.assertEqual(parse_flexible_date(46000), "2025-12-09")
        self.assertEqual(parse_flexible_date(datetime.date(2026, 1, 2)), "2026-01-02")

    def test_empty_vs_invalid(self):
        self.assertEqual(parse_flexible_date(""), "")
        self.assertEqual(parse_flexible_date("—"), "")
        self.assertIsNone(parse_flexible_date("31/02/2026"))
        self.assertIsNone(parse_flexible_date("abc"))


class MonthParsing(unittest.TestCase):
    def test_formats(self):
        for raw in ("2026-11", "11/2026", "نوفمبر 2026", datetime.date(2026, 11, 4)):
            self.assertEqual(parse_flexible_month(raw, "2026-01"), "2026-11", raw)

    def test_default_and_invalid(self):
        self.assertEqual(parse_flexible_month("", "2026-01"), "2026-01")
        self.assertIsNone(parse_flexible_month("2026-13", "2026-01"))
        self.assertIsNone(parse_flexible_month("x", "2026-01"))


class MoneyParsing(unittest.TestCase):
    def test_separators_and_labels(self):
        self.assertEqual(parse_flexible_money("1,500"), 1500)
        self.assertEqual(parse_flexible_money("١٬٥٠٠ ج.م"), 1500)
        self.assertEqual(parse_flexible_money("2.50 جنيه"), 3)  # ROUND_HALF_UP

    def test_empty_and_invalid(self):
        self.assertEqual(parse_flexible_money(""), 0)
        self.assertIsNone(parse_flexible_money("", allow_empty=False))
        self.assertIsNone(parse_flexible_money("-5"))
        self.assertIsNone(parse_flexible_money("abc"))


class HeaderMapping(unittest.TestCase):
    def test_student_headers(self):
        m = auto_map_headers("students", ["الاسم", "رقم الهاتف", "المعلمة", "الشهر"])
        self.assertEqual(m["name"], "الاسم")
        self.assertEqual(m["phone"], "رقم الهاتف")
        self.assertEqual(m["teacher"], "المعلمة")

    def test_no_column_is_mapped_twice(self):
        m = auto_map_headers("students", ["الاسم", "الاسم"])
        self.assertEqual(len(set(m.values())), len(m))


if __name__ == "__main__":
    unittest.main()


class ImportCycle(StoreTestCase):
    CSV = "الاسم,النسبة\nمعلمة أ,70\nمعلمة ب,65\n".encode("utf-8-sig")

    def setUp(self):
        super().setUp()
        self.bk = self.home / "backups"
        self.bk.mkdir()

    def _commit(self, **kw):
        from alahd_erp.services.import_service import commit_import
        return commit_import(self.store, self.CSV, "t.csv", "teachers", backup_folder=self.bk, **kw)

    def test_preview_is_non_destructive(self):
        from alahd_erp.services.import_service import preview_import_file
        before = self.store.rev
        p = preview_import_file(self.CSV, "t.csv", "teachers", self.store.export_state())
        self.assertEqual(p["summary"]["newCount"], 2)
        self.assertEqual(p["errors"], [])
        self.assertEqual(self.store.rev, before)
        self.assertEqual(self.store.export_state()["teachers"], [])

    def test_commit_creates_teachers_with_percentages(self):
        r = self._commit()
        self.assertTrue(r["ok"])
        self.assertEqual(r["committed"], 2)
        st = self.store.export_state()
        self.assertEqual(st["teachers"], ["معلمة أ", "معلمة ب"])
        # `pct` is the key wage calculations and the UI read; `defaultPct` is kept in sync for the importer.
        self.assertEqual(st["teacherMeta"]["معلمة أ"]["pct"], 70)
        self.assertEqual(st["teacherMeta"]["معلمة ب"]["pct"], 65)
        self.assertEqual(st["teacherMeta"]["معلمة ب"]["defaultPct"], 65)

    def test_add_only_skips_existing(self):
        self._commit()
        rev = self.store.rev
        r = self._commit(mode="add_only")
        self.assertEqual(r["committed"], 0)
        self.assertEqual(r["summary"]["duplicateCount"], 2)
        self.assertEqual(self.store.rev, rev)

    def test_undo_restores_pre_import_state(self):
        from alahd_erp.services.import_service import undo_last_import
        self._commit()
        u = undo_last_import(self.store, self.bk, force=True)
        self.assertTrue(u["ok"])
        self.assertEqual(self.store.export_state()["teachers"], [])

    def test_undo_warns_when_later_edits_exist(self):
        from alahd_erp.services.import_service import undo_last_import
        self._commit()
        self.store.upsert_teacher("معلمة ج", {"pct": 50})
        u = undo_last_import(self.store, self.bk)
        self.assertFalse(u["ok"])
        self.assertTrue(u["warning"])
        self.assertIn("معلمة ج", self.store.export_state()["teachers"])

    def test_missing_required_column_is_reported(self):
        from alahd_erp.services.import_service import preview_import_file
        bad = "foo,bar\n1,2\n".encode()
        p = preview_import_file(bad, "t.csv", "teachers", self.store.export_state())
        self.assertTrue(any("name" in e["reason"] or "الاسم" in e["reason"] for e in p["errors"]))


class FlexibleWageAndPctTests(StoreTestCase):
    def test_inline_pct_and_custom_wage_are_editable_and_resettable(self):
        self.store.upsert_teacher("معلمة مرنة", {"pct": 70})
        self.store.upsert_student({
            "id": "stu_test_1",
            "name": "طالبة تجربة",
            "phone": "01012345678",
            "teacher": "معلمة مرنة",
            "type": "باقة 8",
            "pct": 70,
        })
        sid = "stu_test_1" 
        # Patch month with custom pct and customWage overriding automatic math (170 * 60% = 102, but customWage = 105)
        self.store.upsert_student_month(sid, "2026-09", {
            "value": 170,
            "pct": 60,
            "customWage": 105,
            "paymentDate": "2026-09-01",
            "actualStartDate": "2026-09-01",
            "endDate": "2026-09-30",
        })
        st = self.store.export_state()
        s_now = next(s for s in st["students"] if s["id"] == sid)
        self.assertEqual(s_now["pct"], 60)
        self.assertEqual(s_now["months"]["2026-09"]["customWage"], 105)

        # Reset customWage to None -> returns to automatic percentage calculation
        self.store.upsert_student_month(sid, "2026-09", {"customWage": None, "pct": 75})
        st2 = self.store.export_state()
        s_after = next(s for s in st2["students"] if s["id"] == sid)
        self.assertEqual(s_after["pct"], 75)
        self.assertIsNone(s_after["months"]["2026-09"]["customWage"])


class UserControlledPartialAndForceImportTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.bk = self.home / "backups"
        self.bk.mkdir()

    def test_partial_selection_and_force_import_on_inconsistent_rows(self):
        from alahd_erp.services.import_service import commit_import, preview_import_file
        csv_data = (
            "الاسم,المعلمة,الشهر,قيمة الاشتراك,نسبة المعلمة,أجر المعلمة\n"
            "طالبة أولى,خديجة أحمد,شهر 9,170,60%,105\n"
            "طالبة ثانية,خديجة أحمد,شهر 9,لم يتم الدفع,60%,لم يتم الدفع\n"
            "طالبة ثالثة,عائشة,شهر 9,200,60%,تم الاستلام\n"
        ).encode("utf-8-sig")

        prev = preview_import_file(csv_data, "sep.csv", "students", self.store.export_state(), auto_create_refs=True)
        self.assertEqual(prev["summary"]["totalRows"], 3)

        # Partial import: select only row index 0 and row index 2
        res_partial = commit_import(
            self.store,
            csv_data,
            "sep.csv",
            "students",
            backup_folder=self.bk,
            auto_create_refs=True,
            selected_rows=["p:0", "p:2"],
            force_import=True,
        )
        self.assertTrue(res_partial["ok"])
        self.assertEqual(res_partial["added"], 2)
        st = self.store.export_state()
        self.assertEqual(len(st["students"]), 2)
        s1 = next(s for s in st["students"] if s["name"] == "طالبة أولى")
        self.assertEqual(s1["months"]["2026-09"]["value"], 170)
        self.assertEqual(s1["months"]["2026-09"]["customWage"], 105)
        self.assertEqual(s1["adminStatus"], "منتهي الاشتراك")
        self.assertEqual(s1["months"]["2026-09"]["endDate"], "2026-09-30")


class BundledAcademyWorkbooksTests(StoreTestCase):
    def test_ensure_academy_workbooks_loaded_integrates_both_excel_files(self):
        from alahd_erp.domain.academy_data_loader import ensure_academy_workbooks_loaded
        from alahd_erp.domain.finance_engine import compute_month_stats

        res = ensure_academy_workbooks_loaded(self.store)
        self.assertTrue(res["loaded"])
        self.assertEqual(res["studentsCount"], 110)
        self.assertEqual(res["teachersCount"], 8)

        st = self.store.export_state()
        sep_students = [s for s in st["students"] if s.get("code", "").startswith("SEP-")]
        dash_students = [s for s in st["students"] if s.get("code", "").startswith("STU-")]
        self.assertEqual(len(sep_students), 47)
        self.assertEqual(len(dash_students), 63)
        self.assertTrue(all(s.get("adminStatus") == "منتهي الاشتراك" for s in sep_students))
        self.assertTrue(all(s["months"]["2026-09"]["endDate"] == "2026-09-30" for s in sep_students))

        ms9 = compute_month_stats(st["students"], "2026-09", installments=st["installments"], teacher_meta=st["teacherMeta"])
        self.assertEqual(ms9["total"]["subs"], 9300)
        self.assertEqual(ms9["total"]["collected"], 9300)
        self.assertEqual(ms9["total"]["wages"], 5598)

        ms10 = compute_month_stats(st["students"], "2026-10", installments=st["installments"], teacher_meta=st["teacherMeta"])
        self.assertEqual(ms10["total"]["subs"], 9445)
        self.assertEqual(ms10["total"]["collected"], 9270)
        self.assertEqual(ms10["total"]["uncollected"], 175)
        self.assertEqual(ms10["total"]["wages"], 6648)

        ms11 = compute_month_stats(st["students"], "2026-11", installments=st["installments"], teacher_meta=st["teacherMeta"])
        self.assertEqual(ms11["total"]["subs"], 6125)
        self.assertEqual(ms11["total"]["collected"], 5575)
        self.assertEqual(ms11["total"]["uncollected"], 550)
        self.assertEqual(ms11["total"]["wages"], 4333)
