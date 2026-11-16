"""Data-integrity tests: closed-month freeze, teacher archive-not-delete, empty-collection guard.

Every scenario was first reproduced as a real defect against the CP00 baseline (see docs/IMPLEMENTATION_LOG.md);
these tests fail on that baseline and pass now. Each case uses its own isolated store.
"""
from __future__ import annotations

import unittest

from tests._helpers import StoreTestCase  # noqa: F401  (also puts the repo root on sys.path)
from alahd_erp.infrastructure.database import EmptyCollectionError, MonthLockedError

T = "معلمة أ"


def _student(sid="s1", teacher=T, months=None):
    return {"id": sid, "name": "طالبة " + sid, "teacher": teacher, "type": "x", "pct": 70,
            "months": months if months is not None else {"2026-10": {"value": 500}, "2026-11": {"value": 500}}}


class _Base(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_teacher(T, {"pct": 70})
        self.store.upsert_student(_student())

    def counts(self):
        st = self.store.export_state()
        return len(st["installments"]), len(st["payouts"]), len(st["transactions"]), len(st["students"])


class ClosedMonthRecordsCannotBeMovedOrDeleted(_Base):
    """Upserts that reuse an existing id must check the month the row lives in NOW, not just the new one."""

    def _rows(self):
        self.store.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-10", "amount": 500,
                                       "paymentDate": "2026-10-05", "receiptNo": "R1"})
        self.store.upsert_payout({"id": "p1", "teacher": T, "month": "2026-10", "amount": 200, "date": "2026-10-20"})
        self.store.upsert_transaction({"id": "x1", "type": "expense", "category": "rent", "amount": 300,
                                       "date": "2026-10-03", "month": "2026-10"})
        self.store.close_month("2026-10")

    def test_receipt_cannot_be_moved_out_of_closed_month(self):
        self._rows()
        with self.assertRaises(MonthLockedError):
            self.store.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 500,
                                           "paymentDate": "2026-11-05", "receiptNo": "R1"})
        inst = self.store.export_state()["installments"]
        self.assertEqual([(i["id"], i["month"]) for i in inst], [("i1", "2026-10")])

    def test_payout_cannot_be_moved_out_of_closed_month(self):
        self._rows()
        with self.assertRaises(MonthLockedError):
            self.store.upsert_payout({"id": "p1", "teacher": T, "month": "2026-11", "amount": 200, "date": "2026-11-02"})
        self.assertEqual([(p["id"], p["month"]) for p in self.store.export_state()["payouts"]], [("p1", "2026-10")])

    def test_transaction_cannot_be_moved_out_of_closed_month(self):
        self._rows()
        with self.assertRaises(MonthLockedError):
            self.store.upsert_transaction({"id": "x1", "type": "expense", "category": "rent", "amount": 300,
                                           "date": "2026-11-03", "month": "2026-11"})
        self.assertEqual([(t["id"], t["month"]) for t in self.store.export_state()["transactions"]], [("x1", "2026-10")])

    def test_closed_month_rows_cannot_be_deleted(self):
        self._rows()
        for name, call in (
            ("receipt", lambda: self.store.delete_installment("i1")),
            ("payout", lambda: self.store.delete_payout("p1")),
            ("transaction", lambda: self.store.delete_transaction("x1")),
            ("month row", lambda: self.store.delete_student_month("s1", "2026-10")),
        ):
            with self.subTest(name):
                with self.assertRaises(MonthLockedError):
                    call()

    def test_open_month_remains_fully_editable_and_reopen_restores_edit(self):
        self._rows()
        self.store.upsert_installment({"id": "i9", "studentId": "s1", "month": "2026-11", "amount": 100,
                                       "paymentDate": "2026-11-05", "receiptNo": "R9"})
        self.store.upsert_installment({"id": "i9", "studentId": "s1", "month": "2026-11", "amount": 150,
                                       "paymentDate": "2026-11-05", "receiptNo": "R9"})
        self.store.reopen_month("2026-10")
        self.store.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 500,
                                       "paymentDate": "2026-11-05", "receiptNo": "R1"})   # allowed after reopen


class TeacherArchiveNotDelete(_Base):
    def test_unreferenced_teacher_is_really_deleted(self):
        self.store.upsert_teacher("فارغة", {"pct": 60})
        res = self.store.delete_teacher("فارغة")
        self.assertEqual((res["deleted"], res["archived"]), (True, False))
        self.assertNotIn("فارغة", self.store.export_state()["teachers"])

    def test_teacher_with_payout_only_is_archived_and_payout_kept(self):
        self.store.upsert_student(_student("s1", teacher=""))      # no student references her any more
        self.store.upsert_payout({"id": "p1", "teacher": T, "month": "2026-11", "amount": 100, "date": "2026-11-10"})
        res = self.store.delete_teacher(T)
        self.assertEqual((res["deleted"], res["archived"]), (False, True))
        self.assertEqual(res["references"], {"payouts": 1})
        st = self.store.export_state()
        self.assertEqual(len(st["payouts"]), 1)
        self.assertIn(T, st["teachers"])
        self.assertTrue(st["teacherMeta"][T]["archived"])
        self.assertEqual(st["teacherMeta"][T]["pct"], 70)           # finance rule data untouched

    def test_teacher_with_students_keeps_students_and_closed_month_attribution(self):
        self.store.upsert_payout({"id": "p1", "teacher": T, "month": "2026-11", "amount": 100, "date": "2026-11-10"})
        self.store.close_month("2026-10")
        before_stats = self.store.stats("2026-10")
        res = self.store.delete_teacher(T)
        self.assertTrue(res["archived"])
        self.assertEqual(self.store.get_student("s1")["teacher"], T)
        self.assertEqual(self.counts()[1], 1)                        # open-month payout not cascaded away
        self.assertEqual(self.store.stats("2026-10"), before_stats)  # closed month report unchanged

    def test_reupsert_keeps_archived_until_explicit_restore(self):
        self.store.delete_teacher(T)
        self.store.upsert_teacher(T, {"pct": 65})
        self.assertTrue(self.store.export_state()["teacherMeta"][T]["archived"])
        self.assertEqual(self.store.export_state()["teacherMeta"][T]["pct"], 65)
        self.store.upsert_teacher(T, {"archived": False})
        meta = self.store.export_state()["teacherMeta"][T]
        self.assertNotIn("archived", meta)
        self.assertNotIn("archivedAt", meta)

    def test_missing_teacher_is_a_noop(self):
        res = self.store.delete_teacher("غير موجودة")
        self.assertEqual((res["deleted"], res["archived"]), (False, False))


class EmptyCollectionGuard(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.store.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 300,
                                       "paymentDate": "2026-11-05", "receiptNo": "R1"})
        self.store.upsert_installment({"id": "i2", "studentId": "s1", "month": "2026-11", "amount": 200,
                                       "paymentDate": "2026-11-06", "receiptNo": "R2"})
        self.store.upsert_payout({"id": "p1", "teacher": T, "month": "2026-11", "amount": 100, "date": "2026-11-10"})
        self.store.upsert_transaction({"id": "x1", "type": "expense", "category": "c", "amount": 5,
                                       "date": "2026-11-02"})
        self.student = _student(months={"2026-11": {"value": 500}})

    def test_empty_lists_are_rejected_and_change_nothing(self):
        start = self.counts()
        for key in ("installments", "payouts", "transactions"):
            with self.subTest(key):
                with self.assertRaises(EmptyCollectionError):
                    self.store.replace_all({"students": [self.student], key: []})
                self.assertEqual(self.counts(), start)
        with self.assertRaises(EmptyCollectionError):
            self.store.replace_all({"students": []})
        self.assertEqual(self.counts(), start)
        self.assertFalse(self.store.db.in_transaction)               # rollback left no open transaction
        self.store.upsert_transaction({"id": "x2", "type": "expense", "category": "c", "amount": 1, "date": "2026-11-03"})

    def test_absent_keys_leave_tables_untouched(self):
        start = self.counts()
        self.store.replace_all({"students": [self.student]})
        self.assertEqual(self.counts(), start)

    def test_explicit_opt_in_allows_emptying(self):
        self.store.replace_all({"students": [self.student], "installments": []}, allow_empty=True)
        self.assertEqual(self.counts()[0], 0)

    def test_non_empty_replacement_still_replaces(self):
        keep = {"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 300, "paymentDate": "2026-11-05",
                "receiptNo": "R1"}
        self.store.replace_all({"students": [self.student], "installments": [keep]})
        self.assertEqual([i["id"] for i in self.store.export_state()["installments"]], ["i1"])

    def test_legacy_paid_month_without_installments_key_keeps_real_receipts(self):
        legacy = _student(months={"2026-11": {"value": 500, "paidStatus": "paid", "paymentDate": "2026-11-07"}})
        self.store.replace_all({"students": [legacy]})
        ids = sorted(i["id"] for i in self.store.export_state()["installments"])
        self.assertEqual(ids, ["i1", "i2"])                          # real receipts kept, month already covered
        self.assertEqual(sum(i["amount"] for i in self.store.export_state()["installments"]), 500)


if __name__ == "__main__":
    unittest.main()
