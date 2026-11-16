"""HTTP status-code tests for the two Phase-2 integrity guards: closed-month freeze and empty-collection guard.

STATUS: written, NOT EXECUTED in the authoring sandbox (no `fastapi`/`httpx`, no network) - they skip like the rest of
`test_http_api.py`. What WAS verified there, without FastAPI:
  * the real `_handle_domain_or_store_error` source was extracted from server.py by AST and run with a stub
    `HTTPException`: MonthLockedError -> (400, {message, code: "MONTH_LOCKED", month}); EmptyCollectionError (a
    ValueError) -> (400, str); KeyError -> 404; unknown exception -> re-raised (generic 500 handler);
  * every payload below passes `validate_student_record` / `validate_month_entry` / `validate_state`;
  * all ten locked-month writes raise MonthLockedError at Store level and leave rev, state and audit untouched.
Run on a networked machine:  python -m unittest tests.test_http_integrity -v

Contract documented (not changed): a locked month is HTTP **400** with `detail.code == "MONTH_LOCKED"`. The docstring
on MonthLockedError mentions 423, but 423 is never returned; the test pins the real behaviour.
"""
from __future__ import annotations

import unittest

from tests.test_http_api import HAVE_HTTP, HttpBase

T = "معلمة أ"
STUDENT = {"id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": T, "type": "8 حلق", "pct": 70,
           "months": {"2026-10": {"value": 500}, "2026-11": {"value": 500}}}
RECEIPT = {"id": "i1", "studentId": "s1", "month": "2026-10", "amount": 500, "paymentDate": "2026-10-05", "receiptNo": "R1"}
PAYOUT = {"id": "p1", "teacher": T, "month": "2026-10", "amount": 200, "date": "2026-10-20"}
TX = {"id": "x1", "type": "expense", "category": "rent", "amount": 300, "date": "2026-10-03", "month": "2026-10"}


class _Seeded(HttpBase):
    """Teacher, student, one receipt/payout/transaction in 2026-10, all created through the public API."""

    def setUp(self) -> None:
        super().setUp()
        for path, body in (("/api/teachers", {"name": T, "meta": {"pct": 70}}), ("/api/students", STUDENT),
                           ("/api/installments", RECEIPT), ("/api/payouts", PAYOUT), ("/api/transactions", TX)):
            r = self.post(path, body)
            self.assertEqual(r.status_code, 200, f"setup {path}: {r.status_code} {r.text}")

    def snapshot(self) -> dict:
        s = self.get("/api/sync").json()
        return {"rev": s["rev"], "installments": s["installments"], "payouts": s["payouts"],
                "transactions": s["transactions"], "students": s["students"]}

    def call(self, method: str, path: str, body=None):
        return self.client.request(method, path, json=body, headers=self.h)


@unittest.skipUnless(HAVE_HTTP, "fastapi/httpx not installed")
class ClosedMonthStatusCodes(_Seeded):
    def setUp(self) -> None:
        super().setUp()
        r = self.post("/api/months/2026-10/close", {"note": ""})
        self.assertEqual(r.status_code, 200, r.text)

    def test_close_is_listed(self):
        months = [m["month"] for m in self.get("/api/months/closed").json()["closedMonths"]]
        self.assertEqual(months, ["2026-10"])

    def test_every_write_into_a_closed_month_is_400_month_locked_and_changes_nothing(self):
        before = self.snapshot()
        cases = [
            ("move receipt out", "POST", "/api/installments", {**RECEIPT, "month": "2026-11", "paymentDate": "2026-11-05"}),
            ("new receipt in", "POST", "/api/installments", {**RECEIPT, "id": "i2", "receiptNo": "R2", "amount": 10}),
            ("delete receipt", "DELETE", "/api/installments/i1", None),
            ("move payout out", "POST", "/api/payouts", {**PAYOUT, "month": "2026-11", "date": "2026-11-02"}),
            ("new payout in", "POST", "/api/payouts", {**PAYOUT, "id": "p2", "amount": 5}),
            ("delete payout", "DELETE", "/api/payouts/p1", None),
            ("move transaction out", "POST", "/api/transactions", {**TX, "month": "2026-11", "date": "2026-11-03"}),
            ("new transaction in", "POST", "/api/transactions", {**TX, "id": "x2", "type": "revenue", "amount": 5}),
            ("delete transaction", "DELETE", "/api/transactions/x1", None),
            ("patch month", "PATCH", "/api/students/s1/months/2026-10", {"value": 999}),
            ("delete month", "DELETE", "/api/students/s1/months/2026-10", None),
            ("student upsert touching month", "POST", "/api/students",
             {**STUDENT, "months": {"2026-10": {"value": 777}, "2026-11": {"value": 500}}}),
            ("full-state sync touching month", "POST", "/api/sync",
             {"students": [{**STUDENT, "months": {"2026-10": {"value": 777}, "2026-11": {"value": 500}}}]}),
        ]
        for label, method, path, body in cases:
            with self.subTest(label):
                r = self.call(method, path, body)
                self.assertEqual(r.status_code, 400, r.text)          # NOT 423, NOT 500
                detail = r.json()["detail"]
                self.assertIsInstance(detail, dict)
                self.assertEqual(detail["code"], "MONTH_LOCKED")
                self.assertEqual(detail["month"], "2026-10")
                self.assertTrue(detail["message"])
                self.assertNoLeak(r)
        self.assertEqual(self.snapshot(), before)                       # incl. rev: rejected writes leave no trace

    def test_open_month_is_still_editable(self):
        r = self.call("PATCH", "/api/students/s1/months/2026-11", {"value": 650})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.post("/api/installments", {**RECEIPT, "id": "i9", "month": "2026-11", "receiptNo": "R9", "amount": 100,
                                            "paymentDate": "2026-11-05"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.call("DELETE", "/api/installments/i9").status_code, 200)

    def test_reopen_allows_editing_and_close_again_blocks_again(self):
        self.assertEqual(self.post("/api/months/2026-10/reopen", {"note": "fix"}).status_code, 200)
        self.assertEqual(self.call("PATCH", "/api/students/s1/months/2026-10", {"value": 600}).status_code, 200)
        self.assertEqual(self.post("/api/months/2026-10/close", {"note": ""}).status_code, 200)
        r = self.call("PATCH", "/api/students/s1/months/2026-10", {"value": 650})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (400, "MONTH_LOCKED"))

    def test_reads_and_reports_of_a_closed_month_still_work(self):
        self.assertEqual(self.get("/api/stats", params={"month": "2026-10"}).status_code, 200)
        self.assertEqual(self.get("/api/sync").status_code, 200)


@unittest.skipUnless(HAVE_HTTP, "fastapi/httpx not installed")
class EmptyCollectionGuardStatusCodes(_Seeded):
    def test_empty_list_over_existing_rows_is_400_and_changes_nothing(self):
        before = self.snapshot()
        for key in ("installments", "payouts", "transactions"):
            with self.subTest(key):
                r = self.post("/api/sync", {"students": [STUDENT], key: []})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(key, str(r.json()["detail"]))
                self.assertNoLeak(r)
        r = self.post("/api/sync", {"students": []})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.snapshot(), before)

    def test_absent_keys_leave_tables_untouched(self):
        before = self.snapshot()
        r = self.post("/api/sync", {"students": [STUDENT]})
        self.assertEqual(r.status_code, 200, r.text)
        after = self.snapshot()
        for k in ("installments", "payouts", "transactions"):
            self.assertEqual(after[k], before[k], k)

    def test_explicit_opt_in_flag_allows_emptying(self):
        r = self.post("/api/sync", {"students": [STUDENT], "installments": [], "allowEmptyCollections": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.snapshot()["installments"], [])

    def test_opt_in_must_be_literal_true(self):
        """`"true"`, 1, "yes" are NOT accepted (server compares `is True`)."""
        for flag in ("true", 1, "yes", None):
            with self.subTest(flag=flag):
                r = self.post("/api/sync", {"students": [STUDENT], "installments": [], "allowEmptyCollections": flag})
                self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(len(self.snapshot()["installments"]), 1)

    def test_malformed_bodies_are_400_or_413_not_500(self):
        self.assertEqual(self.client.post("/api/sync", content=b"{not json", headers=self.h).status_code, 400)
        self.assertEqual(self.client.post("/api/sync", content=b"", headers=self.h).status_code, 413)


if __name__ == "__main__":
    unittest.main()
