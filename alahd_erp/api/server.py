"""FastAPI Application & Local REST API — Yawm Jadid Academy Suite (v1.1.0).

Clean Architecture API Layer:
- Local loopback-only security (`127.0.0.1` / `::1`) with anti-DNS-rebinding and Origin validation.
- Mandatory per-launch `X-Window-Secret` (via header or `alahd_window_secret` httpOnly cookie; `?wsec=` only on bootstrap paths to plant the cookie)
  on every `/api/*` endpoint and static UI asset except the bootstrap page (`GET /`).
- Optimistic concurrency control (`rev` / `X-Client-Rev` -> HTTP 409 on conflict).
- Off-event-loop synchronous route handlers (`def` / `run_in_threadpool`) for SQLite operations.
- Granular CRUD endpoints + Month Locking + Single-user PIN lock + General Statistics + Alerts Center +
  Safe Excel Import + Read-Only HTML Export + Printable Receipts.
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import APP_ID, APP_NAME, DEFAULT_PORT, __version__
from ..config import resolve_db_path
from ..domain.alerts_engine import compute_alerts
from ..domain.automation_engine import (
    SchedulingConflictError,
    compute_data_quality_audit,
    compute_salary_preparation_report,
    detect_scheduling_conflicts,
    preview_bulk_operation,
    search_operational_entities,
)
from ..domain.settings_registry import build_settings_catalog_payload
from ..infrastructure.autostart import (
    get_autostart_status,
    set_autostart_enabled,
    sync_and_repair_autostart,
)
from ..domain.attendance_engine import (
    DATE_RE,
    MONTH_RE,
    calculate_study_period,
    project_smart_attendance_dates,
)
from ..domain.models import (
    DomainValidationError,
    DuplicateTeacherError,
    clean_teacher_display_name,
    detect_duplicate_students,
    validate_month_entry,
    validate_state,
    validate_student_record,
)
from ..domain.statistics_engine import compute_general_statistics
from ..infrastructure.database import (
    ConcurrencyConflictError,
    MonthLockedError,
    Store,
    rotate,
)
from .security_utils import (
    UploadParseError,
    host_allowed,
    origin_allowed,
    parse_multipart_upload,
    sanitize_upload_filename,
)
from ..services.excel_service import export_bytes
from ..services.html_export_service import (
    SUPPORTED_HTML_SCOPES,
    generate_printable_receipt_html,
    generate_readonly_html,
    sanitize_export_filename,
)
from ..services.import_service import (
    SUPPORTED_DATASETS,
    commit_import,
    generate_import_error_report_xlsx,
    generate_import_template_xlsx,
    preview_import_file,
    undo_last_import,
)

log = logging.getLogger("alahd_erp.api")

MAX_BODY = 25 * 1024 * 1024


class _BodySizeLimitExceed(Exception):
    pass


class MaxBodySizeASGIMiddleware:
    """Pure ASGI body size limiter (F-09): caps both Content-Length and chunked/streaming HTTP bodies."""

    def __init__(self, app, max_bytes: int = MAX_BODY) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        total = 0
        exceeded = False
        started = False

        async def capped_receive():
            nonlocal total, exceeded
            msg = await receive()
            if msg.get("type") == "http.request":
                body = msg.get("body", b"") or b""
                total += len(body)
                if total > self.max_bytes:
                    exceeded = True
                    raise _BodySizeLimitExceed()
            return msg

        async def tracking_send(message):
            nonlocal started
            if message.get("type") == "http.response.start":
                # An inner layer may have turned our exception into a 500; swallow it so the 413 below wins.
                if exceeded and message.get("status") == 500:
                    return
                started = True
            elif exceeded and not started:
                return
            await send(message)

        try:
            await self.app(scope, capped_receive, tracking_send)
        except BaseException:
            # Not only `except _BodySizeLimitExceed`: newer Starlette/anyio wrap it (ExceptionGroup / BaseHTTPMiddleware).
            if not exceeded or started:
                raise
        if exceeded and not started:
            resp = JSONResponse({"detail": "request body too large"}, status_code=413)
            await resp(scope, receive, send)
LOOPBACK = {"127.0.0.1", "::1", "testclient"}
BACKUP_NAME = re.compile(r"^(alahd|academy)_[A-Za-z0-9_\-]+\.sqlite3?$")
BOOTSTRAP_PATHS = {"/", "/index.html", "/favicon.ico", "/assets/favicon.png"}


class RestoreReq(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rev: int | None = None


class CompensateReq(BaseModel):
    sessionId: str = Field(min_length=1, max_length=64)
    rev: int | None = None


class ProjectScheduleReq(BaseModel):
    actualStartDate: str = Field(min_length=10, max_length=10)
    daysOfWeek: list[int]
    totalSessions: int = Field(ge=1, le=500)
    endLimitDate: str | None = None


class StudyPeriodCalcReq(BaseModel):
    actualStartDate: str = ""
    endDate: str = ""
    durationDays: int = Field(default=30, ge=1, le=365)
    referenceDate: str = ""


class ResetAppReq(BaseModel):
    confirmWord: str = Field(default="", max_length=64)
    preserveSettings: bool = False


class MonthLockReq(BaseModel):
    note: str = ""
    reason: str = ""
    rev: int | None = None


class PinReq(BaseModel):
    pin: str = ""
    currentPin: str = ""


class AlertDismissReq(BaseModel):
    key: str = Field(default="", max_length=200)
    alertId: str = Field(default="", max_length=200)
    reason: str = ""
    expiresAt: str = ""
    snoozeDays: int | None = None
    rev: int | None = None


class AlertRestoreReq(BaseModel):
    key: str = Field(default="", max_length=200)
    alertId: str = Field(default="", max_length=200)
    rev: int | None = None


def _extract_expected_rev(request: Request, body_dict: dict[str, Any] | None = None) -> int | None:
    hdr = request.headers.get("X-Client-Rev")
    if hdr is not None and hdr.strip() != "":
        try:
            return int(hdr.strip())
        except ValueError as exc:
            raise HTTPException(400, "invalid X-Client-Rev header") from exc
    qp = request.query_params.get("rev")
    if qp is not None and qp.strip() != "":
        try:
            return int(qp.strip())
        except ValueError as exc:
            raise HTTPException(400, "invalid rev query param") from exc
    if isinstance(body_dict, dict):
        for k in ("rev", "clientRev"):
            if k in body_dict and body_dict[k] is not None and body_dict[k] != "":
                try:
                    return int(body_dict[k])
                except (ValueError, TypeError) as exc:
                    raise HTTPException(400, f"invalid {k} in payload") from exc
    return None


def _parse_bool_opt(val: Any, default: bool = True) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def _extract_auto_create_flags(opts: dict[str, Any], query_params: Any) -> tuple[bool, bool, bool]:
    """Resolves `(auto_create_refs, auto_create_teachers, auto_create_packages)` from payload/query options."""
    has_refs = "autoCreateRefs" in opts or "autoCreateRefs" in query_params
    has_t = "autoCreateTeachers" in opts or "autoCreateTeachers" in query_params
    has_p = "autoCreatePackages" in opts or "autoCreatePackages" in query_params

    base_refs = _parse_bool_opt(opts.get("autoCreateRefs", query_params.get("autoCreateRefs", True)), default=True)
    if not has_refs and (has_t or has_p):
        t_val = _parse_bool_opt(opts.get("autoCreateTeachers", query_params.get("autoCreateTeachers", True)), default=True)
        p_val = _parse_bool_opt(opts.get("autoCreatePackages", query_params.get("autoCreatePackages", True)), default=True)
        return (t_val and p_val), t_val, p_val

    t_val = _parse_bool_opt(opts.get("autoCreateTeachers", query_params.get("autoCreateTeachers", None)), default=base_refs)
    p_val = _parse_bool_opt(opts.get("autoCreatePackages", query_params.get("autoCreatePackages", None)), default=base_refs)
    return base_refs, t_val, p_val


def _parse_upload_payload(raw_body: bytes, content_type: str, query_params: Any) -> tuple[bytes, str, dict[str, Any]]:
    """Extracts `(file_bytes, filename, options_dict)` from either JSON (`fileBase64`) or `multipart/form-data`."""
    ctype = (content_type or "").lower()
    if "multipart/form-data" in ctype:
        try:
            return parse_multipart_upload(raw_body, content_type)
        except UploadParseError as exc:
            raise HTTPException(400, "invalid multipart request body") from exc

    # JSON payload with `fileBase64`
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(400, "invalid import request body") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(400, "import payload must be a JSON object")
    b64 = str(parsed.get("fileBase64") or "").strip()
    if "," in b64 and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        file_bytes = base64.b64decode(b64) if b64 else b""
    except Exception as exc:
        raise HTTPException(400, "invalid base64 file content") from exc
    filename = sanitize_upload_filename(
        str(parsed.get("filename") or query_params.get("filename") or ""), default="import.xlsx"
    )
    return file_bytes, filename, parsed


def create_app(
    home: Path,
    ui_dir: Path,
    port: int = DEFAULT_PORT,
    window_secret: str | None = None,
    seed_on_empty: bool = False,
    **kwargs: Any,
) -> FastAPI:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    db_file = resolve_db_path(home)
    store = Store(db_file)
    if seed_on_empty and store.count_students() == 0:
        try:
            store.load_demo_data()
        except Exception:
            pass

    app = FastAPI(title=APP_NAME, version=__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.window_secret = (
        window_secret
        or os.environ.get("ALAHD_WINDOW_SECRET")
        or secrets.token_urlsafe(24)
    )
    app.state.ui_dir = Path(ui_dir)
    app.state.home = home

    def verify_local_and_secret(request: Request, require_secret: bool = True) -> None:
        client_host = request.client.host if request.client else ""
        if client_host not in LOOPBACK:
            raise HTTPException(403, "local desktop access only")
        if not host_allowed(request.headers.get("host", "")):
            raise HTTPException(403, "local host only")
        if not origin_allowed(request.headers.get("origin"), port):
            raise HTTPException(403, "cross-origin blocked")

        if require_secret:
            expected = app.state.window_secret
            hdr_sec = request.headers.get("X-Window-Secret")
            cookie_sec = request.cookies.get("alahd_window_secret")
            # F-03: the secret is NEVER accepted from the query string here (URLs leak via history/logs/referrers).
            # `?wsec=` is honoured only on BOOTSTRAP_PATHS (see `_secret_presented`) to plant the cookie.
            supplied = hdr_sec if hdr_sec is not None else cookie_sec
            if not supplied or not secrets.compare_digest(str(supplied), str(expected)):
                raise HTTPException(401, "missing or invalid window secret")

    def _secret_presented(request: Request) -> bool:
        supplied = (
            request.headers.get("X-Window-Secret")
            or request.cookies.get("alahd_window_secret")
            or request.query_params.get("wsec")
        )
        return bool(supplied) and secrets.compare_digest(str(supplied), str(app.state.window_secret))

    @app.middleware("http")
    async def sec_headers(request: Request, call_next):
        path = request.url.path
        is_bootstrap = path in BOOTSTRAP_PATHS
        try:
            verify_local_and_secret(request, require_secret=not is_bootstrap)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

        # Reject an oversized declared or streamed/chunked body before it exceeds MAX_BODY in memory (F-09).
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"detail": "request body too large"}, status_code=413)

        r = await call_next(request)
        r.headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        })
        # The window secret is only (re)issued as a cookie to a caller that already presented it (launcher URL
        # `?wsec=...`). Previously ANY local process fetching `/` received the secret, which made it useless.
        # Set ALAHD_ALLOW_OPEN_BOOTSTRAP=1 to restore the old "open the bare URL by hand" behaviour.
        if is_bootstrap and (_secret_presented(request) or os.environ.get("ALAHD_ALLOW_OPEN_BOOTSTRAP")):
            r.set_cookie(
                key="alahd_window_secret",
                value=app.state.window_secret,
                httponly=True,
                samesite="strict",
                path="/",
            )
        return r

    def safety_snapshot(tag: str) -> None:
        try:
            store.snapshot(home / "backups", tag, keep=20)
        except Exception:
            log.exception("safety snapshot failed")

    def _handle_domain_or_store_error(exc: Exception) -> HTTPException:
        if isinstance(exc, ConcurrencyConflictError):
            return HTTPException(
                409,
                {
                    "message": str(exc),
                    "code": "CONCURRENCY_CONFLICT",
                    "currentRev": exc.current_rev,
                    "clientRev": exc.client_rev,
                },
            )
        if isinstance(exc, MonthLockedError):
            return HTTPException(
                400,
                {
                    "message": str(exc),
                    "code": "MONTH_LOCKED",
                    "month": exc.month_key,
                },
            )
        if isinstance(exc, KeyError):
            return HTTPException(404, str(exc))
        if isinstance(exc, DuplicateTeacherError):
            return HTTPException(
                409,
                {
                    "message": str(exc),
                    "code": "DUPLICATE_TEACHER",
                    "candidateName": exc.candidate_name,
                    "existingTeacher": exc.existing_name,
                    "matchType": exc.match_type,
                },
            )
        if isinstance(exc, SchedulingConflictError):
            return HTTPException(
                409,
                {
                    "message": str(exc),
                    "code": "SCHEDULING_CONFLICT",
                    "conflicts": exc.conflicts,
                },
            )
        if isinstance(exc, (DomainValidationError, ValueError)):
            return HTTPException(400, str(exc))
        if isinstance(exc, sqlite3.IntegrityError):
            # A CHECK/NOT NULL/UNIQUE constraint rejected the write (e.g. an over-long name/note). The transaction was
            # rolled back, so nothing changed; tell the user it is a data problem rather than an internal fault.
            log.warning("constraint violation rejected a write: %s", exc)
            return HTTPException(400, "البيانات غير صالحة: قيمة طويلة جدًا أو مكررة أو ناقصة. راجع الحقول وحاول مرة أخرى؛ لم يتم تغيير أي بيانات.")
        # Unexpected failure: keep the technical detail in the internal log only; the client receives the
        # generic 500 body produced by `unhandled_error` below (no `str(exc)`, no traceback).
        log.error("unexpected error in API handler", exc_info=exc)
        raise exc

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        return JSONResponse({"detail": "حدث خطأ داخلي غير متوقع؛ لم يتم تغيير أي بيانات. راجع سجل التطبيق."}, status_code=500)

    # ------------------------------------------------------------------
    # Health, Onboarding Wizard, Settings & PIN Lock (Phases 1 & 2)
    # ------------------------------------------------------------------
    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "appId": APP_ID,
            "appName": APP_NAME,
            "version": __version__,
            "empty": not store.has_data(),
            "onboarded": store.is_onboarded(),
            "pinRequired": store.has_pin(),
            "studentsCount": store.count_students(),
            "rev": store.rev,
        }

    @app.get("/api/onboarding")
    def get_onboarding_status():
        return {
            "onboarded": store.is_onboarded(),
            "settings": store.get_settings(),
            "rev": store.rev,
        }

    @app.post("/api/onboarding")
    @app.post("/api/onboarding/complete")
    async def post_onboarding(request: Request):
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "payload must be a JSON object")
        try:
            return await run_in_threadpool(store.complete_onboarding, payload, "admin")
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/settings")
    def api_get_settings():
        return {
            "settings": store.get_settings(),
            "autostart": get_autostart_status(),
            "backupHealth": store.get_backup_health(home / "backups"),
            "closedMonths": store.list_closed_months(),
            "rev": store.rev,
        }

    @app.patch("/api/settings")
    @app.put("/api/settings")
    async def api_patch_settings(request: Request):
        try:
            patch = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(patch, dict):
            raise HTTPException(400, "settings payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, patch)

        def _do_patch_settings() -> dict[str, Any]:
            autostart_res = None
            if "autoStartEnabled" in patch or "startMinimized" in patch:
                cur_s = store.get_settings()
                want_enabled = bool(patch.get("autoStartEnabled", cur_s.get("autoStartEnabled", False)))
                want_min = bool(patch.get("startMinimized", cur_s.get("startMinimized", False)))
                if "autoStartEnabled" in patch:
                    autostart_res = set_autostart_enabled(want_enabled, minimized=want_min)
                    patch["autoStartInitialized"] = True
                elif cur_s.get("autoStartEnabled"):
                    autostart_res = set_autostart_enabled(True, minimized=want_min)
            res = store.update_settings(patch, "admin", expected_rev)
            res["autostart"] = autostart_res["autostart"] if autostart_res else get_autostart_status()
            if autostart_res:
                res["autostartAction"] = autostart_res
            return res

        try:
            return await run_in_threadpool(_do_patch_settings)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/settings/catalog")
    def api_get_settings_catalog():
        cur_settings = store.get_settings()
        auto_st = get_autostart_status()
        payload = build_settings_catalog_payload(cur_settings, autostart_status=auto_st)
        payload["subTypes"] = store.get_kv("subTypes", {})
        payload["pricingConfig"] = store.get_kv("pricingConfig", {})
        payload["dashConfig"] = store.get_kv("dashConfig", {})
        payload["tableUiConfig"] = store.get_kv("tableUiConfig", {})
        payload["closedMonths"] = store.list_closed_months()
        payload["backupHealth"] = store.get_backup_health(home / "backups")
        payload["rev"] = store.rev
        return payload

    @app.get("/api/settings/audit")
    def api_get_settings_audit(limit: int = 100, category: str = "", q: str = ""):
        items = store.get_settings_audit_log(limit=min(max(limit, 1), 500), category=category, q=q)
        return {"ok": True, "items": items, "count": len(items), "rev": store.rev}

    @app.post("/api/settings/reset")
    async def api_reset_settings(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        key_single = str(body.get("key") or "").strip()
        keys_list = body.get("keys") if isinstance(body.get("keys"), list) else ([key_single] if key_single else None)
        category = str(body.get("category") or "").strip() or None
        reset_all = bool(body.get("resetAll") is True)
        if reset_all and not (body.get("confirm") is True or body.get("confirmed") is True):
            raise HTTPException(400, "يجب تأكيد إعادة ضبط جميع الإعدادات (confirm=true)")

        def _do_reset() -> dict[str, Any]:
            res = store.reset_settings(
                keys=keys_list,
                category=category,
                reset_all=reset_all,
                backup_folder=home / "backups",
                actor="admin",
                expected_rev=expected_rev,
            )
            if "autoStartEnabled" in (res.get("resetKeys") or []):
                set_autostart_enabled(bool(res["settings"].get("autoStartEnabled", False)))
            res["autostart"] = get_autostart_status()
            return res

        try:
            return await run_in_threadpool(_do_reset)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/settings/export")
    def api_export_settings():
        bundle = {
            "schema": "yawm_jadid_settings_v1",
            "appVersion": __version__,
            "exportedAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "settings": store.get_settings(),
            "subTypes": store.get_kv("subTypes", {}),
            "pricingConfig": store.get_kv("pricingConfig", {}),
            "dashConfig": store.get_kv("dashConfig", {}),
            "tableUiConfig": store.get_kv("tableUiConfig", {}),
            "certificateTemplate": store.get_kv("certificateTemplate", {}),
        }
        return bundle

    @app.post("/api/settings/import/preview")
    async def api_preview_settings_import(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        try:
            return await run_in_threadpool(store.preview_settings_import, body)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/settings/import/commit")
    async def api_commit_settings_import(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        confirm = bool(body.get("confirm") is True or body.get("confirmed") is True)
        try:
            res = await run_in_threadpool(
                store.import_settings,
                body,
                home / "backups",
                "admin",
                expected_rev,
                confirm,
            )
            res["autostart"] = get_autostart_status()
            return res
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/autostart/status")
    def api_autostart_status():
        return {"ok": True, "autostart": get_autostart_status(), "rev": store.rev}

    @app.post("/api/autostart/toggle")
    async def api_autostart_toggle(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        cur_s = store.get_settings()
        enabled = bool(body.get("enabled")) if "enabled" in body else (not bool(cur_s.get("autoStartEnabled")))
        minimized = bool(body.get("minimized", cur_s.get("startMinimized", False)))

        def _do_toggle() -> dict[str, Any]:
            auto_res = set_autostart_enabled(enabled, minimized=minimized)
            upd = store.update_settings(
                {
                    "autoStartEnabled": enabled,
                    "autoStartInitialized": True,
                    "startMinimized": minimized,
                },
                actor="admin",
                expected_rev=expected_rev,
                source="autostart_toggle",
            )
            return {
                "ok": auto_res["ok"],
                "action": auto_res["action"],
                "messageAr": auto_res["messageAr"],
                "autostart": auto_res["autostart"],
                "settings": upd["settings"],
                "rev": store.rev,
            }

        try:
            return await run_in_threadpool(_do_toggle)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/autostart/repair")
    def api_autostart_repair():
        cur_s = store.get_settings()
        minimized = bool(cur_s.get("startMinimized", False))
        res = sync_and_repair_autostart(
            settings_enabled=True,
            initialized=True,
            minimized=minimized,
        )
        return {"ok": True, **res, "rev": store.rev}

    @app.get("/api/system/diagnostics")
    def api_system_diagnostics():
        return store.get_system_diagnostics(app_home=home, autostart_status=get_autostart_status())

    @app.get("/api/system/logs")
    def api_system_logs(lines: int = 150):
        max_lines = min(max(int(lines or 150), 10), 500)
        log_file = home / "logs" / "alahd_erp.log"
        if not log_file.is_file():
            return {"ok": True, "logPath": str(log_file), "lines": [], "count": 0}
        try:
            raw_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = raw_lines[-max_lines:]
            return {"ok": True, "logPath": str(log_file), "lines": tail, "count": len(tail)}
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/pin/status")
    def api_pin_status():
        settings = store.get_settings()
        return {
            "pinRequired": store.has_pin(),
            "pinIdleMinutes": int(settings.get("idleLockMinutes") if settings.get("idleLockMinutes") is not None else 15),
        }

    pin_guard = {"fails": 0, "locked_until": 0.0}
    pin_lock = threading.Lock()   # route handlers run in a thread-pool: keep the counter consistent

    def _check_pin(candidate: str) -> bool:
        """Verifies the PIN with exponential back-off after 5 consecutive failures (anti brute-force)."""
        with pin_lock:
            now = time.time()
            if pin_guard["locked_until"] > now:
                wait = int(pin_guard["locked_until"] - now) + 1
                raise HTTPException(429, f"محاولات كثيرة خاطئة؛ حاول مرة أخرى بعد {wait} ثانية")
            if store.verify_pin(candidate):
                pin_guard["fails"] = 0
                return True
            pin_guard["fails"] += 1
            if pin_guard["fails"] >= 5:
                pin_guard["locked_until"] = now + min(300, 2 ** (pin_guard["fails"] - 3))
            return False

    @app.post("/api/pin/verify")
    def api_pin_verify(body: PinReq):
        if _check_pin(body.pin):
            return {"ok": True}
        raise HTTPException(401, "رمز PIN غير صحيح")

    @app.post("/api/pin/set")
    def api_pin_set(body: PinReq):
        if store.has_pin() and not _check_pin(body.currentPin):
            raise HTTPException(401, "رمز PIN الحالي غير صحيح")
        try:
            return store.set_pin(body.pin, actor="admin")
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/pin/clear")
    def api_pin_clear(body: PinReq):
        if store.has_pin() and not _check_pin(body.currentPin):
            raise HTTPException(401, "رمز PIN الحالي غير صحيح")
        return store.clear_pin(actor="admin")

    # ------------------------------------------------------------------
    # Demo Data & Application Reset (Phase 1)
    # ------------------------------------------------------------------
    @app.post("/api/demo/load")
    def api_demo_load(request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.load_demo_data(backup_folder=home / "backups", actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/demo/inventory")
    def api_demo_inventory():
        return store.inventory_demo_data()

    @app.post("/api/demo/clean-quarantine")
    async def api_demo_clean_quarantine(request: Request):
        include_class_b = True
        body: dict[str, Any] = {}
        try:
            raw = await request.body()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    body = parsed
                    if "includeClassB" in parsed:
                        include_class_b = bool(parsed["includeClassB"])
        except Exception:
            pass
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(
                store.clean_demo_data_with_quarantine,
                home / "backups",
                home / "quarantine",
                include_class_b,
                "admin",
                expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/demo/clear")
    def api_demo_clear(request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.clear_demo_data(backup_folder=home / "backups", actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/system/reset")
    @app.post("/api/system/reset-app")
    def api_system_reset(body: ResetAppReq | None = None):
        body = body or ResetAppReq()
        word = body.confirmWord.strip()
        # A destructive wipe must never run on an empty/omitted confirmation (previously `if word and ...`
        # let a bare POST with no body reset the whole academy).
        if word not in ("إعادة ضبط", "اعادة ضبط", "RESET"):
            raise HTTPException(400, "كلمة التأكيد مطلوبة؛ اكتب «إعادة ضبط» للتأكيد")
        return store.reset_application(backup_folder=home / "backups", actor="admin", preserve_settings=body.preserveSettings)

    # ------------------------------------------------------------------
    # Month Locking (Phase 2)
    # ------------------------------------------------------------------
    @app.get("/api/months/closed")
    def api_list_closed_months():
        return {"closedMonths": store.list_closed_months(), "rev": store.rev}

    @app.post("/api/months/{month_key}/close")
    def api_close_month(month_key: str, body: MonthLockReq, request: Request):
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.close_month(
                month_key,
                note=body.note or body.reason,
                backup_folder=home / "backups",
                actor="admin",
                expected_rev=expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/months/{month_key}/reopen")
    def api_reopen_month(month_key: str, body: MonthLockReq, request: Request):
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.reopen_month(
                month_key,
                note=body.note or body.reason,
                actor="admin",
                expected_rev=expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Duplicates, Attendance Projection & Study Period
    # ------------------------------------------------------------------
    @app.get("/api/duplicates")
    def get_duplicates():
        state = store.export_state()
        dups = detect_duplicate_students(state.get("students") or [])
        return {"duplicates": dups, "count": len(dups)}

    @app.post("/api/attendance/project-dates")
    def api_project_dates(body: ProjectScheduleReq):
        """Projects class session dates strictly starting from `actualStartDate`."""
        return project_smart_attendance_dates(
            start_iso=body.actualStartDate,
            days_of_week=body.daysOfWeek,
            total_sessions=body.totalSessions,
            end_limit_iso=body.endLimitDate,
        )

    @app.post("/api/study-period/calculate")
    def api_calculate_study_period(body: StudyPeriodCalcReq):
        """Calculates subscription duration and study period strictly from `actualStartDate`."""
        settings = store.get_settings()
        return calculate_study_period(
            actual_start_date=body.actualStartDate,
            end_date=body.endDate,
            duration_days=body.durationDays or int(settings.get("defaultDurationDays") or 30),
            reference_date=body.referenceDate,
            expiring_soon_days=int(settings.get("expiringSoonDays") or 5),
        )

    @app.post("/api/attendance/auto-compensate")
    def attendance_auto_compensate(body: CompensateReq, request: Request):
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.compensate_session_transactional(
                body.sessionId,
                backup_folder=home / "backups",
                actor="admin:auto_compensate",
                expected_rev=expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Full State Sync (for initial load & bulk import/restore)
    # ------------------------------------------------------------------
    @app.get("/api/sync")
    def sync_get():
        state = store.export_state()
        state["duplicates"] = detect_duplicate_students(state.get("students") or [])
        state["backupHealth"] = store.get_backup_health(home / "backups")
        state["rev"] = store.rev
        return state

    @app.post("/api/sync")
    async def sync_post(request: Request, allow_legacy_dates: bool = False):
        raw = await request.body()
        if not raw or len(raw) > MAX_BODY:
            raise HTTPException(413, "bad size")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "invalid json") from exc

        expected_rev = _extract_expected_rev(request, parsed if isinstance(parsed, dict) else None)

        def _do_sync() -> dict[str, Any]:
            existing_teachers = (
                set(store.export_state().get("teachers") or [])
                if "teachers" not in (parsed if isinstance(parsed, dict) else {})
                else None
            )
            state = validate_state(
                parsed,
                allow_legacy_dates=allow_legacy_dates,
                existing_teachers=existing_teachers,
            )
            before, after = store.count_students(), len(state["students"])
            has_any_existing = before > 0 or bool(store.list_teachers(include_inactive=True))
            if before and (after == 0 or after < before * 0.7):
                safety_snapshot("before_shrink")
            elif has_any_existing:
                app.state.last_safety = time.time()
                safety_snapshot("before_sync")

            allow_empty = isinstance(parsed, dict) and parsed.get("allowEmptyCollections") is True
            store.replace_all(state, "admin", expected_rev=expected_rev, allow_empty=allow_empty)
            dups = detect_duplicate_students(state["students"])
            return {
                "ok": True,
                "students": len(state["students"]),
                "rev": store.rev,
                "duplicates": dups,
            }

        try:
            return await run_in_threadpool(_do_sync)
        except OverflowError as exc:
            raise HTTPException(413, str(exc)) from exc
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/json-restore/preview")
    async def api_json_restore_preview(request: Request, allow_legacy_dates: bool = True):
        raw = await request.body()
        if not raw:
            raise HTTPException(400, "empty request body")
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "request body too large")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "ملف JSON غير صالح (خطأ في الصياغة)") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(400, "payload must be a JSON object")

        filename = str(parsed.get("filename") or request.query_params.get("filename") or "backup.json").strip()
        state_payload = parsed.get("state") if isinstance(parsed.get("state"), dict) else parsed

        def _do_preview_json() -> dict[str, Any]:
            res = store.preview_json_restore(
                state_payload,
                filename=filename,
                allow_legacy_dates=allow_legacy_dates,
            )
            out = dict(res)
            out.pop("_validatedState", None)
            return out

        try:
            return await run_in_threadpool(_do_preview_json)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/json-restore/commit")
    async def api_json_restore_commit(request: Request, allow_legacy_dates: bool = True):
        raw = await request.body()
        if not raw:
            raise HTTPException(400, "empty request body")
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "request body too large")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "ملف JSON غير صالح (خطأ في الصياغة)") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(400, "payload must be a JSON object")

        expected_rev = _extract_expected_rev(request, parsed)
        filename = str(parsed.get("filename") or request.query_params.get("filename") or "backup.json").strip()
        expected_hash = str(parsed.get("expectedPayloadHash") or parsed.get("payloadHash") or "").strip() or None
        confirm_restore = bool(
            parsed.get("confirmRestore") is True
            or parsed.get("confirmed") is True
            or request.query_params.get("confirm") == "true"
        )
        allow_empty = bool(parsed.get("allowEmptyCollections") is True)
        state_payload = parsed.get("state") if isinstance(parsed.get("state"), dict) else parsed

        try:
            return await run_in_threadpool(
                store.restore_from_json,
                state_payload,
                home / "backups",
                filename,
                "admin",
                expected_rev,
                expected_hash,
                allow_legacy_dates,
                allow_empty,
                confirm_restore,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Granular CRUD Endpoints (Phase 2)
    # ------------------------------------------------------------------
    @app.get("/api/teachers")
    def api_list_teachers(include_inactive: bool = True):
        items = store.list_teachers(include_inactive=bool(include_inactive))
        return {
            "ok": True,
            "teachers": [t["name"] for t in items],
            "items": items,
            "rev": store.rev,
        }

    @app.post("/api/teachers")
    async def api_upsert_teacher(request: Request, allow_duplicate: bool = False):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "teacher payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        name = clean_teacher_display_name(body.get("newName") or body.get("name") or "")
        old_name = clean_teacher_display_name(body.get("oldName") or body.get("originalName") or "")
        allow_dup = bool(
            allow_duplicate
            or body.get("allowDuplicate") is True
            or body.get("force") is True
        )
        mode = str(body.get("mode") or "upsert").strip().lower()
        meta = dict(body.get("meta")) if isinstance(body.get("meta"), dict) else dict(body)
        for k in ("pct", "defaultPct", "active", "archived", "phone", "specialization", "notes"):
            if k in body and k not in meta:
                meta[k] = body[k]

        def _do_post_teacher() -> dict[str, Any]:
            if old_name and old_name != name:
                return store.rename_teacher(
                    old_name,
                    name,
                    meta,
                    actor="admin",
                    expected_rev=expected_rev,
                    allow_duplicate=allow_dup,
                )
            return store.upsert_teacher(
                name,
                meta,
                actor="admin",
                expected_rev=expected_rev,
                allow_duplicate=allow_dup,
                mode=mode,
            )

        try:
            return await run_in_threadpool(_do_post_teacher)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.put("/api/teachers/{teacher_name:path}")
    @app.patch("/api/teachers/{teacher_name:path}")
    async def api_update_teacher(
        teacher_name: str,
        request: Request,
        allow_duplicate: bool = False,
    ):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "teacher payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        old_name = clean_teacher_display_name(teacher_name)
        new_name = clean_teacher_display_name(
            body.get("newName") or body.get("name") or old_name
        )
        allow_dup = bool(
            allow_duplicate
            or body.get("allowDuplicate") is True
            or body.get("force") is True
        )
        meta = dict(body.get("meta")) if isinstance(body.get("meta"), dict) else dict(body)
        for k in ("pct", "defaultPct", "active", "archived", "phone", "specialization", "notes"):
            if k in body and k not in meta:
                meta[k] = body[k]

        def _do_update_teacher() -> dict[str, Any]:
            return store.rename_teacher(
                old_name,
                new_name,
                meta,
                actor="admin",
                expected_rev=expected_rev,
                allow_duplicate=allow_dup,
            )

        try:
            return await run_in_threadpool(_do_update_teacher)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/teachers/{teacher_name:path}")
    def api_delete_teacher(teacher_name: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        safety_snapshot("before_teacher_delete")
        try:
            return store.delete_teacher(teacher_name, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/students/{student_id}")
    def api_get_student(student_id: str):
        stu = store.get_student(student_id)
        if not stu:
            raise HTTPException(404, "student not found")
        return stu

    @app.post("/api/students")
    @app.put("/api/students/{student_id}")
    async def api_upsert_student(
        request: Request,
        student_id: str | None = None,
        allow_duplicate: bool = False,
        allow_legacy_dates: bool = False,
    ):
        try:
            student = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(student, dict):
            raise HTTPException(400, "student payload must be a JSON object")
        if student_id and not student.get("id"):
            student = dict(student, id=student_id)
        expected_rev = _extract_expected_rev(request, student)

        def _do_upsert_student() -> dict[str, Any]:
            store.check_rev(expected_rev)
            with store.lock:
                teacher_rows = {
                    r["name"]: json.loads(r["meta"] or "{}")
                    for r in store.db.execute("SELECT name, meta FROM teachers").fetchall()
                }
                known_teachers = set(teacher_rows.keys())
                prev_stu_row = store.db.execute(
                    "SELECT teacher FROM students WHERE id = ?",
                    (str((student or {}).get("id") or ""),),
                ).fetchone()
                prev_teacher = (prev_stu_row["teacher"] or "").strip() if prev_stu_row else ""
            tname = clean_teacher_display_name((student or {}).get("teacher") or "")
            if tname:
                student["teacher"] = tname
            if tname and known_teachers and tname not in known_teachers:
                raise DomainValidationError(f"unknown teacher: {tname}")
            if tname and tname in teacher_rows and tname != prev_teacher:
                tmeta = teacher_rows[tname]
                if (bool(tmeta.get("archived")) or tmeta.get("active") is False) and not student.get("allowInactiveTeacher"):
                    raise DomainValidationError(f"لا يمكن إسناد طالبة جديدة إلى معلمة غير نشطة/مؤرشفة: {tname}")
            validate_student_record(
                student,
                known_teachers=known_teachers if known_teachers else None,
                allow_legacy_dates=allow_legacy_dates,
            )
            if not allow_duplicate:
                with store.lock:
                    other_rows = store.db.execute(
                        "SELECT id, name, phone, teacher FROM students WHERE id != ?",
                        (student["id"],),
                    ).fetchall()
                other_students = [dict(r) for r in other_rows]
                dups = detect_duplicate_students(other_students + [student])
                my_dup = next(
                    (d for d in dups if any(r["id"] == student["id"] for r in d["records"])),
                    None,
                )
                if my_dup:
                    raise HTTPException(
                        409,
                        f"طالبة مكررة بنفس الاسم ورقم الهاتف ({my_dup['name']} — {my_dup['phone']})",
                    )
            return store.upsert_student(student, actor="admin", expected_rev=expected_rev)

        try:
            return await run_in_threadpool(_do_upsert_student)
        except HTTPException:
            raise
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/students/{student_id}")
    def api_delete_student(student_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        safety_snapshot("before_student_delete")
        try:
            deleted = store.delete_student(student_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc
        if not deleted:
            raise HTTPException(404, "student not found")
        return {"ok": True, "id": student_id, "rev": store.rev}

    @app.patch("/api/students/{student_id}/months/{month_key}")
    async def api_patch_student_month(
        student_id: str,
        month_key: str,
        request: Request,
        allow_legacy_dates: bool = False,
    ):
        try:
            m_data = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(m_data, dict):
            raise HTTPException(400, "month payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, m_data)
        try:
            validate_month_entry(
                month_key,
                m_data,
                student_id=student_id,
                allow_legacy_dates=allow_legacy_dates,
            )
            return await run_in_threadpool(
                store.upsert_student_month,
                student_id,
                month_key,
                m_data,
                "admin",
                expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/students/{student_id}/months/{month_key}")
    def api_delete_student_month(student_id: str, month_key: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_student_month(student_id, month_key, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/installments")
    async def api_upsert_installment(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "installment payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_installment, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/installments/{inst_id}")
    def api_delete_installment(inst_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_installment(inst_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/transactions")
    async def api_upsert_transaction(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "transaction payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_transaction, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/transactions/{tx_id}")
    def api_delete_transaction(tx_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_transaction(tx_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/payouts")
    async def api_upsert_payout(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payout payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_payout, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/payouts/{payout_id}")
    def api_delete_payout(payout_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_payout(payout_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/groups")
    async def api_upsert_group(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "group payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_group, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/groups/{group_id}")
    def api_delete_group(group_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_group(group_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/plans")
    async def api_upsert_plan(request: Request, allow_conflicts: bool = False):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "plan payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        allow_conf = bool(allow_conflicts or body.get("allowConflicts") is True or body.get("force") is True)
        try:
            if isinstance(body.get("plan"), dict) and "sessions" in body:
                return await run_in_threadpool(
                    lambda: store.upsert_sessions_batch(
                        body.get("plan"),
                        body.get("sessions") or [],
                        body.get("monthUpdate"),
                        "admin",
                        expected_rev,
                        check_conflicts=True,
                        allow_conflicts=allow_conf,
                    )
                )
            return await run_in_threadpool(store.upsert_plan, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/plans/{plan_id}")
    def api_delete_plan(plan_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_plan(plan_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/sessions")
    async def api_upsert_session(request: Request, allow_conflicts: bool = False):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "session payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        allow_conf = bool(allow_conflicts or body.get("allowConflicts") is True or body.get("force") is True)
        try:
            return await run_in_threadpool(
                lambda: store.upsert_session(
                    body,
                    "admin",
                    expected_rev,
                    check_conflicts=True,
                    allow_conflicts=allow_conf,
                )
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/sessions/batch")
    async def api_upsert_sessions_batch(request: Request, allow_conflicts: bool = False):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "batch payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        allow_conf = bool(allow_conflicts or body.get("allowConflicts") is True or body.get("force") is True)
        try:
            return await run_in_threadpool(
                lambda: store.upsert_sessions_batch(
                    body.get("plan"),
                    body.get("sessions") or [],
                    body.get("monthUpdate"),
                    "admin",
                    expected_rev,
                    check_conflicts=True,
                    allow_conflicts=allow_conf,
                )
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/sessions/{session_id}")
    def api_delete_session(session_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_session(session_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.patch("/api/sessions/{session_id}")
    async def api_patch_session(session_id: str, request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "session patch payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.patch_session, session_id, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/sessions/{session_id}/compensate")
    def api_compensate_session_by_id(session_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.compensate_session_transactional(
                session_id,
                backup_folder=home / "backups",
                actor="admin:auto_compensate",
                expected_rev=expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/memorization")
    async def api_upsert_memorization(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "memorization payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_memorization, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/memorization/{mem_id}")
    def api_delete_memorization(mem_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_memorization(mem_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/madrasa/enrollments")
    async def api_upsert_madrasa_enrollment(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "madrasa enrollment payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        batch_name = str(body.get("batchName") or "").strip()
        try:
            return await run_in_threadpool(
                store.upsert_madrasa_enrollment,
                body,
                batch_name,
                "admin",
                expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/madrasa/enrollments/{enr_id}")
    def api_delete_madrasa_enrollment(enr_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_madrasa_enrollment(enr_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.patch("/api/madrasa/enrollments/{enr_id}")
    async def api_patch_madrasa_enrollment(enr_id: str, request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "madrasa enrollment patch payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.patch_madrasa_enrollment, enr_id, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/certificates")
    async def api_upsert_certificate(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "certificate payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        try:
            return await run_in_threadpool(store.upsert_certificate, body, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.delete("/api/certificates/{cert_id}")
    def api_delete_certificate(cert_id: str, request: Request):
        expected_rev = _extract_expected_rev(request)
        try:
            return store.delete_certificate(cert_id, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.put("/api/kv/{key}")
    async def api_put_kv(key: str, request: Request):
        if key not in ("subTypes", "dashConfig", "tableUiConfig", "pricingConfig", "certificateTemplate", "columnMappings"):
            raise HTTPException(400, "unsupported kv key")
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        expected_rev = _extract_expected_rev(request, body if isinstance(body, dict) else None)
        val = body.get("value", body) if isinstance(body, dict) and "value" in body else body
        try:
            return await run_in_threadpool(store.put_kv, key, val, "admin", expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Financial Reports, General Statistics (Phase 4) & Alerts (Phase 5)
    # ------------------------------------------------------------------
    @app.get("/api/reports/collections")
    def api_collection_report(
        month: str = "",
        start_payment_date: str = "",
        end_payment_date: str = "",
        reference_date: str = "",
    ):
        """Cash-basis collection report strictly driven by receipt `paymentDate`."""
        if month and not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        if start_payment_date and not DATE_RE.match(start_payment_date):
            raise HTTPException(400, "invalid start_payment_date")
        if end_payment_date and not DATE_RE.match(end_payment_date):
            raise HTTPException(400, "invalid end_payment_date")
        if reference_date and not DATE_RE.match(reference_date):
            raise HTTPException(400, "invalid reference_date")
        return store.collection_report(
            month or None,
            start_payment_date=start_payment_date or None,
            end_payment_date=end_payment_date or None,
            reference_date=reference_date or None,
        )

    @app.get("/api/reports/accrual")
    def api_accrual_report(month: str, reference_date: str = ""):
        """Accrual-basis report by subscription month."""
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        if reference_date and not DATE_RE.match(reference_date):
            raise HTTPException(400, "invalid reference_date")
        return store.accrual_report(month, reference_date=reference_date or None)

    @app.get("/api/reports/study-periods")
    def api_study_periods_report(month: str, reference_date: str = ""):
        """Study duration & attendance period report strictly driven by `actualStartDate`."""
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        if reference_date and not DATE_RE.match(reference_date):
            raise HTTPException(400, "invalid reference_date")
        return store.study_periods_report(month, reference_date=reference_date or None)

    @app.get("/api/stats")
    def stats(month: str, reference_date: str = ""):
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        return store.stats(month, reference_date=reference_date or None)

    @app.get("/api/accounting")
    def accounting(month: str = "", reference_date: str = ""):
        if month and not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        return store.accounting_summary(month or None, reference_date=reference_date or None)

    @app.get("/api/statistics")
    def api_general_statistics(
        period: str = "all_time",
        selected_month: str = "",
        custom_from: str = "",
        custom_to: str = "",
        start_date: str = "",
        end_date: str = "",
        reference_date: str = "",
        teacher: str = "",
    ):
        state = store.export_state(teacher=teacher.strip() or None)
        eff_period = period.strip() if period else "all_time"
        from_m = (custom_from or start_date)[:7] if (custom_from or start_date) else None
        to_m = (custom_to or end_date)[:7] if (custom_to or end_date) else None
        if eff_period in ("current_month", "selected_month") and selected_month and MONTH_RE.match(selected_month.strip()):
            eff_period = "custom"
            from_m = selected_month.strip()
            to_m = selected_month.strip()
        return compute_general_statistics(
            state,
            period=eff_period,
            from_month=from_m,
            to_month=to_m,
            reference_date=reference_date or None,
            teacher_filter=teacher.strip() or None,
        )

    @app.get("/api/alerts")
    def api_get_alerts(month: str = "", reference_date: str = "", include_dismissed: bool = False):
        state = store.export_state()
        backup_health = store.get_backup_health(home / "backups")
        return compute_alerts(
            state,
            month=month.strip() or None,
            reference_date=reference_date.strip() or None,
            backup_health=backup_health,
            include_dismissed=bool(include_dismissed),
        )

    @app.post("/api/alerts/dismiss")
    def api_dismiss_alert(body: AlertDismissReq, request: Request):
        target_key = (body.key or body.alertId).strip()
        if not target_key:
            raise HTTPException(400, "alert key is required")
        exp_at = body.expiresAt.strip()
        if not exp_at and body.snoozeDays is not None and body.snoozeDays > 0:
            exp_at = (datetime.date.today() + datetime.timedelta(days=int(body.snoozeDays))).isoformat()
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.dismiss_alert(
                target_key,
                reason=body.reason,
                expires_at=exp_at,
                actor="admin",
                expected_rev=expected_rev,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/alerts/restore")
    def api_restore_alert(body: AlertRestoreReq, request: Request):
        target_key = (body.key or body.alertId).strip()
        if not target_key:
            raise HTTPException(400, "alert key is required")
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.restore_alert(target_key, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/alerts/read")
    def api_mark_alert_read(body: AlertRestoreReq, request: Request):
        target_key = (body.key or body.alertId).strip()
        if not target_key:
            raise HTTPException(400, "alert key is required")
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        try:
            return store.mark_alert_read(target_key, actor="admin", expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/attendance/check-conflicts")
    async def api_check_scheduling_conflicts(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        cand = body.get("sessions") if isinstance(body.get("sessions"), list) else ([body] if body.get("date") else None)
        state = store.export_state()
        conflicts = detect_scheduling_conflicts(state.get("sessions") or [], state.get("students") or [], cand)
        return {"ok": True, "hasConflicts": len(conflicts) > 0, "count": len(conflicts), "conflicts": conflicts}

    @app.get("/api/payroll/preparation")
    def api_payroll_preparation(month: str = "", reference_date: str = ""):
        if month and not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        if reference_date and not DATE_RE.match(reference_date):
            raise HTTPException(400, "invalid reference_date")
        eff_month = month.strip() if month else datetime.date.today().isoformat()[:7]
        state = store.export_state(finance_only=True)
        return compute_salary_preparation_report(state, eff_month, reference_date=reference_date or None)

    @app.get("/api/data-quality")
    def api_data_quality_audit(reference_date: str = ""):
        if reference_date and not DATE_RE.match(reference_date):
            raise HTTPException(400, "invalid reference_date")
        state = store.export_state()
        return compute_data_quality_audit(state, reference_date=reference_date or None)

    @app.post("/api/bulk/preview")
    async def api_bulk_preview(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        op = str(body.get("operation") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else body
        try:
            return await run_in_threadpool(lambda: preview_bulk_operation(store.export_state(), op, params))
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/bulk/execute")
    async def api_bulk_execute(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")
        expected_rev = _extract_expected_rev(request, body)
        op = str(body.get("operation") or "").strip()
        params = body.get("params") if isinstance(body.get("params"), dict) else body
        preview_hash = str(body.get("expectedPreviewHash") or body.get("previewHash") or "").strip() or None
        confirm = bool(body.get("confirm") is True or body.get("confirmed") is True)
        try:
            return await run_in_threadpool(
                lambda: store.execute_bulk_operation(
                    op,
                    params,
                    backup_folder=home / "backups",
                    actor="admin",
                    expected_rev=expected_rev,
                    expected_preview_hash=preview_hash,
                    confirm=confirm,
                )
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.get("/api/search")
    def api_operational_search(q: str = "", limit: int = 30):
        state = store.export_state()
        return search_operational_entities(state, q, limit=limit)

    # ------------------------------------------------------------------
    # Excel Import Endpoints (Phase 6)
    # ------------------------------------------------------------------
    @app.get("/api/import/template")
    def api_import_template(dataset: str = "students"):
        ds = dataset.strip().lower() if dataset else "students"
        if ds not in SUPPORTED_DATASETS:
            raise HTTPException(400, f"unsupported template dataset: {ds}")
        data = generate_import_template_xlsx(ds)
        fname = f"YawmJadid_Import_Template_{ds}.xlsx"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/api/import/preview")
    async def api_import_preview(request: Request):
        raw_body = await request.body()
        if not raw_body:
            raise HTTPException(400, "empty request body")
        if len(raw_body) > MAX_BODY:
            raise HTTPException(413, "import file too large")
        file_bytes, filename, opts = _parse_upload_payload(
            raw_body,
            request.headers.get("content-type", ""),
            request.query_params,
        )
        dataset = str(opts.get("dataset") if opts.get("dataset") is not None else (request.query_params.get("dataset") or "students"))
        mode = str(opts.get("mode") if opts.get("mode") is not None else (request.query_params.get("mode") or "update_existing"))
        auto_refs, auto_teachers, auto_packages = _extract_auto_create_flags(opts, request.query_params)
        col_map = opts.get("columnMapping") if isinstance(opts.get("columnMapping"), dict) else None
        sheet_name_raw = opts.get("sheetName") if "sheetName" in opts else request.query_params.get("sheetName")
        sheet_name = str(sheet_name_raw).strip() if sheet_name_raw is not None and str(sheet_name_raw).strip() != "" else None
        default_month = str(opts.get("defaultMonth") or request.query_params.get("defaultMonth") or "")

        def _do_preview() -> dict[str, Any]:
            state = store.export_state()
            return preview_import_file(
                file_bytes=file_bytes,
                filename=filename,
                dataset=dataset,
                state=state,
                column_mapping=col_map,
                mode=mode,
                auto_create_refs=auto_refs,
                sheet_name=sheet_name,
                default_month=default_month,
                auto_create_teachers=auto_teachers,
                auto_create_packages=auto_packages,
            )

        try:
            return await run_in_threadpool(_do_preview)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/import/error-report")
    async def api_import_error_report(request: Request):
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(400, "invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "payload must be a JSON object")

        filename = sanitize_upload_filename(str(body.get("filename") or ""), default="import.xlsx")
        if "errors" in body and body["errors"] is not None:
            errors = body["errors"]
            if not isinstance(errors, list):
                raise HTTPException(400, "errors must be a list")
        elif body.get("fileBase64"):
            b64 = str(body.get("fileBase64") or "").strip()
            if "," in b64 and b64.startswith("data:"):
                b64 = b64.split(",", 1)[1]
            try:
                file_bytes = base64.b64decode(b64)
            except Exception as exc:
                raise HTTPException(400, "invalid base64 file content") from exc
            dataset = str(body.get("dataset") if body.get("dataset") is not None else "students")
            mode = str(body.get("mode") if body.get("mode") is not None else "update_existing")
            auto_refs, auto_teachers, auto_packages = _extract_auto_create_flags(body, request.query_params)
            col_map = body.get("columnMapping") if isinstance(body.get("columnMapping"), dict) else None
            sheet_raw = body.get("sheetName")
            sheet_name = str(sheet_raw).strip() if sheet_raw is not None and str(sheet_raw).strip() != "" else None
            default_month = str(body.get("defaultMonth") or "")

            def _preview_errs() -> list[dict[str, Any]]:
                state = store.export_state()
                prev = preview_import_file(
                    file_bytes=file_bytes,
                    filename=filename,
                    dataset=dataset,
                    state=state,
                    column_mapping=col_map,
                    mode=mode,
                    auto_create_refs=auto_refs,
                    sheet_name=sheet_name,
                    default_month=default_month,
                    auto_create_teachers=auto_teachers,
                    auto_create_packages=auto_packages,
                )
                return prev.get("errors") or []

            try:
                errors = await run_in_threadpool(_preview_errs)
            except Exception as exc:
                raise _handle_domain_or_store_error(exc) from exc
        else:
            errors = []

        data = await run_in_threadpool(generate_import_error_report_xlsx, errors, filename)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="YawmJadid_Import_Errors.xlsx"'},
        )

    @app.post("/api/import/commit")
    async def api_import_commit(request: Request):
        raw_body = await request.body()
        if not raw_body:
            raise HTTPException(400, "empty request body")
        if len(raw_body) > MAX_BODY:
            raise HTTPException(413, "import file too large")
        try:
            maybe_json = json.loads(raw_body.decode("utf-8"))
        except Exception:
            maybe_json = None
        if isinstance(maybe_json, dict) and maybe_json.get("loadBundledWorkbooks"):
            from ..domain.academy_data_loader import ensure_academy_workbooks_loaded
            res = await run_in_threadpool(ensure_academy_workbooks_loaded, store, force=bool(maybe_json.get("force", True)))
            return {"ok": True, "bundledLoaded": True, "added": res.get("studentsAdded", 0), "updated": res.get("studentsUpdated", 0), "skipped": 0, "summary": res, "rev": store.rev}
        file_bytes, filename, opts = _parse_upload_payload(
            raw_body,
            request.headers.get("content-type", ""),
            request.query_params,
        )
        expected_rev = _extract_expected_rev(request, opts)
        dataset = str(opts.get("dataset") if opts.get("dataset") is not None else (request.query_params.get("dataset") or "students"))
        mode = str(opts.get("mode") if opts.get("mode") is not None else (request.query_params.get("mode") or "update_existing"))
        auto_refs, auto_teachers, auto_packages = _extract_auto_create_flags(opts, request.query_params)
        col_map = opts.get("columnMapping") if isinstance(opts.get("columnMapping"), dict) else None
        sheet_raw = opts.get("sheetName") if "sheetName" in opts else request.query_params.get("sheetName")
        sheet_name = str(sheet_raw).strip() if sheet_raw is not None and str(sheet_raw).strip() != "" else None
        default_month = str(opts.get("defaultMonth") or request.query_params.get("defaultMonth") or "")
        expected_file_hash = str(opts.get("expectedFileHash") or opts.get("fileHash") or "").strip() or None
        selected_rows = opts.get("selectedRows") if isinstance(opts.get("selectedRows"), list) else None
        force_import = bool(opts.get("forceImport") or opts.get("includeInvalid"))
        row_overrides = opts.get("rowOverrides") if isinstance(opts.get("rowOverrides"), dict) else None

        try:
            return await run_in_threadpool(
                commit_import,
                store,
                file_bytes,
                filename,
                dataset,
                mode,
                auto_refs,
                col_map,
                sheet_name,
                default_month,
                home / "backups",
                expected_rev,
                auto_teachers,
                auto_packages,
                expected_file_hash,
                selected_rows,
                force_import,
                row_overrides,
            )
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    @app.post("/api/import/undo")
    async def api_import_undo(request: Request):
        force = False
        try:
            body = await request.json()
            if isinstance(body, dict):
                force = bool(body.get("force"))
        except Exception:
            pass
        try:
            return await run_in_threadpool(undo_last_import, store, home / "backups", force)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc

    # ------------------------------------------------------------------
    # Excel & Read-Only HTML Exports + Printable Receipt (Phase 7)
    # ------------------------------------------------------------------
    @app.get("/api/export/xlsx")
    def export_xlsx(scope: str = "full", teacher: str = "", month: str = ""):
        allowed_scopes = {
            "full", "teacher", "teachers_stats", "students", "payroll",
            "attendance", "memorization", "madrasa", "accounting",
        }
        scope_clean = scope.strip().lower() if scope else "full"
        if scope_clean not in allowed_scopes:
            raise HTTPException(400, "invalid export scope")
        if month and not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        state = store.export_state()
        try:
            data = export_bytes(
                state,
                logo_path=Path(ui_dir) / "assets" / "logo.png",
                scope=scope_clean,
                teacher=teacher.strip() or None,
                month=month.strip() or None,
            )
        except Exception as exc:
            log.exception("xlsx export failed")
            raise HTTPException(500, "تعذّر إنشاء ملف التصدير") from exc
        suffix = f"_{scope_clean}" if scope_clean != "full" else ""
        fname = f"YawmJadid_Academy{suffix}_{time.strftime('%Y-%m-%d')}.xlsx"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/export/html")
    def export_html(
        scope: str = "full",
        month: str = "",
        teacher: str = "",
        student_id: str = "",
        hide_personal_data: bool = True,
        hide_personal: bool | None = None,
        period: str = "all_time",
        start_date: str = "",
        end_date: str = "",
        reference_date: str = "",
    ):
        sc = scope.strip().lower() if scope else "full"
        if sc not in SUPPORTED_HTML_SCOPES:
            raise HTTPException(400, f"invalid html export scope: {sc}")
        if month and not MONTH_RE.match(month):
            raise HTTPException(400, "month=YYYY-MM")
        eff_hide_personal = hide_personal if hide_personal is not None else hide_personal_data
        state = store.export_state()
        backup_health = store.get_backup_health(home / "backups")
        doc = generate_readonly_html(
            state,
            scope=sc,
            month=month.strip() or None,
            teacher=teacher.strip() or None,
            student_id=student_id.strip() or None,
            hide_personal_data=eff_hide_personal,
            period=period,
            start_date=start_date.strip() or None,
            end_date=end_date.strip() or None,
            reference_date=reference_date.strip() or None,
            backup_health=backup_health,
        )
        safe_part = sanitize_export_filename(f"YawmJadid_ReadOnly_{sc}_{month or time.strftime('%Y-%m-%d')}")
        fname = f"{safe_part}.html"
        return Response(
            content=doc.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/export/receipt-html")
    def export_receipt_html(receipt_id: str):
        state = store.export_state()
        try:
            doc = generate_printable_receipt_html(state, receipt_id.strip())
        except KeyError as exc:
            raise HTTPException(404, "receipt not found") from exc
        return HTMLResponse(content=doc)

    # ------------------------------------------------------------------
    # Audit Log & Backups (Phase 2)
    # ------------------------------------------------------------------
    @app.get("/api/audit")
    def audit(limit: int = 100, action: str = "", actor: str = "", q: str = ""):
        return store.audit(
            limit=min(max(limit, 1), 1000),
            action=action,
            actor=actor,
            q=q,
        )

    @app.get("/api/backup")
    def backup():
        dest = home / "backups"
        dest.mkdir(exist_ok=True)
        f = dest / f"alahd_manual_{time.strftime('%Y%m%d_%H%M%S')}.sqlite3"
        try:
            store.backup_to(f)
        except ValueError as exc:
            log.exception("manual backup failed")
            raise HTTPException(500, "تعذّر إنشاء النسخة الاحتياطية") from exc
        rotate(dest, "alahd_manual_*.sqlite3", 30)
        return FileResponse(f, filename=f.name, media_type="application/octet-stream")

    @app.get("/api/backups")
    def backups():
        return store.list_backup_files(home / "backups")

    @app.get("/api/backups/health")
    def backups_health():
        return store.get_backup_health(home / "backups")

    @app.post("/api/restore")
    def restore(body: RestoreReq, request: Request):
        if not BACKUP_NAME.fullmatch(body.name):
            raise HTTPException(400, "bad name")
        f = home / "backups" / body.name
        if not f.is_file():
            raise HTTPException(404, "backup not found")
        expected_rev = _extract_expected_rev(request, {"rev": body.rev})
        safety_snapshot("before_restore")
        try:
            n = store.restore_from(f, expected_rev=expected_rev)
        except Exception as exc:
            raise _handle_domain_or_store_error(exc) from exc
        store.log("admin", "restore", body.name)
        return {"ok": True, "students": n, "rev": store.rev}

    @app.get("/api/{rest:path}")
    def api_404(rest: str):
        return JSONResponse({"detail": "not found"}, status_code=404)

    app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
    app.add_middleware(MaxBodySizeASGIMiddleware, max_bytes=MAX_BODY)
    return app
