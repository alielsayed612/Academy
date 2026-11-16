"""Independent Runtime Configuration & Logging for Yawm Jadid Academy Suite.

Ensures 100% isolation and safe per-user data storage:
- Dedicated environment variables: ALAHD_ERP_HOME, ALAHD_ERP_PORT, ALAHD_ERP_NO_UI
- Dedicated storage directory: %LOCALAPPDATA%\\AlAhdAcademySuite (never inside install/EXE folder)
- Dedicated SQLite database file: alahd_erp_v1.sqlite3
- Dedicated rotating log file: logs/alahd_erp.log
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import DATA_DIR_NAME, DB_FILENAME, DEFAULT_PORT

FROZEN = bool(getattr(sys, "frozen", False))
PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", PACKAGE_DIR))

_SYNCED_FOLDER_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "icloud",
    "iclouddrive",
)


def is_onedrive_or_synced_path(path: Path | str | None) -> bool:
    """Returns True if `path` resides inside a cloud-synced folder (OneDrive, Dropbox, Google Drive, iCloud).

    SQLite WAL files (-wal / -shm) must never be actively mutated inside a cloud-synced folder,
    while read-only backup mirrors to synced folders should emit a clear advisory.
    """
    if not path:
        return False
    raw = str(path).strip()
    if not raw:
        return False
    norm = raw.replace("\\", "/").lower()
    segments = [seg.strip() for seg in norm.split("/") if seg.strip()]
    for seg in segments:
        if any(marker in seg for marker in _SYNCED_FOLDER_MARKERS):
            return True
    return False


def resolve_app_home() -> Path:
    """Resolves the writable application data directory consistently in %LOCALAPPDATA%\\AlAhdAcademySuite
    (or ALAHD_ERP_HOME when explicitly overridden). Never stores data next to the EXE or inside the repo.
    """
    env_home = os.environ.get("ALAHD_ERP_HOME") or os.environ.get("ACADEMY_HOME")
    if env_home:
        candidate = Path(env_home).resolve()
    else:
        appdata = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or Path.home())
        candidate = (appdata / DATA_DIR_NAME).resolve()

    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def resolve_ui_dir() -> Path:
    """Resolves the UI static assets directory for both source and PyInstaller builds."""
    candidates = [
        BUNDLE_DIR / "ui",
        PACKAGE_DIR / "ui",
        BUNDLE_DIR / "alahd_erp" / "ui",
    ]
    for c in candidates:
        if c.is_dir() and (c / "index.html").is_file():
            return c
    return PACKAGE_DIR / "ui"


def resolve_port() -> int:
    raw = os.environ.get("ALAHD_ERP_PORT") or os.environ.get("ACADEMY_PORT")
    if raw:
        try:
            p = int(raw)
            if 1024 <= p <= 65535:
                return p
        except ValueError:
            pass
    return DEFAULT_PORT


def configure_logging(app_home: Path) -> logging.Logger:
    """Configures safe rotating file logging, handling windowed PyInstaller builds where stdout/stderr are None."""
    log_file: Path | None = None
    try:
        log_dir = Path(app_home) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "alahd_erp.log"
    except OSError:
        try:
            fallback_dir = Path.cwd() / ".logs"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            log_file = fallback_dir / "alahd_erp.log"
        except OSError:
            log_file = None

    for stream_name in ("stdout", "stderr"):
        if getattr(sys, stream_name, None) is None:
            setattr(sys, stream_name, open(log_file, "a", encoding="utf-8", buffering=1) if log_file else open(os.devnull, "w"))
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]  # TextIO subclass at runtime
        sys.stderr.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if log_file and not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
        if not FROZEN:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root.addHandler(sh)

    logger = logging.getLogger("alahd_erp")
    if is_onedrive_or_synced_path(app_home):
        logger.warning(
            "STORAGE WARNING: Application data directory '%s' resides inside a cloud-synced folder (e.g. OneDrive). "
            "SQLite WAL files may experience locking conflicts if actively synced.",
            app_home,
        )
    return logger


def resolve_db_path(app_home: Path) -> Path:
    return Path(app_home) / DB_FILENAME
