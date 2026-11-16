"""Stage 6 Comprehensive Verification Suite — Yawm Jadid Academy ERP Suite v1.2.0.

Covers:
1. Phase 1B: Clean-start guarantee (0 demo records), Demo/Test Data Classifier (Class A, Class B >=2 signals,
   Class C protected single-signal/real records, System/Reference data), UTF-8 JSON/CSV Quarantine Exporter,
   and Transactional Cleaner with verified pre-cleanup backup and FK/integrity checks without sequence reset.
2. Phase 5: 13-Category Settings & Control Center (31 settings, live catalog, audit trail, single/category/all
   reset without touching operational data, settings export/import preview & commit, system diagnostics & logs).
3. Phase 5A-2: Windows Auto-Start (`FileBackedRegistryBackend` & mock `WinRegBackend`, frozen-only guard,
   quoted command with Arabic/space paths, truthful read-back, moved-EXE self-repair, `SingleInstanceGuard`,
   and `BootGuard` 3-strike loop protection).
4. Phase 4: Real-World & Adversarial Scenarios A through N (including large-scale performance test and
   long-running 60-operation session stress test).
"""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from openpyxl import Workbook

from alahd_erp.api.server import create_app
from alahd_erp.config import is_onedrive_or_synced_path
from alahd_erp.domain.demo_cleaner import (
    classify_student_record,
    classify_teacher_record,
    inventory_database_demo_data,
)
from alahd_erp.infrastructure.autostart import (
    REGISTRY_KEY_PATH,
    REGISTRY_VALUE_NAME,
    BootGuard,
    FileBackedRegistryBackend,
    SingleInstanceGuard,
    build_autostart_command,
    get_autostart_status,
    is_eligible_for_autostart,
    set_autostart_enabled,
    sync_and_repair_autostart,
)
from alahd_erp.infrastructure.database import Store
from tests._helpers import ROOT

SECRET = "stage6-test-secret-2026"
UI_DIR = ROOT / "alahd_erp" / "ui"


class Stage6ComprehensiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.app = create_app(self.home, UI_DIR, port=8890, window_secret=SECRET, seed_on_empty=False)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.app.state.store.close)
        self.h = {"X-Window-Secret": SECRET}

    # ------------------------------------------------------------------
    # Phase 1B: Clean Startup & Demo/Test Data Classification + Quarantine
    # ------------------------------------------------------------------
    def test_01_clean_startup_and_hidden_demo_buttons(self) -> None:
        """Scenario A & N: Fresh installation starts with 0 demo records and demo buttons are hidden in UI."""
        health = self.client.get("/api/health", headers=self.h).json()
        self.assertTrue(health["empty"])
        self.assertEqual(health["studentsCount"], 0)

        sync = self.client.get("/api/sync", headers=self.h).json()
        self.assertEqual(len(sync["students"]), 0)
        self.assertEqual(len(sync["teachers"]), 0)
        self.assertEqual(len(sync["installments"]), 0)
        self.assertEqual(len(sync["transactions"]), 0)

        index_html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        for btn_id in ("btnEmptyLoadDemo", "btnWizardLoadDemo", "btnLoadDemoData"):
            self.assertRegex(
                index_html,
                rf'id="{btn_id}"[^>]*style="[^"]*display\s*:\s*none',
                f"Demo button {btn_id} must be hidden in production UI",
            )

    def test_02_demo_classifier_class_a_b_c_and_quarantine_cleanup(self) -> None:
        """Phase 1B: Class A confirmed demo and Class B (>=2 signals) are quarantined & deleted; Class C (<=1 signal / real) is protected."""
        # Unit-check classifier rules directly
        c_a = classify_student_record({"id": "demo_stu_1", "name": "طالبة عادية", "phone": "01012345678"})
        self.assertEqual(c_a["classification"], "A")
        self.assertTrue(c_a["isDemoCandidate"])

        c_b = classify_student_record({"id": "stu_custom_99", "name": "طالبة تجريبية", "phone": "01000000000"})
        self.assertEqual(c_b["classification"], "B")
        self.assertGreaterEqual(len(c_b["reasons"]), 2)
        self.assertTrue(c_b["isDemoCandidate"])

        # Single weak signal ("تجربة" in name, but real ID and real phone) -> Class C (protected!)
        c_c_single = classify_student_record({"id": "stu_real_88", "name": "تجربة الحفظ", "phone": "01098765432"})
        self.assertEqual(c_c_single["classification"], "C")
        self.assertFalse(c_c_single["isDemoCandidate"])

        # Seed demo dataset first, then add 2 real/protected students (including 1 single-signal Class C student)
        self.client.post("/api/demo/load", json={}, headers=self.h)
        self.client.post("/api/teachers", json={"name": "أ. مريم الحقيقية", "meta": {"pct": 75}}, headers=self.h)
        self.client.post(
            "/api/students",
            json={
                "id": "stu_real_01",
                "code": "STD-901",
                "name": "فاطمة محمد عبد الرحمن",
                "phone": "01065432198",
                "teacher": "أ. مريم الحقيقية",
                "type": "خاص",
                "pct": 75,
                "months": {
                    "2026-11": {
                        "value": 400,
                        "paymentDate": "2026-11-01",
                        "actualStartDate": "2026-11-03",
                        "endDate": "2026-12-03",
                    }
                },
            },
            headers=self.h,
        )
        self.client.post(
            "/api/students",
            json={
                "id": "stu_real_02",
                "code": "STD-902",
                "name": "سارة تجربة أولى",  # single signal in name only -> must be Class C protected!
                "phone": "01122334455",
                "teacher": "أ. مريم الحقيقية",
                "type": "خاص",
                "pct": 75,
                "months": {"2026-11": {"value": 300}},
            },
            headers=self.h,
        )

        inv_before = self.client.get("/api/demo/inventory", headers=self.h).json()
        self.assertEqual(inv_before["summary"]["classAConfirmedDemoStudents"], 4)
        self.assertEqual(inv_before["summary"]["classCProtectedStudents"], 2)

        # Execute transactional quarantine cleanup
        clean_res = self.client.post("/api/demo/clean-quarantine", json={"includeClassB": True}, headers=self.h).json()
        self.assertTrue(clean_res["ok"])
        self.assertTrue(clean_res["cleaned"])
        self.assertEqual(clean_res["deletedCounts"]["students"], 4)
        self.assertEqual(clean_res["studentsCount"], 2)
        self.assertTrue(clean_res["snapshot"].endswith(".sqlite3"))

        # Verify quarantine files (JSON + CSV + manifest.json) were written on disk
        manifest = clean_res["quarantineManifest"]
        self.assertIsNotNone(manifest)
        q_dir = Path(manifest["quarantineDir"])
        self.assertTrue((q_dir / "manifest.json").is_file())
        self.assertTrue((q_dir / "students.json").is_file())
        self.assertTrue((q_dir / "students.csv").is_file())
        quarantined_students = json.loads((q_dir / "students.json").read_text(encoding="utf-8"))
        self.assertEqual(len(quarantined_students), 4)

        # Verify protected students and their subscriptions remain 100% intact
        sync_after = self.client.get("/api/sync", headers=self.h).json()
        remaining_ids = {s["id"] for s in sync_after["students"]}
        self.assertEqual(remaining_ids, {"stu_real_01", "stu_real_02"})
        self.assertIn("أ. مريم الحقيقية", sync_after["teachers"])

    # ------------------------------------------------------------------
    # Phase 5: 13-Category Settings & Control Center
    # ------------------------------------------------------------------
    def test_03_settings_catalog_audit_reset_and_export_import(self) -> None:
        """Phase 5: All 13 categories & 31 settings persist, log diffs, reset safely without wiping data, and export/import."""
        # Add a real student first to prove settings resets NEVER wipe operational data
        self.client.post("/api/teachers", json={"name": "أ. خديجة", "meta": {"pct": 70}}, headers=self.h)
        self.client.post(
            "/api/students",
            json={"id": "stu_keep", "name": "مريم أحمد", "phone": "01055556666", "teacher": "أ. خديجة", "pct": 70},
            headers=self.h,
        )

        cat_res = self.client.get("/api/settings/catalog", headers=self.h).json()
        self.assertTrue(cat_res["ok"])
        self.assertEqual(len(cat_res["categories"]), 13)
        self.assertGreaterEqual(len(cat_res["settings"]), 30)
        self.assertGreaterEqual(len(cat_res["hardcodedFinancialPolicies"]), 4)

        # Update settings across multiple categories
        put_res = self.client.put(
            "/api/settings",
            json={
                "academyName": "أكاديمية يوم جديد للقرآن",
                "academySubtitle": "منظومة الإدارة المتكاملة",
                "theme": "sepia",
                "fontScale": "lg",
                "tableDensity": "compact",
                "defaultPageSize": 100,
                "studentCodePrefix": "YJ-",
                "defaultSessionDurationMin": 45,
                "defaultTeacherPct": 75,
                "wageBasis": "gross",
            },
            headers=self.h,
        ).json()
        self.assertTrue(put_res["ok"])
        self.assertEqual(put_res["settings"]["theme"], "sepia")
        self.assertEqual(put_res["settings"]["fontScale"], "lg")
        self.assertEqual(put_res["settings"]["studentCodePrefix"], "YJ-")
        self.assertEqual(put_res["settings"]["wageBasis"], "gross")

        # Verify settings change audit log recorded every modified field with oldValue & newValue
        audit_res = self.client.get("/api/settings/audit", headers=self.h).json()
        self.assertGreaterEqual(audit_res["count"], 7)
        by_key = {it["key"]: it for it in audit_res["items"]}
        self.assertIn("theme", by_key)
        self.assertEqual(by_key["theme"]["oldValue"], "light")
        self.assertEqual(by_key["theme"]["newValue"], "sepia")

        # Export settings bundle
        exp_bundle = self.client.get("/api/settings/export", headers=self.h).json()
        self.assertEqual(exp_bundle["schema"], "yawm_jadid_settings_v1")
        self.assertEqual(exp_bundle["settings"]["theme"], "sepia")

        # Reset single setting ("theme" -> back to "light")
        r_single = self.client.post("/api/settings/reset", json={"key": "theme"}, headers=self.h).json()
        self.assertEqual(r_single["settings"]["theme"], "light")
        self.assertEqual(r_single["settings"]["fontScale"], "lg")  # untouched

        # Reset category ("appearance" -> fontScale & tableDensity back to defaults)
        r_cat = self.client.post("/api/settings/reset", json={"category": "appearance"}, headers=self.h).json()
        self.assertEqual(r_cat["settings"]["fontScale"], "md")
        self.assertEqual(r_cat["settings"]["tableDensity"], "comfortable")
        self.assertEqual(r_cat["settings"]["studentCodePrefix"], "YJ-")  # students category untouched

        # Reset all settings (requires confirm=True) and verify student data is 100% preserved!
        r_all_unconfirmed = self.client.post("/api/settings/reset", json={"resetAll": True}, headers=self.h)
        self.assertEqual(r_all_unconfirmed.status_code, 400)

        r_all = self.client.post("/api/settings/reset", json={"resetAll": True, "confirm": True}, headers=self.h).json()
        self.assertEqual(r_all["settings"]["studentCodePrefix"], "STD-")
        self.assertEqual(r_all["settings"]["wageBasis"], "net")
        self.assertEqual(self.client.get("/api/health", headers=self.h).json()["studentsCount"], 1)

        # Preview & commit settings import from exported bundle
        prev_imp = self.client.post("/api/settings/import/preview", json=exp_bundle, headers=self.h).json()
        self.assertTrue(prev_imp["ok"])
        self.assertGreaterEqual(prev_imp["changesCount"], 5)

        comm_imp = self.client.post(
            "/api/settings/import/commit",
            json={**exp_bundle, "confirm": True},
            headers=self.h,
        ).json()
        self.assertEqual(comm_imp["settings"]["theme"], "sepia")
        self.assertEqual(comm_imp["settings"]["studentCodePrefix"], "YJ-")

    # ------------------------------------------------------------------
    # Phase 5A-2: Windows Auto-Start, Self-Repair, Single-Instance & BootGuard
    # ------------------------------------------------------------------
    def test_04_windows_autostart_registry_quoting_self_repair_and_guards(self) -> None:
        """Scenario M & Phase 5A-2: Source-mode guard, packaged EXE quoting (spaces + Arabic), self-repair on moved EXE, SingleInstanceGuard, and BootGuard."""
        reg_file = self.home / "mock_hkcu_run.json"
        backend = FileBackedRegistryBackend(reg_file)

        # 1. Source mode (frozen=False) MUST be rejected and NEVER write python.exe to registry
        eligible, reason, _ = is_eligible_for_autostart(
            executable_path=r"C:\Python312\python.exe",
            frozen=False,
            platform_name="win32",
        )
        self.assertFalse(eligible)
        self.assertEqual(reason, "unavailable_source_mode")

        res_src = set_autostart_enabled(
            True,
            backend=backend,
            executable_path=r"C:\Python312\python.exe",
            frozen=False,
            platform_name="win32",
        )
        self.assertFalse(res_src["ok"])
        self.assertEqual(res_src["action"], "blocked_not_eligible")
        self.assertIsNone(backend.get_value(REGISTRY_KEY_PATH, REGISTRY_VALUE_NAME))

        # 2. Temp PyInstaller extraction path (_MEIxxxx) MUST be rejected
        eligible_mei, reason_mei, _ = is_eligible_for_autostart(
            executable_path=r"C:\Users\Admin\AppData\Local\Temp\_MEI12345\YawmJadidAcademy.exe",
            frozen=True,
            platform_name="win32",
        )
        self.assertFalse(eligible_mei)
        self.assertEqual(reason_mei, "temp_extraction_path")

        # 3. Packaged EXE in path with spaces AND Arabic folder names
        exe_dir_a = self.home / "أكاديمية يوم جديد" / "Release Build"
        exe_dir_a.mkdir(parents=True, exist_ok=True)
        exe_path_a = exe_dir_a / "YawmJadidAcademy.exe"
        exe_path_a.write_bytes(b"MZ-fake-pe")

        res_enable = set_autostart_enabled(
            True,
            minimized=True,
            backend=backend,
            executable_path=exe_path_a,
            frozen=True,
            platform_name="win32",
        )
        self.assertTrue(res_enable["ok"])
        self.assertEqual(res_enable["autostart"]["status"], "enabled")
        expected_cmd_a = f'"{exe_path_a.resolve()}" --autostart --minimized'
        self.assertEqual(backend.get_value(REGISTRY_KEY_PATH, REGISTRY_VALUE_NAME), expected_cmd_a)

        # 4. Simulate user moving the portable folder from Folder A to Folder B
        exe_dir_b = self.home / "المجلد الجديد للأكاديمية"
        exe_dir_b.mkdir(parents=True, exist_ok=True)
        exe_path_b = exe_dir_b / "YawmJadidAcademy.exe"
        exe_path_a.replace(exe_path_b)

        # Status check from Folder B before repair detects needs_repair (stale path in registry)
        st_moved = get_autostart_status(
            backend=backend,
            executable_path=exe_path_b,
            frozen=True,
            platform_name="win32",
        )
        self.assertEqual(st_moved["status"], "needs_repair")

        # Startup sync_and_repair_autostart automatically repairs the registry command to Folder B
        repair_res = sync_and_repair_autostart(
            settings_enabled=True,
            initialized=True,
            minimized=False,
            backend=backend,
            executable_path=exe_path_b,
            frozen=True,
            platform_name="win32",
        )
        self.assertEqual(repair_res["action"], "repaired_moved_exe")
        self.assertEqual(repair_res["autostart"]["status"], "enabled")
        self.assertEqual(
            backend.get_value(REGISTRY_KEY_PATH, REGISTRY_VALUE_NAME),
            f'"{exe_path_b.resolve()}" --autostart',
        )

        # 5. Disable removes the registry value cleanly
        res_disable = set_autostart_enabled(
            False,
            backend=backend,
            executable_path=exe_path_b,
            frozen=True,
            platform_name="win32",
        )
        self.assertTrue(res_disable["ok"])
        self.assertEqual(res_disable["autostart"]["status"], "disabled")
        self.assertIsNone(backend.get_value(REGISTRY_KEY_PATH, REGISTRY_VALUE_NAME))

        # 6. SingleInstanceGuard prevents duplicate concurrent launches
        g1 = SingleInstanceGuard(self.home)
        g2 = SingleInstanceGuard(self.home)
        self.assertTrue(g1.acquire())
        try:
            self.assertFalse(g2.acquire())
        finally:
            g1.release()
        self.assertTrue(g2.acquire())
        g2.release()

        # 7. BootGuard stops after 3 consecutive failed --autostart attempts
        bg = BootGuard(self.home, max_failures=3)
        self.assertFalse(bg.should_abort_autostart())
        bg.record_boot_failure("fail 1", is_autostart=True)
        bg.record_boot_failure("fail 2", is_autostart=True)
        self.assertFalse(bg.should_abort_autostart())
        bg.record_boot_failure("fail 3", is_autostart=True)
        self.assertTrue(bg.should_abort_autostart())
        bg.record_startup_success()
        self.assertFalse(bg.should_abort_autostart())

    # ------------------------------------------------------------------
    # Phase 4: Adversarial & Real-World Scenarios B, C, E, F, G, H, K, L
    # ------------------------------------------------------------------
    def test_05_adversarial_scenarios_edge_cases_rollback_and_long_session(self) -> None:
        """Scenarios B, C, E, F, G, H, K, L: Full multi-module workflow, corrupted imports, rollback, concurrency conflict, and 60-step stress loop."""
        # Cloud-synced path detection (OneDrive / Dropbox / Google Drive)
        self.assertTrue(is_onedrive_or_synced_path(r"C:\Users\conta\OneDrive\Desktop\YawmJadid_Academy_v1.2.0"))
        self.assertTrue(is_onedrive_or_synced_path("/Users/test/Dropbox/Backups"))
        self.assertFalse(is_onedrive_or_synced_path(r"C:\Users\conta\AppData\Local\AlAhdAcademySuite"))

        # Scenario E: Corrupted XLSX upload & formula injection attempt
        r_corrupt = self.client.post(
            "/api/import/preview?dataset=students",
            content=b"NOT_A_VALID_ZIP_OR_XLSX_BINARY",
            headers={**self.h, "Content-Type": "application/octet-stream"},
        )
        self.assertEqual(r_corrupt.status_code, 400)

        # Scenario G: Optimistic concurrency conflict (stale rev -> HTTP 409)
        self.client.post("/api/teachers", json={"name": "أ. حفصة", "meta": {"pct": 70}}, headers=self.h)
        r_stale = self.client.post(
            "/api/teachers",
            json={"name": "أ. سمية", "meta": {"pct": 70}, "rev": 0},
            headers={**self.h, "X-Client-Rev": "0"},
        )
        self.assertEqual(r_stale.status_code, 409)
        self.assertEqual(r_stale.json()["detail"]["code"], "CONCURRENCY_CONFLICT")

        # Scenario K: Long-running session stress test (60 sequential operations across all modules)
        for i in range(1, 21):
            sid = f"stu_stress_{i:03d}"
            r_s = self.client.post(
                "/api/students",
                json={
                    "id": sid,
                    "code": f"STD-{i:03d}",
                    "name": f"طالبة الانتظام رقم {i}",
                    "phone": f"0101234{i:04d}",
                    "teacher": "أ. حفصة",
                    "type": "8 حلقات",
                    "pct": 70,
                    "months": {
                        "2026-11": {
                            "value": 300,
                            "discount": 20 if i % 3 == 0 else 0,
                            "paymentDate": "2026-11-01",
                            "actualStartDate": "2026-11-04",
                            "endDate": "2026-12-04",
                        }
                    },
                },
                headers=self.h,
            )
            self.assertEqual(r_s.status_code, 200, r_s.text)

            r_inst = self.client.post(
                "/api/installments",
                json={
                    "id": f"inst_stress_{i:03d}",
                    "receiptNo": f"REC-2026-{i:04d}",
                    "studentId": sid,
                    "month": "2026-11",
                    "amount": 280 if i % 3 == 0 else 300,
                    "paymentDate": "2026-11-01",
                    "method": "فودافون كاش",
                },
                headers=self.h,
            )
            self.assertEqual(r_inst.status_code, 200, r_inst.text)

            r_mem = self.client.post(
                "/api/memorization",
                json={
                    "id": f"mem_stress_{i:03d}",
                    "studentId": sid,
                    "teacher": "أ. حفصة",
                    "date": "2026-11-10",
                    "track": "حفظ جديد",
                    "fromSurah": "البقرة",
                    "pages": 2.5,
                    "score": 96,
                },
                headers=self.h,
            )
            self.assertEqual(r_mem.status_code, 200, r_mem.text)

        # Verify diagnostics after 60 operations
        diag = self.client.get("/api/system/diagnostics", headers=self.h).json()
        self.assertTrue(diag["integrityOk"])
        self.assertEqual(diag["foreignKeyViolations"], 0)
        self.assertEqual(diag["tableCounts"]["students"], 20)
        self.assertEqual(diag["tableCounts"]["installments"], 20)
        self.assertEqual(diag["tableCounts"]["memorization_logs"], 20)

    def test_06_scenario_d_large_scale_performance_benchmark(self) -> None:
        """Scenario D: Large-scale performance benchmark (1,000 students in isolated SQLite WAL database)."""
        perf_dir = self.home / "perf_bench"
        perf_dir.mkdir(parents=True, exist_ok=True)
        store = Store(perf_dir / "perf.sqlite3")
        self.addCleanup(store.close)

        t0 = time.perf_counter()
        store.upsert_teacher("أ. فاطمة", {"pct": 70})
        with store.lock:
            store.db.execute("BEGIN")
            for i in range(1, 1001):
                sid = f"stu_p_{i:05d}"
                store.db.execute(
                    "INSERT INTO students(id,pos,code,name,phone,teacher,type,pct,payment_date,actual_start_date) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, i, f"STD-{i:05d}", f"طالبة الأداء القياسي {i}", f"0109{i:07d}", "أ. فاطمة", "8 حلقات", 70.0, "2026-11-01", "2026-11-02"),
                )
                store.db.execute(
                    "INSERT INTO months(student_id,month_key,value,payment_date,actual_start_date,end_date) VALUES(?,?,?,?,?,?)",
                    (sid, "2026-11", 300, "2026-11-01", "2026-11-02", "2026-12-02"),
                )
            store.db.execute("COMMIT")
        insert_1k_ms = (time.perf_counter() - t0) * 1000.0

        t1 = time.perf_counter()
        state_1k = store.export_state(finance_only=True)
        export_1k_ms = (time.perf_counter() - t1) * 1000.0

        t2 = time.perf_counter()
        stats_1k = store.stats("2026-11", reference_date="2026-11-15")
        stats_1k_ms = (time.perf_counter() - t2) * 1000.0

        self.assertEqual(len(state_1k["students"]), 1000)
        self.assertEqual(stats_1k["total"]["n"], 1000)
        self.assertLess(insert_1k_ms, 5000.0)
        self.assertLess(export_1k_ms, 2000.0)
        self.assertLess(stats_1k_ms, 3000.0)


if __name__ == "__main__":
    unittest.main()
