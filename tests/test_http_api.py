"""Phase 1 — HTTP-level tests for the FastAPI layer (Starlette `TestClient` + HTTPX).

These need `fastapi` and `httpx` (see `pip install -e .[dev]`). They are SKIPPED, not faked, when those packages are
missing. Every test uses an isolated temporary home directory (temp DB, temp backups) - never real academy data.

Run:  python -m unittest tests.test_http_api -v
STATUS in the delivered package: written but NOT EXECUTED (the authoring sandbox had no fastapi/httpx and no network).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests._helpers import ROOT

try:
    from fastapi.testclient import TestClient
    from alahd_erp.api.server import MAX_BODY, create_app
    HAVE_HTTP = True
except Exception:   # ImportError for fastapi / httpx / pydantic
    HAVE_HTTP = False
    MAX_BODY = 25 * 1024 * 1024

SECRET = "test-secret-0123456789"
PORT = 8890
UI_DIR = ROOT / "alahd_erp" / "ui"
LEAK_MARKERS = ("Traceback", "sqlite3.", "OperationalError", "no such column", ".py", "C:\\", "/home/")


@unittest.skipUnless(HAVE_HTTP, "fastapi/httpx not installed")
class HttpBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.app = create_app(self.home, UI_DIR, port=PORT, window_secret=SECRET)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self._close_store)   # runs AFTER client.close (LIFO) and BEFORE tmp cleanup; needed on Windows
        self.addCleanup(self.client.close)
        self.h = {"X-Window-Secret": SECRET}

    def _close_store(self) -> None:
        try:
            self.app.state.store.close()
        except Exception:
            pass

    def get(self, path, **kw):
        return self.client.get(path, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def post(self, path, payload=None, **kw):
        return self.client.post(path, json=payload, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def assertNoLeak(self, resp):
        for marker in LEAK_MARKERS:
            self.assertNotIn(marker, resp.text, f"{resp.request.url} leaked {marker!r}")


class Authentication(HttpBase):
    def test_api_requires_secret(self):
        self.assertEqual(self.client.get("/api/health").status_code, 401)
        self.assertEqual(self.client.get("/api/sync").status_code, 401)
        self.assertEqual(self.client.post("/api/students", json={}).status_code, 401)
        self.assertEqual(self.client.delete("/api/students/x").status_code, 401)

    def test_wrong_and_empty_secret_rejected(self):
        for bad in ("wrong", "", SECRET + "x", SECRET[:-1]):
            r = self.client.get("/api/health", headers={"X-Window-Secret": bad})
            self.assertEqual(r.status_code, 401, bad)

    def test_valid_secret_header_cookie_and_query(self):
        r = self.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.client.get("/api/health", cookies={"alahd_window_secret": SECRET}).status_code, 200)
        # F-03 (fixed in 1.2.0): the secret in the query string is rejected on every non-bootstrap path.
        self.assertEqual(self.client.get(f"/api/health?wsec={SECRET}").status_code, 401)   # F-03: query secret no longer accepted off-bootstrap

    def test_bootstrap_page_needs_no_secret_and_does_not_leak_it(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("set-cookie", {k.lower() for k in r.headers})
        self.assertNotIn(SECRET, r.text)

    def test_bootstrap_with_secret_sets_httponly_strict_cookie(self):
        r = self.client.get(f"/?wsec={SECRET}")
        cookie = r.headers.get("set-cookie", "")
        self.assertIn("alahd_window_secret=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie.replace("samesite", "SameSite").replace("Strict", "strict"))

    def test_security_headers(self):
        r = self.get("/api/health")
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertEqual(r.headers.get("cache-control"), "no-store")

    def test_unknown_api_path_is_404_json(self):
        r = self.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json(), {"detail": "not found"})


class OriginAndHost(HttpBase):
    def test_foreign_origin_blocked_on_read_and_write(self):
        for origin in ("http://evil.example", "null", "https://127.0.0.1:8890", "http://127.0.0.1:1"):
            self.assertEqual(self.get("/api/health", headers={"Origin": origin}).status_code, 403, origin)
            r = self.post("/api/teachers", {"name": "X", "meta": {"pct": 50}}, headers={"Origin": origin})
            self.assertEqual(r.status_code, 403, origin)
        self.assertNotIn("X", self.get("/api/sync").json()["teachers"])   # blocked write changed nothing

    def test_own_origin_and_missing_origin_allowed(self):
        self.assertEqual(self.get("/api/health", headers={"Origin": f"http://127.0.0.1:{PORT}"}).status_code, 200)
        self.assertEqual(self.get("/api/health", headers={"Origin": f"http://localhost:{PORT}"}).status_code, 200)
        self.assertEqual(self.get("/api/health").status_code, 200)

    def test_foreign_host_header_blocked_dns_rebinding(self):
        for host in ("evil.example", "evil.example:8890", "localhost.evil.example"):
            self.assertEqual(self.get("/api/health", headers={"Host": host}).status_code, 403, host)

    def test_non_loopback_client_blocked(self):
        try:
            remote = TestClient(self.app, client=("10.1.2.3", 5555), raise_server_exceptions=False)
        except TypeError:
            self.skipTest("TestClient(client=...) not supported by this Starlette version")
        self.assertEqual(remote.get("/api/health", headers=self.h).status_code, 403)


class PinGuard(HttpBase):
    def test_set_verify_and_lockout(self):
        self.assertEqual(self.post("/api/pin/set", {"pin": "1234"}).status_code, 200)
        self.assertEqual(self.post("/api/pin/verify", {"pin": "1234"}).status_code, 200)
        for _ in range(5):
            self.assertEqual(self.post("/api/pin/verify", {"pin": "0000"}).status_code, 401)
        locked = self.post("/api/pin/verify", {"pin": "1234"})       # even the right PIN is refused while locked
        self.assertEqual(locked.status_code, 429)

    def test_pin_is_not_stored_or_returned_in_clear(self):
        self.post("/api/pin/set", {"pin": "987654"})
        for path in ("/api/sync", "/api/settings", "/api/pin/status", "/api/health"):
            self.assertNotIn("987654", self.get(path).text, path)

    def test_short_and_long_pin_rejected(self):
        self.assertEqual(self.post("/api/pin/set", {"pin": "12"}).status_code, 400)
        self.assertEqual(self.post("/api/pin/set", {"pin": "1" * 33}).status_code, 400)

    def test_changing_pin_requires_current_pin(self):
        self.post("/api/pin/set", {"pin": "1234"})
        self.assertEqual(self.post("/api/pin/set", {"pin": "5555", "currentPin": "bad"}).status_code, 401)
        self.assertEqual(self.post("/api/pin/set", {"pin": "5555", "currentPin": "1234"}).status_code, 200)

    def test_clear_requires_current_pin(self):
        self.post("/api/pin/set", {"pin": "1234"})
        self.assertEqual(self.post("/api/pin/clear", {"currentPin": "nope"}).status_code, 401)
        self.assertEqual(self.post("/api/pin/clear", {"currentPin": "1234"}).status_code, 200)

    def test_wrong_types_rejected_by_validation(self):
        self.assertEqual(self.post("/api/pin/verify", {"pin": ["1", "2"]}).status_code, 422)


class ValidationAndErrors(HttpBase):
    def test_malformed_json_bodies_are_400_not_500(self):
        for path in ("/api/teachers", "/api/students", "/api/installments", "/api/settings"):
            fn = self.client.patch if path == "/api/settings" else self.client.post
            r = fn(path, content=b"{not json", headers={**self.h, "Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400, path)
            self.assertNoLeak(r)

    def test_non_object_json_is_400(self):
        for payload in ([], "x", 5, None):
            self.assertEqual(self.post("/api/teachers", payload).status_code, 400)

    def test_invalid_pct_rejected_and_nothing_saved(self):
        r = self.post("/api/teachers", {"name": "Bad", "meta": {"pct": 150}})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("Bad", self.get("/api/sync").json()["teachers"])

    def test_invalid_month_and_scope_params(self):
        self.assertEqual(self.get("/api/export/xlsx?scope=../../x").status_code, 400)
        self.assertEqual(self.get("/api/export/xlsx?month=2026-13-99").status_code, 400)
        self.assertEqual(self.get("/api/export/html?scope=nope").status_code, 400)
        self.assertEqual(self.get("/api/import/template?dataset=nope").status_code, 400)

    def test_invalid_rev_values_are_400(self):
        r = self.client.post("/api/teachers", json={"name": "A", "meta": {}}, headers={**self.h, "X-Client-Rev": "abc"})
        self.assertEqual(r.status_code, 400)

    def test_stale_rev_is_409_with_code(self):
        r = self.client.post("/api/teachers", json={"name": "A", "meta": {}}, headers={**self.h, "X-Client-Rev": "999999"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "CONCURRENCY_CONFLICT")

    def test_reset_requires_confirmation_word(self):
        self.post("/api/demo/load")
        n = self.get("/api/health").json()["studentsCount"]
        self.assertGreater(n, 0)
        for body in (None, {}, {"confirmWord": ""}, {"confirmWord": "no"}):
            self.assertEqual(self.post("/api/system/reset", body).status_code, 400)
        self.assertEqual(self.get("/api/health").json()["studentsCount"], n)

    def test_unexpected_exception_returns_generic_500_without_details(self):
        store = self.app.state.store

        def boom(*a, **k):
            raise RuntimeError("secret internal detail C:\\Users\\x\\db.sqlite3")
        store.export_state = boom           # per-test instance; temp store only
        r = self.get("/api/sync")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("secret internal detail", r.text)
        self.assertNoLeak(r)

    def test_export_failure_does_not_leak_exception_text(self):
        import alahd_erp.api.server as srv
        original = srv.export_bytes

        def boom(*a, **k):
            raise RuntimeError("internal path /home/x/secret.xlsx")
        srv.export_bytes = boom
        try:
            r = self.get("/api/export/xlsx")
        finally:
            srv.export_bytes = original
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("internal path", r.text)


class UploadsAndSizes(HttpBase):
    def test_oversized_body_rejected_before_processing(self):
        r = self.client.post("/api/sync", content=b"x" * (MAX_BODY + 1), headers=self.h)
        self.assertEqual(r.status_code, 413)

    def test_chunked_oversized_body_without_content_length_is_413(self):
        def chunk_gen():
            for _ in range(26):
                yield b"x" * (1024 * 1024)
        r = self.client.post("/api/sync", content=chunk_gen(), headers=self.h)
        self.assertEqual(r.status_code, 413)

    def test_empty_import_body_400(self):
        self.assertEqual(self.client.post("/api/import/preview", content=b"", headers=self.h).status_code, 400)

    def test_bad_base64_400(self):
        r = self.post("/api/import/preview", {"fileBase64": "!!!not-base64@@@", "filename": "a.xlsx"})
        self.assertIn(r.status_code, (400,))

    def test_garbage_file_never_500_and_filename_is_reduced(self):
        files = {"file": ("../../evil/..\\x.xlsx", b"this is not an excel file", "application/octet-stream")}
        r = self.client.post("/api/import/preview", files=files, data={"dataset": "students"}, headers=self.h)
        self.assertLess(r.status_code, 500, r.text)
        self.assertNotIn("../", r.text)
        self.assertNoLeak(r)

    def test_multipart_garbage_body_is_400(self):
        r = self.client.post("/api/import/preview", content=b"garbage", headers={**self.h, "Content-Type": "multipart/form-data; boundary=B"})
        self.assertEqual(r.status_code, 400)

    def test_error_report_validates_type(self):
        self.assertEqual(self.post("/api/import/error-report", {"errors": "x"}).status_code, 400)
        self.assertEqual(self.post("/api/import/error-report", {"errors": [], "filename": "a.xlsx"}).status_code, 200)

    def test_corrupt_xlsx_does_not_leak_internal_exception_text(self):
        import base64
        b64 = base64.b64encode(b"PK\x03\x04corrupt-zip-internals-secret-marker").decode("ascii")
        r = self.post("/api/import/preview", {"fileBase64": b64, "filename": "bad.xlsx", "dataset": "students"})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("BadZipFile", r.text)
        self.assertNoLeak(r)


class BackupAndRestore(HttpBase):
    def _put(self, name: str, data: bytes):
        d = self.home / "backups"
        d.mkdir(exist_ok=True)
        (d / name).write_bytes(data)

    def test_manual_backup_download_is_valid_sqlite(self):
        r = self.get("/api/backup")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"SQLite format 3"))

    def test_restore_rejects_bad_names_and_traversal(self):
        for name in ("../x.sqlite3", "..\\x.sqlite3", "alahd_x.sqlite3\n", "evil.sqlite3", "alahd_.db", "/etc/passwd", "alahd_a/b.sqlite3"):
            self.assertEqual(self.post("/api/restore", {"name": name}).status_code, 400, repr(name))

    def test_restore_missing_file_404(self):
        self.assertEqual(self.post("/api/restore", {"name": "alahd_manual_none.sqlite3"}).status_code, 404)

    def test_restore_junk_file_rejected_and_data_kept(self):
        self.post("/api/teachers", {"name": "Keep", "meta": {"pct": 60}})
        self._put("alahd_manual_junk.sqlite3", b"not a database" * 50)
        r = self.post("/api/restore", {"name": "alahd_manual_junk.sqlite3"})
        self.assertEqual(r.status_code, 400)
        self.assertNoLeak(r)
        self.assertIn("Keep", self.get("/api/sync").json()["teachers"])

    def test_restore_incompatible_backup_rolls_back(self):
        import sqlite3
        self.post("/api/teachers", {"name": "Keep", "meta": {"pct": 60}})
        d = self.home / "backups"
        d.mkdir(exist_ok=True)
        c = sqlite3.connect(d / "alahd_manual_bad.sqlite3")
        c.executescript("CREATE TABLE students(x); CREATE TABLE months(x); CREATE TABLE teachers(x);")
        c.commit()
        c.close()
        r = self.post("/api/restore", {"name": "alahd_manual_bad.sqlite3"})
        self.assertEqual(r.status_code, 400)
        self.assertNoLeak(r)
        self.assertIn("Keep", self.get("/api/sync").json()["teachers"])

    def test_restore_valid_backup_roundtrip(self):
        self.post("/api/teachers", {"name": "Keep", "meta": {"pct": 60}})
        self.get("/api/backup")
        name = self.get("/api/backups").json()[0]["name"]
        self.post("/api/teachers", {"name": "Later", "meta": {"pct": 40}})
        r = self.post("/api/restore", {"name": name})
        self.assertEqual(r.status_code, 200, r.text)
        teachers = self.get("/api/sync").json()["teachers"]
        self.assertIn("Keep", teachers)
        self.assertNotIn("Later", teachers)

    def test_backups_health_route_and_ui_keys(self):
        r = self.get("/api/backups/health")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        for k in ("count", "backupTooOld", "staleWarning", "ageDays", "extraBackupFolder", "extraFolder", "extraFolderConfigured", "extraFolderReachable"):
            self.assertIn(k, body)


class FinancialIntegrityThroughApi(HttpBase):
    def test_teacher_pct_and_default_pct_stay_in_sync(self):
        self.assertEqual(self.post("/api/teachers", {"name": "أ", "meta": {"pct": 60}}).status_code, 200)
        meta = self.get("/api/sync").json()["teacherMeta"]["أ"]
        self.assertEqual((meta["pct"], meta["defaultPct"]), (60, 60))
        self.post("/api/teachers", {"name": "أ", "meta": {"pct": 50}})
        meta = self.get("/api/sync").json()["teacherMeta"]["أ"]
        self.assertEqual((meta["pct"], meta["defaultPct"]), (50, 50))

    def test_pct_stays_integer_in_json(self):
        self.post("/api/teachers", {"name": "ب", "meta": {"pct": 65}})
        self.assertIsInstance(self.get("/api/sync").json()["teacherMeta"]["ب"]["pct"], int)

    def test_sync_roundtrip_preserves_money_fields(self):
        self.post("/api/demo/load")
        before = self.get("/api/sync").json()
        r = self.post("/api/sync", before, headers={"X-Client-Rev": str(before["rev"])})
        self.assertEqual(r.status_code, 200, r.text)
        after = self.get("/api/sync").json()
        self.assertEqual(json.dumps(before["students"], sort_keys=True), json.dumps(after["students"], sort_keys=True))
        self.assertEqual(before["teacherMeta"], after["teacherMeta"])

    def test_reports_are_deterministic(self):
        self.post("/api/demo/load")
        for path in ("/api/accounting", "/api/stats?month=2026-11&reference_date=2026-11-14"):
            a, b = self.get(path), self.get(path)
            self.assertEqual(a.status_code, 200, path)
            self.assertEqual(a.json(), b.json(), path)


class XssThroughExports(HttpBase):
    PAYLOAD = '<script>alert(1)</script>"\'><img src=x onerror=alert(1)>'

    def test_payload_is_escaped_in_html_exports_and_json_roundtrips_verbatim(self):
        self.post("/api/teachers", {"name": self.PAYLOAD, "meta": {"pct": 50}})
        self.assertIn(self.PAYLOAD, self.get("/api/sync").json()["teachers"])   # stored verbatim (output encoding is the defence)
        for scope in ("full", "statistics", "collections"):
            r = self.get(f"/api/export/html?scope={scope}")
            self.assertEqual(r.status_code, 200, scope)
            self.assertNotIn("<script>alert(1)</script>", r.text)
            self.assertNotIn("<img src=x", r.text)


class RouteInventory(HttpBase):
    def test_every_registered_api_route_is_protected(self):
        """Walks the real route table: any /api/* route called without the secret must answer 401."""
        for route in self.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", None) or set()
            if not path.startswith("/api/"):
                continue
            concrete = path.replace("{rest:path}", "x").replace("{month_key}", "2026-11")
            for p in ("{teacher_name}", "{student_id}", "{inst_id}", "{tx_id}", "{payout_id}", "{group_id}",
                      "{plan_id}", "{session_id}", "{mem_id}", "{enr_id}", "{cert_id}", "{key}"):
                concrete = concrete.replace(p, "x")
            for m in methods - {"HEAD", "OPTIONS"}:
                r = self.client.request(m, concrete, json={} if m != "GET" else None)
                self.assertEqual(r.status_code, 401, f"{m} {path} reachable without secret")


if __name__ == "__main__":
    unittest.main()
