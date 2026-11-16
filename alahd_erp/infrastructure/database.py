"""Unified Relational SQLite WAL Repository (`Store`) — Yawm Jadid Academy Suite (v1.1.0).

Key Engineering Features:
- SQLite in WAL mode with `PRAGMA synchronous=FULL`, strict Foreign Keys, and CHECK constraints.
- Versioned schema migrations driven by `PRAGMA user_version` (`SCHEMA_VERSION = 3`) with
  per-version transactions and automatic pre-migration safety snapshots.
- Verified backups (`PRAGMA integrity_check` after every backup write) + optional mirror to
  `extraBackupFolder`.
- Optimistic concurrency control (`rev` tracking with `ConcurrencyConflictError` -> HTTP 409).
- Month locking (`closed_months` table) preventing edits to subscriptions, receipts, or wages in closed months.
- Temp-table bulk deletion (`_delete_not_in_temp`) avoiding SQLite parameter limits.
- Dedicated columns for BOTH `payment_date` (Payment Date) and `actual_start_date` (Actual Start Date)
  in `students` and `months`, never conflating the two dates.
- Single-user optional PIN lock stored as salted `hashlib.scrypt` hash.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..domain.attendance_engine import (
    DATE_RE,
    MONTH_RE,
    TIME_RE,
    auto_compensate_cancelled_session,
    evaluate_student_month_dates,
    generate_dynamic_months,
    normalize_time_str,
)
from ..domain.demo_data import build_demo_dataset
from ..domain.finance_engine import (
    compute_accounting_summary,
    compute_accrual_month_report,
    compute_collection_report,
    compute_month_stats,
    migrate_legacy_payments_to_receipts,
    parse_pct_int,
    resolve_actual_start_date,
    resolve_end_date,
    resolve_payment_date,
    round_half_up,
    to_decimal,
)
from ..domain.models import (
    DEFAULT_SETTINGS,
    DomainValidationError,
    DuplicateTeacherError,
    clean_teacher_display_name,
    detect_duplicate_students,
    find_duplicate_teacher,
    normalize_teacher_key,
    validate_settings,
    validate_state,
)
from .. import __version__
from ..config import is_onedrive_or_synced_path
from ..domain.automation_engine import (
    DomainEventBus,
    SchedulingConflictError,
    compute_data_quality_audit,
    compute_salary_preparation_report,
    detect_scheduling_conflicts,
    preview_bulk_operation,
    search_operational_entities,
)
from ..domain.demo_cleaner import (
    export_quarantine_archive,
    inventory_database_demo_data,
)
from ..domain.settings_registry import (
    SETTINGS_BY_KEY,
    build_settings_catalog_payload,
    compute_settings_diff,
    preview_settings_reset_or_import,
)

SCHEMA_VERSION = 3
_LOG = logging.getLogger(__name__)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS teachers(
  name TEXT PRIMARY KEY CHECK(length(trim(name)) > 0 AND length(name) <= 120),
  pos INTEGER DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS students(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  code TEXT DEFAULT '',
  name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND length(name) <= 120),
  phone TEXT DEFAULT '' CHECK(length(phone) <= 40),
  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
  type TEXT DEFAULT '' CHECK(length(type) <= 120),
  pct REAL NOT NULL DEFAULT 0 CHECK(pct >= 0 AND pct <= 100),
  schedule TEXT DEFAULT '' CHECK(length(schedule) <= 200),
  admin_status TEXT DEFAULT '' CHECK(length(admin_status) <= 64),
  payment_date TEXT DEFAULT '',
  actual_start_date TEXT DEFAULT '',
  transfers TEXT DEFAULT '[]',
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_students_teacher ON students(teacher);
CREATE INDEX IF NOT EXISTS ix_students_phone ON students(phone);
CREATE INDEX IF NOT EXISTS ix_students_name_phone ON students(name, phone);

CREATE TABLE IF NOT EXISTS months(
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  month_key TEXT NOT NULL CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  value INTEGER CHECK(value IS NULL OR value >= 0),
  payment_date TEXT DEFAULT '',
  actual_start_date TEXT DEFAULT '',
  pay TEXT DEFAULT '',
  paid_status TEXT DEFAULT '',
  partial_paid INTEGER CHECK(partial_paid IS NULL OR partial_paid >= 0),
  custom_wage INTEGER CHECK(custom_wage IS NULL OR custom_wage >= 0),
  end_date TEXT DEFAULT '',
  att TEXT DEFAULT '{}',
  discount INTEGER CHECK(discount IS NULL OR (discount >= 0 AND (value IS NULL OR discount <= value))),
  invoice_note TEXT DEFAULT '' CHECK(length(invoice_note) <= 1000),
  PRIMARY KEY(student_id, month_key)
);

CREATE TABLE IF NOT EXISTS transactions(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  type TEXT NOT NULL CHECK(type IN ('revenue', 'expense')),
  category TEXT NOT NULL CHECK(length(trim(category)) > 0 AND length(category) <= 120),
  amount INTEGER NOT NULL CHECK(amount >= 0),
  tx_date TEXT NOT NULL CHECK(tx_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  month_key TEXT NOT NULL CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  method TEXT DEFAULT '' CHECK(length(method) <= 80),
  note TEXT DEFAULT '' CHECK(length(note) <= 1000),
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_transactions_month ON transactions(month_key);

CREATE TABLE IF NOT EXISTS installments(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  receipt_no TEXT NOT NULL CHECK(length(trim(receipt_no)) > 0 AND length(receipt_no) <= 64),
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  month_key TEXT NOT NULL CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  amount INTEGER NOT NULL CHECK(amount > 0),
  pay_date TEXT NOT NULL CHECK(pay_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  method TEXT DEFAULT '' CHECK(length(method) <= 80),
  note TEXT DEFAULT '' CHECK(length(note) <= 1000),
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_installments_stu_month ON installments(student_id, month_key);

CREATE TABLE IF NOT EXISTS salaries(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  teacher TEXT NOT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE CASCADE,
  month_key TEXT NOT NULL CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  amount INTEGER NOT NULL CHECK(amount > 0),
  pay_date TEXT DEFAULT '',
  note TEXT DEFAULT '' CHECK(length(note) <= 1000)
);
CREATE INDEX IF NOT EXISTS ix_salaries_teacher_month ON salaries(teacher, month_key);

CREATE TABLE IF NOT EXISTS study_groups(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND length(name) <= 120),
  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
  days_json TEXT NOT NULL DEFAULT '[]',
  start_time TEXT DEFAULT '16:00',
  duration_min INTEGER NOT NULL DEFAULT 40 CHECK(duration_min > 0 AND duration_min <= 600),
  capacity INTEGER NOT NULL DEFAULT 8 CHECK(capacity >= 0),
  notes TEXT DEFAULT '' CHECK(length(notes) <= 1000)
);

CREATE TABLE IF NOT EXISTS attendance_plans(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  student_name TEXT DEFAULT '',
  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
  group_id TEXT DEFAULT NULL REFERENCES study_groups(id) ON DELETE SET NULL,
  count INTEGER NOT NULL CHECK(count > 0 AND count <= 500),
  days_json TEXT NOT NULL DEFAULT '[]',
  start_time TEXT DEFAULT '16:00',
  duration_min INTEGER NOT NULL DEFAULT 40 CHECK(duration_min > 0 AND duration_min <= 600),
  start_date TEXT NOT NULL CHECK(start_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  end_limit TEXT DEFAULT '',
  note TEXT DEFAULT '' CHECK(length(note) <= 1000),
  created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  plan_id TEXT DEFAULT NULL REFERENCES attendance_plans(id) ON DELETE SET NULL,
  group_id TEXT DEFAULT NULL REFERENCES study_groups(id) ON DELETE SET NULL,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
  session_date TEXT NOT NULL CHECK(session_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  start_time TEXT NOT NULL CHECK(start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
  duration_min INTEGER NOT NULL DEFAULT 40 CHECK(duration_min > 0 AND duration_min <= 600),
  status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','done','absent','excused','late','cancel_teacher','cancel_admin','postponed','makeup')),
  compensated_for TEXT DEFAULT '',
  notes TEXT DEFAULT '' CHECK(length(notes) <= 1000),
  created_at TEXT DEFAULT '',
  updated_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_sessions_student ON sessions(student_id);
CREATE INDEX IF NOT EXISTS ix_sessions_date ON sessions(session_date);

CREATE TABLE IF NOT EXISTS memorization_logs(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  pos INTEGER DEFAULT 0,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
  log_date TEXT NOT NULL CHECK(log_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  track TEXT NOT NULL CHECK(length(trim(track)) > 0 AND length(track) <= 120),
  from_surah TEXT DEFAULT '',
  from_ayah INTEGER NOT NULL DEFAULT 1 CHECK(from_ayah >= 1),
  to_surah TEXT DEFAULT '',
  to_ayah INTEGER NOT NULL DEFAULT 1 CHECK(to_ayah >= 1),
  juz INTEGER NOT NULL DEFAULT 30 CHECK(juz >= 1 AND juz <= 30),
  pages REAL NOT NULL DEFAULT 1 CHECK(pages >= 0),
  grade TEXT DEFAULT '',
  score REAL NOT NULL DEFAULT 10 CHECK(score >= 0 AND score <= 100),
  notes TEXT DEFAULT '' CHECK(length(notes) <= 1000),
  created_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_memorization_student ON memorization_logs(student_id);

CREATE TABLE IF NOT EXISTS madrasa_batches(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND length(name) <= 120),
  slogan TEXT DEFAULT 'يوم لا يخلو من القرآن',
  fee INTEGER NOT NULL DEFAULT 60 CHECK(fee >= 0),
  start_date TEXT DEFAULT '',
  supervisor TEXT DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
  meta TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS madrasa_enrollments(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  batch_id TEXT NOT NULL REFERENCES madrasa_batches(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  enrolled_at TEXT DEFAULT '',
  fee_paid INTEGER NOT NULL DEFAULT 0 CHECK(fee_paid >= 0),
  tx_id TEXT DEFAULT '',
  streak INTEGER NOT NULL DEFAULT 0 CHECK(streak >= 0),
  best_streak INTEGER NOT NULL DEFAULT 0 CHECK(best_streak >= 0),
  badges TEXT DEFAULT '[]',
  tracks_progress TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS madrasa_logs(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  batch_id TEXT NOT NULL REFERENCES madrasa_batches(id) ON DELETE CASCADE,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  log_date TEXT NOT NULL CHECK(log_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  prayers TEXT DEFAULT '{}',
  fajr_awake INTEGER NOT NULL DEFAULT 0 CHECK(fajr_awake IN (0, 1)),
  wird_done INTEGER NOT NULL DEFAULT 0 CHECK(wird_done IN (0, 1)),
  adhkar_morning INTEGER NOT NULL DEFAULT 0 CHECK(adhkar_morning IN (0, 1)),
  adhkar_evening INTEGER NOT NULL DEFAULT 0 CHECK(adhkar_evening IN (0, 1)),
  ilm_lesson TEXT DEFAULT '',
  tarbawi_challenge INTEGER NOT NULL DEFAULT 0 CHECK(tarbawi_challenge IN (0, 1)),
  points INTEGER NOT NULL DEFAULT 0 CHECK(points >= 0),
  compensatory_goals TEXT DEFAULT '[]',
  supervisor_note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS certificates(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  cert_no TEXT NOT NULL CHECK(length(trim(cert_no)) > 0),
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  student_name TEXT NOT NULL CHECK(length(trim(student_name)) > 0),
  course_title TEXT NOT NULL CHECK(length(trim(course_title)) > 0),
  grade TEXT DEFAULT 'ممتاز متقن',
  issue_date TEXT NOT NULL CHECK(issue_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  teacher_name TEXT DEFAULT '',
  supervisor_name TEXT DEFAULT '',
  coords_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS closed_months(
  month_key TEXT PRIMARY KEY CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  closed_at TEXT NOT NULL,
  actor TEXT DEFAULT 'admin',
  note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS import_batches(
  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
  created_at TEXT NOT NULL,
  dataset TEXT NOT NULL,
  filename TEXT NOT NULL,
  mode TEXT NOT NULL,
  snapshot_name TEXT DEFAULT '',
  rev_after INTEGER NOT NULL DEFAULT 0,
  summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_students_code ON students(code);
CREATE INDEX IF NOT EXISTS ix_students_pos ON students(pos);
CREATE INDEX IF NOT EXISTS ix_months_month_key ON months(month_key);
CREATE INDEX IF NOT EXISTS ix_installments_month ON installments(month_key);
CREATE INDEX IF NOT EXISTS ix_installments_pay_date ON installments(pay_date);
CREATE INDEX IF NOT EXISTS ix_sessions_teacher_date ON sessions(teacher, session_date);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit(action);
"""

CONFIG_KV_KEYS = (
    "subTypes",
    "dashConfig",
    "settings",
    "alertsState",
    "certificateTemplate",
    "tableUiConfig",
    "pricingConfig",
    "columnMappings",
)


class ConcurrencyConflictError(RuntimeError):
    """Raised when client `rev` does not match current store `rev` (maps to HTTP 409)."""

    def __init__(self, current_rev: int, client_rev: int):
        super().__init__(f"تغيرت البيانات في نافذة أخرى (الإصدار الحالي {current_rev}، إصدار الواجهة {client_rev})")
        self.current_rev = current_rev
        self.client_rev = client_rev


class MonthLockedError(ValueError):
    """Raised when attempting to modify financial records in a locked/closed month (maps to HTTP 400/423)."""

    def __init__(self, month_key: str):
        super().__init__(f"الشهر {month_key} مغلق مالياً؛ لا يمكن تعديل الاشتراكات أو الإيصالات أو الأجور إلا بعد إعادة فتح الشهر.")
        self.month_key = month_key


class EmptyCollectionError(ValueError):
    """Raised when a full-state write would empty a table that currently holds data (maps to HTTP 400).

    Guards against stale/partial clients posting `installments: []`, `payouts: []`, `transactions: []`
    or `students: []` and silently wiping receipts, wage payouts, treasury rows or all students.
    A caller that really wants this must pass `allow_empty=True` (HTTP: body flag `allowEmptyCollections`).
    """

    def __init__(self, collection: str, existing: int):
        super().__init__(
            f"رفض الحفظ: القائمة «{collection}» فارغة بينما يوجد {existing} سجل محفوظ؛ "
            f"هذا قد يمسح بيانات مالية بالخطأ. (empty '{collection}' would delete {existing} stored rows)"
        )
        self.collection = collection
        self.existing = existing


def _int_money(v: Any, allow_none: bool = True, field_name: str = "amount") -> int | None:
    if v is None or v == "":
        if allow_none:
            return None
        raise ValueError(f"missing {field_name}")
    val = round_half_up(v)
    if val < 0:
        raise ValueError(f"negative {field_name} not allowed: {val}")
    return val


def _ro_uri(path: Path | str) -> str:
    """Read-only SQLite URI. `Path.as_uri()` percent-encodes spaces, `#`, `?`, `%` and non-ASCII characters, which a
    hand-built `file:C:/Users/My Name/...` URI does not (Windows profile folders often contain spaces or Arabic)."""
    return Path(path).resolve().as_uri() + "?mode=ro"


def rotate(folder: Path, pattern: str, keep: int) -> None:
    """Retains the newest `keep` files matching `pattern` and deletes older ones (and their -wal/-shm leftovers)."""
    files = sorted(Path(folder).glob(pattern), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep] if keep > 0 else files:
        old.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            old.with_name(old.name + suffix).unlink(missing_ok=True)


@contextlib.contextmanager
def _no_new_wal_companions(path: Path | str):
    """Opening a WAL-mode database read-only creates `-wal`/`-shm` files that a read-only connection cannot remove.
    Use around read-only opens of backup files: companions that did NOT exist beforehand are deleted on exit
    (call only after the connection is closed). Pre-existing companions are never touched."""
    p = Path(path)
    names = [p.with_name(p.name + s) for s in ("-wal", "-shm")]
    existed = [n.exists() for n in names]
    try:
        yield
    finally:
        for n, was in zip(names, existed):
            if not was:
                try:
                    n.unlink()
                except OSError:        # FileNotFoundError, or Windows still releasing the handle: harmless leftover
                    pass


def _sweep_orphan_part_companions(folder: Path) -> None:
    """Removes `*.part-wal` / `*.part-shm` left behind by versions <= 1.1.0 (their `.part` file no longer exists)."""
    try:
        for pat in ("*.sqlite3.part-wal", "*.sqlite3.part-shm"):
            for orphan in Path(folder).glob(pat):
                if not orphan.with_name(orphan.name.rsplit("-", 1)[0]).exists():
                    orphan.unlink(missing_ok=True)
    except OSError:
        pass


def verify_sqlite_backup_file(file_path: Path) -> bool:
    """Runs `PRAGMA integrity_check` on a written SQLite backup file."""
    p = Path(file_path)
    if not p.is_file() or p.stat().st_size == 0:
        return False
    with _no_new_wal_companions(p):
        conn = sqlite3.connect(_ro_uri(p), uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row and row[0] == "ok")
        except Exception:
            return False
        finally:
            conn.close()


def _sync_pct_aliases(meta: dict[str, Any]) -> dict[str, Any]:
    """Keeps teacher `pct` (read by every wage calculation / the UI) and `defaultPct` (written by the importer,
    wizard and settings) identical. `pct` wins when both are present; a blank one is filled from the other."""
    def blank(v: Any) -> bool:
        return v is None or v == ""

    pct, dp = meta.get("pct"), meta.get("defaultPct")
    if not blank(pct):
        meta["defaultPct"] = pct
    elif not blank(dp):
        meta["pct"] = dp
    return meta


_TEACHER_ENVELOPE_KEYS = frozenset({
    "name", "oldName", "originalName", "newName", "mode",
    "allowDuplicate", "force", "rev", "clientRev", "meta",
})


def _sync_teacher_status_aliases(
    meta: dict[str, Any],
    incoming_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keeps `archived` and `active` flags consistent in teacher metadata while preserving backward compatibility."""
    patch = incoming_patch if isinstance(incoming_patch, dict) else {}
    has_archived_in_patch = "archived" in patch
    has_active_in_patch = "active" in patch

    def _is_truthy(val: Any) -> bool:
        if isinstance(val, bool):
            return val
        if val in (None, "", 0, "0", "false", "False", "no", "off"):
            return False
        return True

    if has_archived_in_patch:
        want_archived = _is_truthy(patch.get("archived"))
        if want_archived:
            meta["archived"] = True
            meta["active"] = False
            if not meta.get("archivedAt"):
                meta["archivedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
        else:
            meta.pop("archived", None)
            meta.pop("archivedAt", None)
            if "active" in meta or has_active_in_patch:
                meta["active"] = True
    elif has_active_in_patch:
        want_active = _is_truthy(patch.get("active"))
        if not want_active:
            meta["archived"] = True
            meta["active"] = False
            if not meta.get("archivedAt"):
                meta["archivedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
        else:
            meta.pop("archived", None)
            meta.pop("archivedAt", None)
            meta["active"] = True
    else:
        if meta.get("archived") in (False, None, 0, "", "false"):
            meta.pop("archived", None)
            meta.pop("archivedAt", None)
            if meta.get("active") is False:
                meta["archived"] = True
                if not meta.get("archivedAt"):
                    meta["archivedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
        elif meta.get("archived") is True:
            meta["active"] = False
    return meta


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.rev = 0
        self.event_bus = DomainEventBus()
        with self.lock:
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self._run_versioned_migrations()
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            self._backfill_teacher_pct()
            self._resync_rev()

    def _backfill_teacher_pct(self) -> None:
        """Idempotent repair: teachers saved with only `defaultPct` (e.g. by the importer before this fix)
        get the `pct` key that wage calculations and the UI read. Only touches rows that need it."""
        try:
            for tr in self.db.execute("SELECT name, meta FROM teachers").fetchall():
                try:
                    mobj = json.loads(tr["meta"] or "{}")
                except Exception:
                    continue
                if not isinstance(mobj, dict):
                    continue
                before = (mobj.get("pct"), mobj.get("defaultPct"))
                _sync_pct_aliases(mobj)
                if (mobj.get("pct"), mobj.get("defaultPct")) != before:
                    self.db.execute(
                        "UPDATE teachers SET meta=? WHERE name=?",
                        (json.dumps(mobj, ensure_ascii=False), tr["name"]),
                    )
        except Exception:
            _LOG.exception("teacher pct backfill failed")

    def _resync_rev(self) -> None:
        """Re-derives `rev` from the audit log. `rev` is the id of the newest audit row.

        `audit.id` is AUTOINCREMENT (never reused) and the audit table is trimmed to its latest
        5000 rows, so MAX(id) is monotonic across restarts - unlike COUNT(*), which was capped at
        5000 and made `rev` go backwards after a restart (and let stale windows collide).
        """
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()
        self.rev = int(row[0]) if row else 0

    def _rollback(self) -> None:
        """Rolls back the open transaction and re-syncs `rev` (log() bumps it before COMMIT)."""
        try:
            self.db.execute("ROLLBACK")
        finally:
            try:
                self._resync_rev()
            except Exception:
                pass

    def _existing_tables(self) -> set[str]:
        return {
            r[0]
            for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    def get_schema_version(self) -> int:
        with self.lock:
            row = self.db.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    def _run_versioned_migrations(self) -> None:
        """Executes ordered migrations from current `PRAGMA user_version` up to `SCHEMA_VERSION`."""
        tables = self._existing_tables()
        cur_ver = self.get_schema_version()

        if not tables:
            # Brand new database: create full latest schema and set user_version = SCHEMA_VERSION
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return

        if cur_ver >= SCHEMA_VERSION:
            return

        # Existing database needing upgrade: take automatic pre-migration snapshot first
        backup_dir = self.path.parent / "backups"
        try:
            self.snapshot(backup_dir, f"pre_migrate_v{cur_ver}_to_v{SCHEMA_VERSION}", keep=30)
        except Exception:
            pass

        migrations = [
            (1, self._mig_v0_to_v1),
            (2, self._mig_v1_to_v2),
            (3, self._mig_v2_to_v3),
        ]

        for target_ver, mig_fn in migrations:
            if cur_ver < target_ver:
                self.db.execute("PRAGMA foreign_keys=OFF")
                self.db.execute("BEGIN")
                try:
                    mig_fn()
                    self.db.execute(f"PRAGMA user_version = {target_ver}")
                    self.db.execute("COMMIT")
                    cur_ver = target_ver
                except Exception:
                    self._rollback()
                    self.db.execute("PRAGMA foreign_keys=ON")
                    raise
                self.db.execute("PRAGMA foreign_keys=ON")

    def _mig_v0_to_v1(self) -> None:
        """Migration v0 -> v1: ensures `payment_date`, `actual_start_date`, `discount`, `invoice_note` columns exist."""
        tables = self._existing_tables()
        if "students" in tables:
            scols = {r[1] for r in self.db.execute("PRAGMA table_info(students)").fetchall()}
            if "payment_date" not in scols:
                self.db.execute("ALTER TABLE students ADD COLUMN payment_date TEXT DEFAULT ''")
            if "actual_start_date" not in scols:
                self.db.execute("ALTER TABLE students ADD COLUMN actual_start_date TEXT DEFAULT ''")

        if "months" in tables:
            mcols = {r[1] for r in self.db.execute("PRAGMA table_info(months)").fetchall()}
            if "payment_date" not in mcols:
                self.db.execute("ALTER TABLE months ADD COLUMN payment_date TEXT DEFAULT ''")
                if "pay" in mcols:
                    self.db.execute("UPDATE months SET payment_date = COALESCE(pay, '') WHERE payment_date = ''")
            if "actual_start_date" not in mcols:
                self.db.execute("ALTER TABLE months ADD COLUMN actual_start_date TEXT DEFAULT ''")
            if "discount" not in mcols:
                self.db.execute("ALTER TABLE months ADD COLUMN discount INTEGER")
            if "invoice_note" not in mcols:
                self.db.execute("ALTER TABLE months ADD COLUMN invoice_note TEXT DEFAULT ''")

    def _mig_v1_to_v2(self) -> None:
        """Migration v1 -> v2:
        1) Upgrades `students.pct` constraint to allow integer percentages `0..100` and converts
           v1 fractional percentages (`0 < pct <= 1`) to integer `0..100` (in v1, `1.0` was 100%, `0.7` was 70%).
        2) Converts legacy month-level `pay`/`partial_paid`/`paid_status` into receipt rows in `installments`
           without changing any financial totals.
        """
        tables = self._existing_tables()
        if "students" in tables:
            # Rebuild students table to relax CHECK(pct >= 0 AND pct <= 1) -> CHECK(pct >= 0 AND pct <= 100)
            self.db.execute(
                """
                CREATE TABLE students_v2(
                  id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
                  pos INTEGER DEFAULT 0,
                  code TEXT DEFAULT '',
                  name TEXT NOT NULL CHECK(length(trim(name)) > 0 AND length(name) <= 120),
                  phone TEXT DEFAULT '' CHECK(length(phone) <= 40),
                  teacher TEXT DEFAULT NULL REFERENCES teachers(name) ON UPDATE CASCADE ON DELETE SET NULL,
                  type TEXT DEFAULT '' CHECK(length(type) <= 120),
                  pct REAL NOT NULL DEFAULT 0 CHECK(pct >= 0 AND pct <= 100),
                  schedule TEXT DEFAULT '' CHECK(length(schedule) <= 200),
                  admin_status TEXT DEFAULT '' CHECK(length(admin_status) <= 64),
                  payment_date TEXT DEFAULT '',
                  actual_start_date TEXT DEFAULT '',
                  transfers TEXT DEFAULT '[]',
                  updated_at REAL
                )
                """
            )
            rows = self.db.execute("SELECT * FROM students").fetchall()
            for r in rows:
                raw_pct = float(r["pct"] or 0.0)
                if 0 < raw_pct <= 1.0:
                    new_pct = float(round_half_up(raw_pct * 100))
                else:
                    new_pct = float(parse_pct_int(raw_pct))
                self.db.execute(
                    """
                    INSERT INTO students_v2(
                      id,pos,code,name,phone,teacher,type,pct,schedule,admin_status,
                      payment_date,actual_start_date,transfers,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        r["id"],
                        r["pos"],
                        r["code"] or "",
                        r["name"],
                        r["phone"] or "",
                        r["teacher"],
                        r["type"] or "",
                        new_pct,
                        r["schedule"] or "",
                        r["admin_status"] or "",
                        r["payment_date"] or "",
                        r["actual_start_date"] or "",
                        r["transfers"] or "[]",
                        r["updated_at"],
                    ),
                )
            self.db.execute("DROP TABLE students")
            self.db.execute("ALTER TABLE students_v2 RENAME TO students")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_students_teacher ON students(teacher)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_students_phone ON students(phone)")
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_students_name_phone ON students(name, phone)")

        if "teachers" in tables:
            trows = self.db.execute("SELECT name, meta FROM teachers").fetchall()
            for tr in trows:
                try:
                    mobj = json.loads(tr["meta"] or "{}")
                except Exception:
                    mobj = {}
                if isinstance(mobj, dict) and "defaultPct" in mobj and mobj["defaultPct"] is not None:
                    dp = float(mobj["defaultPct"] or 0)
                    if 0 < dp <= 1.0:
                        mobj["defaultPct"] = round_half_up(dp * 100)
                    else:
                        mobj["defaultPct"] = parse_pct_int(dp)
                    _sync_pct_aliases(mobj)
                    self.db.execute(
                        "UPDATE teachers SET meta=? WHERE name=?",
                        (json.dumps(mobj, ensure_ascii=False), tr["name"]),
                    )

        # Ensure installments table exists before migrating legacy payments
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS installments(
              id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
              pos INTEGER DEFAULT 0,
              receipt_no TEXT NOT NULL CHECK(length(trim(receipt_no)) > 0 AND length(receipt_no) <= 64),
              student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
              month_key TEXT NOT NULL CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
              amount INTEGER NOT NULL CHECK(amount > 0),
              pay_date TEXT NOT NULL CHECK(pay_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
              method TEXT DEFAULT '' CHECK(length(method) <= 80),
              note TEXT DEFAULT '' CHECK(length(note) <= 1000),
              created_at TEXT DEFAULT ''
            )
            """
        )

        if "months" in tables and "students" in tables:
            s_ids = [r["id"] for r in self.db.execute("SELECT id FROM students").fetchall()]
            students_list = []
            for sid in s_ids:
                m_rows = self.db.execute("SELECT * FROM months WHERE student_id=?", (sid,)).fetchall()
                m_dict = {}
                for mr in m_rows:
                    p_dt = mr["payment_date"] if mr["payment_date"] else (mr["pay"] or "")
                    m_dict[mr["month_key"]] = {
                        "value": mr["value"],
                        "discount": mr["discount"],
                        "paymentDate": p_dt,
                        "pay": p_dt,
                        "paidStatus": mr["paid_status"] or "",
                        "partialPaid": mr["partial_paid"],
                    }
                students_list.append({"id": sid, "months": m_dict})

            existing_inst = [
                {
                    "id": r["id"],
                    "receiptNo": r["receipt_no"],
                    "studentId": r["student_id"],
                    "month": r["month_key"],
                    "amount": r["amount"],
                    "date": r["pay_date"],
                    "paymentDate": r["pay_date"],
                    "method": r["method"] or "",
                    "note": r["note"] or "",
                    "createdAt": r["created_at"] or "",
                }
                for r in self.db.execute("SELECT * FROM installments ORDER BY pos, id").fetchall()
            ]
            new_inst = migrate_legacy_payments_to_receipts(students_list, existing_inst)
            if len(new_inst) > len(existing_inst):
                for idx, inst in enumerate(new_inst):
                    self.db.execute(
                        """
                        INSERT OR IGNORE INTO installments(
                          id,pos,receipt_no,student_id,month_key,amount,pay_date,method,note,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            inst["id"],
                            idx,
                            inst["receiptNo"],
                            inst["studentId"],
                            inst["month"],
                            int(inst["amount"]),
                            inst["paymentDate"],
                            inst.get("method") or "نقدي / محول",
                            inst.get("note") or "",
                            inst.get("createdAt") or "",
                        ),
                    )

    def _mig_v2_to_v3(self) -> None:
        """Migration v2 -> v3: adds `closed_months` and `import_batches` tables."""
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS closed_months(
              month_key TEXT PRIMARY KEY CHECK(month_key GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
              closed_at TEXT NOT NULL,
              actor TEXT DEFAULT 'admin',
              note TEXT DEFAULT ''
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS import_batches(
              id TEXT PRIMARY KEY CHECK(length(id) > 0 AND length(id) <= 64),
              created_at TEXT NOT NULL,
              dataset TEXT NOT NULL,
              filename TEXT NOT NULL,
              mode TEXT NOT NULL,
              snapshot_name TEXT DEFAULT '',
              rev_after INTEGER NOT NULL DEFAULT 0,
              summary_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

    def _delete_not_in_temp(
        self,
        table: str,
        pk_col: str,
        kept_ids: list[str],
        where_col: str | None = None,
        where_val: str | None = None,
    ) -> None:
        """Deletes rows not present in `kept_ids` using a temp table to avoid SQLite parameter limits."""
        if not kept_ids:
            if where_col is not None:
                self.db.execute(f"DELETE FROM {table} WHERE {where_col}=?", (where_val,))
            else:
                self.db.execute(f"DELETE FROM {table}")
            return
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_ids(k TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM _keep_ids")
        self.db.executemany(
            "INSERT OR IGNORE INTO _keep_ids(k) VALUES (?)",
            [(str(x),) for x in kept_ids],
        )
        if where_col is not None:
            self.db.execute(
                f"DELETE FROM {table} WHERE {where_col}=? AND {pk_col} NOT IN (SELECT k FROM _keep_ids)",
                (where_val,),
            )
        else:
            self.db.execute(f"DELETE FROM {table} WHERE {pk_col} NOT IN (SELECT k FROM _keep_ids)")
        self.db.execute("DELETE FROM _keep_ids")

    def check_rev(self, expected_rev: int | None) -> None:
        """Verifies client's optimistic concurrency `rev` if supplied."""
        if expected_rev is None:
            return
        with self.lock:
            if int(expected_rev) != int(self.rev):
                raise ConcurrencyConflictError(self.rev, int(expected_rev))

    # ------------------------------------------------------------------
    # Month Locking (Phase 2)
    # ------------------------------------------------------------------
    def _closed_fingerprint(self) -> dict[tuple, tuple]:
        """Snapshot of every financial row that belongs to a closed month.

        Used by bulk/cascading writers (`replace_all`, `upsert_student`, `delete_student`,
        `delete_teacher`) which can touch many months at once: the fingerprint taken before and
        after the write inside the same transaction must be identical, otherwise the write
        would have silently modified a locked month.
        """
        closed = [r[0] for r in self.db.execute("SELECT month_key FROM closed_months").fetchall()]
        if not closed:
            return {}
        marks = ",".join("?" * len(closed))
        fp: dict[tuple, tuple] = {}
        for r in self.db.execute(
            f"SELECT student_id, month_key, value, discount, custom_wage, payment_date, pay, "
            f"paid_status, partial_paid, actual_start_date, end_date, att, invoice_note "
            f"FROM months WHERE month_key IN ({marks})",
            closed,
        ):
            fp[("month", r[0], r[1])] = tuple(r)
        for r in self.db.execute(
            f"SELECT id, student_id, month_key, amount, pay_date, receipt_no FROM installments "
            f"WHERE month_key IN ({marks}) AND id NOT LIKE 'inst_mig_%'",
            closed,
        ):
            fp[("receipt", r[0], r[2])] = tuple(r)
        for r in self.db.execute(
            f"SELECT id, teacher, month_key, amount, pay_date FROM salaries WHERE month_key IN ({marks})",
            closed,
        ):
            fp[("payout", r[0], r[2])] = tuple(r)
        for r in self.db.execute(
            f"SELECT id, type, category, amount, tx_date, month_key FROM transactions WHERE month_key IN ({marks})",
            closed,
        ):
            fp[("tx", r[0], r[5])] = tuple(r)
        return fp

    def _assert_closed_unchanged(self, before: dict[tuple, tuple]) -> None:
        """Raises `MonthLockedError` if any closed-month row differs from `before`."""
        if not before and not self.db.execute("SELECT 1 FROM closed_months LIMIT 1").fetchone():
            return
        after = self._closed_fingerprint()
        if after == before:
            return
        changed = sorted(
            {k[-1] if k[0] != "month" else k[2] for k in (set(before) ^ set(after))}
            | {(k[-1] if k[0] != "month" else k[2]) for k in before if k in after and before[k] != after[k]}
        )
        raise MonthLockedError(changed[0] if changed else "")

    def is_month_closed(self, month_key: str) -> bool:
        mk = str(month_key or "").strip()
        if not mk:
            return False
        with self.lock:
            row = self.db.execute("SELECT 1 FROM closed_months WHERE month_key=?", (mk,)).fetchone()
            return row is not None

    def assert_month_open(self, month_key: str) -> None:
        if self.is_month_closed(month_key):
            raise MonthLockedError(month_key)

    _MONTH_BEARING_TABLES = frozenset({"installments", "salaries", "transactions"})

    def _assert_existing_row_month_open(self, table: str, row_id: str) -> None:
        """An upsert that reuses an existing id must also be allowed to touch the month the row is
        stored in NOW; otherwise sending the same id with an open month moves a record out of a closed month."""
        if table not in self._MONTH_BEARING_TABLES:
            raise ValueError(f"unsupported table: {table}")
        row = self.db.execute(f"SELECT month_key FROM {table} WHERE id=?", (row_id,)).fetchone()
        if row and row["month_key"]:
            self.assert_month_open(row["month_key"])

    def list_closed_months(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {
                    "month": r["month_key"],
                    "closedAt": r["closed_at"],
                    "actor": r["actor"] or "admin",
                    "note": r["note"] or "",
                }
                for r in self.db.execute("SELECT * FROM closed_months ORDER BY month_key DESC").fetchall()
            ]

    def close_month(
        self,
        month_key: str,
        note: str = "",
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        mk = str(month_key or "").strip()
        if not MONTH_RE.match(mk):
            raise ValueError(f"invalid month key: {mk}")
        with self.lock:
            self.check_rev(expected_rev)
            if backup_folder:
                self.snapshot(backup_folder, f"close_month_{mk.replace('-', '')}", keep=20)
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    """
                    INSERT INTO closed_months(month_key, closed_at, actor, note)
                    VALUES(?,?,?,?)
                    ON CONFLICT(month_key) DO UPDATE SET
                      closed_at=excluded.closed_at, actor=excluded.actor, note=excluded.note
                    """,
                    (mk, now_iso, actor, str(note or "")[:500]),
                )
                self.log(actor, "close_month", f"{mk} {note}".strip())
                self.db.execute("COMMIT")
                return {"ok": True, "month": mk, "closedAt": now_iso, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def reopen_month(
        self,
        month_key: str,
        note: str = "",
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        mk = str(month_key or "").strip()
        if not MONTH_RE.match(mk):
            raise ValueError(f"invalid month key: {mk}")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM closed_months WHERE month_key=?", (mk,))
                self.log(actor, "reopen_month", f"{mk} {note}".strip())
                self.db.execute("COMMIT")
                return {"ok": True, "month": mk, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    # ------------------------------------------------------------------
    # Settings, Onboarding Wizard & PIN Lock (Phases 1 & 2)
    # ------------------------------------------------------------------
    def get_kv(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            if not row:
                return default
            try:
                return json.loads(row["v"])
            except Exception:
                return default

    def put_kv(
        self,
        key: str,
        value: Any,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)",
                    (str(key), json.dumps(value, ensure_ascii=False)),
                )
                self.log(actor, "put_kv", str(key))
                self.db.execute("COMMIT")
                return {"ok": True, "key": key, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def is_onboarded(self) -> bool:
        """Returns True if settings have been initialized or existing students are present."""
        with self.lock:
            s = self.get_settings()
            if s.get("onboardingCompleted") or s.get("onboarded"):
                return True
            return self.has_data()

    def get_settings(self) -> dict[str, Any]:
        with self.lock:
            raw = self.get_kv("settings", {})
            merged = dict(DEFAULT_SETTINGS)
            if isinstance(raw, dict):
                merged.update(raw)
            if self.has_data() and "onboarded" not in (raw or {}):
                merged["onboarded"] = True
            is_done = bool(merged.get("onboarded") or merged.get("onboardingCompleted"))
            merged["onboarded"] = is_done
            merged["onboardingCompleted"] = is_done
            return merged

    def update_settings(
        self,
        patch: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
        source: str = "settings_update",
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            before = self.get_settings()
            cur = dict(before)
            cur.update(patch or {})
            validated = validate_settings(cur)
            diffs = compute_settings_diff(before, validated, actor=actor, source=source)
            audit_log = self.get_kv("settingsAuditLog", [])
            if not isinstance(audit_log, list):
                audit_log = []
            if diffs:
                audit_log = (diffs + audit_log)[:500]
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)",
                    (json.dumps(validated, ensure_ascii=False),),
                )
                if diffs:
                    self.db.execute(
                        "INSERT OR REPLACE INTO kv(k, v) VALUES('settingsAuditLog', ?)",
                        (json.dumps(audit_log, ensure_ascii=False),),
                    )
                changed_keys = [d["key"] for d in diffs]
                detail_str = ",".join(changed_keys[:8]) if changed_keys else validated.get("academyName", "")
                self.log(actor, "update_settings", detail_str)
                self.db.execute("COMMIT")
                return {
                    "ok": True,
                    "settings": validated,
                    "changedKeys": changed_keys,
                    "changesCount": len(diffs),
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def get_settings_audit_log(self, limit: int = 100, category: str = "", q: str = "") -> list[dict[str, Any]]:
        with self.lock:
            raw = self.get_kv("settingsAuditLog", [])
            if not isinstance(raw, list):
                return []
            cat = str(category or "").strip()
            query = str(q or "").strip().lower()
            out = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                if cat and entry.get("category") != cat:
                    continue
                if query:
                    hay = f"{entry.get('key', '')} {entry.get('labelAr', '')} {entry.get('labelEn', '')} {entry.get('oldValue', '')} {entry.get('newValue', '')}".lower()
                    if query not in hay:
                        continue
                out.append(entry)
                if len(out) >= max(1, min(limit, 500)):
                    break
            return out

    def reset_settings(
        self,
        keys: list[str] | None = None,
        category: str | None = None,
        reset_all: bool = False,
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Resets a single setting, a category of settings, or all settings to factory defaults.

        SAFETY INVARIANT: Resetting settings NEVER deletes student, teacher, attendance, or financial records.
        """
        with self.lock:
            self.check_rev(expected_rev)
            before = self.get_settings()
            target_keys: list[str] = []
            if keys:
                target_keys = [str(k).strip() for k in keys if str(k).strip() in DEFAULT_SETTINGS]
            elif category:
                cat_clean = str(category).strip()
                target_keys = [
                    k for k, meta in SETTINGS_BY_KEY.items()
                    if meta.get("category") == cat_clean and k in DEFAULT_SETTINGS
                ]
            elif reset_all:
                target_keys = [
                    k for k in DEFAULT_SETTINGS.keys()
                    if k not in ("onboardingCompleted", "onboarded", "isDemoData")
                ]
            else:
                raise DomainValidationError("يرجى تحديد الإعداد أو القسم المراد إعادة ضبطه")

            if not target_keys:
                return {"ok": True, "settings": before, "resetKeys": [], "rev": self.rev}

            snap_name = ""
            if backup_folder:
                snap = self.snapshot(Path(backup_folder), "before_settings_reset", keep=25)
                snap_name = snap.name

            patch = {k: DEFAULT_SETTINGS[k] for k in target_keys if k in DEFAULT_SETTINGS}
            # Preserve onboardingCompleted so resetting settings does not lock the user back into first-run wizard
            if before.get("onboardingCompleted") or before.get("onboarded"):
                patch["onboardingCompleted"] = True
                patch["onboarded"] = True

            res = self.update_settings(
                patch,
                actor=actor,
                expected_rev=None,
                source=f"reset:{category or (all if reset_all else keys)}",
            )
            res["resetKeys"] = target_keys
            res["snapshot"] = snap_name
            return res

    def preview_settings_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise DomainValidationError("ملف الإعدادات غير صالح")
        settings_in = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        validated_target = validate_settings(settings_in)
        with self.lock:
            cur = self.get_settings()
        preview = preview_settings_reset_or_import(cur, validated_target)
        has_sub_types = isinstance(payload.get("subTypes"), dict)
        has_pricing = isinstance(payload.get("pricingConfig"), dict)
        preview["includesSubTypes"] = has_sub_types
        preview["includesPricingConfig"] = has_pricing
        return preview

    def import_settings(
        self,
        payload: dict[str, Any],
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        if not confirm:
            raise DomainValidationError("يجب تأكيد استيراد الإعدادات بعد المعاينة (confirm=true)")
        if not isinstance(payload, dict):
            raise DomainValidationError("ملف الإعدادات غير صالح")
        with self.lock:
            self.check_rev(expected_rev)
            snap_name = ""
            if backup_folder:
                snap = self.snapshot(Path(backup_folder), "before_settings_import", keep=25)
                snap_name = snap.name
            settings_in = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
            res = self.update_settings(settings_in, actor=actor, expected_rev=None, source="settings_import")
            if isinstance(payload.get("subTypes"), dict) and payload["subTypes"]:
                clean_st = {
                    str(k).strip(): max(0, round_half_up(v or 0))
                    for k, v in payload["subTypes"].items()
                    if str(k).strip()
                }
                self.put_kv("subTypes", clean_st, actor=actor, expected_rev=None)
            if isinstance(payload.get("pricingConfig"), dict):
                self.put_kv("pricingConfig", payload["pricingConfig"], actor=actor, expected_rev=None)
            if isinstance(payload.get("dashConfig"), dict):
                self.put_kv("dashConfig", payload["dashConfig"], actor=actor, expected_rev=None)
            if isinstance(payload.get("tableUiConfig"), dict):
                self.put_kv("tableUiConfig", payload["tableUiConfig"], actor=actor, expected_rev=None)
            res["snapshot"] = snap_name
            res["rev"] = self.rev
            return res

    def complete_onboarding(
        self,
        payload: dict[str, Any],
        actor: str = "admin",
    ) -> dict[str, Any]:
        """Saves first-run onboarding wizard settings, teachers, and subscription packages in one transaction."""
        if not isinstance(payload, dict):
            raise ValueError("invalid onboarding payload")
        settings_in = dict(DEFAULT_SETTINGS)
        if isinstance(payload.get("settings"), dict):
            s_obj = dict(payload["settings"])
            if "logoDataUri" in s_obj and "logoDataUrl" not in s_obj:
                s_obj["logoDataUrl"] = s_obj["logoDataUri"]
            if "currency" in s_obj and "currencyLabel" not in s_obj:
                s_obj["currencyLabel"] = s_obj["currency"]
            settings_in.update(s_obj)
        for k in (
            "academyName",
            "academySubtitle",
            "logoDataUrl",
            "currencyLabel",
            "defaultDurationDays",
            "expiringSoonDays",
            "defaultTeacherPct",
            "wageBasis",
            "wageAccrualMode",
            "extraBackupFolder",
            "backupWarnDays",
            "pinIdleMinutes",
        ):
            if k in payload:
                settings_in[k] = payload[k]
        settings_in["onboardingCompleted"] = True
        settings_in["onboarded"] = True
        validated_settings = validate_settings(settings_in)

        teachers_in = payload.get("teachers") or []
        sub_types_in = payload.get("subTypes") or {}

        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)",
                    (json.dumps(validated_settings, ensure_ascii=False),),
                )
                if isinstance(sub_types_in, dict) and sub_types_in:
                    clean_st = {}
                    for k, v in sub_types_in.items():
                        ks = str(k or "").strip()
                        if ks:
                            clean_st[ks] = max(0, round_half_up(v or 0))
                    self.db.execute(
                        "INSERT OR REPLACE INTO kv(k, v) VALUES('subTypes', ?)",
                        (json.dumps(clean_st, ensure_ascii=False),),
                    )
                elif isinstance(sub_types_in, list) and sub_types_in:
                    clean_st = {}
                    for item in sub_types_in:
                        if isinstance(item, str) and "|" in item:
                            parts = [p.strip() for p in item.split("|")]
                            if parts[0]:
                                try:
                                    clean_st[parts[0]] = max(0, round_half_up(parts[1] if len(parts) > 1 and parts[1] else 0))
                                except Exception:
                                    clean_st[parts[0]] = 0
                        elif isinstance(item, str) and item.strip():
                            clean_st[item.strip()] = 0
                        elif isinstance(item, dict) and item.get("name"):
                            clean_st[str(item["name"]).strip()] = max(0, round_half_up(item.get("price") or item.get("value") or 0))
                    if clean_st:
                        self.db.execute(
                            "INSERT OR REPLACE INTO kv(k, v) VALUES('subTypes', ?)",
                            (json.dumps(clean_st, ensure_ascii=False),),
                        )
                if isinstance(teachers_in, list):
                    for idx, t_item in enumerate(teachers_in):
                        if isinstance(t_item, str):
                            t_name = t_item.strip()
                            t_meta = _sync_pct_aliases(
                                {"defaultPct": validated_settings["defaultTeacherPct"], "active": True}
                            )
                        elif isinstance(t_item, dict):
                            t_name = str(t_item.get("name") or "").strip()
                            t_meta = {
                                "phone": str(t_item.get("phone") or ""),
                                "specialty": str(t_item.get("specialty") or "حفظ وتجويد"),
                                "defaultPct": parse_pct_int(
                                    t_item.get("pct", t_item.get("defaultPct", validated_settings["defaultTeacherPct"]))
                                ),
                                "active": True,
                            }
                            _sync_pct_aliases(t_meta)
                        else:
                            continue
                        if not t_name:
                            continue
                        self.db.execute(
                            """
                            INSERT INTO teachers(name, pos, meta) VALUES(?, ?, ?)
                            ON CONFLICT(name) DO UPDATE SET pos=excluded.pos, meta=excluded.meta
                            """,
                            (t_name, idx, json.dumps(t_meta, ensure_ascii=False)),
                        )
                self.log(actor, "complete_onboarding", validated_settings.get("academyName", ""))
                self.db.execute("COMMIT")
                return {"ok": True, "settings": validated_settings, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    # ------------------------------------------------------------------
    # Single-User PIN Lock (`hashlib.scrypt`)
    # ------------------------------------------------------------------
    def has_pin(self) -> bool:
        with self.lock:
            info = self.get_kv("pinAuth", None)
            return bool(isinstance(info, dict) and info.get("hash") and info.get("salt"))

    def set_pin(self, pin_str: str, actor: str = "admin") -> dict[str, Any]:
        pin = str(pin_str or "").strip()
        if len(pin) < 4 or len(pin) > 32:
            raise ValueError("يجب أن يتكون رمز PIN من 4 إلى 32 رقمًا/حرفًا")
        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(pin.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        payload = {"salt": salt.hex(), "hash": dk.hex(), "updatedAt": time.time()}
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('pinAuth', ?)", (json.dumps(payload),))
                s = self.get_settings()
                s["pinEnabled"] = True
                self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)", (json.dumps(s, ensure_ascii=False),))
                self.log(actor, "set_pin", "PIN enabled")
                self.db.execute("COMMIT")
                return {"ok": True, "pinEnabled": True, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def clear_pin(self, actor: str = "admin") -> dict[str, Any]:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM kv WHERE k='pinAuth'")
                s = self.get_settings()
                s["pinEnabled"] = False
                self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)", (json.dumps(s, ensure_ascii=False),))
                self.log(actor, "clear_pin", "PIN disabled")
                self.db.execute("COMMIT")
                return {"ok": True, "pinEnabled": False, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def verify_pin(self, pin_str: str) -> bool:
        with self.lock:
            info = self.get_kv("pinAuth", None)
            if not isinstance(info, dict) or not info.get("hash") or not info.get("salt"):
                return True
            try:
                salt = bytes.fromhex(info["salt"])
                expected = bytes.fromhex(info["hash"])
                dk = hashlib.scrypt(str(pin_str or "").strip().encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
                return hmac.compare_digest(dk, expected)
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Internal FK helpers
    # ------------------------------------------------------------------
    def _ensure_teacher_in_tx(self, teacher_name: str | None) -> str | None:
        t = str(teacher_name or "").strip()
        if not t:
            return None
        row = self.db.execute("SELECT 1 FROM teachers WHERE name=?", (t,)).fetchone()
        if not row:
            max_pos = self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM teachers").fetchone()[0]
            self.db.execute("INSERT INTO teachers(name, pos, meta) VALUES(?, ?, '{}')", (t, max_pos))
        return t

    def _student_exists_in_tx(self, student_id: str | None) -> bool:
        sid = str(student_id or "").strip()
        if not sid:
            return False
        return self.db.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone() is not None

    def _group_exists_in_tx(self, group_id: str | None) -> str | None:
        gid = str(group_id or "").strip()
        if not gid:
            return None
        row = self.db.execute("SELECT 1 FROM study_groups WHERE id=?", (gid,)).fetchone()
        return gid if row else None

    def _plan_exists_in_tx(self, plan_id: str | None) -> str | None:
        pid = str(plan_id or "").strip()
        if not pid:
            return None
        row = self.db.execute("SELECT 1 FROM attendance_plans WHERE id=?", (pid,)).fetchone()
        return pid if row else None

    def _ensure_batch_in_tx(self, batch_id: str | None) -> str:
        bid = str(batch_id or "batch_default").strip() or "batch_default"
        row = self.db.execute("SELECT 1 FROM madrasa_batches WHERE id=?", (bid,)).fetchone()
        if not row:
            self.db.execute(
                """
                INSERT INTO madrasa_batches(id,name,slogan,fee,start_date,supervisor,active,meta)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (bid, "دفعة الفجر - الأولى", "يوم لا يخلو من القرآن", 60, "", "", 1, "{}"),
            )
        return bid

    def close(self) -> None:
        """Flushes WAL and closes the SQLite connection cleanly."""
        with self.lock:
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            try:
                self.db.close()
            except Exception:
                pass

    def log(self, actor: str, action: str, detail: str = "") -> None:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
                (time.time(), actor, action, detail),
            )
            self.rev = int(cur.lastrowid)  # type: ignore[arg-type]  # set by the INSERT above
            self.db.execute("DELETE FROM audit WHERE id <= ? - 5000", (self.rev,))

    def audit(
        self,
        limit: int = 100,
        action: str = "",
        actor: str = "",
        q: str = "",
    ) -> list[dict[str, Any]]:
        with self.lock:
            clauses = []
            params: list[Any] = []
            if action and action.strip():
                clauses.append("action = ?")
                params.append(action.strip())
            if actor and actor.strip():
                clauses.append("actor = ?")
                params.append(actor.strip())
            if q and q.strip():
                clauses.append("(action LIKE ? OR detail LIKE ? OR actor LIKE ?)")
                pat = f"%{q.strip()}%"
                params.extend([pat, pat, pat])
            where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(min(max(int(limit or 100), 1), 1000))
            return [
                dict(r)
                for r in self.db.execute(
                    f"SELECT id, ts, actor, action, detail FROM audit{where_sql} ORDER BY id DESC LIMIT ?",
                    params,
                )
            ]

    def _put_student_in_tx(self, s: dict[str, Any], pos: int) -> None:
        sid = str(s["id"]).strip()
        name = str(s["name"]).strip()
        if not sid or not name:
            raise ValueError("student id and name are required")
        if len(name) > 120:
            raise ValueError("اسم الطالبة طويل جدًا (الحد الأقصى 120 حرفًا)")
        if len(str(s.get("phone") or "")) > 40:
            raise ValueError("رقم الهاتف طويل جدًا (الحد الأقصى 40 حرفًا)")
        t_ref = self._ensure_teacher_in_tx(s.get("teacher"))
        pct_int = parse_pct_int(s.get("pct") or 0)

        # Check previous teacher for automatic transfer history & future scheduled session propagation
        prev_row = self.db.execute("SELECT teacher, transfers FROM students WHERE id=?", (sid,)).fetchone()
        prev_teacher = (prev_row["teacher"] or "").strip() if prev_row else ""
        transfers_list = list(s.get("transferHistory") or [])
        if not transfers_list and prev_row and prev_row["transfers"]:
            try:
                parsed_tr = json.loads(prev_row["transfers"] or "[]")
                if isinstance(parsed_tr, list):
                    transfers_list = parsed_tr
            except Exception:
                pass
        if prev_row and prev_teacher and t_ref and prev_teacher != t_ref:
            last_tr = transfers_list[-1] if transfers_list and isinstance(transfers_list[-1], dict) else {}
            if last_tr.get("from") != prev_teacher or last_tr.get("to") != t_ref:
                transfers_list.append({
                    "from": prev_teacher,
                    "to": t_ref,
                    "date": datetime.date.today().isoformat(),
                })

        # Strictly independent student-level paymentDate and actualStartDate
        stu_payment_date = str(s.get("paymentDate") or "").strip()
        stu_actual_start = str(s.get("actualStartDate") or "").strip()

        self.db.execute(
            """
            INSERT INTO students(
              id,pos,code,name,phone,teacher,type,pct,schedule,admin_status,
              payment_date,actual_start_date,transfers,updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              pos=excluded.pos, code=excluded.code, name=excluded.name, phone=excluded.phone,
              teacher=excluded.teacher, type=excluded.type, pct=excluded.pct, schedule=excluded.schedule,
              admin_status=excluded.admin_status, payment_date=excluded.payment_date,
              actual_start_date=excluded.actual_start_date, transfers=excluded.transfers,
              updated_at=excluded.updated_at
            """,
            (
                sid,
                pos,
                str(s.get("code") or ""),
                name,
                str(s.get("phone") or ""),
                t_ref,
                str(s.get("type") or ""),
                float(pct_int),
                str(s.get("schedule") or ""),
                str(s.get("adminStatus") or ""),
                stu_payment_date,
                stu_actual_start,
                json.dumps(transfers_list, ensure_ascii=False),
                time.time(),
            ),
        )

        months_dict = s.get("months")
        if months_dict is not None:
            if not isinstance(months_dict, dict):
                raise ValueError("months must be a dictionary")
            self._delete_not_in_temp(
                "months",
                "month_key",
                list(months_dict.keys()),
                where_col="student_id",
                where_val=sid,
            )
            for mk, m in months_dict.items():
                self._put_month_in_tx(sid, str(mk), m)
        if prev_row and prev_teacher != (t_ref or ""):
            today_iso = datetime.date.today().isoformat()
            self.db.execute(
                "UPDATE attendance_plans SET teacher=? WHERE student_id=?",
                (t_ref, sid),
            )
            self.db.execute(
                "UPDATE sessions SET teacher=?, updated_at=? WHERE student_id=? AND status='scheduled' AND session_date >= ?",
                (t_ref, today_iso, sid, today_iso),
            )

    def _put_month_in_tx(self, student_id: str, month_key: str, m: dict[str, Any]) -> None:
        if not MONTH_RE.match(str(month_key)) or not isinstance(m, dict):
            raise ValueError(f"invalid month entry {month_key}")
        val = _int_money(m.get("value"), allow_none=True, field_name="subscription value")
        pp = _int_money(m.get("partialPaid"), allow_none=True, field_name="partialPaid")
        cw = _int_money(m.get("customWage"), allow_none=True, field_name="customWage")
        disc = _int_money(m.get("discount"), allow_none=True, field_name="discount")
        if disc is not None and disc > 0:
            if val is None or disc > val:
                raise ValueError(f"discount ({disc}) cannot exceed subscription value ({val})")

        # Strictly independent Payment Date and Actual Start Date
        payment_date = resolve_payment_date(m)
        actual_start_date = resolve_actual_start_date(m)
        end_date = resolve_end_date(m)

        self.db.execute(
            """
            INSERT INTO months(
              student_id,month_key,value,payment_date,actual_start_date,pay,
              paid_status,partial_paid,custom_wage,end_date,att,discount,invoice_note
            )
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(student_id,month_key) DO UPDATE SET
              value=excluded.value,
              payment_date=excluded.payment_date,
              actual_start_date=excluded.actual_start_date,
              pay=excluded.pay,
              paid_status=excluded.paid_status,
              partial_paid=excluded.partial_paid,
              custom_wage=excluded.custom_wage,
              end_date=excluded.end_date,
              att=excluded.att,
              discount=excluded.discount,
              invoice_note=excluded.invoice_note
            """,
            (
                student_id,
                month_key,
                val,
                payment_date,
                actual_start_date,
                payment_date,
                str(m.get("paidStatus") or ""),
                pp,
                cw,
                end_date,
                json.dumps(m.get("att") or {}, ensure_ascii=False),
                disc,
                str(m.get("invoiceNote") or ""),
            ),
        )

    def _sync_operational_tables_in_tx(self, state: dict[str, Any], partial_only: bool = False) -> None:
        if "payouts" in state and isinstance(state["payouts"], list):
            kept_ids = []
            for idx, p in enumerate(state["payouts"]):
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id") or f"sal_{idx+1}").strip()
                tname = self._ensure_teacher_in_tx(p.get("teacher"))
                mk = str(p.get("month") or "").strip()
                amt = _int_money(p.get("amount"), allow_none=False, field_name="payout amount")
                if not tname or not MONTH_RE.match(mk) or not amt or amt <= 0:
                    raise ValueError("invalid salary payout record")
                kept_ids.append(pid)
                self.db.execute(
                    """
                    INSERT INTO salaries(id,pos,teacher,month_key,amount,pay_date,note)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, teacher=excluded.teacher, month_key=excluded.month_key,
                      amount=excluded.amount, pay_date=excluded.pay_date, note=excluded.note
                    """,
                    (pid, idx, tname, mk, amt, str(p.get("date") or ""), str(p.get("note") or "")),
                )
            if not partial_only:
                self._delete_not_in_temp("salaries", "id", kept_ids)

        if "transactions" in state and isinstance(state["transactions"], list):
            kept_ids = []
            for idx, tx in enumerate(state["transactions"]):
                if not isinstance(tx, dict):
                    continue
                tid = str(tx.get("id") or f"tx_{idx+1}").strip()
                ttype = str(tx.get("type") or "").strip()
                cat = str(tx.get("category") or "").strip()
                amt = _int_money(tx.get("amount"), allow_none=False, field_name="transaction amount")
                dt = str(tx.get("date") or "").strip()
                mk = str(tx.get("month") or dt[:7]).strip()
                if ttype not in ("revenue", "expense") or not cat or amt is None or amt < 0 or not DATE_RE.match(dt) or not MONTH_RE.match(mk):
                    raise ValueError("invalid transaction record")
                kept_ids.append(tid)
                self.db.execute(
                    """
                    INSERT INTO transactions(id,pos,type,category,amount,tx_date,month_key,method,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, type=excluded.type, category=excluded.category, amount=excluded.amount,
                      tx_date=excluded.tx_date, month_key=excluded.month_key, method=excluded.method,
                      note=excluded.note, created_at=excluded.created_at
                    """,
                    (
                        tid,
                        idx,
                        ttype,
                        cat,
                        amt,
                        dt,
                        mk,
                        str(tx.get("method") or ""),
                        str(tx.get("note") or ""),
                        str(tx.get("createdAt") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("transactions", "id", kept_ids)

        if "installments" in state and isinstance(state["installments"], list):
            kept_ids = []
            for idx, inst in enumerate(state["installments"]):
                if not isinstance(inst, dict):
                    continue
                iid = str(inst.get("id") or f"inst_{idx+1}").strip()
                sid = str(inst.get("studentId") or "").strip()
                rcp = str(inst.get("receiptNo") or "").strip()
                mk = str(inst.get("month") or "").strip()
                amt = _int_money(inst.get("amount"), allow_none=False, field_name="installment amount")
                dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
                if not iid or not sid or not rcp or not MONTH_RE.match(mk) or not amt or amt <= 0 or not DATE_RE.match(dt):
                    raise ValueError("invalid installment record")
                if not self._student_exists_in_tx(sid):
                    if partial_only:
                        continue
                    raise ValueError(f"installment references unknown student: {sid}")
                kept_ids.append(iid)
                self.db.execute(
                    """
                    INSERT INTO installments(id,pos,receipt_no,student_id,month_key,amount,pay_date,method,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, receipt_no=excluded.receipt_no, student_id=excluded.student_id,
                      month_key=excluded.month_key, amount=excluded.amount, pay_date=excluded.pay_date,
                      method=excluded.method, note=excluded.note, created_at=excluded.created_at
                    """,
                    (
                        iid,
                        idx,
                        rcp,
                        sid,
                        mk,
                        amt,
                        dt,
                        str(inst.get("method") or ""),
                        str(inst.get("note") or ""),
                        str(inst.get("createdAt") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("installments", "id", kept_ids)

        if "groups" in state and isinstance(state["groups"], list):
            kept_ids = []
            for idx, g in enumerate(state["groups"]):
                if not isinstance(g, dict):
                    continue
                gid = str(g.get("id") or f"grp_{idx+1}").strip()
                gname = str(g.get("name") or "").strip()
                if not gid or not gname:
                    raise ValueError("invalid group record")
                t_ref = self._ensure_teacher_in_tx(g.get("teacher"))
                days = g.get("days") if isinstance(g.get("days"), list) else []
                dur = int(float(_n if (_n := g.get("dur")) is not None else 40))
                cap = int(float(_n if (_n := g.get("capacity")) is not None else 8))
                kept_ids.append(gid)
                self.db.execute(
                    """
                    INSERT INTO study_groups(id,pos,name,teacher,days_json,start_time,duration_min,capacity,notes)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, name=excluded.name, teacher=excluded.teacher, days_json=excluded.days_json,
                      start_time=excluded.start_time, duration_min=excluded.duration_min, capacity=excluded.capacity,
                      notes=excluded.notes
                    """,
                    (
                        gid,
                        idx,
                        gname,
                        t_ref,
                        json.dumps(days, ensure_ascii=False),
                        normalize_time_str(g.get("time"), "16:00"),
                        dur,
                        max(0, cap),
                        str(g.get("notes") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("study_groups", "id", kept_ids)

        if "plans" in state and isinstance(state["plans"], list):
            kept_ids = []
            for idx, p in enumerate(state["plans"]):
                if not isinstance(p, dict):
                    continue
                pid = str(p.get("id") or f"pln_{idx+1}").strip()
                sid = str(p.get("studentId") or "").strip()
                if not pid or not sid:
                    raise ValueError("invalid plan record")
                if not self._student_exists_in_tx(sid):
                    if partial_only:
                        continue
                    raise ValueError(f"plan references unknown student: {sid}")
                t_ref = self._ensure_teacher_in_tx(p.get("teacher"))
                g_ref = self._group_exists_in_tx(p.get("groupId"))
                cnt = int(float(p.get("count") or 8))
                dur = int(float(p.get("dur") or 40))
                sdate = str(p.get("actualStartDate") or p.get("startDate") or "").strip()
                kept_ids.append(pid)
                self.db.execute(
                    """
                    INSERT INTO attendance_plans(id,pos,student_id,student_name,teacher,group_id,count,days_json,start_time,duration_min,start_date,end_limit,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, student_id=excluded.student_id, student_name=excluded.student_name,
                      teacher=excluded.teacher, group_id=excluded.group_id, count=excluded.count,
                      days_json=excluded.days_json, start_time=excluded.start_time, duration_min=excluded.duration_min,
                      start_date=excluded.start_date, end_limit=excluded.end_limit, note=excluded.note,
                      created_at=excluded.created_at
                    """,
                    (
                        pid,
                        idx,
                        sid,
                        str(p.get("studentName") or ""),
                        t_ref,
                        g_ref,
                        cnt,
                        json.dumps(p.get("days") or [], ensure_ascii=False),
                        normalize_time_str(p.get("time"), "16:00"),
                        dur,
                        sdate,
                        str(p.get("endLimit") or ""),
                        str(p.get("note") or ""),
                        str(p.get("createdAt") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("attendance_plans", "id", kept_ids)

        if "sessions" in state and isinstance(state["sessions"], list):
            kept_ids = []
            for idx, s in enumerate(state["sessions"]):
                if not isinstance(s, dict):
                    continue
                xid = str(s.get("id") or f"sess_{idx+1}").strip()
                sid = str(s.get("studentId") or "").strip()
                if not xid or not sid:
                    raise ValueError("invalid session record")
                if not self._student_exists_in_tx(sid):
                    if partial_only:
                        continue
                    raise ValueError(f"session references unknown student: {sid}")
                t_ref = self._ensure_teacher_in_tx(s.get("teacher"))
                p_ref = self._plan_exists_in_tx(s.get("planId"))
                g_ref = self._group_exists_in_tx(s.get("groupId"))
                sdate = str(s.get("date") or "").strip()
                stime = normalize_time_str(s.get("time"), "16:00")
                dur = int(float(_n if (_n := s.get("dur")) is not None else 40))
                st_val = str(s.get("status") or "scheduled").strip()
                kept_ids.append(xid)
                self.db.execute(
                    """
                    INSERT INTO sessions(id,pos,plan_id,group_id,student_id,teacher,session_date,start_time,duration_min,status,compensated_for,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, plan_id=excluded.plan_id, group_id=excluded.group_id,
                      student_id=excluded.student_id, teacher=excluded.teacher, session_date=excluded.session_date,
                      start_time=excluded.start_time, duration_min=excluded.duration_min, status=excluded.status,
                      compensated_for=excluded.compensated_for, notes=excluded.notes,
                      created_at=excluded.created_at, updated_at=excluded.updated_at
                    """,
                    (
                        xid,
                        idx,
                        p_ref,
                        g_ref,
                        sid,
                        t_ref,
                        sdate,
                        stime,
                        dur,
                        st_val,
                        str(s.get("compensatedFor") or ""),
                        str(s.get("notes") or ""),
                        str(s.get("createdAt") or ""),
                        str(s.get("updatedAt") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("sessions", "id", kept_ids)

        if "memorization" in state and isinstance(state["memorization"], list):
            kept_ids = []
            for idx, m in enumerate(state["memorization"]):
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or f"mem_{idx+1}").strip()
                sid = str(m.get("studentId") or "").strip()
                if not mid or not sid:
                    raise ValueError("invalid memorization record")
                if not self._student_exists_in_tx(sid):
                    if partial_only:
                        continue
                    raise ValueError(f"memorization references unknown student: {sid}")
                t_ref = self._ensure_teacher_in_tx(m.get("teacher"))
                ldate = str(m.get("date") or "").strip()
                track = str(m.get("track") or "").strip()
                f_ayah = max(1, int(float(m.get("fromAyah") or 1)))
                t_ayah = max(1, int(float(m.get("toAyah") or 1)))
                juz = min(30, max(1, int(float(m.get("juz") or 30))))
                pages = float(to_decimal(m.get("pages") if m.get("pages") is not None else 1))
                score = float(to_decimal(m.get("score") if m.get("score") is not None else 10))
                kept_ids.append(mid)
                self.db.execute(
                    """
                    INSERT INTO memorization_logs(id,pos,student_id,teacher,log_date,track,from_surah,from_ayah,to_surah,to_ayah,juz,pages,grade,score,notes,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, student_id=excluded.student_id, teacher=excluded.teacher,
                      log_date=excluded.log_date, track=excluded.track, from_surah=excluded.from_surah,
                      from_ayah=excluded.from_ayah, to_surah=excluded.to_surah, to_ayah=excluded.to_ayah,
                      juz=excluded.juz, pages=excluded.pages, grade=excluded.grade, score=excluded.score,
                      notes=excluded.notes, created_at=excluded.created_at
                    """,
                    (
                        mid,
                        idx,
                        sid,
                        t_ref,
                        ldate,
                        track,
                        str(m.get("fromSurah") or ""),
                        f_ayah,
                        str(m.get("toSurah") or ""),
                        t_ayah,
                        juz,
                        pages,
                        str(m.get("grade") or ""),
                        score,
                        str(m.get("notes") or ""),
                        str(m.get("createdAt") or ""),
                    ),
                )
            if not partial_only:
                self._delete_not_in_temp("memorization_logs", "id", kept_ids)

        if "madrasaBatches" in state and isinstance(state["madrasaBatches"], list):
            kept_ids = []
            for b in state["madrasaBatches"]:
                if isinstance(b, dict) and b.get("id"):
                    bid = str(b["id"]).strip()
                    fee_int = _int_money(b.get("fee") if b.get("fee") is not None else 60, allow_none=False, field_name="batch fee")
                    kept_ids.append(bid)
                    self.db.execute(
                        """
                        INSERT INTO madrasa_batches(id,name,slogan,fee,start_date,supervisor,active,meta)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            name=excluded.name, slogan=excluded.slogan, fee=excluded.fee,
                            start_date=excluded.start_date, supervisor=excluded.supervisor,
                            active=excluded.active, meta=excluded.meta
                        """,
                        (
                            bid,
                            str(b.get("name") or ""),
                            str(b.get("slogan") or "يوم لا يخلو من القرآن"),
                            fee_int,
                            str(b.get("startDate") or ""),
                            str(b.get("supervisor") or ""),
                            1 if b.get("active", True) else 0,
                            json.dumps(b, ensure_ascii=False),
                        ),
                    )
            if not partial_only:
                self._delete_not_in_temp("madrasa_batches", "id", kept_ids)

        if "madrasaEnrollments" in state and isinstance(state["madrasaEnrollments"], list):
            kept_ids = []
            for e in state["madrasaEnrollments"]:
                if isinstance(e, dict) and e.get("id"):
                    eid = str(e["id"]).strip()
                    bid = self._ensure_batch_in_tx(e.get("batchId"))
                    sid = str(e.get("studentId") or "").strip()
                    if not self._student_exists_in_tx(sid):
                        if partial_only:
                            continue
                        raise ValueError(f"enrollment references unknown student: {sid}")
                    fee_paid_int = _int_money(e.get("feePaid") or 0, allow_none=False, field_name="feePaid")
                    kept_ids.append(eid)
                    self.db.execute(
                        """
                        INSERT INTO madrasa_enrollments(id,batch_id,student_id,enrolled_at,fee_paid,tx_id,streak,best_streak,badges,tracks_progress)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            batch_id=excluded.batch_id, student_id=excluded.student_id,
                            enrolled_at=excluded.enrolled_at, fee_paid=excluded.fee_paid, tx_id=excluded.tx_id,
                            streak=excluded.streak, best_streak=excluded.best_streak, badges=excluded.badges,
                            tracks_progress=excluded.tracks_progress
                        """,
                        (
                            eid,
                            bid,
                            sid,
                            str(e.get("enrolledAt") or ""),
                            fee_paid_int,
                            str(e.get("txId") or ""),
                            max(0, int(e.get("streak") or 0)),
                            max(0, int(e.get("bestStreak") or 0)),
                            json.dumps(e.get("badges") or [], ensure_ascii=False),
                            json.dumps(e.get("tracksProgress") or {}, ensure_ascii=False),
                        ),
                    )
            if not partial_only:
                self._delete_not_in_temp("madrasa_enrollments", "id", kept_ids)

        if "madrasaLogs" in state and isinstance(state["madrasaLogs"], list):
            kept_ids = []
            for lg in state["madrasaLogs"]:
                if isinstance(lg, dict) and lg.get("id"):
                    lid = str(lg["id"]).strip()
                    bid = self._ensure_batch_in_tx(lg.get("batchId"))
                    sid = str(lg.get("studentId") or "").strip()
                    if not self._student_exists_in_tx(sid):
                        if partial_only:
                            continue
                        raise ValueError(f"madrasa log references unknown student: {sid}")
                    kept_ids.append(lid)
                    self.db.execute(
                        """
                        INSERT INTO madrasa_logs(id,batch_id,student_id,log_date,prayers,fajr_awake,wird_done,adhkar_morning,adhkar_evening,ilm_lesson,tarbawi_challenge,points,compensatory_goals,supervisor_note)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            batch_id=excluded.batch_id, student_id=excluded.student_id, log_date=excluded.log_date,
                            prayers=excluded.prayers, fajr_awake=excluded.fajr_awake, wird_done=excluded.wird_done,
                            adhkar_morning=excluded.adhkar_morning, adhkar_evening=excluded.adhkar_evening,
                            ilm_lesson=excluded.ilm_lesson, tarbawi_challenge=excluded.tarbawi_challenge,
                            points=excluded.points, compensatory_goals=excluded.compensatory_goals,
                            supervisor_note=excluded.supervisor_note
                        """,
                        (
                            lid,
                            bid,
                            sid,
                            str(lg.get("date") or ""),
                            json.dumps(lg.get("prayers") or {}, ensure_ascii=False),
                            1 if lg.get("fajrAwake") else 0,
                            1 if lg.get("wirdDone") else 0,
                            1 if lg.get("adhkarMorning") else 0,
                            1 if lg.get("adhkarEvening") else 0,
                            str(lg.get("ilmLesson") or ""),
                            1 if lg.get("tarbawiChallenge") else 0,
                            max(0, int(lg.get("points") or 0)),
                            json.dumps(lg.get("compensatoryGoals") or [], ensure_ascii=False),
                            str(lg.get("supervisorNote") or ""),
                        ),
                    )
            if not partial_only:
                self._delete_not_in_temp("madrasa_logs", "id", kept_ids)

        if "certificates" in state and isinstance(state["certificates"], list):
            kept_ids = []
            for c in state["certificates"]:
                if isinstance(c, dict) and c.get("id"):
                    cid = str(c["id"]).strip()
                    sid = str(c.get("studentId") or "").strip()
                    if not self._student_exists_in_tx(sid):
                        if partial_only:
                            continue
                        raise ValueError(f"certificate references unknown student: {sid}")
                    kept_ids.append(cid)
                    self.db.execute(
                        """
                        INSERT INTO certificates(id,cert_no,student_id,student_name,course_title,grade,issue_date,teacher_name,supervisor_name,coords_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                            cert_no=excluded.cert_no, student_id=excluded.student_id,
                            student_name=excluded.student_name, course_title=excluded.course_title,
                            grade=excluded.grade, issue_date=excluded.issue_date,
                            teacher_name=excluded.teacher_name, supervisor_name=excluded.supervisor_name,
                            coords_json=excluded.coords_json
                        """,
                        (
                            cid,
                            str(c.get("certNo") or ""),
                            sid,
                            str(c.get("studentName") or ""),
                            str(c.get("courseTitle") or ""),
                            str(c.get("grade") or "ممتاز متقن"),
                            str(c.get("issueDate") or datetime.date.today().isoformat()),
                            str(c.get("teacherName") or ""),
                            str(c.get("supervisorName") or ""),
                            json.dumps(c.get("coords") or {}, ensure_ascii=False),
                        ),
                    )
            if not partial_only:
                self._delete_not_in_temp("certificates", "id", kept_ids)

    def _installments_in_state_form_in_tx(self) -> list[dict[str, Any]]:
        """Current receipts as state dicts (used to keep real receipts when only legacy migration is requested)."""
        out: list[dict[str, Any]] = []
        for r in self.db.execute(
            "SELECT id, receipt_no, student_id, month_key, amount, pay_date, method, note, created_at "
            "FROM installments ORDER BY pos"
        ).fetchall():
            out.append({
                "id": r["id"], "receiptNo": r["receipt_no"], "studentId": r["student_id"],
                "month": r["month_key"], "amount": r["amount"], "date": r["pay_date"],
                "paymentDate": r["pay_date"], "method": r["method"] or "", "note": r["note"] or "",
                "createdAt": r["created_at"] or "",
            })
        return out

    def _assert_not_emptying(self, state: dict[str, Any], students_list: list[Any]) -> None:
        checks = (
            ("installments", "installments", state.get("installments")),
            ("payouts", "salaries", state.get("payouts")),
            ("transactions", "transactions", state.get("transactions")),
        )
        for key, table, val in checks:
            if isinstance(val, list) and len(val) == 0:
                n = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if n:
                    raise EmptyCollectionError(key, int(n))
        if not students_list:
            n = self.db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            if n:
                raise EmptyCollectionError("students", int(n))

    def replace_all(
        self,
        state: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
        allow_empty: bool = False,
    ) -> None:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                closed_before = self._closed_fingerprint()
                if not allow_empty:
                    self._assert_not_emptying(state, list(state.get("students") or []))
                meta = state.get("teacherMeta") or {}
                teachers_list = list(state.get("teachers") or [])
                if "teachers" not in state:
                    for s in state.get("students") or []:
                        t = str((s or {}).get("teacher") or "").strip()
                        if t and t not in teachers_list:
                            teachers_list.append(t)

                for i, t in enumerate(teachers_list):
                    t_clean = str(t).strip()
                    if not t_clean:
                        continue
                    t_meta = dict(meta.get(t_clean, {})) if isinstance(meta.get(t_clean), dict) else {}
                    if "defaultPct" in t_meta and t_meta["defaultPct"] is not None:
                        t_meta["defaultPct"] = parse_pct_int(t_meta["defaultPct"])
                    if "pct" in t_meta and t_meta["pct"] not in (None, ""):
                        t_meta["pct"] = parse_pct_int(t_meta["pct"])
                    _sync_pct_aliases(t_meta)
                    self.db.execute(
                        """
                        INSERT INTO teachers(name,pos,meta) VALUES(?,?,?)
                        ON CONFLICT(name) DO UPDATE SET pos=excluded.pos, meta=excluded.meta
                        """,
                        (t_clean, i, json.dumps(t_meta, ensure_ascii=False)),
                    )

                students_list = state.get("students") or []
                kept_sids = []
                for i, s in enumerate(students_list):
                    self._put_student_in_tx(s, i)
                    kept_sids.append(str(s["id"]).strip())

                self._delete_not_in_temp("students", "id", kept_sids)

                if "teachers" in state:
                    clean_teachers = [str(t).strip() for t in teachers_list if str(t).strip()]
                    self._delete_not_in_temp("teachers", "name", clean_teachers)

                # If legacy state has paidStatus/partialPaid on months without receipts, migrate them cleanly
                state_for_ops = dict(state)
                if "installments" in state_for_ops or any(
                    isinstance(m, dict) and (m.get("paidStatus") == "paid" or (m.get("partialPaid") or 0) > 0)
                    for s in students_list
                    for m in (s.get("months") or {}).values()
                ):
                    base_receipts = (
                        state["installments"]
                        if isinstance(state.get("installments"), list)
                        else self._installments_in_state_form_in_tx()  # key absent: keep the real receipts
                    )
                    state_for_ops["installments"] = migrate_legacy_payments_to_receipts(students_list, base_receipts)

                self._sync_operational_tables_in_tx(state_for_ops, partial_only=False)

                for k in CONFIG_KV_KEYS:
                    if k in state:
                        self.db.execute(
                            "INSERT OR REPLACE INTO kv VALUES(?,?)",
                            (k, json.dumps(state[k], ensure_ascii=False)),
                        )

                self._assert_closed_unchanged(closed_before)
                self.log(actor, "replace_all", f"{len(students_list)} students")
                self.db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    # ------------------------------------------------------------------
    # Granular CRUD Methods (Phase 2 — Targeted SQL, no full export_state)
    # ------------------------------------------------------------------
    def upsert_teacher(
        self,
        name: str,
        meta: dict[str, Any] | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
        *,
        allow_duplicate: bool = False,
        mode: str = "upsert",
    ) -> dict[str, Any]:
        t_clean = clean_teacher_display_name(name)
        if not t_clean or len(t_clean) > 120:
            raise ValueError("invalid teacher name")
        raw_patch = dict(meta or {})
        meta_obj = {k: v for k, v in raw_patch.items() if k not in _TEACHER_ENVELOPE_KEYS}
        if "defaultPct" in meta_obj and meta_obj["defaultPct"] is not None:
            meta_obj["defaultPct"] = parse_pct_int(meta_obj["defaultPct"])
        if meta_obj.get("pct") not in (None, ""):
            meta_obj["pct"] = parse_pct_int(meta_obj["pct"])
        _sync_pct_aliases(meta_obj)
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                cur = self.db.execute("SELECT name, pos, meta FROM teachers WHERE name=?", (t_clean,)).fetchone()
                if cur is None and str(name or "").strip() != t_clean:
                    cur = self.db.execute(
                        "SELECT name, pos, meta FROM teachers WHERE name=?",
                        (str(name or "").strip(),),
                    ).fetchone()
                if cur:
                    if mode == "create" and not allow_duplicate:
                        raise DuplicateTeacherError(t_clean, cur["name"], match_type="exact")
                    target_name = cur["name"]
                    pos = cur["pos"]
                    existing_meta = json.loads(cur["meta"] or "{}")
                    existing_meta.update(meta_obj)
                    meta_obj = existing_meta
                    _sync_teacher_status_aliases(meta_obj, raw_patch)
                    _sync_pct_aliases(meta_obj)
                else:
                    existing_names = [
                        r["name"] for r in self.db.execute("SELECT name FROM teachers ORDER BY pos").fetchall()
                    ]
                    if not allow_duplicate:
                        dup = find_duplicate_teacher(t_clean, existing_names)
                        if dup is not None:
                            raise DuplicateTeacherError(
                                t_clean,
                                dup["existingName"],
                                match_type=dup["matchType"],
                            )
                    target_name = t_clean
                    pos = self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM teachers").fetchone()[0]
                    _sync_teacher_status_aliases(meta_obj, raw_patch)
                    _sync_pct_aliases(meta_obj)
                self.db.execute(
                    """
                    INSERT INTO teachers(name, pos, meta) VALUES(?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET pos=excluded.pos, meta=excluded.meta
                    """,
                    (target_name, pos, json.dumps(meta_obj, ensure_ascii=False)),
                )
                self.log(actor, "upsert_teacher", target_name)
                self.db.execute("COMMIT")
                is_archived = bool(meta_obj.get("archived")) or (meta_obj.get("active") is False)
                ev_type = "TeacherUpdated" if cur else "TeacherCreated"
                self.event_bus.emit(ev_type, {"name": target_name, "active": not is_archived, "rev": self.rev}, idempotency_key=f"{ev_type}:{target_name}:{self.rev}")
                return {
                    "ok": True,
                    "name": target_name,
                    "active": not is_archived,
                    "archived": is_archived,
                    "meta": meta_obj,
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def rename_teacher(
        self,
        old_name: str,
        new_name: str,
        meta: dict[str, Any] | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
        *,
        allow_duplicate: bool = False,
    ) -> dict[str, Any]:
        """Atomically renames a teacher and cascades the new name across all active and historical records."""
        old_clean = clean_teacher_display_name(old_name)
        new_clean = clean_teacher_display_name(new_name)
        if not old_clean or not new_clean or len(new_clean) > 120:
            raise ValueError("invalid teacher name")
        raw_patch = dict(meta or {})
        meta_patch = {k: v for k, v in raw_patch.items() if k not in _TEACHER_ENVELOPE_KEYS}
        if "defaultPct" in meta_patch and meta_patch["defaultPct"] is not None:
            meta_patch["defaultPct"] = parse_pct_int(meta_patch["defaultPct"])
        if meta_patch.get("pct") not in (None, ""):
            meta_patch["pct"] = parse_pct_int(meta_patch["pct"])
        _sync_pct_aliases(meta_patch)

        with self.lock:
            self.check_rev(expected_rev)
            cur = self.db.execute("SELECT name, pos, meta FROM teachers WHERE name=?", (old_clean,)).fetchone()
            if cur is None and str(old_name or "").strip() != old_clean:
                cur = self.db.execute(
                    "SELECT name, pos, meta FROM teachers WHERE name=?",
                    (str(old_name or "").strip(),),
                ).fetchone()
            if cur is None:
                raise KeyError(f"teacher not found: {old_clean}")

            actual_old = cur["name"]
            if actual_old == new_clean:
                res = self.upsert_teacher(
                    new_clean,
                    meta=raw_patch,
                    actor=actor,
                    expected_rev=None,
                    allow_duplicate=True,
                    mode="upsert",
                )
                res["oldName"] = actual_old
                res["renamed"] = False
                return res

            other_names = [
                r["name"]
                for r in self.db.execute("SELECT name FROM teachers WHERE name != ? ORDER BY pos", (actual_old,)).fetchall()
            ]
            dup = find_duplicate_teacher(new_clean, other_names, exclude_name=actual_old)
            if dup is not None:
                if dup["matchType"] == "exact" or not allow_duplicate:
                    raise DuplicateTeacherError(
                        new_clean,
                        dup["existingName"],
                        match_type=dup["matchType"],
                    )

            existing_meta = json.loads(cur["meta"] or "{}")
            existing_meta.update(meta_patch)
            _sync_teacher_status_aliases(existing_meta, raw_patch)
            _sync_pct_aliases(existing_meta)

            self.db.execute("BEGIN")
            try:
                # 1. Update primary key in `teachers` — ON UPDATE CASCADE updates:
                #    students.teacher, salaries.teacher, study_groups.teacher,
                #    attendance_plans.teacher, sessions.teacher, memorization_logs.teacher
                self.db.execute(
                    "UPDATE teachers SET name=?, meta=? WHERE name=?",
                    (new_clean, json.dumps(existing_meta, ensure_ascii=False), actual_old),
                )
                # 2. Update non-FK `certificates.teacher_name`
                self.db.execute(
                    "UPDATE certificates SET teacher_name=? WHERE teacher_name=?",
                    (new_clean, actual_old),
                )
                # 3. Update `students.transfers` JSON history references
                for srow in self.db.execute(
                    "SELECT id, transfers FROM students WHERE transfers LIKE ?",
                    (f"%{actual_old}%",),
                ).fetchall():
                    try:
                        transfers_list = json.loads(srow["transfers"] or "[]")
                    except Exception:
                        continue
                    if not isinstance(transfers_list, list):
                        continue
                    changed = False
                    for item in transfers_list:
                        if not isinstance(item, dict):
                            continue
                        for tkey in ("from", "to", "fromTeacher", "toTeacher"):
                            if item.get(tkey) == actual_old:
                                item[tkey] = new_clean
                                changed = True
                    if changed:
                        self.db.execute(
                            "UPDATE students SET transfers=? WHERE id=?",
                            (json.dumps(transfers_list, ensure_ascii=False), srow["id"]),
                        )

                self.log(actor, "rename_teacher", f"{actual_old} -> {new_clean}")
                self.db.execute("COMMIT")
                is_archived = bool(existing_meta.get("archived")) or (existing_meta.get("active") is False)
                return {
                    "ok": True,
                    "oldName": actual_old,
                    "name": new_clean,
                    "renamed": True,
                    "active": not is_archived,
                    "archived": is_archived,
                    "meta": existing_meta,
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def set_teacher_active(
        self,
        name: str,
        active: bool,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Explicitly activates or deactivates (archives) a teacher while preserving all historical data."""
        t_clean = clean_teacher_display_name(name)
        with self.lock:
            self.check_rev(expected_rev)
            cur = self.db.execute("SELECT name, meta FROM teachers WHERE name=?", (t_clean,)).fetchone()
            if cur is None and str(name or "").strip() != t_clean:
                cur = self.db.execute(
                    "SELECT name, meta FROM teachers WHERE name=?",
                    (str(name or "").strip(),),
                ).fetchone()
            if cur is None:
                raise KeyError(f"teacher not found: {t_clean}")
            actual_name = cur["name"]
            self.db.execute("BEGIN")
            try:
                before = self._closed_fingerprint()
                meta_obj = json.loads(cur["meta"] or "{}")
                if active:
                    meta_obj.pop("archived", None)
                    meta_obj.pop("archivedAt", None)
                    if "active" in meta_obj:
                        meta_obj["active"] = True
                else:
                    meta_obj["archived"] = True
                    meta_obj["active"] = False
                    if not meta_obj.get("archivedAt"):
                        meta_obj["archivedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
                self.db.execute(
                    "UPDATE teachers SET meta=? WHERE name=?",
                    (json.dumps(meta_obj, ensure_ascii=False), actual_name),
                )
                self._assert_closed_unchanged(before)
                self.log(actor, "reactivate_teacher" if active else "deactivate_teacher", actual_name)
                self.db.execute("COMMIT")
                ev_type = "TeacherReactivated" if active else "TeacherDeactivated"
                self.event_bus.emit(ev_type, {"name": actual_name, "active": bool(active), "rev": self.rev}, idempotency_key=f"{ev_type}:{actual_name}:{self.rev}")
                return {
                    "ok": True,
                    "name": actual_name,
                    "active": bool(active),
                    "archived": not bool(active),
                    "meta": meta_obj,
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def list_teachers(self, include_inactive: bool = True) -> list[dict[str, Any]]:
        """Returns authoritative list of teachers with active/archived status, percentage, and reference counts."""
        with self.lock:
            rows = self.db.execute("SELECT name, pos, meta FROM teachers ORDER BY pos").fetchall()
            stu_counts = {
                r[0]: int(r[1])
                for r in self.db.execute(
                    "SELECT teacher, COUNT(*) FROM students WHERE teacher IS NOT NULL GROUP BY teacher"
                ).fetchall()
            }
            out: list[dict[str, Any]] = []
            for r in rows:
                tname = r["name"]
                meta_obj = json.loads(r["meta"] or "{}")
                _sync_pct_aliases(meta_obj)
                is_archived = bool(meta_obj.get("archived")) or (meta_obj.get("active") is False)
                if not include_inactive and is_archived:
                    continue
                pct_val = parse_pct_int(meta_obj.get("pct") if meta_obj.get("pct") is not None else 70)
                out.append({
                    "name": tname,
                    "pos": r["pos"],
                    "pct": pct_val,
                    "defaultPct": pct_val,
                    "active": not is_archived,
                    "archived": is_archived,
                    "studentsCount": stu_counts.get(tname, 0),
                    "meta": meta_obj,
                })
            return out

    # Tables that reference a teacher by name. A teacher with ANY row here is never hard-deleted:
    # `salaries` would be CASCADE-deleted (silent loss of wage payouts) and the other tables would be
    # SET NULL (history and closed-month wage attribution would change retroactively).
    _TEACHER_REF_TABLES: tuple[tuple[str, str], ...] = (
        ("students", "students"),
        ("payouts", "salaries"),
        ("groups", "study_groups"),
        ("plans", "attendance_plans"),
        ("sessions", "sessions"),
        ("memorization", "memorization_logs"),
    )

    def teacher_references(self, name: str) -> dict[str, int]:
        """Counts of rows that reference the teacher (only non-zero entries are returned)."""
        t_clean = str(name or "").strip()
        with self.lock:
            out: dict[str, int] = {}
            for label, table in self._TEACHER_REF_TABLES:
                n = self.db.execute(f"SELECT COUNT(*) FROM {table} WHERE teacher=?", (t_clean,)).fetchone()[0]
                if n:
                    out[label] = int(n)
            n_certs = self.db.execute("SELECT COUNT(*) FROM certificates WHERE teacher_name=?", (t_clean,)).fetchone()[0]
            if n_certs:
                out["certificates"] = int(n_certs)
            return out

    def delete_teacher(
        self,
        name: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Deletes a teacher that nothing references; otherwise ARCHIVES it (archive-not-delete).

        Archiving sets `meta.archived = True` (+ `archivedAt`) and changes nothing else, so payouts,
        students, sessions and closed-month wage attribution are preserved byte-for-byte.
        Response keeps the old keys (`ok`, `deleted`, `rev`) and adds `archived` / `references`.
        """
        t_clean = clean_teacher_display_name(name)
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                before = self._closed_fingerprint()
                row = self.db.execute("SELECT name, meta FROM teachers WHERE name=?", (t_clean,)).fetchone()
                if row is None and str(name or "").strip() != t_clean:
                    row = self.db.execute(
                        "SELECT name, meta FROM teachers WHERE name=?",
                        (str(name or "").strip(),),
                    ).fetchone()
                if row is None:
                    self.db.execute("COMMIT")
                    return {"ok": True, "deleted": False, "archived": False, "rev": self.rev}
                actual_name = row["name"]
                refs = self.teacher_references(actual_name)
                if refs:
                    meta_obj = json.loads(row["meta"] or "{}")
                    meta_obj["archived"] = True
                    meta_obj["active"] = False
                    meta_obj["archivedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
                    self.db.execute(
                        "UPDATE teachers SET meta=? WHERE name=?",
                        (json.dumps(meta_obj, ensure_ascii=False), actual_name),
                    )
                    self._assert_closed_unchanged(before)
                    self.log(actor, "archive_teacher", actual_name)
                    self.db.execute("COMMIT")
                    return {
                        "ok": True,
                        "name": actual_name,
                        "deleted": False,
                        "archived": True,
                        "active": False,
                        "references": refs,
                        "rev": self.rev,
                    }
                deleted = self.db.execute("DELETE FROM teachers WHERE name=?", (actual_name,)).rowcount > 0
                self._assert_closed_unchanged(before)
                if deleted:
                    self.log(actor, "delete_teacher", actual_name)
                self.db.execute("COMMIT")
                return {"ok": True, "name": actual_name, "deleted": deleted, "archived": False, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def get_student(self, student_id: str) -> dict[str, Any] | None:
        sid = str(student_id or "").strip()
        if not sid:
            return None
        with self.lock:
            r = self.db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
            if not r:
                return None
            months: dict[str, Any] = {}
            for m in self.db.execute("SELECT * FROM months WHERE student_id=?", (sid,)).fetchall():
                pay_dt = m["payment_date"] if m["payment_date"] else (m["pay"] or "")
                actual_start_dt = m["actual_start_date"] or ""
                end_dt = m["end_date"] or ""
                m_entry: dict[str, Any] = {
                    "value": m["value"],
                    "paymentDate": pay_dt,
                    "actualStartDate": actual_start_dt,
                    "pay": pay_dt,
                    "startDate": actual_start_dt,
                    "paidStatus": m["paid_status"] or "",
                    "partialPaid": m["partial_paid"],
                    "customWage": m["custom_wage"],
                    "end": end_dt,
                    "endDate": end_dt,
                    "att": json.loads(m["att"] or "{}"),
                }
                if m["discount"] is not None:
                    m_entry["discount"] = m["discount"]
                if m["invoice_note"]:
                    m_entry["invoiceNote"] = m["invoice_note"]
                months[m["month_key"]] = m_entry
            return {
                "id": r["id"],
                "code": r["code"] or "",
                "name": r["name"],
                "phone": r["phone"] or "",
                "teacher": r["teacher"] or "",
                "type": r["type"] or "",
                "pct": parse_pct_int(r["pct"]),
                "schedule": r["schedule"] or "",
                "adminStatus": r["admin_status"] or "",
                "paymentDate": r["payment_date"] or "",
                "actualStartDate": r["actual_start_date"] or "",
                "transferHistory": json.loads(r["transfers"] or "[]"),
                "months": months,
            }

    def upsert_student(
        self,
        student: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                before = self._closed_fingerprint()
                cur = self.db.execute("SELECT pos FROM students WHERE id=?", (student["id"],)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM students").fetchone()[0]
                self._put_student_in_tx(student, pos)
                # Closed months are immutable: any added/changed/removed closed-month row aborts the write.
                self._assert_closed_unchanged(before)
                self.log(actor, "upsert_student", f"{student.get('id')}:{student.get('name')}")
                self.db.execute("COMMIT")
                ev_type = "StudentUpdated" if cur else "StudentCreated"
                self.event_bus.emit(ev_type, {"studentId": student["id"], "rev": self.rev}, idempotency_key=f"{ev_type}:{student['id']}:{self.rev}")
                return {"ok": True, "id": student["id"], "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_student(
        self,
        student_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> bool:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                before = self._closed_fingerprint()
                deleted = self.db.execute("DELETE FROM students WHERE id=?", (student_id,)).rowcount > 0
                self._assert_closed_unchanged(before)
                if deleted:
                    self.log(actor, "delete_student", student_id)
                self.db.execute("COMMIT")
                return deleted
            except Exception:
                self._rollback()
                raise

    def upsert_student_month(
        self,
        student_id: str,
        month_key: str,
        month_data: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.assert_month_open(month_key)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(student_id):
                    raise KeyError(f"student not found: {student_id}")
                cur_row = self.db.execute(
                    "SELECT * FROM months WHERE student_id = ? AND month_key = ?",
                    (student_id, month_key),
                ).fetchone()
                merged_m: dict[str, Any] = {}
                if cur_row is not None:
                    try:
                        att_parsed = json.loads(cur_row["att"] or "{}")
                    except Exception:
                        att_parsed = {}
                    merged_m = {
                        "value": cur_row["value"],
                        "paymentDate": cur_row["payment_date"] or cur_row["pay"] or "",
                        "pay": cur_row["pay"] or cur_row["payment_date"] or "",
                        "actualStartDate": cur_row["actual_start_date"] or "",
                        "paidStatus": cur_row["paid_status"] or "",
                        "partialPaid": cur_row["partial_paid"],
                        "customWage": cur_row["custom_wage"],
                        "endDate": cur_row["end_date"] or "",
                        "att": att_parsed,
                        "discount": cur_row["discount"],
                        "invoiceNote": cur_row["invoice_note"] or "",
                    }
                if isinstance(month_data, dict):
                    merged_m.update(month_data)
                month_data = merged_m
                self._put_month_in_tx(student_id, month_key, month_data)

                # Sync paymentDate with installments
                p_dt = resolve_payment_date(month_data)
                if p_dt and DATE_RE.match(p_dt):
                    existing = self.db.execute("SELECT id FROM installments WHERE student_id = ? AND month_key = ?", (student_id, month_key)).fetchall()
                    if len(existing) == 1:
                        self.db.execute("UPDATE installments SET pay_date = ? WHERE id = ?", (p_dt, existing[0][0]))

                # Optional convenience: if caller explicitly passes `receiptAmount` (> 0) with a valid `paymentDate`,
                # record or update an installment receipt for that month in the same transaction.
                if "receiptAmount" in month_data and month_data.get("receiptAmount") is not None:
                    r_amt = _int_money(month_data.get("receiptAmount"), allow_none=True, field_name="receiptAmount")
                    p_dt = resolve_payment_date(month_data) or datetime.date.today().isoformat()
                    if r_amt and r_amt > 0 and DATE_RE.match(p_dt):
                        iid = f"inst_{student_id}_{month_key.replace('-', '')}_quick"
                        rcp = f"RCP-Q-{student_id[-4:]}-{month_key.replace('-', '')}"
                        max_pos = self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM installments").fetchone()[0]
                        self.db.execute(
                            """
                            INSERT INTO installments(id,pos,receipt_no,student_id,month_key,amount,pay_date,method,note,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET
                              amount=excluded.amount, pay_date=excluded.pay_date, method=excluded.method, note=excluded.note
                            """,
                            (
                                iid,
                                max_pos,
                                rcp,
                                student_id,
                                month_key,
                                r_amt,
                                p_dt,
                                str(month_data.get("receiptMethod") or "فودافون كاش"),
                                str(month_data.get("invoiceNote") or ""),
                                p_dt,
                            ),
                        )

                if "pct" in month_data and month_data.get("pct") is not None and month_data.get("pct") != "":
                    pct_int = parse_pct_int(month_data.get("pct"), default=0)
                    self.db.execute("UPDATE students SET pct=?, updated_at=? WHERE id=?", (float(pct_int), time.time(), student_id))
                else:
                    self.db.execute("UPDATE students SET updated_at=? WHERE id=?", (time.time(), student_id))
                self.log(actor, "upsert_month", f"{student_id}:{month_key}")
                self.db.execute("COMMIT")
                ev_type = "SubscriptionUpdated" if cur_row is not None else "SubscriptionCreated"
                self.event_bus.emit(ev_type, {"studentId": student_id, "month": month_key, "rev": self.rev}, idempotency_key=f"{ev_type}:{student_id}:{month_key}:{self.rev}")
                return {
                    "ok": True,
                    "studentId": student_id,
                    "month": month_key,
                    "paymentDate": resolve_payment_date(month_data),
                    "actualStartDate": resolve_actual_start_date(month_data),
                    "endDate": resolve_end_date(month_data),
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def delete_student_month(
        self,
        student_id: str,
        month_key: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.assert_month_open(month_key)
            self.db.execute("BEGIN")
            try:
                deleted = (
                    self.db.execute(
                        "DELETE FROM months WHERE student_id=? AND month_key=?",
                        (student_id, month_key),
                    ).rowcount
                    > 0
                )
                if deleted:
                    self.log(actor, "delete_month", f"{student_id}:{month_key}")
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_installment(
        self,
        inst: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(inst, dict):
            raise ValueError("invalid installment payload")
        iid = str(inst.get("id") or f"inst_{secrets.token_hex(4)}").strip()
        sid = str(inst.get("studentId") or "").strip()
        mk = str(inst.get("month") or "").strip()
        rcp = str(inst.get("receiptNo") or f"RCP-{int(time.time()*1000)%1000000:06d}").strip()
        amt = _int_money(inst.get("amount"), allow_none=False, field_name="installment amount")
        dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
        if not iid or not sid or not rcp or not MONTH_RE.match(mk) or not amt or amt <= 0 or not DATE_RE.match(dt):
            raise ValueError("بيانات إيصال الدفع غير مكتملة أو غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            self.assert_month_open(mk)
            self._assert_existing_row_month_open("installments", iid)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                cur = self.db.execute("SELECT pos FROM installments WHERE id=?", (iid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM installments").fetchone()[0]
                created_at = str(inst.get("createdAt") or datetime.date.today().isoformat())
                self.db.execute(
                    """
                    INSERT INTO installments(id,pos,receipt_no,student_id,month_key,amount,pay_date,method,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, receipt_no=excluded.receipt_no, student_id=excluded.student_id,
                      month_key=excluded.month_key, amount=excluded.amount, pay_date=excluded.pay_date,
                      method=excluded.method, note=excluded.note, created_at=excluded.created_at
                    """,
                    (
                        iid,
                        pos,
                        rcp,
                        sid,
                        mk,
                        amt,
                        dt,
                        str(inst.get("method") or ""),
                        str(inst.get("note") or ""),
                        created_at,
                    ),
                )
                # Also update month's latest payment_date if month row exists and payment_date is empty
                mrow = self.db.execute(
                    "SELECT payment_date FROM months WHERE student_id=? AND month_key=?",
                    (sid, mk),
                ).fetchone()
                if mrow and (not mrow["payment_date"] or dt > mrow["payment_date"]):
                    self.db.execute(
                        "UPDATE months SET payment_date=?, pay=? WHERE student_id=? AND month_key=?",
                        (dt, dt, sid, mk),
                    )
                self.log(actor, "upsert_installment", f"{rcp}:{sid}:{mk}:{amt}")
                self.db.execute("COMMIT")
                self.event_bus.emit("ReceiptRecorded", {"id": iid, "studentId": sid, "month": mk, "amount": amt, "rev": self.rev}, idempotency_key=f"ReceiptRecorded:{iid}:{self.rev}")
                return {
                    "ok": True,
                    "installment": {
                        "id": iid,
                        "receiptNo": rcp,
                        "studentId": sid,
                        "month": mk,
                        "amount": amt,
                        "date": dt,
                        "paymentDate": dt,
                        "method": str(inst.get("method") or ""),
                        "note": str(inst.get("note") or ""),
                        "createdAt": created_at,
                    },
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def delete_installment(
        self,
        inst_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            row = self.db.execute("SELECT month_key FROM installments WHERE id=?", (inst_id,)).fetchone()
            if row:
                self.assert_month_open(row["month_key"])
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM installments WHERE id=?", (inst_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_installment", inst_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_transaction(
        self,
        tx: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(tx, dict):
            raise ValueError("invalid transaction payload")
        tid = str(tx.get("id") or f"tx_{secrets.token_hex(4)}").strip()
        ttype = str(tx.get("type") or "").strip()
        cat = str(tx.get("category") or "").strip()
        amt = _int_money(tx.get("amount"), allow_none=False, field_name="transaction amount")
        dt = str(tx.get("date") or "").strip()
        mk = str(tx.get("month") or dt[:7]).strip()
        if ttype not in ("revenue", "expense") or not cat or amt is None or amt < 0 or not DATE_RE.match(dt) or not MONTH_RE.match(mk):
            raise ValueError("بيانات حركة الخزينة غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            self.assert_month_open(mk)
            self._assert_existing_row_month_open("transactions", tid)
            self.db.execute("BEGIN")
            try:
                cur = self.db.execute("SELECT pos FROM transactions WHERE id=?", (tid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM transactions").fetchone()[0]
                created_at = str(tx.get("createdAt") or datetime.date.today().isoformat())
                self.db.execute(
                    """
                    INSERT INTO transactions(id,pos,type,category,amount,tx_date,month_key,method,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, type=excluded.type, category=excluded.category, amount=excluded.amount,
                      tx_date=excluded.tx_date, month_key=excluded.month_key, method=excluded.method,
                      note=excluded.note, created_at=excluded.created_at
                    """,
                    (
                        tid,
                        pos,
                        ttype,
                        cat,
                        amt,
                        dt,
                        mk,
                        str(tx.get("method") or ""),
                        str(tx.get("note") or ""),
                        created_at,
                    ),
                )
                self.log(actor, "upsert_transaction", f"{tid}:{ttype}:{amt}")
                self.db.execute("COMMIT")
                return {
                    "ok": True,
                    "transaction": {
                        "id": tid,
                        "type": ttype,
                        "category": cat,
                        "amount": amt,
                        "date": dt,
                        "month": mk,
                        "method": str(tx.get("method") or ""),
                        "note": str(tx.get("note") or ""),
                        "createdAt": created_at,
                    },
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def delete_transaction(
        self,
        tx_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            row = self.db.execute("SELECT month_key FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if row:
                self.assert_month_open(row["month_key"])
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM transactions WHERE id=?", (tx_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_transaction", tx_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_payout(
        self,
        payout: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payout, dict):
            raise ValueError("invalid payout payload")
        pid = str(payout.get("id") or f"sal_{secrets.token_hex(4)}").strip()
        teacher = str(payout.get("teacher") or "").strip()
        mk = str(payout.get("month") or "").strip()
        amt = _int_money(payout.get("amount"), allow_none=False, field_name="payout amount")
        dt = str(payout.get("date") or datetime.date.today().isoformat()).strip()
        if not pid or not teacher or not MONTH_RE.match(mk) or not amt or amt <= 0:
            raise ValueError("بيانات صرف الراتب غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            self.assert_month_open(mk)
            self._assert_existing_row_month_open("salaries", pid)
            self.db.execute("BEGIN")
            try:
                tname = self._ensure_teacher_in_tx(teacher)
                cur = self.db.execute("SELECT pos FROM salaries WHERE id=?", (pid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM salaries").fetchone()[0]
                self.db.execute(
                    """
                    INSERT INTO salaries(id,pos,teacher,month_key,amount,pay_date,note)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, teacher=excluded.teacher, month_key=excluded.month_key,
                      amount=excluded.amount, pay_date=excluded.pay_date, note=excluded.note
                    """,
                    (pid, pos, tname, mk, amt, dt, str(payout.get("note") or "")),
                )
                self.log(actor, "upsert_payout", f"{pid}:{tname}:{mk}:{amt}")
                self.db.execute("COMMIT")
                self.event_bus.emit("SalaryDataUpdated", {"id": pid, "teacher": tname, "month": mk, "amount": amt, "rev": self.rev}, idempotency_key=f"SalaryDataUpdated:{pid}:{self.rev}")
                return {
                    "ok": True,
                    "payout": {
                        "id": pid,
                        "teacher": tname,
                        "month": mk,
                        "amount": amt,
                        "date": dt,
                        "note": str(payout.get("note") or ""),
                    },
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def delete_payout(
        self,
        payout_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            row = self.db.execute("SELECT month_key FROM salaries WHERE id=?", (payout_id,)).fetchone()
            if row:
                self.assert_month_open(row["month_key"])
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM salaries WHERE id=?", (payout_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_payout", payout_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_group(
        self,
        grp: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(grp, dict):
            raise ValueError("invalid group payload")
        gid = str(grp.get("id") or f"grp_{secrets.token_hex(4)}").strip()
        gname = str(grp.get("name") or "").strip()
        if not gid or not gname:
            raise ValueError("اسم الحلقة مطلوب")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                t_ref = self._ensure_teacher_in_tx(grp.get("teacher"))
                cur = self.db.execute("SELECT pos FROM study_groups WHERE id=?", (gid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM study_groups").fetchone()[0]
                days = grp.get("days") if isinstance(grp.get("days"), list) else []
                dur = max(1, min(600, int(float(_n if (_n := grp.get("dur")) is not None else 40))))
                cap = max(0, int(float(_n if (_n := grp.get("capacity")) is not None else 8)))
                stime = normalize_time_str(grp.get("time"), "16:00")
                self.db.execute(
                    """
                    INSERT INTO study_groups(id,pos,name,teacher,days_json,start_time,duration_min,capacity,notes)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, name=excluded.name, teacher=excluded.teacher, days_json=excluded.days_json,
                      start_time=excluded.start_time, duration_min=excluded.duration_min, capacity=excluded.capacity,
                      notes=excluded.notes
                    """,
                    (gid, pos, gname, t_ref, json.dumps(days, ensure_ascii=False), stime, dur, cap, str(grp.get("notes") or "")),
                )
                self.log(actor, "upsert_group", f"{gid}:{gname}")
                self.db.execute("COMMIT")
                return {
                    "ok": True,
                    "group": {
                        "id": gid,
                        "name": gname,
                        "teacher": t_ref or "",
                        "days": days,
                        "time": stime,
                        "dur": dur,
                        "capacity": cap,
                        "notes": str(grp.get("notes") or ""),
                    },
                    "rev": self.rev,
                }
            except Exception:
                self._rollback()
                raise

    def delete_group(
        self,
        group_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM study_groups WHERE id=?", (group_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_group", group_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_plan(
        self,
        plan: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(plan, dict):
            raise ValueError("invalid plan payload")
        pid = str(plan.get("id") or f"pln_{secrets.token_hex(4)}").strip()
        sid = str(plan.get("studentId") or "").strip()
        sdate = str(plan.get("actualStartDate") or plan.get("startDate") or "").strip()
        if not pid or not sid or not DATE_RE.match(sdate):
            raise ValueError("بيانات الخطة الدراسية غير صالحة (تاريخ البداية الفعلي مطلوب)")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                t_ref = self._ensure_teacher_in_tx(plan.get("teacher"))
                g_ref = self._group_exists_in_tx(plan.get("groupId"))
                cur = self.db.execute("SELECT pos FROM attendance_plans WHERE id=?", (pid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM attendance_plans").fetchone()[0]
                cnt = max(1, min(500, int(float(plan.get("count") or 8))))
                dur = max(1, min(600, int(float(plan.get("dur") or 40))))
                stime = normalize_time_str(plan.get("time"), "16:00")
                days = plan.get("days") if isinstance(plan.get("days"), list) else []
                created_at = str(plan.get("createdAt") or datetime.date.today().isoformat())
                self.db.execute(
                    """
                    INSERT INTO attendance_plans(id,pos,student_id,student_name,teacher,group_id,count,days_json,start_time,duration_min,start_date,end_limit,note,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, student_id=excluded.student_id, student_name=excluded.student_name,
                      teacher=excluded.teacher, group_id=excluded.group_id, count=excluded.count,
                      days_json=excluded.days_json, start_time=excluded.start_time, duration_min=excluded.duration_min,
                      start_date=excluded.start_date, end_limit=excluded.end_limit, note=excluded.note,
                      created_at=excluded.created_at
                    """,
                    (
                        pid,
                        pos,
                        sid,
                        str(plan.get("studentName") or ""),
                        t_ref,
                        g_ref,
                        cnt,
                        json.dumps(days, ensure_ascii=False),
                        stime,
                        dur,
                        sdate,
                        str(plan.get("endLimit") or ""),
                        str(plan.get("note") or ""),
                        created_at,
                    ),
                )
                self.log(actor, "upsert_plan", f"{pid}:{sid}")
                self.db.execute("COMMIT")
                return {"ok": True, "id": pid, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_plan(
        self,
        plan_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM attendance_plans WHERE id=?", (plan_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_plan", plan_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_session(
        self,
        sess: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
        *,
        check_conflicts: bool = False,
        allow_conflicts: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(sess, dict):
            raise ValueError("invalid session payload")
        xid = str(sess.get("id") or f"sess_{secrets.token_hex(4)}").strip()
        sid = str(sess.get("studentId") or "").strip()
        sdate = str(sess.get("date") or "").strip()
        stime = normalize_time_str(sess.get("time"), "16:00")
        dur = max(1, min(600, int(float(_n if (_n := sess.get("dur")) is not None else 40))))
        st_val = str(sess.get("status") or "scheduled").strip()
        if not xid or not sid or not DATE_RE.match(sdate) or not TIME_RE.match(stime):
            raise ValueError("بيانات الحصة غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            if check_conflicts and not allow_conflicts:
                st = self.export_state()
                cand = dict(sess, id=xid, studentId=sid, date=sdate, time=stime, dur=dur, status=st_val)
                conflicts = detect_scheduling_conflicts(st.get("sessions") or [], st.get("students") or [], [cand])
                if conflicts:
                    raise SchedulingConflictError(conflicts)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                t_ref = self._ensure_teacher_in_tx(sess.get("teacher"))
                p_ref = self._plan_exists_in_tx(sess.get("planId"))
                g_ref = self._group_exists_in_tx(sess.get("groupId"))
                cur = self.db.execute("SELECT pos FROM sessions WHERE id=?", (xid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM sessions").fetchone()[0]
                now_iso = datetime.date.today().isoformat()
                self.db.execute(
                    """
                    INSERT INTO sessions(id,pos,plan_id,group_id,student_id,teacher,session_date,start_time,duration_min,status,compensated_for,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, plan_id=excluded.plan_id, group_id=excluded.group_id,
                      student_id=excluded.student_id, teacher=excluded.teacher, session_date=excluded.session_date,
                      start_time=excluded.start_time, duration_min=excluded.duration_min, status=excluded.status,
                      compensated_for=excluded.compensated_for, notes=excluded.notes,
                      created_at=excluded.created_at, updated_at=excluded.updated_at
                    """,
                    (
                        xid,
                        pos,
                        p_ref,
                        g_ref,
                        sid,
                        t_ref,
                        sdate,
                        stime,
                        dur,
                        st_val,
                        str(sess.get("compensatedFor") or ""),
                        str(sess.get("notes") or ""),
                        str(sess.get("createdAt") or now_iso),
                        now_iso,
                    ),
                )
                self.log(actor, "upsert_session", f"{xid}:{sid}:{sdate}:{st_val}")
                self.db.execute("COMMIT")
                return {"ok": True, "id": xid, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_sessions_batch(
        self,
        plan: dict[str, Any] | None,
        sessions_list: list[dict[str, Any]],
        month_update: dict[str, Any] | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
        *,
        check_conflicts: bool = False,
        allow_conflicts: bool = False,
    ) -> dict[str, Any]:
        """Creates a plan and its projected sessions atomically in one transaction."""
        with self.lock:
            self.check_rev(expected_rev)
            if check_conflicts and not allow_conflicts and sessions_list:
                st = self.export_state()
                conflicts = detect_scheduling_conflicts(st.get("sessions") or [], st.get("students") or [], list(sessions_list))
                if conflicts:
                    raise SchedulingConflictError(conflicts)
            self.db.execute("BEGIN")
            try:
                if plan and isinstance(plan, dict):
                    pid = str(plan.get("id") or f"pln_{secrets.token_hex(4)}").strip()
                    sid = str(plan.get("studentId") or "").strip()
                    sdate = str(plan.get("actualStartDate") or plan.get("startDate") or "").strip()
                    if pid and sid and DATE_RE.match(sdate) and self._student_exists_in_tx(sid):
                        t_ref = self._ensure_teacher_in_tx(plan.get("teacher"))
                        g_ref = self._group_exists_in_tx(plan.get("groupId"))
                        pos = self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM attendance_plans").fetchone()[0]
                        self.db.execute(
                            """
                            INSERT INTO attendance_plans(id,pos,student_id,student_name,teacher,group_id,count,days_json,start_time,duration_min,start_date,end_limit,note,created_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET
                              start_date=excluded.start_date, end_limit=excluded.end_limit, count=excluded.count
                            """,
                            (
                                pid,
                                pos,
                                sid,
                                str(plan.get("studentName") or ""),
                                t_ref,
                                g_ref,
                                max(1, int(plan.get("count") or 8)),
                                json.dumps(plan.get("days") or [], ensure_ascii=False),
                                normalize_time_str(plan.get("time"), "16:00"),
                                max(1, int(plan.get("dur") or 40)),
                                sdate,
                                str(plan.get("endLimit") or ""),
                                str(plan.get("note") or ""),
                                str(plan.get("createdAt") or datetime.date.today().isoformat()),
                            ),
                        )
                base_pos = self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM sessions").fetchone()[0]
                now_iso = datetime.date.today().isoformat()
                for idx, sess in enumerate(sessions_list or []):
                    if not isinstance(sess, dict):
                        continue
                    xid = str(sess.get("id") or f"sess_{secrets.token_hex(4)}").strip()
                    sid = str(sess.get("studentId") or "").strip()
                    sdate = str(sess.get("date") or "").strip()
                    stime = normalize_time_str(sess.get("time"), "16:00")
                    if not xid or not sid or not DATE_RE.match(sdate) or not self._student_exists_in_tx(sid):
                        continue
                    t_ref = self._ensure_teacher_in_tx(sess.get("teacher"))
                    p_ref = self._plan_exists_in_tx(sess.get("planId"))
                    g_ref = self._group_exists_in_tx(sess.get("groupId"))
                    self.db.execute(
                        """
                        INSERT INTO sessions(id,pos,plan_id,group_id,student_id,teacher,session_date,start_time,duration_min,status,compensated_for,notes,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          session_date=excluded.session_date, start_time=excluded.start_time, status=excluded.status
                        """,
                        (
                            xid,
                            base_pos + idx,
                            p_ref,
                            g_ref,
                            sid,
                            t_ref,
                            sdate,
                            stime,
                            max(1, int(sess.get("dur") or 40)),
                            str(sess.get("status") or "scheduled"),
                            str(sess.get("compensatedFor") or ""),
                            str(sess.get("notes") or ""),
                            str(sess.get("createdAt") or now_iso),
                            now_iso,
                        ),
                    )
                if month_update and isinstance(month_update, dict):
                    m_sid = str(month_update.get("studentId") or "").strip()
                    m_mk = str(month_update.get("month") or "").strip()
                    if m_sid and MONTH_RE.match(m_mk) and not self.is_month_closed(m_mk):
                        mrow = self.db.execute(
                            "SELECT * FROM months WHERE student_id=? AND month_key=?",
                            (m_sid, m_mk),
                        ).fetchone()
                        if mrow:
                            asd = str(month_update.get("actualStartDate") or mrow["actual_start_date"] or "")
                            ed = str(month_update.get("endDate") or mrow["end_date"] or "")
                            self.db.execute(
                                "UPDATE months SET actual_start_date=?, end_date=? WHERE student_id=? AND month_key=?",
                                (asd, ed, m_sid, m_mk),
                            )
                        else:
                            self._put_month_in_tx(
                                m_sid,
                                m_mk,
                                {
                                    "actualStartDate": str(month_update.get("actualStartDate") or ""),
                                    "endDate": str(month_update.get("endDate") or ""),
                                },
                            )
                self.log(actor, "upsert_sessions_batch", f"{len(sessions_list or [])} sessions")
                self.db.execute("COMMIT")
                return {"ok": True, "count": len(sessions_list or []), "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_session(
        self,
        session_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_session", session_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def patch_session(
        self,
        session_id: str,
        patch: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("invalid session patch payload")
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError(f"session not found: {session_id}")
            merged = {
                "id": row["id"],
                "planId": row["plan_id"] or "",
                "groupId": row["group_id"] or "",
                "studentId": row["student_id"],
                "teacher": row["teacher"] or "",
                "date": row["session_date"],
                "time": row["start_time"],
                "dur": row["duration_min"],
                "status": row["status"],
                "compensatedFor": row["compensated_for"] or "",
                "notes": row["notes"] or "",
                "createdAt": row["created_at"] or "",
            }
            merged.update(patch)
            merged["id"] = session_id
            return self.upsert_session(merged, actor=actor, expected_rev=expected_rev)

    def upsert_memorization(
        self,
        mem: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(mem, dict):
            raise ValueError("invalid memorization payload")
        mid = str(mem.get("id") or f"mem_{secrets.token_hex(4)}").strip()
        sid = str(mem.get("studentId") or "").strip()
        ldate = str(mem.get("date") or datetime.date.today().isoformat()).strip()
        track = str(mem.get("track") or "حفظ جديد").strip()
        if not mid or not sid or not DATE_RE.match(ldate) or not track:
            raise ValueError("بيانات سجل الحفظ غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                t_ref = self._ensure_teacher_in_tx(mem.get("teacher"))
                cur = self.db.execute("SELECT pos FROM memorization_logs WHERE id=?", (mid,)).fetchone()
                pos = cur["pos"] if cur else self.db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM memorization_logs").fetchone()[0]
                f_ayah = max(1, int(float(mem.get("fromAyah") or 1)))
                t_ayah = max(1, int(float(mem.get("toAyah") or 1)))
                juz = min(30, max(1, int(float(mem.get("juz") or 30))))
                pages = float(to_decimal(mem.get("pages") if mem.get("pages") is not None else 1))
                score = float(to_decimal(mem.get("score") if mem.get("score") is not None else 10))
                created_at = str(mem.get("createdAt") or datetime.date.today().isoformat())
                self.db.execute(
                    """
                    INSERT INTO memorization_logs(id,pos,student_id,teacher,log_date,track,from_surah,from_ayah,to_surah,to_ayah,juz,pages,grade,score,notes,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      pos=excluded.pos, student_id=excluded.student_id, teacher=excluded.teacher,
                      log_date=excluded.log_date, track=excluded.track, from_surah=excluded.from_surah,
                      from_ayah=excluded.from_ayah, to_surah=excluded.to_surah, to_ayah=excluded.to_ayah,
                      juz=excluded.juz, pages=excluded.pages, grade=excluded.grade, score=excluded.score,
                      notes=excluded.notes, created_at=excluded.created_at
                    """,
                    (
                        mid,
                        pos,
                        sid,
                        t_ref,
                        ldate,
                        track,
                        str(mem.get("fromSurah") or ""),
                        f_ayah,
                        str(mem.get("toSurah") or ""),
                        t_ayah,
                        juz,
                        pages,
                        str(mem.get("grade") or ""),
                        score,
                        str(mem.get("notes") or ""),
                        created_at,
                    ),
                )
                self.log(actor, "upsert_memorization", f"{mid}:{sid}:{ldate}")
                self.db.execute("COMMIT")
                return {"ok": True, "id": mid, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_memorization(
        self,
        mem_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM memorization_logs WHERE id=?", (mem_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_memorization", mem_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def upsert_madrasa_enrollment(
        self,
        enr: dict[str, Any],
        batch_name: str = "",
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(enr, dict):
            raise ValueError("invalid madrasa enrollment payload")
        eid = str(enr.get("id") or f"enr_{secrets.token_hex(4)}").strip()
        sid = str(enr.get("studentId") or "").strip()
        if not eid or not sid:
            raise ValueError("بيانات التسجيل في المدرسة الإيمانية غير صالحة")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                bid = str(enr.get("batchId") or "batch_default").strip() or "batch_default"
                if batch_name:
                    self.db.execute(
                        """
                        INSERT INTO madrasa_batches(id,name,slogan,fee,start_date,supervisor,active,meta)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET name=excluded.name
                        """,
                        (bid, batch_name.strip(), "يوم لا يخلو من القرآن", 60, datetime.date.today().isoformat(), "", 1, "{}"),
                    )
                else:
                    bid = self._ensure_batch_in_tx(bid)
                fee_paid_int = _int_money(enr.get("feePaid") or 0, allow_none=False, field_name="feePaid")
                streak = max(0, int(enr.get("streak") or 0))
                best_streak = max(streak, int(enr.get("bestStreak") or 0))
                self.db.execute(
                    """
                    INSERT INTO madrasa_enrollments(id,batch_id,student_id,enrolled_at,fee_paid,tx_id,streak,best_streak,badges,tracks_progress)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      batch_id=excluded.batch_id, student_id=excluded.student_id,
                      enrolled_at=excluded.enrolled_at, fee_paid=excluded.fee_paid, tx_id=excluded.tx_id,
                      streak=excluded.streak, best_streak=excluded.best_streak, badges=excluded.badges,
                      tracks_progress=excluded.tracks_progress
                    """,
                    (
                        eid,
                        bid,
                        sid,
                        str(enr.get("enrolledAt") or datetime.date.today().isoformat()),
                        fee_paid_int,
                        str(enr.get("txId") or ""),
                        streak,
                        best_streak,
                        json.dumps(enr.get("badges") or [], ensure_ascii=False),
                        json.dumps(enr.get("tracksProgress") or {}, ensure_ascii=False),
                    ),
                )
                self.log(actor, "upsert_madrasa_enrollment", f"{eid}:{sid}")
                self.db.execute("COMMIT")
                return {"ok": True, "id": eid, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_madrasa_enrollment(
        self,
        enr_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM madrasa_enrollments WHERE id=?", (enr_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_madrasa_enrollment", enr_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def patch_madrasa_enrollment(
        self,
        enr_id: str,
        patch: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ValueError("invalid madrasa enrollment patch payload")
        with self.lock:
            row = self.db.execute("SELECT * FROM madrasa_enrollments WHERE id=?", (enr_id,)).fetchone()
            if not row:
                raise KeyError(f"madrasa enrollment not found: {enr_id}")
            try:
                badges = json.loads(row["badges"] or "[]")
            except Exception:
                badges = []
            try:
                tp = json.loads(row["tracks_progress"] or "{}")
            except Exception:
                tp = {}
            merged = {
                "id": row["id"],
                "batchId": row["batch_id"],
                "studentId": row["student_id"],
                "enrolledAt": row["enrolled_at"],
                "feePaid": row["fee_paid"],
                "txId": row["tx_id"] or "",
                "streak": row["streak"],
                "bestStreak": row["best_streak"],
                "badges": badges,
                "tracksProgress": tp,
            }
            merged.update(patch)
            merged["id"] = enr_id
            return self.upsert_madrasa_enrollment(
                merged,
                batch_name=str(patch.get("batchName") or "").strip(),
                actor=actor,
                expected_rev=expected_rev,
            )

    def upsert_certificate(
        self,
        cert: dict[str, Any],
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(cert, dict):
            raise ValueError("invalid certificate payload")
        cid = str(cert.get("id") or f"crt_{secrets.token_hex(4)}").strip()
        sid = str(cert.get("studentId") or "").strip()
        title = str(cert.get("courseTitle") or "").strip()
        if not cid or not sid or not title:
            raise ValueError("بيانات الشهادة غير مكتملة")
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                if not self._student_exists_in_tx(sid):
                    raise KeyError(f"student not found: {sid}")
                srow = self.db.execute("SELECT name, teacher FROM students WHERE id=?", (sid,)).fetchone()
                sname = str(cert.get("studentName") or (srow["name"] if srow else "")).strip()
                tname = str(cert.get("teacherName") or (srow["teacher"] if srow else "")).strip()
                cno = str(cert.get("certNo") or f"CERT-{int(time.time()*1000)%100000:05d}").strip()
                idate = str(cert.get("issueDate") or datetime.date.today().isoformat()).strip()
                self.db.execute(
                    """
                    INSERT INTO certificates(id,cert_no,student_id,student_name,course_title,grade,issue_date,teacher_name,supervisor_name,coords_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                      cert_no=excluded.cert_no, student_id=excluded.student_id,
                      student_name=excluded.student_name, course_title=excluded.course_title,
                      grade=excluded.grade, issue_date=excluded.issue_date,
                      teacher_name=excluded.teacher_name, supervisor_name=excluded.supervisor_name,
                      coords_json=excluded.coords_json
                    """,
                    (
                        cid,
                        cno,
                        sid,
                        sname,
                        title,
                        str(cert.get("grade") or "ممتاز متقن"),
                        idate,
                        tname,
                        str(cert.get("supervisorName") or ""),
                        json.dumps(cert.get("coords") or {}, ensure_ascii=False),
                    ),
                )
                self.log(actor, "upsert_certificate", f"{cid}:{cno}:{sid}")
                self.db.execute("COMMIT")
                return {"ok": True, "id": cid, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def delete_certificate(
        self,
        cert_id: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            self.db.execute("BEGIN")
            try:
                deleted = self.db.execute("DELETE FROM certificates WHERE id=?", (cert_id,)).rowcount > 0
                if deleted:
                    self.log(actor, "delete_certificate", cert_id)
                self.db.execute("COMMIT")
                return {"ok": True, "deleted": deleted, "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    # ------------------------------------------------------------------
    # Alerts Dismiss / Restore (Phase 5)
    # ------------------------------------------------------------------
    def dismiss_alert(
        self,
        alert_key: str,
        reason: str = "",
        expires_at: str = "",
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        akey = str(alert_key or "").strip()
        if not akey:
            raise ValueError("alert key is required")
        with self.lock:
            self.check_rev(expected_rev)
            alerts_state = self.get_kv("alertsState", {})
            if not isinstance(alerts_state, dict):
                alerts_state = {}
            dismissed = alerts_state.get("dismissed")
            if not isinstance(dismissed, dict):
                dismissed = {}
            dismissed[akey] = {
                "reason": str(reason or "تم الاطلاع").strip()[:300],
                "dismissedAt": datetime.date.today().isoformat(),
                "expiresAt": str(expires_at or "").strip(),
            }
            alerts_state["dismissed"] = dismissed
            return self.put_kv("alertsState", alerts_state, actor=actor, expected_rev=None)

    def restore_alert(
        self,
        alert_key: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        akey = str(alert_key or "").strip()
        with self.lock:
            self.check_rev(expected_rev)
            alerts_state = self.get_kv("alertsState", {})
            if isinstance(alerts_state, dict) and isinstance(alerts_state.get("dismissed"), dict):
                if akey == "__all__":
                    alerts_state["dismissed"] = {}
                else:
                    alerts_state["dismissed"].pop(akey, None)
            return self.put_kv("alertsState", alerts_state, actor=actor, expected_rev=None)

    # ------------------------------------------------------------------
    # Demo Data & Reset Application (Phase 1)
    # ------------------------------------------------------------------
    def load_demo_data(
        self,
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            if backup_folder:
                self.snapshot(backup_folder, "before_load_demo", keep=20)
            demo_state = build_demo_dataset()
            self.replace_all(demo_state, actor=actor, allow_empty=True)
            return {"ok": True, "studentsCount": self.count_students(), "rev": self.rev}

    def clear_demo_data(
        self,
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Removes demo records (IDs starting with `demo_` or synthetic demo teachers) while preserving user settings."""
        with self.lock:
            self.check_rev(expected_rev)
            if backup_folder:
                self.snapshot(backup_folder, "before_clear_demo", keep=20)
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM students WHERE id LIKE 'demo_%' OR id IN ('stu0001', 'stu0002', 'stu0003', 'stu0004')")
                self.db.execute("DELETE FROM installments WHERE id LIKE 'demo_%' OR student_id IN ('stu0001', 'stu0002', 'stu0003', 'stu0004')")
                self.db.execute("DELETE FROM salaries WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM transactions WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM study_groups WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM attendance_plans WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM sessions WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM memorization_logs WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM madrasa_batches WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM madrasa_enrollments WHERE id LIKE 'demo_%'")
                self.db.execute("DELETE FROM certificates WHERE id LIKE 'demo_%'")
                self.db.execute(
                    "DELETE FROM teachers WHERE name LIKE 'معلمة تجريبية%' AND name NOT IN (SELECT DISTINCT teacher FROM students WHERE teacher IS NOT NULL)"
                )
                s = self.get_settings()
                s["isDemoLoaded"] = False
                self.db.execute(
                    "INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)",
                    (json.dumps(s, ensure_ascii=False),),
                )
                self.log(actor, "clear_demo_data", "cleared demo records")
                self.db.execute("COMMIT")
                return {"ok": True, "studentsCount": self.count_students(), "rev": self.rev}
            except Exception:
                self._rollback()
                raise

    def reset_application(
        self,
        backup_folder: Path | None = None,
        actor: str = "admin",
        preserve_settings: bool = False,
    ) -> dict[str, Any]:
        """Takes a safety snapshot, wipes all operational data (and optionally configuration), and returns status."""
        with self.lock:
            snap_name = ""
            if backup_folder:
                snap_path = self.snapshot(backup_folder, "before_reset_app", keep=30)
                snap_name = snap_path.name
            self.db.execute("BEGIN")
            try:
                tables = [
                    "certificates",
                    "madrasa_logs",
                    "madrasa_enrollments",
                    "madrasa_batches",
                    "memorization_logs",
                    "sessions",
                    "attendance_plans",
                    "study_groups",
                    "salaries",
                    "installments",
                    "transactions",
                    "months",
                    "students",
                    "teachers",
                    "closed_months",
                    "import_batches",
                ]
                if not preserve_settings:
                    tables.append("kv")
                for tbl in tables:
                    self.db.execute(f"DELETE FROM {tbl}")
                self.log(actor, "reset_application", f"snapshot={snap_name};preserve_settings={preserve_settings}")
                self.db.execute("COMMIT")
                return {"ok": True, "snapshot": snap_name, "rev": self.rev, "studentsCount": 0}
            except Exception:
                self._rollback()
                raise

    # ------------------------------------------------------------------
    # Import Batches Tracking (Phase 6)
    # ------------------------------------------------------------------
    def record_import_batch(self, batch: dict[str, Any], actor: str = "admin") -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    """
                    INSERT OR REPLACE INTO import_batches(
                      id, created_at, dataset, filename, mode, snapshot_name, rev_after, summary_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(batch["id"]),
                        str(batch.get("createdAt") or datetime.datetime.now().isoformat(timespec="seconds")),
                        str(batch.get("dataset") or ""),
                        str(batch.get("filename") or ""),
                        str(batch.get("mode") or ""),
                        str(batch.get("snapshotName") or ""),
                        int(self.rev + 1),
                        json.dumps(batch.get("summary") or {}, ensure_ascii=False),
                    ),
                )
                self.log(actor, "excel_import", f"{batch.get('id')}:{batch.get('dataset')}:{batch.get('filename')}")
                self.db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

    def list_import_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {
                    "id": r["id"],
                    "createdAt": r["created_at"],
                    "dataset": r["dataset"],
                    "filename": r["filename"],
                    "mode": r["mode"],
                    "snapshotName": r["snapshot_name"],
                    "revAfter": r["rev_after"],
                    "summary": json.loads(r["summary_json"] or "{}"),
                }
                for r in self.db.execute(
                    "SELECT * FROM import_batches ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]

    def get_last_import_batch(self) -> dict[str, Any] | None:
        batches = self.list_import_batches(limit=1)
        return batches[0] if batches else None

    def compensate_session_transactional(
        self,
        session_id: str,
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            self.check_rev(expected_rev)
            state = self.export_state()
            sessions = state.get("sessions") or []
            plans = state.get("plans") or []
            students = state.get("students") or []
            res = auto_compensate_cancelled_session(session_id, sessions, plans, students)
            if not res.get("compensated"):
                return res

            if backup_folder:
                self.snapshot(backup_folder, "before_compensate", keep=20)

            new_sess = res["newSession"]
            new_end = res.get("newEndDate") or ""
            mk = res.get("monthKey") or str(new_sess.get("date") or "")[:7]
            sid = new_sess["studentId"]

            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    "UPDATE sessions SET status='cancel_teacher', updated_at=? WHERE id=?",
                    (datetime.date.today().isoformat(), session_id),
                )
                max_pos = self.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM sessions").fetchone()[0]
                t_ref = self._ensure_teacher_in_tx(new_sess.get("teacher"))
                p_ref = self._plan_exists_in_tx(new_sess.get("planId"))
                g_ref = self._group_exists_in_tx(new_sess.get("groupId"))
                self.db.execute(
                    """
                    INSERT INTO sessions(id,pos,plan_id,group_id,student_id,teacher,session_date,start_time,duration_min,status,compensated_for,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_sess["id"],
                        max_pos,
                        p_ref,
                        g_ref,
                        sid,
                        t_ref,
                        new_sess["date"],
                        new_sess["time"],
                        int(new_sess.get("dur") or 40),
                        new_sess["status"],
                        new_sess["compensatedFor"],
                        new_sess["notes"],
                        new_sess["createdAt"],
                        new_sess["updatedAt"],
                    ),
                )
                if new_end and MONTH_RE.match(mk) and not self.is_month_closed(mk):
                    mrow = self.db.execute(
                        "SELECT end_date FROM months WHERE student_id=? AND month_key=?",
                        (sid, mk),
                    ).fetchone()
                    if mrow and (not mrow["end_date"] or new_end > mrow["end_date"]):
                        self.db.execute(
                            "UPDATE months SET end_date=? WHERE student_id=? AND month_key=?",
                            (new_end, sid, mk),
                        )
                self.log(actor, "auto_compensate", f"{session_id} -> {new_sess['id']} ({new_sess['date']})")
                self.db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise
            res["makeupSession"] = new_sess
            res["rev"] = self.rev
            return res

    def has_data(self) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM students LIMIT 1").fetchone() is not None

    def count_students(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM students").fetchone()[0]

    def export_state(self, teacher: str | None = None, finance_only: bool = False) -> dict[str, Any]:
        """Serializes the full application state.

        `finance_only=True` returns just what the money engines need (students + months, teachers,
        settings, receipts, payouts, transactions) and skips attendance / memorization / madrasa /
        certificates, which dominate the cost on large academies.
        """
        with self.lock:
            args = (teacher,) if teacher else ()
            srows = self.db.execute(
                "SELECT * FROM students" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos",
                args,
            ).fetchall()
            by_student: dict[str, dict[str, Any]] = {}
            mq = "SELECT m.* FROM months m JOIN students s ON s.id=m.student_id" + (
                " WHERE s.teacher=?" if teacher else ""
            )
            for m in self.db.execute(mq, args):
                pay_dt = m["payment_date"] if m["payment_date"] is not None and m["payment_date"] != "" else (m["pay"] or "")
                actual_start_dt = m["actual_start_date"] or ""
                end_dt = m["end_date"] or ""
                m_entry: dict[str, Any] = {
                    "value": m["value"],
                    "paymentDate": pay_dt,
                    "actualStartDate": actual_start_dt,
                    "pay": pay_dt,
                    "startDate": actual_start_dt,
                    "paidStatus": m["paid_status"] or "",
                    "partialPaid": m["partial_paid"],
                    "customWage": m["custom_wage"],
                    "end": end_dt,
                    "endDate": end_dt,
                    "att": json.loads(m["att"] or "{}"),
                }
                if m["discount"] is not None:
                    m_entry["discount"] = m["discount"]
                if m["invoice_note"]:
                    m_entry["invoiceNote"] = m["invoice_note"]
                by_student.setdefault(m["student_id"], {})[m["month_key"]] = m_entry

            studs = [
                {
                    "id": r["id"],
                    "code": r["code"] or "",
                    "name": r["name"],
                    "phone": r["phone"] or "",
                    "teacher": r["teacher"] or "",
                    "type": r["type"] or "",
                    "pct": parse_pct_int(r["pct"]),
                    "schedule": r["schedule"] or "",
                    "adminStatus": r["admin_status"] or "",
                    "paymentDate": r["payment_date"] or "",
                    "actualStartDate": r["actual_start_date"] or "",
                    "transferHistory": json.loads(r["transfers"] or "[]"),
                    "months": by_student.get(r["id"], {}),
                }
                for r in srows
            ]
            trows = self.db.execute(
                "SELECT name, meta FROM teachers" + (" WHERE name=?" if teacher else "") + " ORDER BY pos",
                args,
            ).fetchall()
            out: dict[str, Any] = {
                "teachers": [t["name"] for t in trows],
                "teacherMeta": {t["name"]: json.loads(t["meta"] or "{}") for t in trows},
                "students": studs,
                "rev": self.rev,
                "onboarded": self.is_onboarded(),
                "pinRequired": self.has_pin(),
            }

            for k, v in self.db.execute("SELECT k, v FROM kv"):
                if k in CONFIG_KV_KEYS:
                    out[k] = json.loads(v)

            if "settings" not in out or not isinstance(out["settings"], dict):
                out["settings"] = self.get_settings()
            else:
                merged_s = dict(DEFAULT_SETTINGS)
                merged_s.update(out["settings"])
                out["settings"] = merged_s

            out["closedMonths"] = self.list_closed_months()
            out["importBatches"] = self.list_import_batches(limit=15)

            out["payouts"] = [
                {
                    "id": r["id"],
                    "teacher": r["teacher"] or "",
                    "month": r["month_key"],
                    "amount": r["amount"],
                    "date": r["pay_date"] or "",
                    "note": r["note"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM salaries" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos, id",
                    args,
                ).fetchall()
            ]
            out["transactions"] = [
                {
                    "id": r["id"],
                    "type": r["type"],
                    "category": r["category"],
                    "amount": r["amount"],
                    "date": r["tx_date"],
                    "month": r["month_key"],
                    "method": r["method"] or "",
                    "note": r["note"] or "",
                    "createdAt": r["created_at"] or "",
                }
                for r in self.db.execute("SELECT * FROM transactions ORDER BY pos, tx_date DESC, id").fetchall()
            ]
            out["installments"] = [
                {
                    "id": r["id"],
                    "receiptNo": r["receipt_no"],
                    "studentId": r["student_id"],
                    "month": r["month_key"],
                    "amount": r["amount"],
                    "date": r["pay_date"],
                    "paymentDate": r["pay_date"],
                    "method": r["method"] or "",
                    "note": r["note"] or "",
                    "createdAt": r["created_at"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM installments"
                    + (" WHERE student_id IN (SELECT id FROM students WHERE teacher=?)" if teacher else "")
                    + " ORDER BY pos, pay_date, id",
                    args,
                ).fetchall()
            ]
            if finance_only:
                return out
            out["groups"] = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "teacher": r["teacher"] or "",
                    "days": json.loads(r["days_json"] or "[]"),
                    "time": r["start_time"] or "16:00",
                    "dur": r["duration_min"],
                    "capacity": r["capacity"],
                    "notes": r["notes"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM study_groups" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos, id",
                    args,
                ).fetchall()
            ]
            out["plans"] = [
                {
                    "id": r["id"],
                    "studentId": r["student_id"],
                    "studentName": r["student_name"] or "",
                    "teacher": r["teacher"] or "",
                    "groupId": r["group_id"] or "",
                    "count": r["count"],
                    "days": json.loads(r["days_json"] or "[]"),
                    "time": r["start_time"] or "16:00",
                    "dur": r["duration_min"],
                    "startDate": r["start_date"],
                    "actualStartDate": r["start_date"],
                    "endLimit": r["end_limit"] or "",
                    "note": r["note"] or "",
                    "createdAt": r["created_at"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM attendance_plans" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos, id",
                    args,
                ).fetchall()
            ]
            out["sessions"] = [
                {
                    "id": r["id"],
                    "planId": r["plan_id"] or "",
                    "groupId": r["group_id"] or "",
                    "studentId": r["student_id"],
                    "teacher": r["teacher"] or "",
                    "date": r["session_date"],
                    "time": r["start_time"],
                    "dur": r["duration_min"],
                    "status": r["status"],
                    "compensatedFor": r["compensated_for"] or "",
                    "notes": r["notes"] or "",
                    "createdAt": r["created_at"] or "",
                    "updatedAt": r["updated_at"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM sessions" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos, session_date, start_time, id",
                    args,
                ).fetchall()
            ]
            out["memorization"] = [
                {
                    "id": r["id"],
                    "studentId": r["student_id"],
                    "teacher": r["teacher"] or "",
                    "date": r["log_date"],
                    "track": r["track"],
                    "fromSurah": r["from_surah"] or "",
                    "fromAyah": r["from_ayah"],
                    "toSurah": r["to_surah"] or "",
                    "toAyah": r["to_ayah"],
                    "juz": r["juz"],
                    "pages": r["pages"],
                    "grade": r["grade"] or "",
                    "score": r["score"],
                    "notes": r["notes"] or "",
                    "createdAt": r["created_at"] or "",
                }
                for r in self.db.execute(
                    "SELECT * FROM memorization_logs" + (" WHERE teacher=?" if teacher else "") + " ORDER BY pos, log_date DESC, id",
                    args,
                ).fetchall()
            ]
            out["madrasaBatches"] = [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "slogan": r["slogan"] or "يوم لا يخلو من القرآن",
                    "fee": r["fee"],
                    "startDate": r["start_date"] or "",
                    "supervisor": r["supervisor"] or "",
                    "active": bool(r["active"]),
                }
                for r in self.db.execute("SELECT * FROM madrasa_batches ORDER BY rowid").fetchall()
            ]
            out["madrasaEnrollments"] = [
                {
                    "id": r["id"],
                    "batchId": r["batch_id"],
                    "studentId": r["student_id"],
                    "enrolledAt": r["enrolled_at"] or "",
                    "feePaid": r["fee_paid"],
                    "txId": r["tx_id"] or "",
                    "streak": r["streak"],
                    "bestStreak": r["best_streak"],
                    "badges": json.loads(r["badges"] or "[]"),
                    "tracksProgress": json.loads(r["tracks_progress"] or "{}"),
                }
                for r in self.db.execute("SELECT * FROM madrasa_enrollments ORDER BY rowid").fetchall()
            ]
            out["madrasaLogs"] = [
                {
                    "id": r["id"],
                    "batchId": r["batch_id"],
                    "studentId": r["student_id"],
                    "date": r["log_date"],
                    "prayers": json.loads(r["prayers"] or "{}"),
                    "fajrAwake": bool(r["fajr_awake"]),
                    "wirdDone": bool(r["wird_done"]),
                    "adhkarMorning": bool(r["adhkar_morning"]),
                    "adhkarEvening": bool(r["adhkar_evening"]),
                    "ilmLesson": r["ilm_lesson"] or "",
                    "tarbawiChallenge": bool(r["tarbawi_challenge"]),
                    "points": r["points"],
                    "compensatoryGoals": json.loads(r["compensatory_goals"] or "[]"),
                    "supervisorNote": r["supervisor_note"] or "",
                }
                for r in self.db.execute("SELECT * FROM madrasa_logs ORDER BY log_date DESC, rowid DESC").fetchall()
            ]
            out["certificates"] = [
                {
                    "id": r["id"],
                    "certNo": r["cert_no"],
                    "studentId": r["student_id"],
                    "studentName": r["student_name"],
                    "courseTitle": r["course_title"],
                    "grade": r["grade"] or "ممتاز متقن",
                    "issueDate": r["issue_date"],
                    "teacherName": r["teacher_name"] or "",
                    "supervisorName": r["supervisor_name"] or "",
                    "coords": json.loads(r["coords_json"] or "{}"),
                }
                for r in self.db.execute("SELECT * FROM certificates ORDER BY issue_date DESC, rowid DESC").fetchall()
            ]
            out["dynamicMonths"] = generate_dynamic_months(out)  # type: ignore[arg-type]  # LATENT-1: passes the state as reference_date (see docs/ISSUES.md)
        return out

    @staticmethod
    def _ref_date(reference_date: str | None) -> str:
        """Reports default to *today*: receipts dated in the future are not collected yet."""
        return str(reference_date or "").strip() or datetime.date.today().isoformat()

    def stats(self, month: str, reference_date: str | None = None) -> dict[str, Any]:
        state = self.export_state(finance_only=True)
        settings = state.get("settings") or {}
        return compute_month_stats(
            state.get("students") or [],
            month,
            installments=state.get("installments") or [],
            teacher_meta=state.get("teacherMeta") or {},
            wage_basis=str(settings.get("wageBasis") or "net"),
            wage_accrual_mode=str(settings.get("wageAccrualMode") or "accrued"),
            reference_date=self._ref_date(reference_date),
        )

    def collection_report(
        self,
        month: str | None = None,
        start_payment_date: str | None = None,
        end_payment_date: str | None = None,
        reference_date: str | None = None,
    ) -> dict[str, Any]:
        state = self.export_state(finance_only=True)
        return compute_collection_report(
            state.get("students") or [],
            month_key=month,
            installments=state.get("installments") or [],
            start_payment_date=start_payment_date,
            end_payment_date=end_payment_date,
            reference_date=self._ref_date(reference_date),
        )

    def accrual_report(self, month: str, reference_date: str | None = None) -> dict[str, Any]:
        state = self.export_state(finance_only=True)
        settings = state.get("settings") or {}
        return compute_accrual_month_report(
            state.get("students") or [],
            month_key=month,
            installments=state.get("installments") or [],
            teacher_meta=state.get("teacherMeta") or {},
            wage_basis=str(settings.get("wageBasis") or "net"),
            wage_accrual_mode=str(settings.get("wageAccrualMode") or "accrued"),
            reference_date=self._ref_date(reference_date),
        )

    def study_periods_report(self, month: str, reference_date: str | None = None) -> dict[str, Any]:
        """Generates a study duration & period report strictly anchored to `actualStartDate`."""
        state = self.export_state()
        settings = state.get("settings") or {}
        dur_days = int(settings.get("defaultDurationDays") or 30)
        exp_soon = int(settings.get("expiringSoonDays") or 5)
        records = []
        for stu in state.get("students") or []:
            m = (stu.get("months") or {}).get(month)
            if not isinstance(m, dict):
                continue
            eval_info = evaluate_student_month_dates(
                m,
                reference_date=reference_date,
                duration_days=dur_days,
                expiring_soon_days=exp_soon,
            )
            if not eval_info["actualStartDate"] and not eval_info["endDate"] and not eval_info["paymentDate"]:
                continue
            records.append({
                "studentId": stu.get("id", ""),
                "studentName": stu.get("name", ""),
                "teacher": stu.get("teacher", ""),
                "subscriptionType": stu.get("type", ""),
                "month": month,
                "paymentDate": eval_info["paymentDate"],
                "actualStartDate": eval_info["actualStartDate"],
                "endDate": eval_info["endDate"],
                "paidInAdvance": eval_info["paidInAdvance"],
                "paidAfterStart": eval_info["paidAfterStart"],
                "studyPeriod": eval_info["studyPeriod"],
            })
        return {"month": month, "count": len(records), "records": records}

    def accounting_summary(self, month: str | None = None, reference_date: str | None = None) -> dict[str, Any]:
        state = self.export_state(finance_only=True)
        settings = state.get("settings") or {}
        return compute_accounting_summary(
            state.get("students") or [],
            payouts=state.get("payouts") or [],
            transactions=state.get("transactions") or [],
            installments=state.get("installments") or [],
            month_key=month or None,
            teacher_meta=state.get("teacherMeta") or {},
            wage_basis=str(settings.get("wageBasis") or "net"),
            wage_accrual_mode=str(settings.get("wageAccrualMode") or "accrued"),
            reference_date=reference_date,
        )

    # ------------------------------------------------------------------
    # Backups with Integrity Verification & Extra Backup Folder (Phase 2)
    # ------------------------------------------------------------------
    def backup_to(self, dest: Path, mirror_to_extra: bool = True) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _sweep_orphan_part_companions(dest.parent)
        tmp = dest.with_name(dest.name + ".part")
        try:
            with self.lock:
                out = sqlite3.connect(tmp)
                try:
                    self.db.backup(out)
                finally:
                    out.close()
            if not verify_sqlite_backup_file(tmp):
                raise ValueError("backup integrity check failed")
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            _sweep_orphan_part_companions(dest.parent)
            raise

        if mirror_to_extra:
            settings = self.get_settings()
            extra_folder = str(settings.get("extraBackupFolder") or "").strip()
            if extra_folder:
                try:
                    extra_dir = Path(extra_folder)
                    extra_dir.mkdir(parents=True, exist_ok=True)
                    extra_dest = extra_dir / dest.name
                    shutil.copy2(dest, extra_dest)
                    # Rotate per kind (auto / daily / manual / safety): a flat "keep 30" let the
                    # 48 auto-backups a day push every daily backup out of the mirror folder.
                    kind = dest.name.split("_")[1] if dest.name.count("_") >= 1 else "other"
                    keep_by_kind = {"auto": 48, "daily": 60, "manual": 30, "safety": 30}
                    rotate(extra_dir, f"alahd_{kind}_*.sqlite3", keep_by_kind.get(kind, 30))
                except Exception as exc:
                    _LOG.warning("Failed to mirror backup %s to extraBackupFolder (%s): %s", dest.name, extra_folder, exc)

    def snapshot(self, folder: Path, tag: str, keep: int = 20) -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"alahd_safety_{tag}_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time()*1000)%1000:03d}.sqlite3"
        self.backup_to(f, mirror_to_extra=False)
        rotate(folder, "alahd_safety_*.sqlite3", keep)
        return f

    def _revert_to_snapshot(self, snap: Path) -> None:
        """Puts the live database back exactly as captured in `snap` (used when a restore fails midway)."""
        with self.lock:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.execute("PRAGMA foreign_keys=OFF")
            with _no_new_wal_companions(snap):
                src = sqlite3.connect(_ro_uri(snap), uri=True)
                try:
                    src.backup(self.db)
                finally:
                    src.close()
            self.db.execute("PRAGMA foreign_keys=ON")
            self._resync_rev()

    def restore_from(self, src_path: Path, expected_rev: int | None = None) -> int:
        with _no_new_wal_companions(src_path):          # DEFECT-B1: do not litter the backups folder with -wal/-shm
            return self._restore_from_impl(src_path, expected_rev)

    def _restore_from_impl(self, src_path: Path, expected_rev: int | None = None) -> int:
        with self.lock:
            self.check_rev(expected_rev)
        if not verify_sqlite_backup_file(Path(src_path)):
            raise ValueError("not a valid academy backup (integrity check failed)")
        src = sqlite3.connect(_ro_uri(src_path), uri=True)
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"students", "months", "teachers"} <= tables:
                raise ValueError("not a valid academy backup")
            with self.lock:
                prev_rev = int(self.rev)
                snap = self.snapshot(self.path.parent / "backups", "pre_restore", keep=20)
                self.db.execute("PRAGMA foreign_keys=OFF")
                try:
                    src.backup(self.db)
                    self._run_versioned_migrations()
                    self.db.executescript(SCHEMA)
                    # Post-restore verification: a backup that passed the pre-checks but is incompatible with the
                    # current schema must never be left half-applied (Phase 1, finding F-01).
                    chk = self.db.execute("PRAGMA integrity_check").fetchone()
                    if not chk or chk[0] != "ok":
                        raise ValueError("integrity check failed after restore")
                    self.db.execute("SELECT COUNT(*) FROM students").fetchone()
                    self.db.execute("SELECT COUNT(*) FROM teachers").fetchone()
                    self._backfill_teacher_pct()
                    fk_errs = self.db.execute("PRAGMA foreign_key_check").fetchall()
                    if fk_errs:
                        raise ValueError("foreign key integrity check failed after restore")
                    self.db.execute("PRAGMA foreign_keys=ON")
                    self.db.execute("PRAGMA synchronous=FULL")
                    # The restored audit table may be older than the live one: force a strictly larger
                    # revision so stale browser windows can never match the post-restore state.
                    restored_max = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()[0]
                    new_id = max(prev_rev, int(restored_max)) + 1
                    self.db.execute(
                        "INSERT INTO audit(id,ts,actor,action,detail) VALUES(?,?,?,?,?)",
                        (new_id, time.time(), "admin", "restore_from", Path(src_path).name),
                    )
                    self.rev = new_id
                    return self.count_students()
                except Exception:
                    _LOG.exception("restore failed; rolling back to pre-restore snapshot")
                    self._revert_to_snapshot(snap)
                    raise ValueError(
                        "تعذّرت استعادة النسخة الاحتياطية (غير متوافقة أو تالفة)؛ أُعيدت البيانات الحالية كما كانت"
                    ) from None
                finally:
                    try:
                        self.db.execute("PRAGMA foreign_keys=ON")
                        self.db.execute("PRAGMA synchronous=FULL")
                    except Exception:
                        pass
        finally:
            src.close()

    def list_backup_files(self, folder: Path) -> list[dict[str, Any]]:
        folder = Path(folder)
        if not folder.exists():
            return []
        out = []
        for pattern in ("alahd_*.sqlite3", "academy_*.sqlite"):
            for f in folder.glob(pattern):
                st = f.stat()
                kind = f.name.split("_")[1] if f.name.count("_") >= 1 else "other"
                out.append({"name": f.name, "kind": kind, "size": st.st_size, "mtime": st.st_mtime})
        return sorted(out, key=lambda d: d["mtime"], reverse=True)

    def get_backup_health(self, folder: Path) -> dict[str, Any]:
        """Evaluates backup freshness and extra backup folder reachability."""
        settings = self.get_settings()
        warn_days = int(settings.get("backupWarnDays") or 3)
        extra_folder = str(settings.get("extraBackupFolder") or "").strip()
        files = self.list_backup_files(folder)
        last_mtime = files[0]["mtime"] if files else None
        age_days: float | None = None
        if last_mtime is not None:
            age_days = round(max(0.0, (time.time() - float(last_mtime)) / 86400.0), 2)

        extra_reachable = True
        extra_error = ""
        if extra_folder:
            try:
                ep = Path(extra_folder)
                ep.mkdir(parents=True, exist_ok=True)
                probe = ep / ".alahd_backup_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
            except Exception as exc:
                extra_reachable = False
                extra_error = str(exc)

        backup_too_old = bool(age_days is not None and age_days > warn_days)
        missing_expected = bool(len(files) == 0 and self.count_students() > 0)
        return {
            "count": len(files),
            "lastBackupTime": last_mtime,
            "lastBackupAgeDays": age_days,
            "daysSinceLastBackup": age_days,
            "ageDays": age_days,
            "warnDays": warn_days,
            "backupTooOld": backup_too_old,
            "staleWarning": backup_too_old or missing_expected,
            "missingExpectedBackup": missing_expected,
            "extraBackupFolder": extra_folder,
            "extraFolder": extra_folder,
            "extraFolderConfigured": bool(extra_folder),
            "extraFolderReachable": extra_reachable,
            "extraFolderUnreachable": bool(extra_folder) and not extra_reachable,
            "extraFolderError": extra_error,
        }

    # ------------------------------------------------------------------
    # Safe Preview-First JSON Full-State Restore (Task 10)
    # ------------------------------------------------------------------
    _RECOGNIZED_JSON_STATE_KEYS: frozenset[str] = frozenset({
        "students", "teachers", "teacherMeta", "installments", "payouts",
        "transactions", "groups", "plans", "sessions", "memorization",
        "madrasaBatches", "madrasaEnrollments", "madrasaLogs",
        "certificates", "settings", "subTypes", "closedMonths",
    })

    def preview_json_restore(
        self,
        raw_payload: Any,
        filename: str = "backup.json",
        allow_legacy_dates: bool = True,
    ) -> dict[str, Any]:
        """Validates a full-state JSON backup payload and computes a complete dataset change summary without modifying DB."""
        fname = str(filename or "backup.json").strip()
        if fname and not fname.lower().endswith(".json"):
            raise DomainValidationError("صيغة الملف غير مدعومة؛ يجب اختيار ملف نسخ احتياطي بصيغة (.json)")
        if not isinstance(raw_payload, dict) or not raw_payload:
            raise DomainValidationError("بنية ملف JSON غير صالحة؛ يجب أن يكون كائن JSON يحتوي على بيانات الأكاديمية")

        present_keys = set(raw_payload.keys()) & self._RECOGNIZED_JSON_STATE_KEYS
        if not present_keys or "students" not in raw_payload:
            raise DomainValidationError(
                "ملف JSON لا يتضمن الهيكل الإلزامي لقاعدة بيانات الأكاديمية (حقل «students» مطلوب)"
            )

        with self.lock:
            current_state = self.export_state()
            existing_teachers = (
                set(current_state.get("teachers") or [])
                if "teachers" not in raw_payload
                else None
            )

        payload_copy = json.loads(json.dumps(raw_payload))
        validated = validate_state(
            payload_copy,
            allow_legacy_dates=allow_legacy_dates,
            existing_teachers=existing_teachers,
        )

        # Cross-entity relationship & referential integrity validation
        stu_ids = {str(s.get("id") or "").strip() for s in (validated.get("students") or []) if isinstance(s, dict)}
        relationship_errors: list[str] = []
        for coll_key, label in (
            ("installments", "إيصالات السداد"),
            ("plans", "خطط الحضور"),
            ("sessions", "جلسات الحضور"),
            ("memorization", "سجلات التسميع"),
            ("madrasaEnrollments", "انتسابات المدرسة الإيمانية"),
            ("madrasaLogs", "سجلات المدرسة الإيمانية"),
            ("certificates", "الشهادات"),
        ):
            items = validated.get(coll_key)
            if isinstance(items, list):
                for idx, item in enumerate(items, start=1):
                    if isinstance(item, dict):
                        sid = str(item.get("studentId") or "").strip()
                        if sid and sid not in stu_ids:
                            relationship_errors.append(
                                f"{label} (عنصر #{idx}): يشير إلى طالبة غير موجودة في الملف ({sid})"
                            )
        if relationship_errors:
            raise DomainValidationError("؛ ".join(relationship_errors[:5]))

        # Canonical SHA-256 hash of validated payload
        canonical_bytes = json.dumps(validated, ensure_ascii=False, sort_keys=True).encode("utf-8")
        payload_hash = hashlib.sha256(canonical_bytes).hexdigest()

        def _diff_by_key(
            cur_list: list[Any],
            inc_list: list[Any] | None,
            key_fn,
        ) -> dict[str, int]:
            if inc_list is None:
                return {
                    "currentCount": len(cur_list),
                    "incomingCount": len(cur_list),
                    "added": 0,
                    "updated": 0,
                    "deleted": 0,
                    "unchanged": len(cur_list),
                }
            cur_map = {
                key_fn(x): json.dumps(x, ensure_ascii=False, sort_keys=True)
                for x in cur_list
                if key_fn(x)
            }
            inc_map = {
                key_fn(x): json.dumps(x, ensure_ascii=False, sort_keys=True)
                for x in inc_list
                if key_fn(x)
            }
            added = sum(1 for k in inc_map if k not in cur_map)
            deleted = sum(1 for k in cur_map if k not in inc_map)
            updated = sum(1 for k in inc_map if k in cur_map and inc_map[k] != cur_map[k])
            unchanged = sum(1 for k in inc_map if k in cur_map and inc_map[k] == cur_map[k])
            return {
                "currentCount": len(cur_map),
                "incomingCount": len(inc_map),
                "added": added,
                "updated": updated,
                "deleted": deleted,
                "unchanged": unchanged,
            }

        cur_teachers = list(current_state.get("teachers") or [])
        if "teachers" in validated:
            inc_teachers = [str(t).strip() for t in (validated.get("teachers") or []) if str(t).strip()]
        else:
            inc_teachers = list(cur_teachers)
            for s in validated.get("students") or []:
                t = str((s or {}).get("teacher") or "").strip()
                if t and t not in inc_teachers:
                    inc_teachers.append(t)

        cur_meta = current_state.get("teacherMeta") or {}
        inc_meta = validated.get("teacherMeta") if isinstance(validated.get("teacherMeta"), dict) else cur_meta
        teacher_diff = _diff_by_key(
            [{"name": t, "meta": cur_meta.get(t, {})} for t in cur_teachers],
            [{"name": t, "meta": inc_meta.get(t, {})} for t in inc_teachers],
            lambda x: x["name"],
        )

        def _extract_pkg_list(st_obj: dict[str, Any]) -> list[str]:
            raw_sub = st_obj.get("subTypes")
            if isinstance(raw_sub, list):
                return [str(x).strip() for x in raw_sub if str(x).strip()]
            if isinstance(raw_sub, dict):
                return [str(k).strip() for k in raw_sub.keys() if str(k).strip()]
            return []

        cur_pkgs = _extract_pkg_list(current_state)
        inc_pkgs = _extract_pkg_list(validated) if "subTypes" in validated else None
        pkg_diff = _diff_by_key(cur_pkgs, inc_pkgs, lambda x: str(x))

        datasets_spec = [
            ("teachers", "المعلمات", teacher_diff),
            ("packages", "باقات الاشتراك", pkg_diff),
            (
                "students",
                "الطالبات والاشتراكات",
                _diff_by_key(
                    current_state.get("students") or [],
                    validated.get("students") or [],
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "installments",
                "إيصالات السداد",
                _diff_by_key(
                    current_state.get("installments") or [],
                    validated.get("installments") if "installments" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "payouts",
                "رواتب المعلمات المصروفة",
                _diff_by_key(
                    current_state.get("payouts") or [],
                    validated.get("payouts") if "payouts" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "transactions",
                "قيود الخزينة والمحاسبة",
                _diff_by_key(
                    current_state.get("transactions") or [],
                    validated.get("transactions") if "transactions" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "sessions",
                "جلسات الحضور والمواعيد",
                _diff_by_key(
                    current_state.get("sessions") or [],
                    validated.get("sessions") if "sessions" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "memorization",
                "سجلات التسميع والمراجعة",
                _diff_by_key(
                    current_state.get("memorization") or [],
                    validated.get("memorization") if "memorization" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "madrasaEnrollments",
                "المدرسة الإيمانية",
                _diff_by_key(
                    current_state.get("madrasaEnrollments") or [],
                    validated.get("madrasaEnrollments") if "madrasaEnrollments" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
            (
                "certificates",
                "الشهادات التقديرية",
                _diff_by_key(
                    current_state.get("certificates") or [],
                    validated.get("certificates") if "certificates" in validated else None,
                    lambda x: str(x.get("id") or ""),
                ),
            ),
        ]

        datasets_out: dict[str, dict[str, Any]] = {}
        dataset_rows: list[dict[str, Any]] = []
        affected_datasets: list[str] = []
        tot_cur = 0
        tot_inc = 0
        tot_add = 0
        tot_upd = 0
        tot_del = 0

        for ds_key, ds_label, diff_obj in datasets_spec:
            entry = {"dataset": ds_key, "label": ds_label, **diff_obj}
            datasets_out[ds_key] = entry
            dataset_rows.append(entry)
            tot_cur += diff_obj["currentCount"]
            tot_inc += diff_obj["incomingCount"]
            tot_add += diff_obj["added"]
            tot_upd += diff_obj["updated"]
            tot_del += diff_obj["deleted"]
            if diff_obj["added"] > 0 or diff_obj["updated"] > 0 or diff_obj["deleted"] > 0:
                affected_datasets.append(ds_key)

        dups = detect_duplicate_students(validated.get("students") or [])
        return {
            "ok": True,
            "valid": True,
            "filename": fname,
            "payloadHash": payload_hash,
            "currentRev": self.rev,
            "summary": {
                "totalCurrentRecords": tot_cur,
                "totalIncomingRecords": tot_inc,
                "totalAdded": tot_add,
                "totalUpdated": tot_upd,
                "totalDeleted": tot_del,
                "affectedDatasets": affected_datasets,
                "willDeleteExistingData": tot_del > 0,
                "duplicateStudentsCount": len(dups),
            },
            "datasets": datasets_out,
            "datasetRows": dataset_rows,
            "duplicates": dups,
            "_validatedState": validated,
        }

    def restore_from_json(
        self,
        raw_payload: Any,
        backup_folder: Path,
        filename: str = "backup.json",
        actor: str = "admin",
        expected_rev: int | None = None,
        expected_payload_hash: str | None = None,
        allow_legacy_dates: bool = True,
        allow_empty: bool = False,
        confirm_restore: bool = False,
    ) -> dict[str, Any]:
        """Executes a validated, previewed full-state JSON restore with a guaranteed immediate pre-restore snapshot and rollback on failure."""
        if not confirm_restore:
            raise DomainValidationError(
                "يجب تأكيد عملية استعادة نسخة JSON صراحةً بعد معاينة ملخص التغييرات (confirmRestore=true)"
            )
        with self.lock:
            self.check_rev(expected_rev)
            preview = self.preview_json_restore(
                raw_payload,
                filename=filename,
                allow_legacy_dates=allow_legacy_dates,
            )
            if expected_payload_hash and str(expected_payload_hash).strip() != preview["payloadHash"]:
                raise DomainValidationError(
                    "تغير محتوى ملف JSON بعد المعاينة؛ يرجى إعادة المعاينة قبل اعتماد الاستعادة"
                )
            validated_state = preview["_validatedState"]
            eff_allow_empty = bool(
                allow_empty
                or (isinstance(raw_payload, dict) and raw_payload.get("allowEmptyCollections") is True)
            )

            # Always create a guaranteed immediate pre-restore snapshot before modifying state
            backup_folder = Path(backup_folder)
            backup_folder.mkdir(parents=True, exist_ok=True)
            snap = self.snapshot(backup_folder, "pre_json_restore", keep=30)
            if not verify_sqlite_backup_file(snap):
                raise ValueError("pre-restore snapshot integrity check failed")

            try:
                self.replace_all(
                    validated_state,
                    actor=actor,
                    expected_rev=expected_rev,
                    allow_empty=eff_allow_empty,
                )
                # Post-restore database & foreign-key integrity verification
                chk = self.db.execute("PRAGMA integrity_check").fetchone()
                if not chk or chk[0] != "ok":
                    raise ValueError("integrity check failed after JSON restore")
                fk_errs = self.db.execute("PRAGMA foreign_key_check").fetchall()
                if fk_errs:
                    raise ValueError("foreign key integrity check failed after JSON restore")
                restored_students = self.count_students()
                if restored_students != len(validated_state.get("students") or []):
                    raise ValueError("restored student count mismatch after JSON restore")
                self.log(
                    actor,
                    "json_restore",
                    f"file={preview['filename']};snapshot={snap.name};students={restored_students}",
                )
            except Exception:
                self._revert_to_snapshot(snap)
                raise

            return {
                "ok": True,
                "snapshot": snap.name,
                "payloadHash": preview["payloadHash"],
                "summary": preview["summary"],
                "datasets": preview["datasets"],
                "datasetRows": preview["datasetRows"],
                "students": self.count_students(),
                "duplicates": preview["duplicates"],
                "rev": self.rev,
            }

    # ------------------------------------------------------------------
    # Phase 4 Automation Methods: Notifications Read State & Bulk Operations
    # ------------------------------------------------------------------
    def mark_alert_read(
        self,
        alert_key: str,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Marks an alert/notification key (or `__all__`) as read without deleting historical alerts."""
        akey = str(alert_key or "").strip()
        if not akey:
            raise ValueError("alert key is required")
        with self.lock:
            self.check_rev(expected_rev)
            alerts_state = self.get_kv("alertsState", {})
            if not isinstance(alerts_state, dict):
                alerts_state = {}
            read_map = alerts_state.get("read")
            if not isinstance(read_map, dict):
                read_map = {}
            now_iso = datetime.datetime.now().isoformat(timespec="seconds")
            if akey == "__all__":
                read_map["__all__"] = now_iso
                from ..domain.alerts_engine import compute_alerts
                cur_alerts = compute_alerts(self.export_state(), include_dismissed=True)
                for it in cur_alerts.get("items") or []:
                    k = str(it.get("alertKey") or it.get("id") or "")
                    if k:
                        read_map[k] = now_iso
            else:
                read_map[akey] = now_iso
            alerts_state["read"] = read_map
            return self.put_kv("alertsState", alerts_state, actor=actor, expected_rev=None)

    def execute_bulk_operation(
        self,
        operation: str,
        params: dict[str, Any],
        backup_folder: Path | None = None,
        actor: str = "admin",
        expected_rev: int | None = None,
        expected_preview_hash: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Executes a validated, previewed bulk operation atomically with an automatic pre-operation safety snapshot."""
        if not confirm:
            raise DomainValidationError("يجب تأكيد العملية الجماعية صراحةً بعد المعاينة (confirm=true)")
        with self.lock:
            self.check_rev(expected_rev)
            state = self.export_state()
            preview = preview_bulk_operation(state, operation, params)
            if expected_preview_hash and str(expected_preview_hash).strip() != preview["previewHash"]:
                raise DomainValidationError("تغيرت البيانات أو المعاملات بعد المعاينة؛ يرجى إعادة معاينة العملية الجماعية")

            affected = preview["affectedRecords"]
            if not affected:
                return {
                    "ok": True,
                    "operation": preview["operation"],
                    "affectedCount": 0,
                    "affectedRecords": [],
                    "snapshot": "",
                    "rev": self.rev,
                }

            snap_name = ""
            if backup_folder:
                snap = self.snapshot(Path(backup_folder), f"before_bulk_{preview['operation']}", keep=25)
                snap_name = snap.name

            op = preview["operation"]
            today_iso = datetime.date.today().isoformat()
            self.db.execute("BEGIN")
            try:
                before_fp = self._closed_fingerprint()
                if op == "assign_teacher":
                    for rec in affected:
                        sid = rec["id"]
                        new_t = rec["after"]["teacher"]
                        new_pct = float(rec["after"]["pct"])
                        old_t = rec["before"]["teacher"]
                        srow = self.db.execute("SELECT transfers FROM students WHERE id=?", (sid,)).fetchone()
                        try:
                            tr_list = json.loads((srow["transfers"] if srow else "[]") or "[]")
                        except Exception:
                            tr_list = []
                        if not isinstance(tr_list, list):
                            tr_list = []
                        if old_t and old_t != "بدون معلمة" and old_t != new_t:
                            tr_list.append({"from": old_t, "to": new_t, "date": today_iso})
                        self.db.execute(
                            "UPDATE students SET teacher=?, pct=?, transfers=?, updated_at=? WHERE id=?",
                            (new_t, new_pct, json.dumps(tr_list, ensure_ascii=False), time.time(), sid),
                        )
                        self.db.execute("UPDATE attendance_plans SET teacher=? WHERE student_id=?", (new_t, sid))
                        self.db.execute(
                            "UPDATE sessions SET teacher=?, updated_at=? WHERE student_id=? AND status='scheduled' AND session_date >= ?",
                            (new_t, today_iso, sid, today_iso),
                        )

                elif op == "assign_package":
                    for rec in affected:
                        self.db.execute(
                            "UPDATE students SET type=?, updated_at=? WHERE id=?",
                            (rec["after"]["type"], time.time(), rec["id"]),
                        )

                elif op == "set_actual_start_date":
                    for rec in affected:
                        sid = rec["id"]
                        mk = rec["month"]
                        start_dt = rec["after"]["actualStartDate"]
                        end_dt = rec["after"]["endDate"]
                        self.db.execute(
                            "UPDATE months SET actual_start_date=?, end_date=? WHERE student_id=? AND month_key=?",
                            (start_dt, end_dt, sid, mk),
                        )
                        self.db.execute(
                            "UPDATE students SET actual_start_date=?, updated_at=? WHERE id=? AND (actual_start_date IS NULL OR actual_start_date='')",
                            (start_dt, time.time(), sid),
                        )

                elif op == "update_session_status":
                    for rec in affected:
                        self.db.execute(
                            "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
                            (rec["after"]["status"], today_iso, rec["id"]),
                        )

                elif op == "normalize_phones_and_codes":
                    for rec in affected:
                        self.db.execute(
                            "UPDATE students SET name=?, phone=?, code=?, updated_at=? WHERE id=?",
                            (rec["after"].get("name", rec.get("name", "")), rec["after"]["phone"], rec["after"]["code"], time.time(), rec["id"]),
                        )

                self._assert_closed_unchanged(before_fp)
                self.log(actor, f"bulk_{op}", f"count={len(affected)};snapshot={snap_name}")
                self.db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

            self.event_bus.emit(
                "BulkOperationExecuted",
                {"operation": op, "affectedCount": len(affected), "rev": self.rev},
                idempotency_key=f"BulkOperationExecuted:{op}:{self.rev}",
            )
            return {
                "ok": True,
                "operation": op,
                "updatedCount": len(affected),
                "affectedCount": len(affected),
                "affectedRecords": affected,
                "snapshot": snap_name,
                "rev": self.rev,
            }

    # ------------------------------------------------------------------
    # Stage 6 (Phase 1B & Phase 5): Demo Data Quarantine Cleaner & System Diagnostics
    # ------------------------------------------------------------------
    def inventory_demo_data(self) -> dict[str, Any]:
        """Inventories and classifies all database rows into Class A (confirmed demo), Class B (probable demo >=2 signals), Class C (protected real/uncertain), and System/Reference data."""
        with self.lock:
            return inventory_database_demo_data(self.db)

    def clean_demo_data_with_quarantine(
        self,
        backup_folder: Path | None = None,
        quarantine_folder: Path | None = None,
        include_class_b: bool = True,
        actor: str = "admin",
        expected_rev: int | None = None,
    ) -> dict[str, Any]:
        """Executes safe transactional demo/test data cleanup with mandatory pre-cleanup SQLite backup and UTF-8 JSON/CSV quarantine export."""
        with self.lock:
            self.check_rev(expected_rev)
            inv_before = inventory_database_demo_data(self.db)
            candidates = inv_before.get("candidatesByTable") or {}
            cand_students = candidates.get("students") or []
            cand_teachers = candidates.get("teachers") or []

            target_classes = {"A", "B"} if include_class_b else {"A"}
            del_student_ids = [
                str(r["id"]) for r in cand_students
                if r.get("_classification", {}).get("classification") in target_classes
            ]
            del_teacher_names = [
                str(r["name"]) for r in cand_teachers
                if r.get("_classification", {}).get("classification") in target_classes
            ]

            total_cand = sum(len(rows) for rows in candidates.values())
            if total_cand == 0 and not del_student_ids and not del_teacher_names:
                return {
                    "ok": True,
                    "cleaned": False,
                    "message": "لا توجد بيانات تجريبية أو اختبارية في قاعدة البيانات",
                    "deletedCounts": {t: 0 for t in candidates.keys()},
                    "totalDeleted": 0,
                    "snapshot": "",
                    "quarantineManifest": None,
                    "studentsCount": self.count_students(),
                    "rev": self.rev,
                }

            b_folder = Path(backup_folder) if backup_folder else (self.path.parent / "backups")
            b_folder.mkdir(parents=True, exist_ok=True)
            snap = self.snapshot(b_folder, "pre_demo_cleanup", keep=30)
            if not verify_sqlite_backup_file(snap):
                raise ValueError("pre-cleanup backup integrity check failed")

            q_folder = Path(quarantine_folder) if quarantine_folder else (self.path.parent / "quarantine")
            manifest = export_quarantine_archive(inv_before, q_folder, backup_path=snap)

            deleted_counts: dict[str, int] = {}
            self.db.execute("BEGIN")
            try:
                # Delete child tables first in FK-safe order without resetting sqlite_sequence
                for child_tbl, id_col in (
                    ("certificates", "id"),
                    ("madrasa_logs", "id"),
                    ("madrasa_enrollments", "id"),
                    ("madrasa_batches", "id"),
                    ("memorization_logs", "id"),
                    ("sessions", "id"),
                    ("attendance_plans", "id"),
                    ("study_groups", "id"),
                    ("salaries", "id"),
                    ("installments", "id"),
                    ("transactions", "id"),
                ):
                    tbl_cands = candidates.get(child_tbl) or []
                    ids_to_del = [str(r[id_col]) for r in tbl_cands if r.get(id_col)]
                    if ids_to_del:
                        marks = ",".join("?" * len(ids_to_del))
                        cur = self.db.execute(f"DELETE FROM {child_tbl} WHERE {id_col} IN ({marks})", ids_to_del)
                        deleted_counts[child_tbl] = cur.rowcount
                    else:
                        deleted_counts[child_tbl] = 0

                if del_student_ids:
                    marks = ",".join("?" * len(del_student_ids))
                    m_cur = self.db.execute(f"DELETE FROM months WHERE student_id IN ({marks})", del_student_ids)
                    deleted_counts["months"] = m_cur.rowcount
                    s_cur = self.db.execute(f"DELETE FROM students WHERE id IN ({marks})", del_student_ids)
                    deleted_counts["students"] = s_cur.rowcount
                else:
                    deleted_counts["months"] = 0
                    deleted_counts["students"] = 0

                if del_teacher_names:
                    marks = ",".join("?" * len(del_teacher_names))
                    # Only delete teachers that have no remaining protected students referencing them
                    t_cur = self.db.execute(
                        f"DELETE FROM teachers WHERE name IN ({marks}) AND name NOT IN (SELECT DISTINCT teacher FROM students WHERE teacher IS NOT NULL)",
                        del_teacher_names,
                    )
                    deleted_counts["teachers"] = t_cur.rowcount
                else:
                    deleted_counts["teachers"] = 0

                s = self.get_settings()
                s["isDemoData"] = False
                s["isDemoLoaded"] = False
                self.db.execute(
                    "INSERT OR REPLACE INTO kv(k, v) VALUES('settings', ?)",
                    (json.dumps(s, ensure_ascii=False),),
                )

                chk = self.db.execute("PRAGMA integrity_check").fetchone()
                if not chk or chk[0] != "ok":
                    raise ValueError("integrity_check failed after demo cleanup")
                fk_errs = self.db.execute("PRAGMA foreign_key_check").fetchall()
                if fk_errs:
                    raise ValueError(f"foreign_key_check failed after demo cleanup: {len(fk_errs)} violations")

                tot_del = sum(deleted_counts.values())
                self.log(
                    actor,
                    "clean_demo_data_quarantine",
                    f"deleted={tot_del};snapshot={snap.name};quarantine={manifest.get('quarantineDir', '')}",
                )
                self.db.execute("COMMIT")
            except Exception:
                self._rollback()
                raise

            return {
                "ok": True,
                "cleaned": True,
                "deletedCounts": deleted_counts,
                "totalDeleted": sum(deleted_counts.values()),
                "snapshot": snap.name,
                "quarantineManifest": manifest,
                "studentsCount": self.count_students(),
                "rev": self.rev,
            }

    def get_system_diagnostics(
        self,
        app_home: Path | None = None,
        autostart_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Returns comprehensive runtime, database, integrity, storage, and auto-start diagnostics."""
        import sys as _sys
        home_dir = Path(app_home) if app_home else self.path.parent
        with self.lock:
            settings = self.get_settings()
            ver_tuple = _sys.version_info[:2]
            if ver_tuple == (3, 12):
                py_tier = "Tier 1 — Release-Qualifying (Python 3.12)"
            elif ver_tuple == (3, 14):
                py_tier = "Tier 2 — Provisional Build (Python 3.14)"
            else:
                py_tier = f"Development / Container Runtime (Python {_sys.version_info.major}.{_sys.version_info.minor})"

            chk_row = self.db.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = bool(chk_row and chk_row[0] == "ok")
            fk_rows = self.db.execute("PRAGMA foreign_key_check").fetchall()
            wal_row = self.db.execute("PRAGMA journal_mode").fetchone()

            tables = (
                "students", "teachers", "months", "installments", "salaries", "transactions",
                "sessions", "attendance_plans", "study_groups", "memorization_logs",
                "madrasa_batches", "madrasa_enrollments", "madrasa_logs", "certificates",
                "closed_months", "import_batches", "audit",
            )
            table_counts = {
                t: int(self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                for t in tables
            }

            db_size = self.path.stat().st_size if self.path.is_file() else 0
            wal_path = self.path.with_name(self.path.name + "-wal")
            wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
            log_path = home_dir / "logs" / "alahd_erp.log"
            log_size = log_path.stat().st_size if log_path.is_file() else 0

            demo_inv = inventory_database_demo_data(self.db)
            extra_backup = str(settings.get("extraBackupFolder") or "").strip()

            return {
                "ok": True,
                "appVersion": __version__,
                "schemaVersion": self.get_schema_version(),
                "pythonVersion": _sys.version.split()[0],
                "pythonExecutable": _sys.executable,
                "pythonTier": py_tier,
                "platform": _sys.platform,
                "frozen": bool(getattr(_sys, "frozen", False)),
                "appHome": str(home_dir),
                "dbPath": str(self.path),
                "dbSizeBytes": db_size,
                "walSizeBytes": wal_size,
                "logPath": str(log_path),
                "logSizeBytes": log_size,
                "backupsDir": str(home_dir / "backups"),
                "quarantineDir": str(home_dir / "quarantine"),
                "isSyncedAppHome": is_onedrive_or_synced_path(home_dir),
                "isSyncedExtraBackup": is_onedrive_or_synced_path(extra_backup) if extra_backup else False,
                "integrityOk": integrity_ok,
                "integrityResult": chk_row[0] if chk_row else "unknown",
                "foreignKeyViolations": len(fk_rows),
                "journalMode": wal_row[0] if wal_row else "unknown",
                "tableCounts": table_counts,
                "demoSummary": demo_inv["summary"],
                "backupHealth": self.get_backup_health(home_dir / "backups"),
                "autostart": autostart_status or {},
                "rev": self.rev,
            }

