"""End-to-end UI ↔ Backend API contract verification tests (Phase 3/4/5/6).

Verifies that every HTTP route, method, and JSON payload sent by `alahd_erp/ui/js/app.js`,
`wizard_and_system.js`, `alerts.js`, `statistics.js`, and `store.js` works end-to-end against `server.py`
and `database.py`, plus verifies headless application startup on a real Uvicorn loopback socket.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from tests._helpers import ROOT

try:
    from fastapi.testclient import TestClient
    from alahd_erp.api.server import create_app
    HAVE_HTTP = True
except Exception:
    HAVE_HTTP = False

SECRET = "contract-test-secret-999"
UI_DIR = ROOT / "alahd_erp" / "ui"


@unittest.skipUnless(HAVE_HTTP, "fastapi/httpx not installed")
class UiBackendContractParity(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.app = create_app(self.home, UI_DIR, port=8890, window_secret=SECRET)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.store.close)
        self.h = {"X-Window-Secret": SECRET}

    def test_01_onboarding_complete_with_array_subtypes_and_teacher_pct(self):
        """wizard_and_system.js posts to /api/onboarding/complete with array subTypes and teacher.pct."""
        r = self.client.post(
            "/api/onboarding/complete",
            json={
                "settings": {
                    "academyName": "أكاديمية يوم جديد",
                    "currency": "ج.م",
                    "defaultDurationDays": 30,
                    "expiringSoonDays": 5,
                    "defaultTeacherPct": 70,
                    "wageBasis": "net",
                    "wageAccrualMode": "accrued",
                },
                "teachers": [{"name": "أ/ خديجة", "pct": 75}],
                "subTypes": ["8 حلقات | 300", "4 حلقات | 175"],
            },
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200, r.text)
        sync = self.client.get("/api/sync", headers=self.h).json()
        self.assertIn("أ/ خديجة", sync["teachers"])
        self.assertEqual(sync["teacherMeta"]["أ/ خديجة"]["pct"], 75)
        self.assertEqual(sync["subTypes"]["8 حلقات"], 300)

    def test_02_put_settings_route(self):
        """wizard_and_system.js sends PUT /api/settings."""
        r = self.client.put(
            "/api/settings",
            json={"defaultDurationDays": 28, "defaultTeacherPct": 65},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/settings", headers=self.h).json()["settings"]["defaultDurationDays"], 28)

    def test_03_student_put_and_partial_month_patch_preserves_existing_columns(self):
        """app.js edits students via PUT /api/students/{id} and inline-patches single fields via PATCH /months/{mk}."""
        self.client.post("/api/teachers", json={"name": "أ/ سارة", "meta": {"pct": 70}}, headers=self.h)
        stu_payload = {
            "id": "stu_001",
            "code": "STU-0001",
            "name": "طالبة اختبار العقد",
            "phone": "01000000099",
            "teacher": "أ/ سارة",
            "type": "8 حلقات",
            "pct": 70,
            "paymentDate": "2026-11-01",
            "actualStartDate": "2026-11-02",
            "months": {
                "2026-11": {
                    "value": 400,
                    "discount": 50,
                    "paymentDate": "2026-11-01",
                    "actualStartDate": "2026-11-02",
                    "endDate": "2026-12-02",
                }
            },
        }
        r_create = self.client.post("/api/students", json=stu_payload, headers=self.h)
        self.assertEqual(r_create.status_code, 200, r_create.text)

        # Edit student via PUT /api/students/{id}
        stu_payload["phone"] = "01000000088"
        r_put = self.client.put("/api/students/stu_001", json=stu_payload, headers=self.h)
        self.assertEqual(r_put.status_code, 200, r_put.text)

        # Inline partial PATCH of only actualStartDate (must NOT wipe value=400, discount=50, paymentDate="2026-11-01")
        r_patch = self.client.patch(
            "/api/students/stu_001/months/2026-11",
            json={"actualStartDate": "2026-11-05", "endDate": "2026-12-05"},
            headers=self.h,
        )
        self.assertEqual(r_patch.status_code, 200, r_patch.text)

        stu_after = self.client.get("/api/students/stu_001", headers=self.h).json()
        m_after = stu_after["months"]["2026-11"]
        self.assertEqual(m_after["value"], 400)
        self.assertEqual(m_after["discount"], 50)
        self.assertEqual(m_after["paymentDate"], "2026-11-01")
        self.assertEqual(m_after["actualStartDate"], "2026-11-05")
        self.assertEqual(m_after["endDate"], "2026-12-05")

    def test_04_plans_wrapped_batch_and_session_patch_and_compensate(self):
        """app.js posts {plan, sessions} to /api/plans, patches /api/sessions/{id}, and posts /api/sessions/{id}/compensate."""
        self.client.post("/api/teachers", json={"name": "أ/ منى", "meta": {"pct": 70}}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "stu_plan",
                "name": "طالبة الحصص",
                "phone": "01000000077",
                "teacher": "أ/ منى",
                "pct": 70,
                "months": {"2026-11": {"value": 300, "actualStartDate": "2026-11-01", "endDate": "2026-11-30"}},
            },
            headers=self.h,
        )
        r_plan = self.client.post(
            "/api/plans",
            json={
                "plan": {
                    "id": "pln_1",
                    "studentId": "stu_plan",
                    "studentName": "طالبة الحصص",
                    "teacher": "أ/ منى",
                    "count": 2,
                    "days": [0, 3],
                    "time": "16:00",
                    "dur": 40,
                    "startDate": "2026-11-01",
                    "actualStartDate": "2026-11-01",
                },
                "sessions": [
                    {"id": "sess_1", "planId": "pln_1", "studentId": "stu_plan", "teacher": "أ/ منى", "date": "2026-11-01", "time": "16:00", "dur": 40, "status": "scheduled"},
                    {"id": "sess_2", "planId": "pln_1", "studentId": "stu_plan", "teacher": "أ/ منى", "date": "2026-11-04", "time": "16:00", "dur": 40, "status": "scheduled"},
                ],
            },
            headers=self.h,
        )
        self.assertEqual(r_plan.status_code, 200, r_plan.text)

        # Patch session status via PATCH /api/sessions/sess_1
        r_sp = self.client.patch("/api/sessions/sess_1", json={"status": "done"}, headers=self.h)
        self.assertEqual(r_sp.status_code, 200, r_sp.text)

        # Auto-compensate session via POST /api/sessions/sess_2/compensate
        r_comp = self.client.post("/api/sessions/sess_2/compensate", json={}, headers=self.h)
        self.assertEqual(r_comp.status_code, 200, r_comp.text)
        comp_body = r_comp.json()
        self.assertTrue(comp_body.get("compensated"))
        self.assertIn("makeupSession", comp_body)
        self.assertTrue(comp_body["makeupSession"]["date"] > "2026-11-04")

    def test_05_madrasa_enrollment_streak_patch(self):
        """app.js patches /api/madrasa/enrollments/{eid} with {streak: nextStreak}."""
        self.client.post("/api/demo/load", json={}, headers=self.h)
        sync = self.client.get("/api/sync", headers=self.h).json()
        sid = sync["students"][0]["id"]
        r_enr = self.client.post(
            "/api/madrasa/enrollments",
            json={"id": "enr_test_1", "batchId": "batch_1", "batchName": "دفعة النور", "studentId": sid, "streak": 7, "bestStreak": 7},
            headers=self.h,
        )
        self.assertEqual(r_enr.status_code, 200, r_enr.text)
        r_bump = self.client.patch("/api/madrasa/enrollments/enr_test_1", json={"streak": 8}, headers=self.h)
        self.assertEqual(r_bump.status_code, 200, r_bump.text)
        sync_after = self.client.get("/api/sync", headers=self.h).json()
        enr_after = next(e for e in sync_after["madrasaEnrollments"] if e["id"] == "enr_test_1")
        self.assertEqual(enr_after["streak"], 8)
        self.assertEqual(enr_after["bestStreak"], 8)

    def test_06_alerts_and_statistics_ui_shapes_and_dismiss_by_alert_id(self):
        """alerts.js and statistics.js expect specific JSON shapes and alertId/snoozeDays payloads."""
        self.client.post("/api/demo/load", json={}, headers=self.h)
        r_al = self.client.get("/api/alerts?reference_date=2026-11-15&include_dismissed=false", headers=self.h)
        self.assertEqual(r_al.status_code, 200, r_al.text)
        al_data = r_al.json()
        self.assertIn("counts", al_data)
        self.assertIn("items", al_data)
        self.assertGreater(len(al_data["items"]), 0)

        first_id = al_data["items"][0]["id"]
        r_dis = self.client.post(
            "/api/alerts/dismiss",
            json={"alertId": first_id, "reason": "تأجيل", "snoozeDays": 7},
            headers=self.h,
        )
        self.assertEqual(r_dis.status_code, 200, r_dis.text)
        r_res = self.client.post("/api/alerts/restore", json={"alertId": first_id}, headers=self.h)
        self.assertEqual(r_res.status_code, 200, r_res.text)

        r_st = self.client.get(
            "/api/statistics?period=current_month&selected_month=2026-11&reference_date=2026-11-15",
            headers=self.h,
        )
        self.assertEqual(r_st.status_code, 200, r_st.text)
        st_data = r_st.json()
        for key in ("students", "finance", "teachers", "sessions", "memorization", "madrasa", "monthlyTrend", "byTeacher", "byPackage", "byPaymentMethod"):
            self.assertIn(key, st_data, key)


    def test_07_import_preview_error_report_commit_and_undo_http_contract(self):
        """Verifies the complete HTTP contract between import_view.js / app.js and server.py."""
        import base64
        import io
        from openpyxl import Workbook, load_workbook

        wb = Workbook()
        ws1 = wb.active
        ws1.title = "ورقة فارغة"
        ws1.append(["اسم الطالبة", "قيمة الاشتراك"])
        ws1.append(["طالبة الورقة الأولى", 200])

        ws2 = wb.create_sheet("طالبات نوفمبر")
        ws2.append(["الكود", "اسم الطالبة", "تليفون الطالبة", "اسم المعلمة", "نوع الاشتراك", "الشهر", "الاشتراك الشهري", "المبلغ المسدد", "تاريخ الدفع", "تاريخ البداية الفعلي"])
        ws2.append(["STU-101", "فاطمة الزهراء", "01011112222", "أ/ ندى", "باقة التميز", "2026-11", 400, 400, "2026-11-03", "2026-11-05"])
        ws2.append(["STU-101", "فاطمة الزهراء", "01011112222", "أ/ ندى", "باقة التميز", "2026-11", 400, 400, "2026-11-03", "2026-11-05"])
        ws2.append(["STU-102", "", "01033334444", "أ/ ندى", "باقة التميز", "2026-11", 400, 0, "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        # 1. Invalid dataset, mode, or sheetName must return HTTP 400
        for bad_payload in (
            {"dataset": "invalid_ds", "filename": "f.xlsx", "fileBase64": b64},
            {"dataset": "students", "mode": "invalid_mode", "filename": "f.xlsx", "fileBase64": b64},
            {"dataset": "students", "sheetName": "ورقة غير موجودة", "filename": "f.xlsx", "fileBase64": b64},
        ):
            r_bad = self.client.post("/api/import/preview", json=bad_payload, headers=self.h)
            self.assertEqual(r_bad.status_code, 400, bad_payload)

        # 2. Preview with autoCreateTeachers=False, autoCreatePackages=False (without autoCreateRefs)
        r_prev_no_auto = self.client.post(
            "/api/import/preview",
            json={
                "dataset": "students",
                "mode": "update_existing",
                "filename": "f.xlsx",
                "fileBase64": b64,
                "sheetName": "طالبات نوفمبر",
                "autoCreateTeachers": False,
                "autoCreatePackages": False,
                "columnMapping": {"phone": "تليفون الطالبة"},
            },
            headers=self.h,
        )
        self.assertEqual(r_prev_no_auto.status_code, 200, r_prev_no_auto.text)
        prev_no_auto = r_prev_no_auto.json()
        self.assertEqual(prev_no_auto["summary"]["invalid"], 3)  # rows 1 & 2 fail unregistered teacher/pkg, row 3 fails empty name

        # 3. Preview with autoCreateTeachers=True, autoCreatePackages=True
        r_prev = self.client.post(
            "/api/import/preview",
            json={
                "dataset": "students",
                "mode": "update_existing",
                "filename": "f.xlsx",
                "fileBase64": b64,
                "sheetName": "طالبات نوفمبر",
                "autoCreateTeachers": True,
                "autoCreatePackages": True,
                "columnMapping": {"phone": "تليفون الطالبة"},
            },
            headers=self.h,
        )
        self.assertEqual(r_prev.status_code, 200, r_prev.text)
        prev = r_prev.json()
        self.assertEqual(prev["sheetNames"], ["ورقة فارغة", "طالبات نوفمبر"])
        self.assertEqual(prev["selectedSheet"], "طالبات نوفمبر")
        self.assertEqual(prev["summary"]["total"], 3)
        self.assertEqual(prev["summary"]["valid"], 1)
        self.assertEqual(prev["summary"]["new"], 1)
        self.assertEqual(prev["summary"]["duplicate"], 1)
        self.assertEqual(prev["summary"]["invalid"], 1)
        self.assertEqual(prev["rows"][0]["rowIndex"], 2)
        self.assertEqual(prev["rows"][0]["normalized"]["phone"], "01011112222")

        # 4. Download error report using UI payload (fileBase64 without explicit errors array)
        r_err_rep = self.client.post(
            "/api/import/error-report",
            json={
                "dataset": "students",
                "filename": "f.xlsx",
                "fileBase64": b64,
                "sheetName": "طالبات نوفمبر",
                "columnMapping": {"phone": "تليفون الطالبة"},
            },
            headers=self.h,
        )
        self.assertEqual(r_err_rep.status_code, 200)
        err_wb = load_workbook(io.BytesIO(r_err_rep.content))
        self.assertGreaterEqual(err_wb.active.max_row, 2)

        # 5. Commit with mismatched expectedFileHash is rejected with HTTP 400
        r_stale = self.client.post(
            "/api/import/commit",
            json={
                "dataset": "students",
                "filename": "f.xlsx",
                "fileBase64": b64,
                "expectedFileHash": "0000badhash",
                "sheetName": "طالبات نوفمبر",
            },
            headers=self.h,
        )
        self.assertEqual(r_stale.status_code, 400)

        # 6. Commit valid preview
        r_commit = self.client.post(
            "/api/import/commit",
            json={
                "dataset": "students",
                "mode": "update_existing",
                "filename": "f.xlsx",
                "fileBase64": b64,
                "expectedFileHash": prev["fileHash"],
                "sheetName": "طالبات نوفمبر",
                "autoCreateTeachers": True,
                "autoCreatePackages": True,
                "columnMapping": {"phone": "تليفون الطالبة"},
            },
            headers=self.h,
        )
        self.assertEqual(r_commit.status_code, 200, r_commit.text)
        c_body = r_commit.json()
        self.assertEqual((c_body["added"], c_body["updated"], c_body["skipped"], c_body["committed"]), (1, 0, 2, 1))

        # 7. Verify HTML export honours hide_personal=false vs hide_personal=true
        r_html_masked = self.client.get("/api/export/html?scope=full&month=2026-11&hide_personal=true", headers=self.h)
        self.assertEqual(r_html_masked.status_code, 200)
        self.assertNotIn("01011112222", r_html_masked.text)

        r_html_visible = self.client.get("/api/export/html?scope=full&month=2026-11&hide_personal=false", headers=self.h)
        self.assertEqual(r_html_visible.status_code, 200)
        self.assertIn("01011112222", r_html_visible.text)

        # 8. Undo import restores clean state
        r_undo = self.client.post("/api/import/undo", json={}, headers=self.h)
        self.assertEqual(r_undo.status_code, 200, r_undo.text)
        self.assertEqual(self.client.get("/api/sync", headers=self.h).json()["students"], [])


class HeadlessDesktopServerLifecycle(unittest.TestCase):
    """Boots the real `alahd_erp.main` desktop entrypoint in headless mode (`ALAHD_ERP_NO_UI=1`) on a free loopback port,
    verifies `/api/health`, `/`, and `.alahd_instance.json` single-instance lock, and verifies clean SIGTERM shutdown + exit backup."""

    def test_headless_server_boot_health_and_clean_exit_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                free_port = s.getsockname()[1]

            env = {
                **os.environ,
                "ALAHD_ERP_HOME": tmp,
                "ALAHD_ERP_PORT": str(free_port),
                "ALAHD_WINDOW_SECRET": "headless-smoke-sec-777",
                "ALAHD_ERP_NO_UI": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "alahd_erp.main"],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),   # Windows: lets us send CTRL_BREAK
            )
            try:
                health_ok = False
                inst_file = Path(tmp) / ".alahd_instance.json"
                for _ in range(80):
                    time.sleep(0.1)
                    if not inst_file.is_file():
                        continue
                    try:
                        info = json.loads(inst_file.read_text(encoding="utf-8"))
                        actual_port = info["port"]
                        actual_sec = info["windowSecret"]
                        req = urllib.request.Request(
                            f"http://127.0.0.1:{actual_port}/api/health",
                            headers={"X-Window-Secret": actual_sec},
                        )
                        with urllib.request.urlopen(req, timeout=1.0) as resp:
                            body = json.loads(resp.read().decode("utf-8"))
                            if resp.status == 200 and body.get("ok") is True:
                                health_ok = True
                                break
                    except Exception:
                        continue
                self.assertTrue(health_ok, "Headless desktop server did not answer /api/health")
            finally:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)   # graceful (SIGBREAK handler); terminate() would hard-kill
                else:
                    proc.terminate()                            # SIGTERM
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

            # Verify instance lock cleaned up and exit backup created
            self.assertFalse((Path(tmp) / ".alahd_instance.json").exists())
            exit_backups = list((Path(tmp) / "backups").glob("alahd_auto_*_exit.sqlite3"))
            self.assertEqual(len(exit_backups), 1)



    def test_live_frontend_js_and_server_e2e_import_export_workflow(self):
        """Executes the complete 11-step Import & Export workflow using the real frontend ES modules
        (import_view.js, api.js, store.js, dom.js, csv.js) in Node.js against a live Uvicorn server and SQLite DB.
        """
        import base64
        import io
        import shutil
        from openpyxl import Workbook, load_workbook

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("node not installed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Build a multi-sheet .xlsx file with custom headers, valid/new, update, duplicate, and invalid rows
            wb = Workbook()
            ws1 = wb.active
            ws1.title = "ورقة أولى"
            ws1.append(["اسم الطالبة", "قيمة الاشتراك"])
            ws1.append(["طالبة غير مختارة", 100])

            ws2 = wb.create_sheet("طالبات الأكاديمية")
            ws2.append([
                "كود الطالبة", "اسم الطالبة", "رقم الموبايل", "اسم المعلمة",
                "نوع الاشتراك", "الشهر", "قيمة الرسوم", "المبلغ المسدد",
                "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية"
            ])
            ws2.append([
                "STU-501", "مريم عبد الرحمن", "01012345678", "أ/ فاطمة الزهراء",
                "باقة النور", "2026-11", 350, 350,
                "2026-11-02", "2026-11-04", "2026-12-03"
            ])
            ws2.append([
                "STU-502", "سارة محمود", "01087654321", "أ. فاطمه الزهراء",
                "باقة النور", "2026-11", 350, 200,
                "2026-11-03", "2026-11-05", "2026-12-04"
            ])
            ws2.append([
                "STU-501", "مريم عبد الرحمن", "01012345678", "أ/ فاطمة الزهراء",
                "باقة النور", "2026-11", 350, 350,
                "2026-11-02", "2026-11-04", "2026-12-03"
            ])
            ws2.append([
                "STU-503", "", "01099998888", "أ/ فاطمة الزهراء",
                "باقة النور", "2026-11", 350, 0, "", "", ""
            ])
            buf = io.BytesIO()
            wb.save(buf)
            xlsx_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                free_port = s.getsockname()[1]

            env = {
                **os.environ,
                "ALAHD_ERP_HOME": str(tmp_path / "home"),
                "ALAHD_ERP_PORT": str(free_port),
                "ALAHD_ERP_NO_UI": "1",
            }
            proc = subprocess.Popen(
                [sys.executable, "-m", "alahd_erp.main"],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                inst_file = tmp_path / "home" / ".alahd_instance.json"
                server_ready = False
                actual_port = free_port
                secret = ""
                for _ in range(80):
                    time.sleep(0.1)
                    if not inst_file.is_file():
                        continue
                    try:
                        info = json.loads(inst_file.read_text(encoding="utf-8"))
                        actual_port = int(info["port"])
                        secret = str(info["windowSecret"])
                        req_h = urllib.request.Request(
                            f"http://127.0.0.1:{actual_port}/api/health",
                            headers={"X-Window-Secret": secret},
                        )
                        with urllib.request.urlopen(req_h, timeout=1.0) as resp_h:
                            if resp_h.status == 200:
                                server_ready = True
                                break
                    except Exception:
                        continue
                self.assertTrue(server_ready, "Live server did not start")

                # Copy actual frontend JS modules to a temp Node workspace and run the 11-step UI workflow
                js_dir = tmp_path / "js_run"
                js_dir.mkdir()
                for js_f in (ROOT / "alahd_erp" / "ui" / "js" / "modules").glob("*.js"):
                    shutil.copy2(js_f, js_dir / js_f.name)
                shutil.copy2(ROOT / "alahd_erp" / "ui" / "js" / "finance-engine.js", js_dir / "finance-engine.js")
                (js_dir / "package.json").write_text('{"type": "module"}', encoding="utf-8")

                harness_code = f"""
import fs from 'fs';
const BASE = 'http://127.0.0.1:{actual_port}';
const SECRET = {json.dumps(secret)};
const XLSX_B64 = {json.dumps(xlsx_b64)};

// Minimal DOM & Browser globals for running real UI modules
const elements = new Map();
function makeEl(id, tag = 'DIV') {{
  const el = {{
    id,
    tagName: tag,
    value: '',
    checked: true,
    disabled: false,
    textContent: '',
    _innerHTML: '',
    style: {{ display: '' }},
    files: null,
    appendChild(c) {{ toasts.push(c.textContent); }},
    removeChild() {{}},
    classList: {{ add() {{}}, remove() {{}} }},
    listeners: {{}},
    attrs: {{}},
    addEventListener(ev, fn) {{
      this.listeners[ev] = this.listeners[ev] || [];
      this.listeners[ev].push(fn);
    }},
    trigger(ev) {{
      const list = this.listeners[ev] || [];
      return Promise.all(list.map(fn => fn({{ target: this, preventDefault() {{}} }})));
    }},
    getAttribute(k) {{ return this.attrs[k] || ''; }},
    setAttribute(k, v) {{ this.attrs[k] = String(v); }},
    get innerHTML() {{ return this._innerHTML; }},
    set innerHTML(v) {{
      this._innerHTML = String(v || '');
      this.textContent = this._innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
    }}
  }};
  elements.set('#' + id, el);
  return el;
}}

[
  'importFileInput', 'importDatasetSelect', 'importModeSelect', 'importSheetGroup',
  'importSheetSelect', 'importAutoCreateDeps', 'importAutoCreateTeachers', 'importAutoCreatePackages',
  'btnDownloadImportTemplate', 'btnPreviewImport', 'btnDownloadImportErrorReport', 'btnCommitImport',
  'btnUndoLastImport', 'importPreviewContainer', 'importStaleBanner', 'importCommitResultBanner',
  'importAutoCreateWarningBanner', 'importSummaryKpis', 'importMappingWrap', 'importPreviewTableWrap',
  'syncStatusChip', 'btnRetrySave', 'conflictModal', 'toastContainer'
].forEach(id => makeEl(id));

elements.get('#importDatasetSelect').value = 'students';
elements.get('#importModeSelect').value = 'update_existing';
elements.get('#importAutoCreateDeps').checked = true;
elements.get('#importAutoCreateTeachers').checked = true;
elements.get('#importAutoCreatePackages').checked = true;

const toasts = [];
let dynamicSelects = [];
const origFetch = globalThis.fetch;
globalThis.fetch = (url, opts = {{}}) => {{
  const fullUrl = String(url).startsWith('http') ? url : (BASE + url);
  return origFetch(fullUrl, opts);
}};

globalThis.window = {{
  location: {{ href: BASE + '/?wsec=' + SECRET }},
  history: {{ state: null, replaceState() {{}} }},
  addEventListener() {{}},
  FinanceEngine: {{
    roundHalfUp: (v) => Math.round(Number(v || 0)),
    parsePctInt: (v, d = 70) => (v !== undefined && v !== null && v !== '' ? Number(v) : d),
    generateDynamicMonths: () => [{{ key: '2026-11', label: 'نوفمبر 2026' }}],
    resolvePaymentDate: (m) => (m && (m.paymentDate || m.pay)) || '',
    resolveActualStartDate: (m) => (m && (m.actualStartDate || m.startDate)) || '',
    resolveEndDate: (m) => (m && (m.endDate || m.end)) || '',
  }}
}};

globalThis.FileReader = class {{
  readAsDataURL(file) {{
    this.result = 'data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,' + file._b64;
    if (this.onload) this.onload();
  }}
}};

globalThis.document = {{
  querySelector(sel) {{
    if (elements.has(sel)) return elements.get(sel);
    return null;
  }},
  querySelectorAll(sel) {{
    if (sel === '.import-col-map-select') return dynamicSelects;
    if (sel === '.import-filter-btn') return [];
    return [];
  }},
  createElement(tag) {{ return makeEl('dyn_' + Math.random(), tag); }},
  body: {{ appendChild() {{}}, removeChild() {{}} }}
}};
globalThis.URL.createObjectURL = () => 'blob:mock';
globalThis.URL.revokeObjectURL = () => {{}};
globalThis.setTimeout = () => ({{ unref() {{ return this; }}, ref() {{ return this; }}, refresh() {{ return this; }}, [Symbol.toPrimitive]() {{ return 0; }} }});

const {{ state, uiState, applyLoadedState }} = await import('./store.js');
const {{ apiRequest }} = await import('./api.js');
const {{ wireImportEvents, runImportPreview, isPreviewStale }} = await import('./import_view.js');

uiState.selectedMonth = '2026-11';
let reloadCount = 0;
async function reloadFullState() {{
  const s = await apiRequest('/sync');
  applyLoadedState(s);
  reloadCount++;
}}

await reloadFullState();
wireImportEvents(reloadFullState);

// Simulate choosing the multi-sheet file
const fakeFile = {{ name: 'academy_import.xlsx', size: 4096, lastModified: 1700000000000, _b64: XLSX_B64 }};
elements.get('#importFileInput').files = [fakeFile];

// Step 3 & 5: Trigger initial preview (defaults to sheet 1 "ورقة أولى")
const prev1 = await runImportPreview();

// Step 5: Switch worksheet to "طالبات الأكاديمية"
elements.get('#importSheetSelect').value = 'طالبات الأكاديمية';
const prev2 = await runImportPreview();

// Step 7: Create a dynamic mapping select for "value" -> "قيمة الرسوم" and "phone" -> "رقم الموبايل"
const selVal = makeEl('map_val', 'SELECT');
selVal.setAttribute('data-field', 'value');
selVal.value = 'قيمة الرسوم';
const selPhone = makeEl('map_phone', 'SELECT');
selPhone.setAttribute('data-field', 'phone');
selPhone.value = 'رقم الموبايل';
dynamicSelects = [selVal, selPhone];

// Re-render to wire dynamic selects, then trigger change
const prev3 = await runImportPreview();
await selVal.trigger('change');
await selPhone.trigger('change');

const kpiHtml = elements.get('#importSummaryKpis').innerHTML;
const tableHtml = elements.get('#importPreviewTableWrap').innerHTML;
const warnHtml = elements.get('#importAutoCreateWarningBanner').innerHTML;

// Step 8: Commit import via #btnCommitImport
await elements.get('#btnCommitImport').trigger('click');
const commitBannerHtml = elements.get('#importCommitResultBanner').innerHTML;

console.log(JSON.stringify({{
  toasts,
  prev1Sheet: prev1.selectedSheet,
  prev2Sheet: prev2.selectedSheet,
  sheetNames: prev2.sheetNames,
  kpiHasUndefined: kpiHtml.includes('undefined'),
  tableHasUndefined: tableHtml.includes('undefined'),
  commitHasUndefined: commitBannerHtml.includes('undefined'),
  tableHasMappedValue: tableHtml.includes('قيمة الاشتراك: 350'),
  tableHasMappedPhone: tableHtml.includes('01012345678'),
  warnHasTeacher: warnHtml.includes('فاطمة الزهراء'),
  commitBannerText: elements.get('#importCommitResultBanner').textContent,
  studentsCountAfterCommit: state.students.length,
  teachersAfterCommit: state.teachers,
  subTypesAfterCommit: state.subTypes,
  installmentsAfterCommit: state.installments.length,
  reloadCount
}}));
"""
                (js_dir / "run_e2e.mjs").write_text(harness_code, encoding="utf-8")
                out = subprocess.run(
                    [node_bin, "run_e2e.mjs"],
                    cwd=str(js_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                )
                self.assertEqual(out.returncode, 0, out.stderr)
                res = json.loads(out.stdout.strip().splitlines()[-1])
                self.assertTrue(any("إضافة: 2 | تحديث: 0 | تخطي: 2" in t for t in res["toasts"]), res["toasts"])

                self.assertEqual(res["prev1Sheet"], "ورقة أولى")
                self.assertEqual(res["prev2Sheet"], "طالبات الأكاديمية")
                self.assertEqual(res["sheetNames"], ["ورقة أولى", "طالبات الأكاديمية"])
                self.assertFalse(res["kpiHasUndefined"])
                self.assertFalse(res["tableHasUndefined"])
                self.assertFalse(res["commitHasUndefined"])
                self.assertTrue(res["tableHasMappedValue"])
                self.assertTrue(res["tableHasMappedPhone"])
                self.assertTrue(res["warnHasTeacher"])
                self.assertEqual(res["studentsCountAfterCommit"], 2)
                self.assertEqual(len(res["teachersAfterCommit"]), 1)
                self.assertIn("باقة النور", res["subTypesAfterCommit"])
                self.assertEqual(res["installmentsAfterCommit"], 2)

                # Step 10 & 11: Export the resulting data from the live server and verify the exported workbook
                req_xlsx = urllib.request.Request(
                    f"http://127.0.0.1:{actual_port}/api/export/xlsx?scope=full",
                    headers={"X-Window-Secret": secret},
                )
                with urllib.request.urlopen(req_xlsx, timeout=5.0) as resp_x:
                    self.assertEqual(resp_x.status, 200)
                    xlsx_bytes = resp_x.read()
                exp_wb = load_workbook(io.BytesIO(xlsx_bytes))
                self.assertIn("الطلاب", exp_wb.sheetnames)
                stu_rows = list(exp_wb["الطلاب"].iter_rows(values_only=True))
                flat_text = " ".join(str(c) for r in stu_rows for c in r if c is not None)
                self.assertIn("مريم عبد الرحمن", flat_text)
                self.assertIn("سارة محمود", flat_text)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
