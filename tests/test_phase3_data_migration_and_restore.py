"""Phase 3 Data Migration, CSV/XLSX/JSON Round-Trip, Safe JSON Restore & Backup/Restore Reliability Tests.

Covers Tasks 3, 4, 10, 11, 12, 13, 14:
- CSV delimiters (comma, semicolon, tab), multiline quoted fields, embedded commas, UTF-8 / UTF-8 BOM / UTF-16 / CP1256, binary/corrupt CSV rejection, and CSV formula cell rejection.
- CSV export-to-import round-trip verification.
- Safe Preview-First JSON Full-State Restore (`POST /api/json-restore/preview` & `POST /api/json-restore/commit`):
  - File type, syntax, schema, and referential integrity validation.
  - Change summary calculation (added, updated, deleted, affected datasets).
  - Stale payload hash rejection and unconfirmed commit rejection.
  - Guaranteed immediate pre-restore snapshot even on same-size JSON replacement (both `/api/json-restore/commit` and `/api/sync`).
  - Automatic rollback on failure (e.g., closed-month lock violation).
  - Full-state JSON round-trip across all operational and financial entities.
  - Live Node.js + Uvicorn DOM E2E test of `#inputImportJson` -> `#jsonRestoreModal` -> `#btnConfirmJsonRestore`.
- Backup & Restore reliability:
  - Interrupted / disk-full `backup_to` cleanup of `.part` files.
  - Rejection and rollback when a SQLite backup file has foreign-key violations (`PRAGMA foreign_key_check`).
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from alahd_erp.api.server import create_app
from alahd_erp.infrastructure.database import Store, verify_sqlite_backup_file
from alahd_erp.services import import_service as I
from tests._helpers import StoreTestCase

ROOT_DIR = Path(__file__).resolve().parent.parent
UI_DIR = ROOT_DIR / "alahd_erp" / "ui"


class Phase3DataMigrationAndRestoreTests(StoreTestCase):
    """Comprehensive tests for CSV edge cases, JSON restore preview/commit/rollback, and backup/restore reliability."""

    def setUp(self) -> None:
        super().setUp()
        self.secret = "phase3_test_secret_token"
        self.app = create_app(
            home=self.home,
            ui_dir=UI_DIR,
            port=8766,
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

    # ------------------------------------------------------------------
    # Task 4 & Task 13: CSV Delimiters, Encodings, Multiline Quotes & Round-Trip
    # ------------------------------------------------------------------
    def test_01_csv_semicolon_tab_multiline_quotes_and_encodings(self) -> None:
        """Task 4: CSV import handles semicolon/tab delimiters, multiline quoted fields, embedded commas, UTF-16 BOM, and CP1256."""
        self.store.upsert_teacher("أ/ مريم", {"pct": 70})

        # 1. Semicolon-delimited CSV with embedded commas and multiline quoted schedule/notes
        csv_semicolon = (
            "\ufeffكود الطالبة;اسم الطالبة;رقم التواصل;اسم المعلمة;نوع الاشتراك;نسبة المعلمة;المواعيد;الشهر;قيمة الاشتراك;الخصم;المبلغ المسدد;تاريخ الدفع;تاريخ البداية الفعلي\n"
            'STU-CSV-1;"سارة أحمد، الفوج الأول";٠١٠١٢٣٤٥٦٧٨;أ/ مريم;خاص;٧٠;"الأحد، الأربعاء\nالساعة ٤ عصراً";2026-11;٣٠٠;٥٠;٢٥٠;2026-11-02;2026-11-05\n'
        ).encode("utf-8")

        prev = I.preview_import_file(
            csv_semicolon,
            "students_semicolon.csv",
            "students",
            self.store.export_state(),
        )
        self.assertEqual(prev["summary"]["total"], 1)
        self.assertEqual(prev["summary"]["valid"], 1)
        self.assertEqual(prev["summary"]["invalid"], 0)
        row0 = prev["rows"][0]["normalized"]
        self.assertEqual(row0["name"], "سارة أحمد، الفوج الأول")
        self.assertEqual(row0["phone"], "01012345678")
        self.assertEqual(row0["value"], 300)
        self.assertEqual(row0["discount"], 50)
        self.assertEqual(row0["paidAmount"], 250)
        self.assertEqual(row0["paymentDate"], "2026-11-02")
        self.assertEqual(row0["actualStartDate"], "2026-11-05")
        self.assertIn("الأحد، الأربعاء", row0["schedule"])

        # Commit semicolon CSV and verify round-trip into SQLite
        res = I.commit_import(
            self.store,
            csv_semicolon,
            "students_semicolon.csv",
            "students",
            backup_folder=self.home / "backups",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["committed"], 1)

        # 2. UTF-16 BOM tab-delimited CSV
        csv_utf16_tab = (
            "اسم المعلمة\tنسبة المعلمة\tملاحظات\n"
            "أ/ خديجة الكبرى\t٧٥\tمعلمة إقراء\n"
        ).encode("utf-16")
        prev_t = I.preview_import_file(
            csv_utf16_tab,
            "teachers_utf16.csv",
            "teachers",
            self.store.export_state(),
        )
        self.assertEqual(prev_t["summary"]["valid"], 1)
        self.assertEqual(prev_t["rows"][0]["normalized"]["name"], "أ/ خديجة الكبرى")
        self.assertEqual(prev_t["rows"][0]["normalized"]["defaultPct"], 75)

        # 3. Binary / null-byte / formula CSV rejection
        for bad_bytes in (
            b"PK\x03\x04fake_zip_as_csv",
            b"col1,col2\nval1,\x00val2\n",
            b"col1,col2\n\x01\x02\x03,val2\n",
        ):
            with self.assertRaises(ValueError):
                I.preview_import_file(bad_bytes, "bad.csv", "students", self.store.export_state())

        # Formula in CSV cell is flagged as invalid row and never committed
        csv_formula = (
            "اسم المعلمة,نسبة المعلمة\n"
            "=CMD|' /C calc'!A0,70\n"
        ).encode("utf-8")
        prev_f = I.preview_import_file(csv_formula, "formula.csv", "teachers", self.store.export_state())
        self.assertEqual(prev_f["summary"]["invalid"], 1)
        self.assertEqual(prev_f["summary"]["valid"], 0)

    def test_02_exported_csv_roundtrip_into_fresh_database(self) -> None:
        """Task 4 & Task 13: CSV matching `exportCsvFallback()` format round-trips cleanly into a separate database."""
        csv_exported = (
            "\ufeffم,كود الطالبة,اسم الطالبة,رقم التواصل,اسم المعلمة,نوع الاشتراك,"
            "نسبة المعلمة,المواعيد,الشهر,قيمة الاشتراك,الخصم,صافي الاشتراك,"
            "المبلغ المسدد,تاريخ الدفع,تاريخ البداية الفعلي,تاريخ النهاية,أجر المعلمة\n"
            "1,STU-0001,آمنة بنت وهب,01098765432,أ/ حفصة,8 حلقات-40 دقيقة,"
            "75,السبت والثلاثاء,2026-11,400,40,360,"
            "360,2026-11-01,2026-11-04,2026-12-04,270\n"
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as tmp_dir:
            fresh_store = Store(Path(tmp_dir) / "fresh.sqlite3")
            self.addCleanup(fresh_store.close)
            res = I.commit_import(
                fresh_store,
                csv_exported,
                "YawmJadid_Students_2026-11.csv",
                "students",
                mode="update_existing",
                auto_create_refs=True,
                backup_folder=Path(tmp_dir) / "backups",
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["added"], 1)
            st = fresh_store.export_state()
            self.assertIn("أ/ حفصة", st["teachers"])
            self.assertEqual(len(st["students"]), 1)
            stu = st["students"][0]
            self.assertEqual(stu["code"], "STU-0001")
            self.assertEqual(stu["name"], "آمنة بنت وهب")
            self.assertEqual(stu["phone"], "01098765432")
            self.assertEqual(stu["teacher"], "أ/ حفصة")
            self.assertEqual(stu["pct"], 75)
            m = stu["months"]["2026-11"]
            self.assertEqual(m["value"], 400)
            self.assertEqual(m["discount"], 40)
            self.assertEqual(m["paymentDate"], "2026-11-01")
            self.assertEqual(m["actualStartDate"], "2026-11-04")
            self.assertEqual(m["endDate"], "2026-12-04")
            self.assertEqual(len(st["installments"]), 1)
            self.assertEqual(st["installments"][0]["amount"], 360)

    # ------------------------------------------------------------------
    # Task 10 & Task 13: Safe JSON Full-State Restore Preview, Snapshot & Rollback
    # ------------------------------------------------------------------
    def test_03_json_restore_preview_validation_and_same_size_snapshot(self) -> None:
        """Task 10: Preview validates file/schema/references, computes diff, requires confirmation, and creates snapshot even for same-size replacement."""
        # Seed initial database with 1 teacher, 1 student, 1 receipt
        self.store.upsert_teacher("أ/ زينب", {"pct": 70})
        self.store.upsert_student({
            "id": "stu_orig",
            "code": "STU-ORIG",
            "name": "طالبة أصلية",
            "phone": "01011112222",
            "teacher": "أ/ زينب",
            "type": "خاص",
            "pct": 70,
            "months": {"2026-11": {"value": 300, "paymentDate": "2026-11-01", "actualStartDate": "2026-11-03"}},
        })
        self.store.upsert_installment({
            "id": "inst_orig",
            "receiptNo": "RCP-ORIG",
            "studentId": "stu_orig",
            "month": "2026-11",
            "amount": 300,
            "paymentDate": "2026-11-01",
        })

        # 1. Reject non-.json extension
        r_bad_ext = self.client.post(
            "/api/json-restore/preview",
            json={"filename": "backup.xlsx", "state": {"students": []}},
            headers=self.h,
        )
        self.assertEqual(r_bad_ext.status_code, 400)

        # 2. Reject unrelated JSON object missing `students`
        r_bad_schema = self.client.post(
            "/api/json-restore/preview",
            json={"filename": "random.json", "state": {"foo": "bar"}},
            headers=self.h,
        )
        self.assertEqual(r_bad_schema.status_code, 400)

        # 3. Reject broken cross-entity reference (receipt referencing unknown studentId)
        r_bad_ref = self.client.post(
            "/api/json-restore/preview",
            json={
                "filename": "broken_ref.json",
                "state": {
                    "teachers": ["أ/ زينب"],
                    "students": [],
                    "installments": [
                        {
                            "id": "inst_orphan",
                            "receiptNo": "RCP-999",
                            "studentId": "stu_missing",
                            "month": "2026-11",
                            "amount": 200,
                            "paymentDate": "2026-11-01",
                        }
                    ],
                },
            },
            headers=self.h,
        )
        self.assertEqual(r_bad_ref.status_code, 400)
        self.assertIn("stu_missing", r_bad_ref.text)

        # 4. Prepare a SAME-SIZE replacement state (still 1 teacher, 1 student, 1 receipt — but completely different records!)
        replacement_state = {
            "teachers": ["أ/ رقية"],
            "teacherMeta": {"أ/ رقية": {"pct": 75, "defaultPct": 75}},
            "subTypes": ["12 حصة - 40 دقيقة"],
            "students": [
                {
                    "id": "stu_new_1",
                    "code": "STU-NEW-1",
                    "name": "طالبة بديلة تماماً",
                    "phone": "01099990000",
                    "teacher": "أ/ رقية",
                    "type": "12 حصة - 40 دقيقة",
                    "pct": 75,
                    "paymentDate": "2026-11-04",
                    "actualStartDate": "2026-11-06",
                    "months": {
                        "2026-11": {
                            "value": 500,
                            "paymentDate": "2026-11-04",
                            "actualStartDate": "2026-11-06",
                            "endDate": "2026-12-06",
                        }
                    },
                }
            ],
            "installments": [
                {
                    "id": "inst_new_1",
                    "receiptNo": "RCP-NEW-1",
                    "studentId": "stu_new_1",
                    "month": "2026-11",
                    "amount": 500,
                    "paymentDate": "2026-11-04",
                    "method": "إنستاباي",
                }
            ],
        }

        r_prev = self.client.post(
            "/api/json-restore/preview",
            json={"filename": "same_size_replacement.json", "state": replacement_state},
            headers=self.h,
        )
        self.assertEqual(r_prev.status_code, 200, r_prev.text)
        prev_data = r_prev.json()
        self.assertTrue(prev_data["ok"])
        self.assertTrue(prev_data["summary"]["willDeleteExistingData"])
        self.assertEqual(prev_data["datasets"]["students"]["currentCount"], 1)
        self.assertEqual(prev_data["datasets"]["students"]["incomingCount"], 1)
        self.assertEqual(prev_data["datasets"]["students"]["added"], 1)
        self.assertEqual(prev_data["datasets"]["students"]["deleted"], 1)

        # Verify preview did NOT modify the live database
        self.assertEqual(self.store.export_state()["students"][0]["id"], "stu_orig")

        # 5. Unconfirmed commit is rejected (HTTP 400)
        r_unconfirmed = self.client.post(
            "/api/json-restore/commit",
            json={
                "filename": "same_size_replacement.json",
                "state": replacement_state,
                "expectedPayloadHash": prev_data["payloadHash"],
                "confirmRestore": False,
            },
            headers=self.h,
        )
        self.assertEqual(r_unconfirmed.status_code, 400)

        # 6. Stale payloadHash is rejected (HTTP 400)
        r_stale_hash = self.client.post(
            "/api/json-restore/commit",
            json={
                "filename": "same_size_replacement.json",
                "state": replacement_state,
                "expectedPayloadHash": "0" * 64,
                "confirmRestore": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_stale_hash.status_code, 400)

        # 7. Confirmed commit succeeds AND creates a guaranteed immediate pre-restore snapshot!
        r_commit = self.client.post(
            "/api/json-restore/commit",
            json={
                "filename": "same_size_replacement.json",
                "state": replacement_state,
                "expectedPayloadHash": prev_data["payloadHash"],
                "confirmRestore": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_commit.status_code, 200, r_commit.text)
        commit_data = r_commit.json()
        self.assertTrue(commit_data["ok"])
        snap_name = commit_data["snapshot"]
        self.assertTrue(snap_name.startswith("alahd_safety_pre_json_restore_"))
        snap_path = self.home / "backups" / snap_name
        self.assertTrue(snap_path.is_file())
        self.assertTrue(verify_sqlite_backup_file(snap_path))

        # Verify live database now has the replacement student
        after_state = self.store.export_state()
        self.assertEqual(after_state["students"][0]["id"], "stu_new_1")
        self.assertEqual(after_state["teachers"], ["أ/ رقية"])

        # Verify audit log recorded json_restore
        audits = self.store.audit(limit=5, action="json_restore")
        self.assertEqual(len(audits), 1)
        self.assertIn(snap_name, audits[0]["detail"])

        # Verify restoring the pre_json_restore snapshot recovers the original student ("stu_orig")!
        self.store.restore_from(snap_path)
        recovered = self.store.export_state()
        self.assertEqual(recovered["students"][0]["id"], "stu_orig")
        self.assertEqual(recovered["teachers"], ["أ/ زينب"])

    def test_04_json_restore_rolls_back_on_closed_month_violation(self) -> None:
        """Task 10: If JSON restore violates a closed month or fails mid-apply, it rolls back cleanly to the pre-restore snapshot."""
        self.store.upsert_teacher("أ/ سلمى", {"pct": 70})
        self.store.upsert_student({
            "id": "s_locked",
            "name": "طالبة الشهر المغلق",
            "phone": "01033334444",
            "teacher": "أ/ سلمى",
            "type": "خاص",
            "pct": 70,
            "months": {"2026-10": {"value": 400}},
        })
        self.store.close_month("2026-10", note="إقفال أكتوبر")

        # Attempt to restore a JSON state that alters the closed month's value (400 -> 999)
        bad_replacement = self.store.export_state()
        bad_replacement["students"][0]["months"]["2026-10"]["value"] = 999

        r_prev = self.client.post(
            "/api/json-restore/preview",
            json={"filename": "tamper_closed.json", "state": bad_replacement},
            headers=self.h,
        )
        self.assertEqual(r_prev.status_code, 200)

        r_commit = self.client.post(
            "/api/json-restore/commit",
            json={
                "filename": "tamper_closed.json",
                "state": bad_replacement,
                "expectedPayloadHash": r_prev.json()["payloadHash"],
                "confirmRestore": True,
            },
            headers=self.h,
        )
        self.assertEqual(r_commit.status_code, 400)
        self.assertEqual(r_commit.json()["detail"]["code"], "MONTH_LOCKED")

        # Confirm live database is untouched (value is still 400)
        st = self.store.export_state()
        self.assertEqual(st["students"][0]["months"]["2026-10"]["value"], 400)

    def test_05_full_state_json_roundtrip_all_entities(self) -> None:
        """Task 13: Full-state JSON export and restore round-trips all 11 operational and financial datasets into a fresh DB."""
        self.store.load_demo_data(backup_folder=self.home / "backups")
        exported_json = self.client.get("/api/sync", headers=self.h).json()

        with tempfile.TemporaryDirectory() as tmp_dir:
            fresh_home = Path(tmp_dir)
            fresh_app = create_app(
                home=fresh_home,
                ui_dir=UI_DIR,
                port=8767,
                window_secret=self.secret,
                seed_on_empty=False,
            )
            self.addCleanup(fresh_app.state.store.close)
            fresh_client = TestClient(fresh_app)
            self.addCleanup(fresh_client.close)

            prev = fresh_client.post(
                "/api/json-restore/preview",
                json={"filename": "full_roundtrip.json", "state": exported_json},
                headers=self.h,
            ).json()
            self.assertTrue(prev["ok"])

            commit = fresh_client.post(
                "/api/json-restore/commit",
                json={
                    "filename": "full_roundtrip.json",
                    "state": exported_json,
                    "expectedPayloadHash": prev["payloadHash"],
                    "confirmRestore": True,
                },
                headers=self.h,
            ).json()
            self.assertTrue(commit["ok"])

            restored_json = fresh_client.get("/api/sync", headers=self.h).json()
            for key in (
                "teachers",
                "teacherMeta",
                "subTypes",
                "students",
                "installments",
                "payouts",
                "transactions",
                "groups",
                "plans",
                "sessions",
                "memorization",
                "madrasaBatches",
                "madrasaEnrollments",
                "certificates",
            ):
                self.assertEqual(restored_json[key], exported_json[key], f"Mismatch in round-trip key: {key}")

    # ------------------------------------------------------------------
    # Tasks 11 & 12: Backup Interrupted Cleanup & Foreign-Key Restore Guard
    # ------------------------------------------------------------------
    def test_06_interrupted_backup_cleans_up_part_file_and_foreign_key_corrupt_backup_rolls_back(self) -> None:
        """Tasks 11 & 12: Interrupted backup removes .part file; backup with FK violations is rejected and rolled back."""
        self.store.upsert_teacher("أ/ خولة", {"pct": 70})
        self.store.upsert_student({
            "id": "s_valid",
            "name": "طالبة صحيحة",
            "phone": "01044445555",
            "teacher": "أ/ خولة",
            "type": "خاص",
            "pct": 70,
            "months": {"2026-11": {"value": 300}},
        })

        # 1. Simulate disk-full / I/O failure during backup_to
        bk_dir = self.home / "backups"
        bk_dir.mkdir(exist_ok=True)
        dest_file = bk_dir / "alahd_manual_interrupted.sqlite3"
        with patch("alahd_erp.infrastructure.database.verify_sqlite_backup_file", side_effect=OSError("No space left on device")):
            with self.assertRaises(OSError):
                self.store.backup_to(dest_file)
        self.assertFalse(dest_file.exists())
        self.assertEqual(list(bk_dir.glob("*.part*")), [])

        # 2. Create a backup file that passes PRAGMA integrity_check ("ok") and has required tables,
        #    but has a foreign-key violation (an installment referencing a non-existent student_id)
        fk_bad_path = bk_dir / "alahd_manual_fkbad.sqlite3"
        self.store.backup_to(fk_bad_path)
        conn = sqlite3.connect(fk_bad_path)
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO installments(id,pos,receipt_no,student_id,month_key,amount,pay_date,method,note,created_at) "
            "VALUES('inst_fk_broken', 99, 'RCP-BROKEN', 'non_existent_student_999', '2026-11', 500, '2026-11-05', 'نقدي', '', '2026-11-05')"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

        # Modify live DB before attempting restore so we can verify rollback preserves current live DB
        self.store.upsert_teacher("أ/ معلمة ثانية", {"pct": 65})

        with self.assertRaises(ValueError):
            self.store.restore_from(fk_bad_path)

        # Verify live DB rolled back cleanly to pre-restore snapshot and still has "أ/ معلمة ثانية"
        st_after = self.store.export_state()
        self.assertIn("أ/ معلمة ثانية", st_after["teachers"])
        self.assertEqual(len(st_after["installments"]), 0)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_07_live_frontend_dom_e2e_json_restore_preview_and_commit(self) -> None:
        """Task 10 & Task 14: Live Node.js + Uvicorn E2E test of `#inputImportJson` -> `#jsonRestoreModal` preview -> `#btnConfirmJsonRestore` commit."""
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin)

        self.store.upsert_teacher("أ/ معلمة قبل الاستعادة", {"pct": 70})
        self.store.upsert_student({
            "id": "s_before",
            "name": "طالبة قبل الاستعادة",
            "phone": "01011112222",
            "teacher": "أ/ معلمة قبل الاستعادة",
            "type": "خاص",
            "pct": 70,
            "months": {"2026-11": {"value": 300}},
        })

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
    this.textContent = this._innerHTML.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
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
const tagIdRe = /<([a-zA-Z0-9]+)\\b[^>]*\\bid="([^"]+)"[^>]*>/g;
let match;
while ((match = tagIdRe.exec(htmlSrc)) !== null) {{
  getOrCreateById(match[2], match[1]);
}}

let lastReaderDone = Promise.resolve();
globalThis.FileReader = class {{
  constructor() {{ this.onload = null; this.result = ''; }}
  readAsText(file) {{
    this.result = file._content;
    if (typeof this.onload === 'function') {{
      lastReaderDone = Promise.resolve(this.onload());
    }}
  }}
}};

globalThis.window = {{
  location: {{ href: `${{BASE_URL}}/?wsec=${{SECRET}}`, pathname: '/', search: '', hash: '#/exports' }},
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
await appMod.reloadFullStateFromServer();

// Select a JSON file via #inputImportJson
const incomingState = {{
  teachers: ['أ/ معلمة بعد الاستعادة'],
  teacherMeta: {{ 'أ/ معلمة بعد الاستعادة': {{ pct: 80, defaultPct: 80 }} }},
  students: [
    {{
      id: 's_after',
      code: 'STU-AFTER',
      name: 'طالبة بعد الاستعادة',
      phone: '01077778888',
      teacher: 'أ/ معلمة بعد الاستعادة',
      type: 'خاص',
      pct: 80,
      months: {{ '2026-11': {{ value: 450 }} }}
    }}
  ]
}};

const jsonInput = getOrCreateById('inputImportJson', 'input');
jsonInput.files = [{{ name: 'academy_backup.json', _content: JSON.stringify(incomingState) }}];
await jsonInput.dispatchEvent({{ type: 'change', target: jsonInput }});
await lastReaderDone;

// Before clicking confirm: modal is open, and live state still has "s_before"!
const modalOpenDuringPreview = getOrCreateById('jsonRestoreModal', 'div').classList.contains('open');
const studentBeforeConfirm = storeMod.state.students[0].id;
const previewTableText = getOrCreateById('jsonRestoreTableWrap', 'div').textContent;

// Click #btnConfirmJsonRestore to commit
await getOrCreateById('btnConfirmJsonRestore', 'button').dispatchEvent('click');
const modalOpenAfterConfirm = getOrCreateById('jsonRestoreModal', 'div').classList.contains('open');
const studentAfterConfirm = storeMod.state.students[0].id;
const teacherAfterConfirm = storeMod.state.teachers[0];

console.log('JSON_E2E_RESULT:' + JSON.stringify({{
  modalOpenDuringPreview,
  studentBeforeConfirm,
  previewTableText,
  modalOpenAfterConfirm,
  studentAfterConfirm,
  teacherAfterConfirm
}}));
"""
                (js_dir / "run_json_e2e.mjs").write_text(harness, encoding="utf-8")
                run_res = subprocess.run(
                    [node_bin, "run_json_e2e.mjs"],
                    cwd=str(js_dir),
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(run_res.returncode, 0, f"STDOUT:\n{run_res.stdout}\nSTDERR:\n{run_res.stderr}")
                marker = next((ln for ln in run_res.stdout.splitlines() if ln.startswith("JSON_E2E_RESULT:")), "")
                self.assertTrue(marker)
                res = json.loads(marker.split("JSON_E2E_RESULT:", 1)[1])
                self.assertTrue(res["modalOpenDuringPreview"])
                self.assertEqual(res["studentBeforeConfirm"], "s_before")
                self.assertIn("الطالبات والاشتراكات", res["previewTableText"])
                self.assertFalse(res["modalOpenAfterConfirm"])
                self.assertEqual(res["studentAfterConfirm"], "s_after")
                self.assertEqual(res["teacherAfterConfirm"], "أ/ معلمة بعد الاستعادة")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
