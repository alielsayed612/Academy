#!/usr/bin/env python3
"""Standalone Desktop Application Entry Point — Yawm Jadid Academy Suite (`YawmJadidAcademy.exe`).

Operates as an independent desktop product (v1.2.0):
- Dedicated local server on 127.0.0.1:8890 (configurable via ALAHD_ERP_PORT).
- Dedicated storage directory (`%LOCALAPPDATA%\\AlAhdAcademySuite` or `ALAHD_ERP_HOME`)
  with SQLite WAL database `alahd_erp_v1.sqlite3` (`PRAGMA synchronous=FULL`).
- Single-instance OS mutex + lockfile guard (`SingleInstanceGuard`) and `BootGuard` loop protection.
- Ephemeral per-launch `X-Window-Secret` stored in `APP_HOME/.alahd_instance.json` for single-instance
  handover and local authentication.
- Windows per-user auto-start synchronization & self-repair (`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`).
- Ensures the bundled Academy Excel workbooks (`قواعد شهر 9.xlsx` + `Academy_Management_Dashboard_2026-11-11.xlsx`)
  are loaded into the local database on desktop launch.
- Dedicated rotating backup service (`backups/alahd_auto_*.sqlite3` and `backups/alahd_daily_*.sqlite3`)
  with `PRAGMA integrity_check` verification and optional mirror to `extraBackupFolder`.
- Native desktop window via pywebview with graceful fallback to the user's local browser.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Normalize working directory when launched as a packaged EXE (especially from Windows Run key at login)
if getattr(sys, "frozen", False):
    try:
        os.chdir(Path(sys.executable).resolve().parent)
    except OSError:
        pass

# Ensure package root is importable when launched directly or frozen via PyInstaller
_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from alahd_erp import APP_NAME, __version__  # noqa: E402
from alahd_erp.config import (  # noqa: E402
    configure_logging,
    resolve_app_home,
    resolve_port,
    resolve_ui_dir,
)
from alahd_erp.infrastructure.instance_probe import probe_instance  # noqa: E402
from alahd_erp.infrastructure.autostart import (  # noqa: E402
    BootGuard,
    SingleInstanceGuard,
    sync_and_repair_autostart,
)

APP_HOME = resolve_app_home()
log = configure_logging(APP_HOME)

import uvicorn  # noqa: E402
from alahd_erp.api.server import create_app  # noqa: E402
from alahd_erp.domain.academy_data_loader import ensure_academy_workbooks_loaded  # noqa: E402
from alahd_erp.infrastructure.database import rotate  # noqa: E402

HOST = "127.0.0.1"
PORT = resolve_port()
BACKUP_EVERY = 1800
KEEP_AUTO, KEEP_DAILY = 48, 30
INSTANCE_FILE = APP_HOME / ".alahd_instance.json"


def message_box(text: str, title: str = APP_NAME, error: bool = False) -> None:
    log.error(text) if error else log.info(text)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x40)  # type: ignore[attr-defined]  # Windows only
        except Exception:
            pass


def read_instance_info() -> dict[str, object]:
    try:
        if INSTANCE_FILE.is_file():
            return json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def write_instance_info(port: int, window_secret: str) -> None:
    try:
        INSTANCE_FILE.write_text(
            json.dumps({"port": port, "windowSecret": window_secret, "pid": os.getpid()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def already_running(port: int, secret_hint: str = "") -> str | None:
    """Checks if an Academy Suite instance is already running on `port`.
    Returns its valid `window_secret` if running, or `None` if not running (or if the port is held by something else).
    """
    info = read_instance_info()
    cand_secret = secret_hint or str(info.get("windowSecret") or os.environ.get("ALAHD_WINDOW_SECRET") or "")
    return probe_instance(port, cand_secret)


def backup_loop(store, stop: threading.Event) -> None:
    d = APP_HOME / "backups"
    d.mkdir(parents=True, exist_ok=True)
    last_rev = -1
    while not stop.is_set():
        try:
            if store.rev != last_rev:
                store.backup_to(d / f"alahd_auto_{time.strftime('%Y%m%d_%H%M%S')}.sqlite3")
                last_rev = store.rev
                rotate(d, "alahd_auto_*.sqlite3", KEEP_AUTO)
            daily = d / f"alahd_daily_{time.strftime('%Y%m%d')}.sqlite3"
            if not daily.exists():
                store.backup_to(daily)
                rotate(d, "alahd_daily_*.sqlite3", KEEP_DAILY)
        except Exception:
            log.exception("Periodic backup warning")
        stop.wait(BACKUP_EVERY)


def start_server(app, port: int = PORT):
    cfg = uvicorn.Config(app, host=HOST, port=port, log_config=None, access_log=False, log_level="warning")
    server = uvicorn.Server(cfg)
    err: list[BaseException] = []

    def run():
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001
            err.append(exc)
            log.exception("Server crashed")

    threading.Thread(target=run, daemon=True, name="alahd-uvicorn").start()
    for _ in range(100):
        if server.started or err:
            break
        time.sleep(0.1)
    return server, err


def open_window(url: str, home: Path, minimized: bool = False) -> bool:
    try:
        import webview
    except ImportError:
        return False
    try:
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        storage = home / "alahd_webview_storage"
        storage.mkdir(parents=True, exist_ok=True)
        webview.create_window(
            f"{APP_NAME} — v{__version__}",
            url,
            width=1480,
            height=920,
            min_size=(1080, 700),
            minimized=bool(minimized),
            text_select=True,
        )
        webview.start(private_mode=False, storage_path=str(storage))
        return True
    except Exception:
        log.exception("pywebview window failed; falling back to default browser")
        return False


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    is_autostart = "--autostart" in args
    cli_minimized = "--minimized" in args

    boot_guard = BootGuard(APP_HOME)
    if is_autostart and boot_guard.should_abort_autostart():
        log.error(
            "BootGuard: Aborting --autostart launch after %d consecutive boot failures to prevent login loop.",
            boot_guard.max_failures,
        )
        return 1

    active_port = PORT
    instance_guard = SingleInstanceGuard(APP_HOME)
    if not instance_guard.acquire():
        info = read_instance_info()
        running_port = int(info.get("port") or active_port)
        existing_sec = already_running(running_port)
        if existing_sec is not None:
            log.info("Academy Suite instance already running on port %s (SingleInstanceGuard)", running_port)
            existing_url = f"http://127.0.0.1:{running_port}/?wsec={existing_sec}"
            if not is_autostart:
                if not open_window(existing_url, APP_HOME):
                    webbrowser.open(existing_url)
            return 0
        log.info("Another instance holds the SingleInstanceGuard lock; exiting cleanly.")
        return 0

    try:
        existing_sec = already_running(active_port)
        if existing_sec is not None:
            log.info("Academy Suite instance already running on port %s", active_port)
            existing_url = f"http://127.0.0.1:{active_port}/?wsec={existing_sec}"
            if not is_autostart:
                if not open_window(existing_url, APP_HOME):
                    webbrowser.open(existing_url)
            return 0

        window_secret = os.environ.get("ALAHD_WINDOW_SECRET") or secrets.token_urlsafe(24)
        # Phase 1B Mandate: Desktop application NEVER seeds demo data on startup
        seed_on_empty = False
        ui_dir = resolve_ui_dir()
        app = create_app(APP_HOME, ui_dir, active_port, window_secret=window_secret, seed_on_empty=seed_on_empty)
        server, err = start_server(app, port=active_port)

        # If default port is occupied by an unrelated service and no explicit port was forced, try fallback range
        if (err or not server.started) and not os.environ.get("ALAHD_ERP_PORT") and not os.environ.get("ACADEMY_PORT"):
            for candidate in range(PORT + 1, PORT + 10):
                cand_sec = already_running(candidate)
                if cand_sec is not None:
                    active_port = candidate
                    app.state.store.close()
                    cand_url = f"http://127.0.0.1:{active_port}/?wsec={cand_sec}"
                    if not is_autostart:
                        if not open_window(cand_url, APP_HOME):
                            webbrowser.open(cand_url)
                    return 0
                app.state.store.close()
                app = create_app(APP_HOME, ui_dir, candidate, window_secret=window_secret, seed_on_empty=seed_on_empty)
                server, err = start_server(app, port=candidate)
                if server.started and not err:
                    active_port = candidate
                    break

        if err or not server.started:
            app.state.store.close()
            why = f"\n\nالسبب: {err[0]}" if err else ""
            boot_guard.record_boot_failure(str(err[0]) if err else "server_start_failed", is_autostart=is_autostart)
            if not is_autostart:
                message_box(
                    f"تعذّر تشغيل خادم أكاديمية يوم جديد على المنفذ {active_port}.\n"
                    f"يمكنك تحديد منفذ بديل عبر المتغير: set ALAHD_ERP_PORT=8895{why}\n\n"
                    f"ملف السجل: {APP_HOME / 'logs' / 'alahd_erp.log'}",
                    error=True,
                )
            return 1

        # Ensure bundled Academy Excel workbooks (September + Oct/Nov Dashboard) are loaded in normal desktop mode
        if not os.environ.get("ALAHD_ERP_NO_UI") and not os.environ.get("ACADEMY_NO_UI") and not os.environ.get("ALAHD_SKIP_BUNDLED_LOAD"):
            try:
                load_res = ensure_academy_workbooks_loaded(app.state.store)
                if load_res.get("loaded"):
                    log.info("Loaded bundled Academy Excel workbooks into store: %s", load_res)
            except Exception:
                log.exception("Bundled Academy workbooks auto-load warning")

        # Synchronize and self-repair Windows auto-start registration on startup
        try:
            cur_settings = app.state.store.get_settings()
            sync_res = sync_and_repair_autostart(
                settings_enabled=bool(cur_settings.get("autoStartEnabled", False)),
                initialized=bool(cur_settings.get("autoStartInitialized", False)),
                minimized=bool(cur_settings.get("startMinimized", False)),
            )
            if sync_res.get("action") == "first_run_default_enabled":
                app.state.store.update_settings(
                    {"autoStartEnabled": True, "autoStartInitialized": True},
                    actor="system",
                    source="first_run_autostart",
                )
        except Exception:
            log.exception("Auto-start startup sync warning")

        boot_guard.record_startup_success()
        write_instance_info(active_port, window_secret)
        base_url = f"http://127.0.0.1:{active_port}/"
        launch_url = f"http://127.0.0.1:{active_port}/?wsec={window_secret}"
        stop = threading.Event()

        def _handle_stop_signal(signum, frame):  # noqa: ANN001, ARG001
            stop.set()
            server.should_exit = True

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle_stop_signal)
                except Exception:
                    pass

        threading.Thread(target=backup_loop, args=(app.state.store, stop), daemon=True, name="alahd-backup").start()
        log.info("%s v%s running locally at %s (autostart=%s)", APP_NAME, __version__, base_url, is_autostart)

        start_min = cli_minimized or (is_autostart and bool(app.state.store.get_settings().get("startMinimized", False)))

        try:
            if os.environ.get("ALAHD_ERP_NO_UI") or os.environ.get("ACADEMY_NO_UI"):
                while not stop.is_set():
                    stop.wait(0.5)
            elif not open_window(launch_url, APP_HOME, minimized=start_min):
                webbrowser.open(launch_url)
                if sys.stdin is not None and sys.stdin.isatty():
                    input("أكاديمية يوم جديد تعمل محليًا. اضغط Enter للإغلاق...\n")
                elif os.name == "nt":
                    message_box("أكاديمية يوم جديد تعمل محليًا ومفتوحة في المتصفح.\nاضغط «موافق» لإيقاف البرنامج.")
                else:
                    while not stop.is_set():
                        stop.wait(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            try:
                INSTANCE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                d = APP_HOME / "backups"
                d.mkdir(parents=True, exist_ok=True)
                app.state.store.backup_to(d / f"alahd_auto_{time.strftime('%Y%m%d_%H%M%S')}_exit.sqlite3")
            except Exception:
                log.exception("Exit backup failed")
            try:
                app.state.store.close()
            except Exception:
                pass
            server.should_exit = True
        return 0
    finally:
        instance_guard.release()


if __name__ == "__main__":
    sys.exit(main())
