import datetime
import sqlite3
import time
import unittest

from tests._helpers import StoreTestCase
from alahd_erp.domain.models import validate_state
from alahd_erp.infrastructure.database import MonthLockedError, Store


class RevisionCounter(StoreTestCase):
    def test_rev_monotonic_across_restart_even_after_audit_trim(self):
        """rev used to be COUNT(audit) -> capped at 5000 and went backwards after a restart."""
        self.seed()
        with self.store.lock:
            for i in range(5100):
                self.store.log("t", "noop", str(i))
        before = self.store.rev
        self.assertGreater(before, 5000)
        after = self.reopen().rev
        self.assertEqual(after, before)

    def test_rev_not_bumped_by_rolled_back_write(self):
        """log() bumps rev before COMMIT; a later failure + ROLLBACK must not leave rev ahead of the DB."""
        self.seed()
        before = self.store.rev
        with self.store.lock:
            self.store.db.execute("BEGIN")
            self.store.log("t", "will-be-rolled-back")
            self.assertEqual(self.store.rev, before + 1)
            self.store._rollback()
        self.assertEqual(self.store.rev, before)
        self.assertEqual(self.store.rev, self.reopen().rev)

    def test_failed_business_write_leaves_rev_untouched(self):
        self.seed()
        before = self.store.rev
        with self.assertRaises(Exception):
            self.store.upsert_installment(
                {"id": "x", "receiptNo": "R1", "studentId": "ghost-student", "month": "2026-11",
                 "amount": 100, "paymentDate": "2026-11-05"})
        self.assertEqual(self.store.rev, before)

    def test_rev_strictly_increases_after_restore(self):
        self.seed()
        backup = self.home / "alahd_manual_old.sqlite3"
        self.store.backup_to(backup)
        for i in range(10):
            self.store.log("t", "noop", str(i))
        live = self.store.rev
        self.store.restore_from(backup)
        self.assertGreater(self.store.rev, live)
        self.assertGreater(self.reopen().rev, live)


class MonthLocking(StoreTestCase):
    def _lock(self, month="2026-11"):
        self.store.close_month(month, note="test")

    def test_upsert_student_cannot_edit_closed_month(self):
        self.seed()
        self._lock()
        with self.assertRaises(MonthLockedError):
            self.store.upsert_student({
                "id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": "معلمة أ", "pct": 70,
                "months": {"2026-11": {"value": 9999, "actualStartDate": "2026-11-02", "endDate": "2026-11-28"}},
            })
        self.assertEqual(self.store.get_student("s1")["months"]["2026-11"]["value"], 500)

    def test_upsert_student_cannot_drop_closed_month(self):
        self.seed()
        self._lock()
        with self.assertRaises(MonthLockedError):
            self.store.upsert_student({"id": "s1", "name": "طالبة ١", "teacher": "معلمة أ", "pct": 70, "months": {}})
        self.assertIn("2026-11", self.store.get_student("s1")["months"])

    def test_unchanged_student_resave_still_allowed(self):
        self.seed()
        self._lock()
        stu = self.store.get_student("s1")
        stu["name"] = "طالبة ١ (معدلة)"      # non-financial edit, same months
        self.store.upsert_student(stu)
        self.assertEqual(self.store.get_student("s1")["name"], "طالبة ١ (معدلة)")

    def test_open_month_still_editable_while_other_is_closed(self):
        self.seed()
        self._lock("2026-11")
        stu = self.store.get_student("s1")
        stu["months"]["2026-12"] = {"value": 400}
        self.store.upsert_student(stu)
        self.assertEqual(self.store.get_student("s1")["months"]["2026-12"]["value"], 400)

    def test_replace_all_cannot_modify_closed_month(self):
        self.seed()
        self._lock()
        state = self.store.export_state()
        state["students"][0]["months"]["2026-11"]["value"] = 1
        with self.assertRaises(MonthLockedError):
            self.store.replace_all(validate_state(state))

    def test_replace_all_roundtrip_of_unchanged_state_is_allowed(self):
        self.seed()
        self.store.upsert_installment({"id": "i1", "receiptNo": "R1", "studentId": "s1", "month": "2026-11",
                                       "amount": 500, "paymentDate": "2026-11-05"})
        self._lock()
        self.store.replace_all(validate_state(self.store.export_state()))   # must not raise

    def test_delete_student_blocked_when_it_has_closed_month(self):
        self.seed()
        self._lock()
        with self.assertRaises(MonthLockedError):
            self.store.delete_student("s1")
        self.assertIsNotNone(self.store.get_student("s1"))

    def test_delete_teacher_cannot_erase_closed_month_payout(self):
        self.seed()
        self.store.upsert_payout({"id": "p1", "teacher": "معلمة أ", "month": "2026-11", "amount": 350, "date": "2026-11-28"})
        self._lock()
        # Phase-2 decision (archive-not-delete): a teacher with payouts is archived, never erased,
        # so the closed-month payout (and the student's teacher link) must survive untouched.
        res = self.store.delete_teacher("معلمة أ")
        self.assertTrue(res["archived"])
        self.assertFalse(res["deleted"])
        self.assertEqual(len(self.store.export_state()["payouts"]), 1)
        self.assertEqual(self.store.get_student("s1")["teacher"], "معلمة أ")


class Reports(StoreTestCase):
    def test_future_receipt_excluded_by_default_everywhere(self):
        today = datetime.date.today()
        mk = today.strftime("%Y-%m")
        future = (today + datetime.timedelta(days=40)).isoformat()
        self.seed(month=mk, value=500)
        self.store.upsert_installment({"id": "i1", "receiptNo": "R1", "studentId": "s1", "month": mk,
                                       "amount": 500, "paymentDate": future})
        self.assertEqual(self.store.stats(mk)["total"]["collected"], 0)
        self.assertEqual(self.store.accounting_summary(mk)["collected"], 0)
        self.assertEqual(self.store.accrual_report(mk)["totalCollected"], 0)
        self.assertEqual(self.store.collection_report(mk)["totalCollected"], 0)

    def test_finance_only_export_gives_identical_reports(self):
        self.store.load_demo_data()
        mk = sorted({m for s in self.store.export_state()["students"] for m in s["months"]})[-1]
        full = self.store.export_state()
        light = self.store.export_state(finance_only=True)
        for key in ("students", "teacherMeta", "settings", "installments", "payouts", "transactions"):
            self.assertEqual(full[key], light[key], key)
        self.assertNotIn("sessions", light)
        self.assertIn("total", self.store.stats(mk))


class Backups(StoreTestCase):
    def test_unreachable_extra_backup_folder_logs_warning_and_does_not_abort_primary_backup(self):
        blocker = self.home / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")          # a file used as a parent folder -> mkdir fails on Windows and Linux
        self.store.update_settings({"extraBackupFolder": str(blocker / "sub")}, "t", None)
        dest = self.home / "backups" / "alahd_manual_f15.sqlite3"
        with self.assertLogs("alahd_erp.infrastructure.database", level="WARNING") as cm:
            self.store.backup_to(dest)
        self.assertTrue(dest.is_file())
        self.assertTrue(any("extraBackupFolder" in msg for msg in cm.output))

    def test_mirror_rotation_is_per_kind(self):
        extra = self.home / "mirror"
        self.store.update_settings({"extraBackupFolder": str(extra)}, "t", None)
        self.store.backup_to(self.home / "bk" / "alahd_daily_20260101.sqlite3")
        for i in range(60):          # many auto backups must not evict the daily one
            self.store.backup_to(self.home / "bk" / f"alahd_auto_20260102_{i:06d}.sqlite3")
        self.assertTrue((extra / "alahd_daily_20260101.sqlite3").exists())
        self.assertLessEqual(len(list(extra.glob("alahd_auto_*.sqlite3"))), 48)


if __name__ == "__main__":
    unittest.main()
