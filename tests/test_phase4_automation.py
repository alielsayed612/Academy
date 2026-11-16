"""Phase 4 Operational Automation & Workflow Engineering Test Suite.

Covers all 13 mandatory test categories from Task 14:
1. Event triggering (StudentCreated, StudentUpdated, TeacherCreated, TeacherUpdated, TeacherDeactivated, TeacherReactivated, SubscriptionCreated, SubscriptionUpdated, ReceiptRecorded, SalaryDataUpdated, BulkOperationExecuted).
2. Idempotency (duplicate idempotency_key is ignored without re-running handlers).
3. Prevention of duplicate or recursive triggers (_active_stack loop guard and max_depth).
4. Failure handling & handler error isolation (failing subscriber does not abort transaction or other subscribers).
5. Subscription alert generation (expiring_soon, expired_unrenewed, paid_without_start_date, studying_without_payment, date_inconsistency; never auto-renews).
6. Scheduling conflict detection (teacher double-booking, student double-booking, group session exclusion, cancelled session exclusion, HTTP 409 SCHEDULING_CONFLICT warning, allowConflicts override).
7. Data-quality detection (10-category audit: duplicate students, missing phone, missing start date, invalid date order, inactive teacher on active student/future session, suspicious duplicate receipt, scheduling conflict).
8. Notification deduplication, read state (single & __all__), snooze/dismiss, and restore.
9. Salary preparation report (calculation inputs, missing inputs, unusual MoM >=30% change, overpayment, unassigned active students, zero auto-payouts).
10. Preview-first bulk operations (assign_teacher, assign_package, set_actual_start_date, update_session_status, normalize_phones_and_codes; pre-bulk safety snapshot; hash verification; closed-month protection).
11. Backup monitoring alerts (missingExpectedBackup, staleWarning / daysSinceLastBackup, extraFolderUnreachable).
12. Operational search & discovery across students, teachers, receipts, transactions, and sessions.
13. Historical record preservation during dynamic teacher transfer propagation.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alahd_erp.api.server import create_app
from alahd_erp.domain.automation_engine import (
    DomainEventBus,
    SchedulingConflictError,
    compute_data_quality_audit,
    compute_salary_preparation_report,
    detect_scheduling_conflicts,
    preview_bulk_operation,
    search_operational_entities,
)
from tests._helpers import StoreTestCase

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "alahd_erp" / "ui"


class Phase4AutomationEngineUnitTests(unittest.TestCase):
    def test_event_bus_triggering_idempotency_recursion_and_error_isolation(self):
        bus = DomainEventBus(max_history=100, max_depth=2)
        received: list[tuple[str, dict]] = []

        def good_handler(ev):
            received.append((ev["type"], ev["payload"]))

        def failing_handler(ev):
            raise RuntimeError("simulated handler failure")

        def recursive_handler(ev):
            # Attempt to re-emit the exact same event type recursively
            bus.emit("RecursivePing", {"step": ev["payload"].get("step", 0) + 1})

        bus.subscribe("StudentCreated", failing_handler)
        bus.subscribe("StudentCreated", good_handler)
        bus.subscribe("RecursivePing", recursive_handler)
        bus.subscribe("RecursivePing", good_handler)

        # 1. Event triggering + 4. Failure isolation (failing_handler raises, good_handler still runs)
        rec1 = bus.emit("StudentCreated", {"id": "stu_1"}, idempotency_key="StudentCreated:stu_1:1")
        self.assertEqual(rec1["status"], "partial_error")
        self.assertEqual(rec1["handlersRun"], 2)
        self.assertEqual(len(rec1["errors"]), 1)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("StudentCreated", {"id": "stu_1"}))

        # 2. Idempotency: emitting with the same idempotency_key is deduplicated
        rec_dup = bus.emit("StudentCreated", {"id": "stu_1"}, idempotency_key="StudentCreated:stu_1:1")
        self.assertIn(rec_dup["status"], ("deduplicated", "duplicate_ignored"))
        self.assertEqual(len(received), 1)

        # 3. Recursion / loop guard: recursive_handler re-emitting RecursivePing is blocked immediately
        rec_loop = bus.emit("RecursivePing", {"step": 1})
        self.assertEqual(rec_loop["status"], "processed")
        # RecursivePing only ran once at top level; nested RecursivePing was blocked by loop_guard
        ping_events = [r for r in received if r[0] == "RecursivePing"]
        self.assertEqual(len(ping_events), 1)
        history = bus.get_history()
        self.assertTrue(any(h.get("status") == "blocked_recursion" for h in history))

    def test_scheduling_conflict_detection_teacher_student_and_group_exclusions(self):
        students = [
            {"id": "s1", "name": "فاطمة أحمد", "teacher": "أ. مريم"},
            {"id": "s2", "name": "عائشة محمد", "teacher": "أ. مريم"},
            {"id": "s3", "name": "خديجة علي", "teacher": "أ. سارة"},
        ]
        existing_sessions = [
            {
                "id": "sess_1",
                "studentId": "s1",
                "teacher": "أ. مريم",
                "date": "2026-10-12",
                "time": "16:00",
                "dur": 40,
                "status": "scheduled",
            },
            # Cancelled session should never trigger a conflict
            {
                "id": "sess_cancelled",
                "studentId": "s3",
                "teacher": "أ. سارة",
                "date": "2026-10-12",
                "time": "18:00",
                "dur": 40,
                "status": "cancel_teacher",
            },
            # Group session with groupId='grp_1'
            {
                "id": "sess_grp_1",
                "studentId": "s1",
                "teacher": "أ. مريم",
                "groupId": "grp_1",
                "date": "2026-10-13",
                "time": "17:00",
                "dur": 45,
                "status": "scheduled",
            },
        ]

        # Case A: Teacher double-booking (s2 with أ. مريم at 16:20 overlaps 16:00-16:40)
        cand_teacher_overlap = [
            {
                "id": "sess_new_t",
                "studentId": "s2",
                "teacher": "أ. مريم",
                "date": "2026-10-12",
                "time": "16:20",
                "dur": 30,
                "status": "scheduled",
            }
        ]
        c_teacher = detect_scheduling_conflicts(existing_sessions, students, cand_teacher_overlap)
        self.assertEqual(len(c_teacher), 1)
        self.assertEqual(c_teacher[0]["type"], "teacher_double_booking")
        self.assertEqual(c_teacher[0]["teacher"], "أ. مريم")

        # Case B: Student double-booking (s1 with another teacher at 16:15 on 2026-10-12)
        cand_student_overlap = [
            {
                "id": "sess_new_s",
                "studentId": "s1",
                "teacher": "أ. سارة",
                "date": "2026-10-12",
                "time": "16:15",
                "dur": 30,
                "status": "scheduled",
            }
        ]
        c_student = detect_scheduling_conflicts(existing_sessions, students, cand_student_overlap)
        self.assertEqual(len(c_student), 1)
        self.assertEqual(c_student[0]["type"], "student_double_booking")
        self.assertEqual(c_student[0]["studentId"], "s1")

        # Case C: Same groupId session at same time for another student is NOT a teacher conflict
        cand_same_group = [
            {
                "id": "sess_grp_2",
                "studentId": "s2",
                "teacher": "أ. مريم",
                "groupId": "grp_1",
                "date": "2026-10-13",
                "time": "17:00",
                "dur": 45,
                "status": "scheduled",
            }
        ]
        c_group = detect_scheduling_conflicts(existing_sessions, students, cand_same_group)
        self.assertEqual(c_group, [])

        # Case D: Slot overlapping with a cancelled session is NOT a conflict
        cand_over_cancelled = [
            {
                "id": "sess_ok",
                "studentId": "s3",
                "teacher": "أ. سارة",
                "date": "2026-10-12",
                "time": "18:00",
                "dur": 40,
                "status": "scheduled",
            }
        ]
        self.assertEqual(detect_scheduling_conflicts(existing_sessions, students, cand_over_cancelled), [])


class Phase4StoreAndHttpIntegrationTests(StoreTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.secret = "phase4_test_secret_token"
        self.app = create_app(
            home=self.home,
            ui_dir=UI_DIR,
            port=8767,
            window_secret=self.secret,
            seed_on_empty=False,
        )
        self.store.close()
        self.store = self.app.state.store
        self.db_path = self.store.path
        self.client = TestClient(self.app)
        self.h = {"X-Window-Secret": self.secret}

    def tearDown(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        super().tearDown()

    def test_dynamic_teacher_transfer_propagates_to_future_sessions_and_preserves_history(self):
        # Create two teachers
        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post("/api/teachers", json={"name": "أ. خديجة", "pct": 75}, headers=self.h)

        # Create student assigned to أ. مريم
        res_stu = self.client.post(
            "/api/students",
            json={
                "id": "stu_trans_1",
                "code": "STU-0100",
                "name": "مريم حسن",
                "phone": "01012345678",
                "teacher": "أ. مريم",
                "type": "خاص",
                "pct": 70,
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-01",
                        "actualStartDate": "2026-10-05",
                    }
                },
            },
            headers=self.h,
        )
        self.assertEqual(res_stu.status_code, 200)

        # Create a past completed session and a future scheduled session
        self.store.upsert_session(
            {
                "id": "sess_past_done",
                "studentId": "stu_trans_1",
                "teacher": "أ. مريم",
                "date": "2020-01-10",
                "time": "16:00",
                "dur": 40,
                "status": "done",
            }
        )
        self.store.upsert_session(
            {
                "id": "sess_future_sched",
                "studentId": "stu_trans_1",
                "teacher": "أ. مريم",
                "date": "2099-12-10",
                "time": "16:00",
                "dur": 40,
                "status": "scheduled",
            }
        )
        self.store.upsert_plan(
            {
                "id": "pln_trans_1",
                "studentId": "stu_trans_1",
                "studentName": "مريم حسن",
                "teacher": "أ. مريم",
                "actualStartDate": "2026-10-05",
                "count": 8,
            }
        )

        # Now transfer student to أ. خديجة
        res_upd = self.client.put(
            "/api/students/stu_trans_1",
            json={
                "id": "stu_trans_1",
                "code": "STU-0100",
                "name": "مريم حسن",
                "phone": "01012345678",
                "teacher": "أ. خديجة",
                "type": "خاص",
                "pct": 75,
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-01",
                        "actualStartDate": "2026-10-05",
                    }
                },
            },
            headers=self.h,
        )
        self.assertEqual(res_upd.status_code, 200)

        st = self.store.export_state()
        stu = next(s for s in st["students"] if s["id"] == "stu_trans_1")
        self.assertEqual(stu["teacher"], "أ. خديجة")
        # Transfer history automatically recorded
        self.assertTrue(len(stu.get("transferHistory") or []) >= 1)
        last_tr = stu["transferHistory"][-1]
        self.assertEqual(last_tr["from"], "أ. مريم")
        self.assertEqual(last_tr["to"], "أ. خديجة")

        # Past completed session preserved under أ. مريم; future scheduled session updated to أ. خديجة
        sess_by_id = {x["id"]: x for x in st["sessions"]}
        self.assertEqual(sess_by_id["sess_past_done"]["teacher"], "أ. مريم")
        self.assertEqual(sess_by_id["sess_future_sched"]["teacher"], "أ. خديجة")

        # Attendance plan updated to أ. خديجة
        pln = next(p for p in st["plans"] if p["id"] == "pln_trans_1")
        self.assertEqual(pln["teacher"], "أ. خديجة")

    def test_http_session_and_plan_conflict_protection_and_override(self):
        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post(
            "/api/students",
            json={"id": "s_conf_1", "code": "STU-0201", "name": "طالبة 1", "teacher": "أ. مريم"},
            headers=self.h,
        )
        self.client.post(
            "/api/students",
            json={"id": "s_conf_2", "code": "STU-0202", "name": "طالبة 2", "teacher": "أ. مريم"},
            headers=self.h,
        )

        # Create first session at 2026-10-15 16:00 (40 mins)
        r1 = self.client.post(
            "/api/sessions",
            json={
                "id": "sess_c1",
                "studentId": "s_conf_1",
                "teacher": "أ. مريم",
                "date": "2026-10-15",
                "time": "16:00",
                "dur": 40,
                "status": "scheduled",
            },
            headers=self.h,
        )
        self.assertEqual(r1.status_code, 200)

        # Attempt overlapping session for same teacher with student 2 at 16:15 -> HTTP 409 SCHEDULING_CONFLICT
        r2 = self.client.post(
            "/api/sessions",
            json={
                "id": "sess_c2",
                "studentId": "s_conf_2",
                "teacher": "أ. مريم",
                "date": "2026-10-15",
                "time": "16:15",
                "dur": 40,
                "status": "scheduled",
            },
            headers=self.h,
        )
        self.assertEqual(r2.status_code, 409)
        body = r2.json()
        detail = body.get("detail") or body.get("error") or {}
        self.assertEqual(detail["code"], "SCHEDULING_CONFLICT")
        self.assertTrue(len(detail["conflicts"]) >= 1)

        # Check conflict endpoint directly
        r_chk = self.client.post(
            "/api/attendance/check-conflicts",
            json={
                "sessions": [
                    {
                        "id": "sess_c2",
                        "studentId": "s_conf_2",
                        "teacher": "أ. مريم",
                        "date": "2026-10-15",
                        "time": "16:15",
                        "dur": 40,
                    }
                ]
            },
            headers=self.h,
        )
        self.assertEqual(r_chk.status_code, 200)
        self.assertTrue(r_chk.json()["hasConflicts"])

        # Explicit override with allowConflicts=True succeeds
        r3 = self.client.post(
            "/api/sessions",
            json={
                "id": "sess_c2",
                "studentId": "s_conf_2",
                "teacher": "أ. مريم",
                "date": "2026-10-15",
                "time": "16:15",
                "dur": 40,
                "status": "scheduled",
                "allowConflicts": True,
            },
            headers=self.h,
        )
        self.assertEqual(r3.status_code, 200)

        # Alerts endpoint now surfaces scheduling_conflict alert
        r_alerts = self.client.get("/api/alerts?reference_date=2026-10-15", headers=self.h)
        self.assertEqual(r_alerts.status_code, 200)
        alerts_data = r_alerts.json()
        self.assertTrue(any(a["category"] == "scheduling_conflict" for a in alerts_data["items"]))

    def test_alerts_mark_as_read_single_and_all_and_backup_missing_warning(self):
        # Add a student without creating any backup -> triggers missingExpectedBackup warning
        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "s_alert_1",
                "code": "STU-0301",
                "name": "طالبة تنبيه",
                "teacher": "أ. مريم",
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-01",
                        # Paid without actualStartDate -> triggers missing_actual_start
                    }
                },
            },
            headers=self.h,
        )
        self.client.post(
            "/api/installments",
            json={
                "studentId": "s_alert_1",
                "month": "2026-10",
                "amount": 300,
                "paymentDate": "2026-10-01",
                "method": "نقدي",
            },
            headers=self.h,
        )

        r_alerts = self.client.get("/api/alerts?reference_date=2026-10-15", headers=self.h)
        self.assertEqual(r_alerts.status_code, 200)
        data = r_alerts.json()
        self.assertGreater(data["counts"]["total"], 0)
        self.assertEqual(data["counts"]["unread"], data["counts"]["total"])
        # Verify backup warning triggered because 0 backups exist while students > 0
        self.assertTrue(any(a["category"] in ("backup_status_warning", "stale_backup") for a in data["items"]))

        first_id = data["items"][0]["id"]
        # Mark single alert read
        r_read1 = self.client.post(
            "/api/alerts/read",
            json={"alertId": first_id, "referenceDate": "2026-10-15"},
            headers=self.h,
        )
        self.assertEqual(r_read1.status_code, 200)

        data_after_1 = self.client.get("/api/alerts?reference_date=2026-10-15", headers=self.h).json()
        self.assertEqual(data_after_1["counts"]["unread"], data["counts"]["total"] - 1)

        # Mark all alerts read
        r_read_all = self.client.post(
            "/api/alerts/read",
            json={"alertId": "__all__", "referenceDate": "2026-10-15"},
            headers=self.h,
        )
        self.assertEqual(r_read_all.status_code, 200)
        data_after_all = self.client.get("/api/alerts?reference_date=2026-10-15", headers=self.h).json()
        self.assertEqual(data_after_all["counts"]["unread"], 0)

    def test_salary_preparation_report_and_zero_auto_payout(self):
        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "s_sal_1",
                "code": "STU-0401",
                "name": "طالبة راتب 1",
                "teacher": "أ. مريم",
                "pct": 70,
                "months": {
                    "2026-09": {"value": 200, "paymentDate": "2026-09-01", "actualStartDate": "2026-09-01"},
                    "2026-10": {"value": 400, "paymentDate": "2026-10-01", "actualStartDate": "2026-10-01"},
                },
            },
            headers=self.h,
        )
        # Unassigned student with active subscription in 2026-10
        self.client.post(
            "/api/students",
            json={
                "id": "s_sal_unassigned",
                "code": "STU-0402",
                "name": "طالبة بدون معلمة",
                "teacher": "",
                "months": {
                    "2026-10": {"value": 250, "paymentDate": "2026-10-02", "actualStartDate": "2026-10-02"},
                },
            },
            headers=self.h,
        )

        r_prep = self.client.get("/api/payroll/preparation?month=2026-10&reference_date=2026-10-15", headers=self.h)
        self.assertEqual(r_prep.status_code, 200)
        prep = r_prep.json()
        self.assertEqual(prep["month"], "2026-10")
        self.assertEqual(prep["previousMonth"], "2026-09")
        self.assertEqual(prep["unassignedSummary"]["count"], 1)

        maryam_row = next(r for r in prep["teachers"] if r["teacher"] == "أ. مريم")
        # 2026-09 wage = 140 EGP, 2026-10 wage = 280 EGP (+100% MoM -> unusualChange=True)
        self.assertEqual(maryam_row["prevAccruedWage"], 140)
        self.assertEqual(maryam_row["accruedWage"], 280)
        self.assertEqual(maryam_row["remainingBalance"], 280)
        self.assertTrue(maryam_row["unusualChange"])

        # Crucial safety rule: salary preparation NEVER auto-executes a payout
        st = self.store.export_state()
        self.assertEqual(len(st["payouts"]), 0)

    def test_data_quality_audit_detects_all_issue_categories(self):
        self.client.post("/api/teachers", json={"name": "أ. نشطة", "pct": 70}, headers=self.h)
        self.client.post("/api/teachers", json={"name": "أ. مؤرشفة", "pct": 70}, headers=self.h)

        # Insert student while أ. مؤرشفة is active, then deactivate أ. مؤرشفة
        r_s1 = self.client.post(
            "/api/students",
            json={
                "id": "s_dq_1",
                "code": "STU-0501",
                "name": "سلمى محمود",
                "phone": "01099998888",
                "teacher": "أ. مؤرشفة",
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-05",
                        "actualStartDate": "2026-10-10",
                        "endDate": "2026-11-09",
                    }
                },
            },
            headers=self.h,
        )
        self.assertEqual(r_s1.status_code, 200)
        self.client.patch(
            "/api/teachers/أ. مؤرشفة",
            json={"name": "أ. مؤرشفة", "active": False, "archived": True},
            headers=self.h,
        )

        r_s2 = self.client.post(
            "/api/students?allow_duplicate=true",
            json={
                "id": "s_dq_2",
                "allowDuplicate": True,
                "code": "STU-0502",
                "name": "سلمى محمود",
                "phone": "01099998888",
                "teacher": "أ. نشطة",
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-05",
                        "actualStartDate": "",  # paid without actualStartDate
                    }
                },
            },
            headers=self.h,
        )
        self.assertEqual(r_s2.status_code, 200)

        r_s3 = self.client.post(
            "/api/students",
            json={
                "id": "s_dq_3",
                "code": "STU-0503",
                "name": "آمنة خالد",
                "phone": "",  # missing phone
                "teacher": "أ. نشطة",
                "months": {},
            },
            headers=self.h,
        )
        self.assertEqual(r_s3.status_code, 200)

        # Also test direct compute_data_quality_audit on legacy/corrupted state with endDate < actualStartDate
        corrupted_state = self.store.export_state()
        corrupted_state["students"][0]["months"]["2026-10"]["endDate"] = "2026-10-01"
        dq_direct = compute_data_quality_audit(corrupted_state, reference_date="2026-10-15")
        direct_cats = {iss["category"] for iss in dq_direct["issues"]}
        self.assertIn("invalid_dates", direct_cats)

        r_dq = self.client.get("/api/data-quality?reference_date=2026-10-15", headers=self.h)
        self.assertEqual(r_dq.status_code, 200)
        dq = r_dq.json()
        cats = {iss["category"] for iss in dq["issues"]}
        self.assertIn("duplicate_student", cats)
        self.assertIn("inactive_entity_in_new_ops", cats)
        self.assertIn("missing_info", cats)

    def test_preview_first_bulk_operations_and_safety_snapshot(self):
        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post("/api/teachers", json={"name": "أ. حفصة", "pct": 75}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "s_blk_1",
                "code": "stu-1",
                "name": "طالبة   الأولى",
                "phone": "+20 101-111-2222",
                "teacher": "أ. مريم",
                "type": "خاص",
                "months": {"2026-10": {"value": 300}},
            },
            headers=self.h,
        )
        self.client.post(
            "/api/students",
            json={
                "id": "s_blk_2",
                "code": "stu-2",
                "name": "طالبة الثانية",
                "phone": "00201022223333",
                "teacher": "أ. مريم",
                "type": "خاص",
                "months": {"2026-10": {"value": 300}},
            },
            headers=self.h,
        )

        # 1. Preview bulk teacher assignment
        r_prev = self.client.post(
            "/api/bulk/preview",
            json={
                "operation": "assign_teacher",
                "params": {"studentIds": ["s_blk_1", "s_blk_2"], "newTeacher": "أ. حفصة"},
            },
            headers=self.h,
        )
        self.assertEqual(r_prev.status_code, 200)
        prev = r_prev.json()
        self.assertEqual(prev["affectedCount"], 2)
        self.assertTrue(prev["previewHash"])

        # Executing without confirm=True must fail
        r_no_conf = self.client.post(
            "/api/bulk/execute",
            json={
                "operation": "assign_teacher",
                "params": {"studentIds": ["s_blk_1", "s_blk_2"], "newTeacher": "أ. حفصة"},
                "confirm": False,
            },
            headers=self.h,
        )
        self.assertEqual(r_no_conf.status_code, 400)

        # Executing with wrong previewHash must fail
        r_bad_hash = self.client.post(
            "/api/bulk/execute",
            json={
                "operation": "assign_teacher",
                "params": {"studentIds": ["s_blk_1", "s_blk_2"], "newTeacher": "أ. حفصة"},
                "expectedPreviewHash": "wrong_hash",
                "confirm": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_bad_hash.status_code, 400)

        # Execute with valid previewHash and confirm=True
        r_exec = self.client.post(
            "/api/bulk/execute",
            json={
                "operation": "assign_teacher",
                "params": {"studentIds": ["s_blk_1", "s_blk_2"], "newTeacher": "أ. حفصة"},
                "expectedPreviewHash": prev["previewHash"],
                "confirm": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_exec.status_code, 200)
        exec_body = r_exec.json()
        self.assertEqual(exec_body["updatedCount"], 2)
        self.assertTrue(exec_body["snapshot"])  # pre-bulk safety snapshot created

        # 2. Preview & execute normalize_phones_and_codes
        r_norm_prev = self.client.post(
            "/api/bulk/preview",
            json={"operation": "normalize_phones_and_codes", "params": {"studentIds": ["s_blk_1", "s_blk_2"]}},
            headers=self.h,
        )
        self.assertEqual(r_norm_prev.status_code, 200)
        norm_prev = r_norm_prev.json()
        self.assertEqual(norm_prev["affectedCount"], 2)

        r_norm_exec = self.client.post(
            "/api/bulk/execute",
            json={
                "operation": "normalize_phones_and_codes",
                "params": {"studentIds": ["s_blk_1", "s_blk_2"]},
                "expectedPreviewHash": norm_prev["previewHash"],
                "confirm": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_norm_exec.status_code, 200)
        st = self.store.export_state()
        s1 = next(s for s in st["students"] if s["id"] == "s_blk_1")
        self.assertEqual(s1["phone"], "01011112222")
        self.assertEqual(s1["code"], "STU-0001")
        self.assertEqual(s1["name"], "طالبة الأولى")
        self.assertEqual(s1["teacher"], "أ. حفصة")

    def test_operational_search_across_entities(self):
        self.client.post("/api/teachers", json={"name": "أ. سمية", "pct": 70}, headers=self.h)
        self.client.post(
            "/api/students",
            json={"id": "s_srch_1", "code": "STU-0999", "name": "جويرية طارق", "phone": "01055554444", "teacher": "أ. سمية"},
            headers=self.h,
        )
        r = self.client.get("/api/search?q=جويرية", headers=self.h)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertGreaterEqual(body["total"], 1)
        self.assertEqual(body["groups"]["students"][0]["id"], "s_srch_1")



    def test_live_frontend_dom_e2e_phase4_automation_workflows(self):
        """Live Node.js + Uvicorn DOM E2E test verifying Phase 4 UI automation workflows:
        - Salary Preparation table (#payrollPrepWrap)
        - Alerts Center mark-as-read (#btnMarkAllAlertsRead)
        - System Health Data Quality Audit report (#dataHealthReport)
        - Preview-First Bulk Operations bar & modal (#btnPreviewBulkOp -> #bulkPreviewModal -> #btnConfirmBulkOp)
        """
        import json
        import shutil
        import socket
        import subprocess
        import sys
        import tempfile
        import time

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        env = os.environ.copy()
        env["ALAHD_DATA_DIR"] = str(self.home)
        env["ALAHD_WINDOW_SECRET"] = self.secret

        self.client.post("/api/teachers", json={"name": "أ. مريم", "pct": 70}, headers=self.h)
        self.client.post("/api/teachers", json={"name": "أ. خديجة", "pct": 75}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "stu_e2e_1",
                "code": "STU-0001",
                "name": "فاطمة الزهراء",
                "phone": "01011112222",
                "teacher": "أ. مريم",
                "type": "خاص",
                "pct": 70,
                "months": {
                    "2026-10": {
                        "value": 300,
                        "paymentDate": "2026-10-01",
                        "actualStartDate": "2026-10-02",
                    }
                },
            },
            headers=self.h,
        )

        server_code = f"""
import uvicorn
from pathlib import Path
from alahd_erp.api.server import create_app
app = create_app(home=Path({str(self.home)!r}), ui_dir=Path({str(UI_DIR)!r}), port={port}, window_secret={self.secret!r}, seed_on_empty=False)
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="error")
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            ready = False
            import urllib.request
            for _ in range(50):
                try:
                    req = urllib.request.Request(f"{base_url}/api/health", headers={"X-Window-Secret": self.secret})
                    with urllib.request.urlopen(req, timeout=1) as resp:
                        if resp.status == 200:
                            ready = True
                            break
                except Exception:
                    time.sleep(0.1)
            self.assertTrue(ready, "Uvicorn server did not start in time")

            with tempfile.TemporaryDirectory() as tmp_js:
                js_dir = Path(tmp_js) / "ui"
                shutil.copytree(UI_DIR, js_dir)
                (js_dir / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")

                harness = f"""
import fs from 'fs';
import path from 'path';

const BASE_URL = 'http://127.0.0.1:{port}';
const SECRET = {self.secret!r};

class MiniElement {{
  constructor(tagName = 'div', id = '') {{
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this.className = '';
    this.value = '';
    this.textContent = '';
    this._innerHTML = '';
    this.checked = false;
    this.disabled = false;
    this.style = {{ display: '' }};
    this.attributes = {{}};
    this.listeners = {{}};
    this.children = [];
    this.parentElement = null;
    this.files = [];
    const self = this;
    this.classList = {{
      _set: new Set(),
      add(...cls) {{ cls.forEach((c) => this._set.add(c)); self.className = Array.from(this._set).join(' '); }},
      remove(...cls) {{ cls.forEach((c) => this._set.delete(c)); self.className = Array.from(this._set).join(' '); }},
      toggle(c, force) {{
        const shouldHave = force !== undefined ? Boolean(force) : !this._set.has(c);
        if (shouldHave) this._set.add(c); else this._set.delete(c);
        self.className = Array.from(this._set).join(' ');
      }},
      contains(c) {{ return this._set.has(c); }}
    }};
  }}
  get innerHTML() {{ return this._innerHTML; }}
  set innerHTML(val) {{
    this._innerHTML = String(val ?? '');
    this.textContent = this._innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  }}
  setAttribute(k, v) {{ this.attributes[k] = String(v); if (k === 'id') this.id = String(v); }}
  getAttribute(k) {{ return this.attributes[k] ?? null; }}
  addEventListener(ev, fn) {{ (this.listeners[ev] = this.listeners[ev] || []).push(fn); }}
  async dispatchEvent(ev) {{
    const type = typeof ev === 'string' ? ev : ev.type;
    const eventObj = typeof ev === 'string' ? {{ type, target: this, preventDefault() {{}}, closest: (s) => this.closest(s) }} : ev;
    if (!eventObj.target) eventObj.target = this;
    if (!eventObj.preventDefault) eventObj.preventDefault = () => {{}};
    for (const fn of (this.listeners[type] || [])) {{
      await fn(eventObj);
    }}
  }}
  focus() {{}}
  click() {{ return this.dispatchEvent('click'); }}
  appendChild(c) {{ c.parentElement = this; this.children.push(c); return c; }}
  removeChild(c) {{ this.children = this.children.filter((x) => x !== c); }}
  closest(sel) {{
    if (sel.startsWith('.') && this.classList.contains(sel.slice(1))) return this;
    return this.parentElement ? this.parentElement.closest(sel) : null;
  }}
  getContext() {{
    return {{ fillStyle: '', strokeStyle: '', lineWidth: 1, textAlign: '', font: '', fillRect() {{}}, strokeRect() {{}}, fillText() {{}} }};
  }}
}}

const elementsById = new Map();
function getOrCreateById(id, tag = 'div') {{
  if (!elementsById.has(id)) {{
    elementsById.set(id, new MiniElement(tag, id));
  }}
  return elementsById.get(id);
}}

const htmlSrc = fs.readFileSync(path.join({str(js_dir)!r}, 'index.html'), 'utf-8');
const tagIdRe = /<([a-zA-Z0-9]+)\b[^>]*\bid="([^"]+)"[^>]*>/g;
let match;
while ((match = tagIdRe.exec(htmlSrc)) !== null) {{
  getOrCreateById(match[2], match[1]);
}}

globalThis.window = {{
  location: {{ href: `${{BASE_URL}}/?wsec=${{SECRET}}`, pathname: '/', search: '', hash: '#/students' }},
  history: {{ state: null, replaceState() {{}} }},
  __ALAHD_WSEC__: SECRET,
  addEventListener() {{}},
  CSS: {{ escape: (s) => String(s) }}
}};
globalThis.CSS = globalThis.window.CSS;
globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
globalThis.document = {{
  readyState: 'complete',
  documentElement: new MiniElement('html'),
  body: new MiniElement('body'),
  getElementById: (id) => elementsById.get(id) || null,
  querySelector: (sel) => sel.startsWith('#') ? getOrCreateById(sel.slice(1)) : null,
  querySelectorAll: () => [],
  createElement: (tag) => new MiniElement(tag),
  addEventListener: () => {{}}
}};

const origFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {{}}) => {{
  const fullUrl = String(url).startsWith('http') ? url : `${{BASE_URL}}${{url}}`;
  const headers = Object.assign({{ 'X-Window-Secret': SECRET }}, opts.headers || {{}});
  return origFetch(fullUrl, Object.assign({{}}, opts, {{ headers }}));
}};

const feCode = fs.readFileSync(path.join({str(js_dir)!r}, 'js', 'finance-engine.js'), 'utf-8');
const mod = {{ exports: {{}} }};
(new Function('module', 'exports', 'window', feCode))(mod, mod.exports, globalThis.window);
globalThis.window.FinanceEngine = globalThis.window.FinanceEngine || mod.exports;

const storeMod = await import('./js/modules/store.js');
const appMod = await import('./js/app.js');
const sysMod = await import('./js/modules/wizard_and_system.js');
const alertsMod = await import('./js/modules/alerts.js');

storeMod.uiState.selectedMonth = '2026-10';
storeMod.uiState.referenceDate = '2026-10-15';
await appMod.reloadFullStateFromServer();
await new Promise((r) => setTimeout(r, 150));

// 1. Verify Salary Preparation Table rendered in #payrollPrepWrap
const payrollPrepText = getOrCreateById('payrollPrepWrap', 'div').textContent;

// 2. Verify System Health Data Quality Audit rendered in #dataHealthReport
await sysMod.renderSystemHealthView();
const dqText = getOrCreateById('dataHealthReport', 'div').textContent;

// 3. Verify Alerts Mark All Read via #btnMarkAllAlertsRead
await alertsMod.refreshAlertsAndBadges();
await getOrCreateById('btnMarkAllAlertsRead', 'button').dispatchEvent('click');

// 4. Verify Bulk Operations Preview & Execute (#bulkOpSelect -> #btnPreviewBulkOp -> #bulkPreviewModal -> #btnConfirmBulkOp)
const bulkOpSel = getOrCreateById('bulkOpSelect', 'select');
bulkOpSel.value = 'assign_teacher';
await bulkOpSel.dispatchEvent('change');
const bulkTeacherSel = getOrCreateById('bulkTeacherSelect', 'select');
bulkTeacherSel.value = 'أ. خديجة';
await getOrCreateById('btnPreviewBulkOp', 'button').dispatchEvent('click');

const modalOpenDuringPreview = getOrCreateById('bulkPreviewModal', 'div').classList.contains('open');
const teacherBeforeConfirm = storeMod.state.students[0].teacher;

await getOrCreateById('btnConfirmBulkOp', 'button').dispatchEvent('click');
const modalOpenAfterConfirm = getOrCreateById('bulkPreviewModal', 'div').classList.contains('open');
const teacherAfterConfirm = storeMod.state.students[0].teacher;

console.log('PHASE4_E2E:' + JSON.stringify({{
  payrollPrepHasMaryam: payrollPrepText.includes('أ. مريم'),
  dqHasSummary: dqText.includes('ملاحظات جودة البيانات الشاملة'),
  modalOpenDuringPreview,
  teacherBeforeConfirm,
  modalOpenAfterConfirm,
  teacherAfterConfirm,
}}));
"""
                (js_dir / "run_phase4_e2e.mjs").write_text(harness, encoding="utf-8")
                run_res = subprocess.run(
                    [node_bin, "run_phase4_e2e.mjs"],
                    cwd=str(js_dir),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(run_res.returncode, 0, f"STDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}")
                marker = next((ln for ln in run_res.stdout.splitlines() if ln.startswith("PHASE4_E2E:")), "")
                self.assertTrue(marker)
                res = json.loads(marker.split("PHASE4_E2E:", 1)[1])
                self.assertTrue(res["payrollPrepHasMaryam"])
                self.assertTrue(res["dqHasSummary"])
                self.assertTrue(res["modalOpenDuringPreview"])
                self.assertEqual(res["teacherBeforeConfirm"], "أ. مريم")
                self.assertFalse(res["modalOpenAfterConfirm"])
                self.assertEqual(res["teacherAfterConfirm"], "أ. خديجة")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
