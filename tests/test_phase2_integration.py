"""Phase 2 Integration & Dynamic Teacher Management Tests (Scenarios 1..15).

Covers:
1. Add teacher
2. Edit teacher name (rename)
3. Deactivate teacher
4. Reactivate teacher
5. Duplicate prevention (whitespace, Arabic normalization, honorifics, compound names, repeated submissions)
6. Historical record preservation across rename and deactivation
7. Teacher availability in all relevant dropdowns
8. Teacher availability in filters
9. Teacher availability in attendance workflows
10. Teacher availability in salary workflows
11. Teacher availability in reports (statistics, XLSX, HTML)
12. API contract consistency
13. Automatic UI refresh after changes (live Node.js + FastAPI DOM E2E test)
14. Concurrent or repeated submissions
15. Database migration & state compatibility
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from alahd_erp.api.server import create_app
from alahd_erp.domain.models import (
    DuplicateTeacherError,
    clean_teacher_display_name,
    find_duplicate_teacher,
    normalize_teacher_key,
)
from tests._helpers import StoreTestCase

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "alahd_erp" / "ui"


class Phase2DynamicTeacherAndIntegrationTests(StoreTestCase):
    """Comprehensive backend, database, and API integration tests for Tasks 1..7."""

    def setUp(self) -> None:
        super().setUp()
        self.secret = "phase2_test_secret_token"
        self.app = create_app(
            home=self.home,
            ui_dir=UI_DIR,
            port=8765,
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

    def test_scenario_01_add_teacher_and_list_contract(self) -> None:
        """Scenario 1 & 12: Adding a teacher persists cleanly and exposes consistent API contract."""
        r = self.client.post(
            "/api/teachers",
            json={"name": "  أ/   فاطمة الزهراء  ", "pct": 75, "mode": "create"},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["name"], "أ/ فاطمة الزهراء")
        self.assertTrue(body["active"])
        self.assertFalse(body["archived"])
        self.assertEqual(body["meta"]["pct"], 75)
        self.assertEqual(body["meta"]["defaultPct"], 75)
        # Ensure transport envelope keys did not pollute teacher metadata
        self.assertNotIn("mode", body["meta"])
        self.assertNotIn("name", body["meta"])

        # Verify GET /api/teachers and GET /api/sync
        r_list = self.client.get("/api/teachers", headers=self.h)
        self.assertEqual(r_list.status_code, 200)
        self.assertEqual(r_list.json()["teachers"], ["أ/ فاطمة الزهراء"])
        self.assertEqual(r_list.json()["items"][0]["pct"], 75)
        self.assertTrue(r_list.json()["items"][0]["active"])

        sync = self.client.get("/api/sync", headers=self.h).json()
        self.assertIn("أ/ فاطمة الزهراء", sync["teachers"])
        self.assertEqual(sync["teacherMeta"]["أ/ فاطمة الزهراء"]["pct"], 75)

    def test_scenario_02_and_06_rename_teacher_preserves_all_historical_records(self) -> None:
        """Scenario 2 & 6: Renaming a teacher updates all FK & non-FK tables while preserving historical records and closed months."""
        old_t = "أ/ سارة محمد"
        new_t = "أ/ سارة محمد العبد"
        self.store.upsert_teacher(old_t, {"pct": 70})
        self.store.upsert_teacher("أ/ معلمة سابقة", {"pct": 65})

        # Create student with transfer history, closed month (2026-10) + open month (2026-11)
        self.store.upsert_student({
            "id": "stu_hist_1",
            "code": "STU-101",
            "name": "مريم طارق",
            "phone": "01012345678",
            "teacher": old_t,
            "type": "8 حلقات-40 دقيقة",
            "pct": 70,
            "transferHistory": [
                {"from": "أ/ معلمة سابقة", "to": old_t, "date": "2026-10-01"}
            ],
            "months": {
                "2026-10": {
                    "value": 400,
                    "paymentDate": "2026-10-02",
                    "actualStartDate": "2026-10-05",
                    "endDate": "2026-11-04",
                },
                "2026-11": {
                    "value": 400,
                    "paymentDate": "2026-11-02",
                    "actualStartDate": "2026-11-05",
                    "endDate": "2026-12-05",
                },
            },
        })
        self.store.upsert_installment({
            "id": "inst_oct",
            "receiptNo": "RCP-OCT-1",
            "studentId": "stu_hist_1",
            "month": "2026-10",
            "amount": 400,
            "paymentDate": "2026-10-02",
            "method": "فودافون كاش",
        })
        self.store.upsert_payout({
            "id": "pay_oct",
            "teacher": old_t,
            "month": "2026-10",
            "amount": 280,
            "date": "2026-10-30",
            "note": "راتب أكتوبر",
        })
        self.store.close_month("2026-10", note="إقفال أكتوبر")

        # Create study group, attendance plan, session, memorization log, and certificate
        self.store.upsert_group({"id": "grp_1", "name": "حلقة النور", "teacher": old_t})
        self.store.upsert_sessions_batch(
            {
                "id": "pln_1",
                "studentId": "stu_hist_1",
                "studentName": "مريم طارق",
                "teacher": old_t,
                "count": 2,
                "days": [0, 3],
                "time": "16:00",
                "dur": 40,
                "actualStartDate": "2026-11-05",
            },
            [
                {
                    "id": "sess_1",
                    "planId": "pln_1",
                    "studentId": "stu_hist_1",
                    "teacher": old_t,
                    "date": "2026-11-08",
                    "time": "16:00",
                    "dur": 40,
                    "status": "done",
                }
            ],
            None,
        )
        self.store.upsert_memorization({
            "id": "mem_1",
            "studentId": "stu_hist_1",
            "teacher": old_t,
            "date": "2026-11-08",
            "track": "حفظ جديد",
            "fromSurah": "الملك",
            "pages": 2,
            "score": 99,
        })
        self.store.upsert_certificate({
            "id": "cert_1",
            "studentId": "stu_hist_1",
            "studentName": "مريم طارق",
            "courseTitle": "إتمام جزء تبارك",
            "grade": "ممتاز",
            "issueDate": "2026-11-09",
            "teacherName": old_t,
        })

        # Rename via PUT /api/teachers/{teacher_name}
        r = self.client.put(
            f"/api/teachers/{old_t}",
            json={"oldName": old_t, "newName": new_t, "name": new_t, "pct": 75},
            headers=self.h,
        )
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()
        self.assertTrue(res["ok"])
        self.assertTrue(res["renamed"])
        self.assertEqual(res["oldName"], old_t)
        self.assertEqual(res["name"], new_t)
        self.assertEqual(res["meta"]["pct"], 75)

        # Verify full state and FK integrity
        st = self.store.export_state()
        self.assertIn(new_t, st["teachers"])
        self.assertNotIn(old_t, st["teachers"])
        self.assertEqual(st["students"][0]["teacher"], new_t)
        self.assertEqual(st["students"][0]["transferHistory"][0]["to"], new_t)
        self.assertEqual(st["payouts"][0]["teacher"], new_t)
        self.assertEqual(st["payouts"][0]["amount"], 280)
        self.assertEqual(st["groups"][0]["teacher"], new_t)
        self.assertEqual(st["plans"][0]["teacher"], new_t)
        self.assertEqual(st["sessions"][0]["teacher"], new_t)
        self.assertEqual(st["memorization"][0]["teacher"], new_t)
        self.assertEqual(st["certificates"][0]["teacherName"], new_t)
        self.assertEqual(len(st["installments"]), 1)

        # Verify SQLite foreign_key_check is completely clean
        fk_errors = self.store.db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(fk_errors, [])

    def test_scenario_03_and_04_deactivate_and_reactivate_teacher(self) -> None:
        """Scenario 3 & 4: Deactivating hides teacher from new assignments while preserving records; reactivating restores availability."""
        tname = "أ/ عائشة حسن"
        self.client.post("/api/teachers", json={"name": tname, "pct": 70}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "stu_aisha",
                "name": "حفصة عمر",
                "phone": "01022223333",
                "teacher": tname,
                "type": "خاص",
                "pct": 70,
                "months": {"2026-11": {"value": 300}},
            },
            headers=self.h,
        )

        # Deactivate teacher via PATCH /api/teachers/{name}
        r_deact = self.client.patch(
            f"/api/teachers/{tname}",
            json={"active": False, "archived": True},
            headers=self.h,
        )
        self.assertEqual(r_deact.status_code, 200, r_deact.text)
        self.assertFalse(r_deact.json()["active"])
        self.assertTrue(r_deact.json()["archived"])

        # Active-only query excludes deactivated teacher
        r_active = self.client.get("/api/teachers?include_inactive=false", headers=self.h)
        self.assertEqual(r_active.json()["teachers"], [])
        # All-teachers query still includes her with archived=True
        r_all = self.client.get("/api/teachers?include_inactive=true", headers=self.h)
        self.assertEqual(r_all.json()["teachers"], [tname])
        self.assertTrue(r_all.json()["items"][0]["archived"])

        # Assigning a BRAND-NEW student to a deactivated teacher is rejected (HTTP 400)
        r_new_stu = self.client.post(
            "/api/students",
            json={
                "id": "stu_new_blocked",
                "name": "طالبة جديدة",
                "phone": "01099998888",
                "teacher": tname,
                "type": "خاص",
                "pct": 70,
                "months": {},
            },
            headers=self.h,
        )
        self.assertEqual(r_new_stu.status_code, 400)

        # Updating the EXISTING student already assigned to that deactivated teacher succeeds!
        r_upd_existing = self.client.put(
            "/api/students/stu_aisha",
            json={
                "id": "stu_aisha",
                "name": "حفصة عمر",
                "phone": "01022224444",
                "teacher": tname,
                "type": "خاص",
                "pct": 70,
                "months": {"2026-11": {"value": 350}},
            },
            headers=self.h,
        )
        self.assertEqual(r_upd_existing.status_code, 200, r_upd_existing.text)

        # Reactivate teacher via PATCH /api/teachers/{name}
        r_react = self.client.patch(
            f"/api/teachers/{tname}",
            json={"active": True, "archived": False},
            headers=self.h,
        )
        self.assertEqual(r_react.status_code, 200, r_react.text)
        self.assertTrue(r_react.json()["active"])
        self.assertFalse(r_react.json()["archived"])

        # Now assigning the new student to the reactivated teacher succeeds!
        r_new_stu_ok = self.client.post(
            "/api/students",
            json={
                "id": "stu_new_allowed",
                "name": "طالبة جديدة",
                "phone": "01099998888",
                "teacher": tname,
                "type": "خاص",
                "pct": 70,
                "months": {},
            },
            headers=self.h,
        )
        self.assertEqual(r_new_stu_ok.status_code, 200, r_new_stu_ok.text)

    def test_scenario_05_duplicate_teacher_prevention(self) -> None:
        """Scenario 5: Prevents duplicate teachers across whitespace, Arabic spelling variants, honorifics, and compound spacing."""
        self.client.post(
            "/api/teachers",
            json={"name": "فاطمة عبد الرحمن", "pct": 70, "mode": "create"},
            headers=self.h,
        )

        # 1. Exact duplicate in create mode -> HTTP 409 DUPLICATE_TEACHER
        for variant in (
            "فاطمة عبد الرحمن",
            "  فاطمة   عبد الرحمن  ",
            "فاطمه عبدالرحمن",
            "أ/ فاطمه عبد الرحمن",
            "أ. فاطمة عبدالرحمن",
            "استاذة فاطمه عبد الرحمن",
        ):
            r = self.client.post(
                "/api/teachers",
                json={"name": variant, "pct": 65, "mode": "create"},
                headers=self.h,
            )
            self.assertEqual(r.status_code, 409, f"Expected 409 for variant {variant!r}, got {r.status_code}: {r.text}")
            detail = r.json()["detail"]
            self.assertEqual(detail["code"], "DUPLICATE_TEACHER")
            self.assertEqual(detail["existingTeacher"], "فاطمة عبد الرحمن")

        # Confirm only 1 teacher exists in DB
        self.assertEqual(self.store.export_state()["teachers"], ["فاطمة عبد الرحمن"])

        # 2. Rename collision with another existing teacher -> HTTP 409 DUPLICATE_TEACHER
        self.client.post(
            "/api/teachers",
            json={"name": "خديجة محمود", "pct": 65, "mode": "create"},
            headers=self.h,
        )
        r_rename_dup = self.client.put(
            "/api/teachers/خديجة محمود",
            json={"newName": "أ/ فاطمه عبدالرحمن", "pct": 65},
            headers=self.h,
        )
        self.assertEqual(r_rename_dup.status_code, 409)
        self.assertEqual(r_rename_dup.json()["detail"]["existingTeacher"], "فاطمة عبد الرحمن")

        # 3. Explicit user confirmation (allowDuplicate=True) permits creating a distinct teacher with similar name
        r_forced = self.client.post(
            "/api/teachers",
            json={"name": "فاطمة عبد الرحمن (فرع ٢)", "pct": 60, "mode": "create"},
            headers=self.h,
        )
        self.assertEqual(r_forced.status_code, 200)

    def test_scenario_09_10_11_attendance_salary_and_reports_integration(self) -> None:
        """Scenarios 9, 10, 11: Teacher availability and filtering across attendance, salaries, statistics, XLSX, and HTML exports."""
        t1 = "أ/ سمية ياسر"
        t2 = "أ/ أسماء بكر"
        self.store.upsert_teacher(t1, {"pct": 70})
        self.store.upsert_teacher(t2, {"pct": 60})

        self.store.upsert_student({
            "id": "s_t1",
            "code": "S-1",
            "name": "طالبة سمية",
            "phone": "01011110001",
            "teacher": t1,
            "type": "خاص",
            "pct": 70,
            "months": {"2026-11": {"value": 500, "paymentDate": "2026-11-01", "actualStartDate": "2026-11-02"}},
        })
        self.store.upsert_student({
            "id": "s_t2",
            "code": "S-2",
            "name": "طالبة أسماء",
            "phone": "01011110002",
            "teacher": t2,
            "type": "خاص",
            "pct": 60,
            "months": {"2026-11": {"value": 400, "paymentDate": "2026-11-01", "actualStartDate": "2026-11-02"}},
        })
        self.store.upsert_installment({
            "id": "i_t1", "receiptNo": "R-1", "studentId": "s_t1", "month": "2026-11", "amount": 500, "paymentDate": "2026-11-01"
        })
        self.store.upsert_installment({
            "id": "i_t2", "receiptNo": "R-2", "studentId": "s_t2", "month": "2026-11", "amount": 400, "paymentDate": "2026-11-01"
        })
        self.store.upsert_payout({
            "id": "p_t1", "teacher": t1, "month": "2026-11", "amount": 350, "date": "2026-11-15"
        })
        self.store.upsert_payout({
            "id": "p_t2", "teacher": t2, "month": "2026-11", "amount": 240, "date": "2026-11-15"
        })
        self.store.upsert_session({
            "id": "sess_t1", "studentId": "s_t1", "teacher": t1, "date": "2026-11-05", "time": "16:00", "dur": 40, "status": "done"
        })
        self.store.upsert_session({
            "id": "sess_t2", "studentId": "s_t2", "teacher": t2, "date": "2026-11-05", "time": "17:00", "dur": 40, "status": "absent"
        })

        # Teacher-scoped Statistics endpoint (`/api/statistics?teacher=...`)
        r_stat_t1 = self.client.get(
            f"/api/statistics?period=all_time&teacher={t1}&reference_date=2026-11-20",
            headers=self.h,
        )
        self.assertEqual(r_stat_t1.status_code, 200)
        st_t1 = r_stat_t1.json()
        self.assertEqual(st_t1["kpis"]["totalStudents"], 1)
        self.assertEqual(st_t1["kpis"]["netSubscriptions"], 500)
        self.assertEqual(st_t1["kpis"]["collectedTotal"], 500)
        self.assertEqual(st_t1["kpis"]["teacherWagesAccrued"], 350)
        self.assertEqual(st_t1["kpis"]["teacherWagesPaid"], 350)
        self.assertEqual(st_t1["attendance"]["totalSessions"], 1)
        self.assertEqual(st_t1["attendance"]["doneCount"], 1)
        self.assertEqual(st_t1["attendance"]["absentCount"], 0)

        # Teacher-scoped XLSX export
        r_xlsx = self.client.get(
            f"/api/export/xlsx?scope=teacher&teacher={t1}&month=2026-11",
            headers=self.h,
        )
        self.assertEqual(r_xlsx.status_code, 200)
        self.assertTrue(len(r_xlsx.content) > 1000)

        # Teacher-scoped HTML export
        r_html = self.client.get(
            f"/api/export/html?scope=teacher&teacher={t1}&month=2026-11&hide_personal_data=false",
            headers=self.h,
        )
        self.assertEqual(r_html.status_code, 200)
        self.assertIn(t1, r_html.text)
        self.assertIn("طالبة سمية", r_html.text)
        self.assertNotIn("طالبة أسماء", r_html.text)

    def test_scenario_07_08_13_14_live_frontend_dom_e2e_teacher_lifecycle(self) -> None:
        """Scenarios 7, 8, 13, 14: Live Node.js + Uvicorn E2E test of the actual UI modules (app.js, store.js, router.js, statistics.js)."""
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node binary must be available")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT_DIR)
        server_code = f"""
import uvicorn
from pathlib import Path
from alahd_erp.api.server import create_app

app = create_app(
    home=Path({str(self.home)!r}),
    ui_dir=Path({str(UI_DIR)!r}),
    port={port},
    window_secret={self.secret!r},
    seed_on_empty=False,
)
uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", server_code],
            cwd=str(ROOT_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # Wait for live server to accept connections
            deadline = time.time() + 10.0
            while time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)

            with tempfile.TemporaryDirectory() as tmp_js_dir:
                js_dir = Path(tmp_js_dir)
                shutil.copytree(UI_DIR / "js", js_dir / "js")
                shutil.copy2(UI_DIR / "index.html", js_dir / "index.html")
                (js_dir / "package.json").write_text('{"type": "module"}\n', encoding="utf-8")

                harness = f"""
import fs from 'fs';
import path from 'path';

const BASE_URL = 'http://127.0.0.1:{port}';
const SECRET = {self.secret!r};

// Minimal DOM implementation sufficient for index.html + app.js + router.js + statistics.js
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
    this.dataset = {{}};
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
    this.textContent = this._innerHTML.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
    // Parse <option value="...">...</option> for <select> elements
    if (this.tagName === 'SELECT') {{
      const opts = [];
      const re = /<option\\s+([^>]*)>([\\s\\S]*?)<\\/option>/gi;
      let m;
      while ((m = re.exec(this._innerHTML)) !== null) {{
        const attrs = m[1] || '';
        const vm = /value="([^"]*)"/i.exec(attrs);
        const valStr = vm ? vm[1] : m[2].trim();
        const isSel = /\\bselected\\b/i.test(attrs);
        opts.push({{ value: valStr, text: m[2].replace(/<[^>]+>/g, '').trim(), selected: isSel }});
      }}
      this.options = opts;
      const selOpt = opts.find((o) => o.selected) || opts[0];
      if (selOpt && !opts.some((o) => o.value === this.value)) {{
        this.value = selOpt.value;
      }}
    }}
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
   scrollIntoView() {{}}
  appendChild(c) {{ c.parentElement = this; this.children.push(c); return c; }}
  removeChild(c) {{ this.children = this.children.filter((x) => x !== c); }}
  querySelector() {{ return null; }}
  closest(sel) {{
    if (sel.startsWith('.') && this.classList.contains(sel.slice(1))) return this;
    return this.parentElement ? this.parentElement.closest(sel) : null;
  }}
  getContext() {{
    return {{
      fillStyle: '', strokeStyle: '', lineWidth: 1, textAlign: '', font: '',
      fillRect() {{}}, strokeRect() {{}}, fillText() {{}}
    }};
  }}
}}

const elementsById = new Map();
const allElements = [];
function getOrCreateById(id, tag = 'div') {{
  if (!elementsById.has(id)) {{
    const el = new MiniElement(tag, id);
    elementsById.set(id, el);
    allElements.push(el);
  }}
  return elementsById.get(id);
}}

// Pre-populate elements from index.html
const htmlSrc = fs.readFileSync(path.join({str(js_dir)!r}, 'index.html'), 'utf-8');
const tagIdRe = /<([a-zA-Z0-9]+)\\b[^>]*\\bid="([^"]+)"[^>]*>/g;
let match;
while ((match = tagIdRe.exec(htmlSrc)) !== null) {{
  getOrCreateById(match[2], match[1]);
}}

const winListeners = {{}};
globalThis.window = {{
  location: {{
    href: `${{BASE_URL}}/?wsec=${{SECRET}}`,
    pathname: '/',
    search: '',
    _hash: '#/dashboard',
    get hash() {{ return this._hash; }},
    set hash(v) {{
      this._hash = v;
      (winListeners['hashchange'] || []).forEach((fn) => fn());
    }}
  }},
  history: {{ state: null, replaceState(s, t, url) {{}} }},
  __ALAHD_WSEC__: SECRET,
  addEventListener(ev, fn) {{ (winListeners[ev] = winListeners[ev] || []).push(fn); }},
  CSS: {{ escape: (s) => String(s) }}
}};
globalThis.CSS = globalThis.window.CSS;
globalThis.localStorage = {{ getItem: () => null, setItem: () => {{}} }};
globalThis.document = {{
  readyState: 'complete',
  documentElement: new MiniElement('html'),
  body: new MiniElement('body'),
  getElementById: (id) => elementsById.get(id) || null,
  querySelector: (sel) => {{
    if (sel.startsWith('#')) return getOrCreateById(sel.slice(1));
    return null;
  }},
  querySelectorAll: (sel) => {{
    if (sel === '.nav-btn' || sel === '.view-panel' || sel === '[data-export-scope]') return [];
    return [];
  }},
  createElement: (tag) => new MiniElement(tag),
  addEventListener: () => {{}}
}};

const origFetch = globalThis.fetch;
globalThis.fetch = async (url, opts = {{}}) => {{
  const fullUrl = String(url).startsWith('http') ? url : `${{BASE_URL}}${{url}}`;
  const headers = Object.assign({{ 'X-Window-Secret': SECRET }}, opts.headers || {{}});
  return origFetch(fullUrl, Object.assign({{}}, opts, {{ headers }}));
}};

// Load finance_engine.js onto window.FinanceEngine
const feCode = fs.readFileSync(path.join({str(js_dir)!r}, 'js', 'finance-engine.js'), 'utf-8');
const mod = {{ exports: {{}} }}; (new Function('module', 'exports', 'window', feCode))(mod, mod.exports, globalThis.window); globalThis.window.FinanceEngine = globalThis.window.FinanceEngine || mod.exports;

// Import store.js and app.js
const storeMod = await import('./js/modules/store.js');
const appMod = await import('./js/app.js');

// Wait for initial bootstrap reloadFullStateFromServer
await appMod.reloadFullStateFromServer();

// 1. Add Teacher 1 ("أ/ نسيبة الأنصارية", pct=75) via #formAddTeacher submit
getOrCreateById('tOriginalNameInput', 'input').value = '';
getOrCreateById('tNameInput', 'input').value = '  أ/   نسيبة الأنصارية  ';
getOrCreateById('tPctInput', 'input').value = '75';
await getOrCreateById('formAddTeacher', 'form').dispatchEvent('submit');

const afterAddTeachers = storeMod.state.teachers.slice();
const modalTeacherOptsAfterAdd = (getOrCreateById('modalStudentTeacher', 'select').options || []).map((o) => o.value);
const stuFilterOptsAfterAdd = (getOrCreateById('stuFilterTeacher', 'select').options || []).map((o) => o.value);
const statsFilterOptsAfterAdd = (getOrCreateById('statsTeacherFilter', 'select').options || []).map((o) => o.value);

// 2. Attempt duplicate teacher ("نسيبه الانصاريه") via #formAddTeacher -> must be rejected by duplicate prevention
getOrCreateById('tOriginalNameInput', 'input').value = '';
getOrCreateById('tNameInput', 'input').value = 'نسيبه الانصاريه';
getOrCreateById('tPctInput', 'input').value = '60';
await getOrCreateById('formAddTeacher', 'form').dispatchEvent('submit');
const teachersCountAfterDupAttempt = storeMod.state.teachers.length;

// 3. Add a student assigned to "أ/ نسيبة الأنصارية" via #formStudentModal
getOrCreateById('modalStudentId', 'input').value = '';
getOrCreateById('modalStudentName', 'input').value = 'سلمى خالد';
getOrCreateById('modalStudentPhone', 'input').value = '01055556666';
getOrCreateById('modalStudentCode', 'input').value = 'STU-0001';
getOrCreateById('modalStudentTeacher', 'select').value = 'أ/ نسيبة الأنصارية';
getOrCreateById('modalStudentType', 'select').value = storeMod.state.subTypes[0];
getOrCreateById('modalStudentPct', 'input').value = '75';
getOrCreateById('modalMonthValue', 'input').value = '400';
getOrCreateById('modalMonthDiscount', 'input').value = '0';
getOrCreateById('modalMonthCustomWage', 'input').value = '';
getOrCreateById('modalMonthPaymentDate', 'input').value = '2026-11-03';
getOrCreateById('modalMonthActualStartDate', 'input').value = '2026-11-05';
getOrCreateById('modalMonthEndDate', 'input').value = '2026-12-05';
await getOrCreateById('formStudentModal', 'form').dispatchEvent('submit');

// 4. Rename "أ/ نسيبة الأنصارية" -> "أ/ نسيبة بنت كعب" via #formAddTeacher in edit mode
getOrCreateById('stuFilterTeacher', 'select').value = 'أ/ نسيبة الأنصارية';
getOrCreateById('tOriginalNameInput', 'input').value = 'أ/ نسيبة الأنصارية';
getOrCreateById('tNameInput', 'input').value = 'أ/ نسيبة بنت كعب';
getOrCreateById('tPctInput', 'input').value = '80';
await getOrCreateById('formAddTeacher', 'form').dispatchEvent('submit');

const afterRenameTeachers = storeMod.state.teachers.slice();
const studentTeacherAfterRename = storeMod.state.students[0].teacher;
const stuFilterValueAfterRename = getOrCreateById('stuFilterTeacher', 'select').value;

// 5. Deactivate "أ/ نسيبة بنت كعب" via .btn-deactivate-teacher click on #teachersCardsGrid
const fakeDeactBtn = new MiniElement('button');
fakeDeactBtn.classList.add('btn-deactivate-teacher');
fakeDeactBtn.setAttribute('data-teacher', 'أ/ نسيبة بنت كعب');
await getOrCreateById('teachersCardsGrid', 'div').dispatchEvent({{
  type: 'click',
  target: fakeDeactBtn,
  preventDefault() {{}}
}});

// Open modal for a NEW student -> deactivated teacher must be hidden from #modalStudentTeacher!
getOrCreateById('btnAddStudentMain', 'button').click();
const modalOptsForNewStuWhenDeactivated = (getOrCreateById('modalStudentTeacher', 'select').options || []).map((o) => o.value);
const stuFilterOptsWhenDeactivated = (getOrCreateById('stuFilterTeacher', 'select').options || []).map((o) => o.value);

// 6. Reactivate "أ/ نسيبة بنت كعب" via .btn-restore-teacher click on #teachersCardsGrid
const fakeRestoreBtn = new MiniElement('button');
fakeRestoreBtn.classList.add('btn-restore-teacher');
fakeRestoreBtn.setAttribute('data-teacher', 'أ/ نسيبة بنت كعب');
await getOrCreateById('teachersCardsGrid', 'div').dispatchEvent({{
  type: 'click',
  target: fakeRestoreBtn,
  preventDefault() {{}}
}});

getOrCreateById('btnAddStudentMain', 'button').click();
const modalOptsAfterReactivate = (getOrCreateById('modalStudentTeacher', 'select').options || []).map((o) => o.value);

console.log('E2E_RESULT:' + JSON.stringify({{
  afterAddTeachers,
  modalTeacherOptsAfterAdd,
  stuFilterOptsAfterAdd,
  statsFilterOptsAfterAdd,
  teachersCountAfterDupAttempt,
  afterRenameTeachers,
  studentTeacherAfterRename,
  stuFilterValueAfterRename,
  modalOptsForNewStuWhenDeactivated,
  stuFilterOptsWhenDeactivated,
  modalOptsAfterReactivate
}}));
"""
                (js_dir / "run_phase2_e2e.mjs").write_text(harness, encoding="utf-8")
                run_res = subprocess.run(
                    [node_bin, "run_phase2_e2e.mjs"],
                    cwd=str(js_dir),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(run_res.returncode, 0, f"Node E2E failed:\nSTDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}")
                marker = next(
                    (line for line in run_res.stdout.splitlines() if line.startswith("E2E_RESULT:")),
                    "",
                )
                self.assertTrue(marker, f"Missing E2E_RESULT in:\n{run_res.stdout}")
                data = json.loads(marker.split("E2E_RESULT:", 1)[1])

                # Verify step 1: Added teacher appears in state and all dropdowns/filters
                self.assertEqual(data["afterAddTeachers"], ["أ/ نسيبة الأنصارية"])
                self.assertIn("أ/ نسيبة الأنصارية", data["modalTeacherOptsAfterAdd"])
                self.assertIn("أ/ نسيبة الأنصارية", data["stuFilterOptsAfterAdd"])
                self.assertIn("أ/ نسيبة الأنصارية", data["statsFilterOptsAfterAdd"])

                # Verify step 2: Duplicate spelling variant was rejected
                self.assertEqual(data["teachersCountAfterDupAttempt"], 1)

                # Verify step 4: Renaming updated state, student record, and active filter selection
                self.assertEqual(data["afterRenameTeachers"], ["أ/ نسيبة بنت كعب"])
                self.assertEqual(data["studentTeacherAfterRename"], "أ/ نسيبة بنت كعب")
                self.assertEqual(data["stuFilterValueAfterRename"], "أ/ نسيبة بنت كعب")

                # Verify step 5: Deactivating hid teacher from new student assignment modal while keeping her in filter dropdown
                self.assertNotIn("أ/ نسيبة بنت كعب", data["modalOptsForNewStuWhenDeactivated"])
                self.assertIn("أ/ نسيبة بنت كعب", data["stuFilterOptsWhenDeactivated"])

                # Verify step 6: Reactivating restored teacher to new student assignment modal
                self.assertIn("أ/ نسيبة بنت كعب", data["modalOptsAfterReactivate"])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

    def test_scenario_14_concurrent_rev_and_repeated_idempotent_submissions(self) -> None:
        """Scenario 14: Optimistic concurrency (`rev`) rejects stale writes (409 CONCURRENCY_CONFLICT) while repeated upsert is idempotent."""
        r1 = self.client.post(
            "/api/teachers",
            json={"name": "أ/ هدى سالم", "pct": 70},
            headers=self.h,
        )
        self.assertEqual(r1.status_code, 200)
        rev1 = r1.json()["rev"]

        # Repeated identical upsert is idempotent and does not create a second row
        r2 = self.client.post(
            "/api/teachers",
            json={"name": "  أ/   هدى سالم  ", "pct": 70},
            headers=self.h,
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self.store.export_state()["teachers"], ["أ/ هدى سالم"])

        # Stale `rev` header or body triggers 409 CONCURRENCY_CONFLICT
        r_stale = self.client.put(
            "/api/teachers/أ/ هدى سالم",
            json={"name": "أ/ هدى سالم", "pct": 80, "rev": rev1 - 1},
            headers=self.h,
        )
        self.assertEqual(r_stale.status_code, 409)
        self.assertEqual(r_stale.json()["detail"]["code"], "CONCURRENCY_CONFLICT")

    def test_scenario_15_database_migration_and_state_compatibility(self) -> None:
        """Scenario 15: Legacy teacher rows (only defaultPct, or archived without active) are normalized and pass integrity_check & foreign_key_check."""
        self.store.upsert_teacher("معلمة قديمة", {"pct": 65})
        with self.store.lock:
            self.store.db.execute(
                "UPDATE teachers SET meta=? WHERE name=?",
                (json.dumps({"defaultPct": 60, "archived": True}), "معلمة قديمة"),
            )
            self.store.db.commit()

        reopened = self.reopen()
        st = reopened.export_state()
        self.assertEqual(st["teacherMeta"]["معلمة قديمة"]["pct"], 60)
        self.assertEqual(st["teacherMeta"]["معلمة قديمة"]["defaultPct"], 60)
        self.assertTrue(st["teacherMeta"]["معلمة قديمة"]["archived"])

        items = reopened.list_teachers(include_inactive=True)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["active"])
        self.assertTrue(items[0]["archived"])

        # Verify SQLite PRAGMA integrity_check and foreign_key_check
        with reopened.lock:
            ic = reopened.db.execute("PRAGMA integrity_check").fetchone()[0]
            fk = reopened.db.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(ic, "ok")
        self.assertEqual(fk, [])


if __name__ == "__main__":
    unittest.main()
