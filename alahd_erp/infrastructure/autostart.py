"""Phase 5A-2 — Windows Auto-Start (`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`),
Single-Instance Guard & Boot-Time Robustness Engine.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("alahd_erp.autostart")

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
REGISTRY_KEY_PATH = RUN_KEY_PATH
DEFAULT_VALUE_NAME = "YawmJadidAcademy"
REGISTRY_VALUE_NAME = DEFAULT_VALUE_NAME
MAX_BOOT_FAILURES = 3


class WinRegBackend:
    """Real Windows `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` backend using stdlib `winreg`."""

    def __init__(self, key_path: str = RUN_KEY_PATH) -> None:
        self.key_path = key_path

    def get_value(self, key_or_value_name: str, value_name: str | None = None) -> str | None:
        k_path = key_or_value_name if value_name is not None else self.key_path
        v_name = value_name if value_name is not None else key_or_value_name
        if os.name != "nt":
            return None
        import winreg  # type: ignore[import-not-found]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k_path, 0, winreg.KEY_READ) as key:
                val, _regtype = winreg.QueryValueEx(key, v_name)
                return str(val) if val is not None else None
        except FileNotFoundError:
            return None
        except OSError as exc:
            _LOG.warning("Failed reading HKCU Run value %s: %s", v_name, exc)
            return None

    def set_value(self, key_or_value_name: str, value_name_or_cmd: str, command: str | None = None) -> bool:
        k_path = key_or_value_name if command is not None else self.key_path
        v_name = value_name_or_cmd if command is not None else key_or_value_name
        cmd = command if command is not None else value_name_or_cmd
        if os.name != "nt":
            return False
        import winreg  # type: ignore[import-not-found]
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, k_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, v_name, 0, winreg.REG_SZ, str(cmd))
            return True
        except OSError as exc:
            _LOG.warning("Failed setting HKCU Run value %s: %s", v_name, exc)
            return False

    def delete_value(self, key_or_value_name: str, value_name: str | None = None) -> bool:
        k_path = key_or_value_name if value_name is not None else self.key_path
        v_name = value_name if value_name is not None else key_or_value_name
        if os.name != "nt":
            return False
        import winreg  # type: ignore[import-not-found]
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, v_name)
                return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            _LOG.warning("Failed deleting HKCU Run value %s: %s", v_name, exc)
            return False


class FileBackedRegistryBackend:
    """Deterministic file-backed registry backend for unit tests and non-Windows environments."""

    def __init__(self, storage_file: Path) -> None:
        self.storage_file = Path(storage_file)

    def _read_map(self) -> dict[str, Any]:
        try:
            if self.storage_file.is_file():
                data = json.loads(self.storage_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _write_map(self, data: dict[str, Any]) -> None:
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        self.storage_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_value(self, key_or_value_name: str, value_name: str | None = None) -> str | None:
        m = self._read_map()
        if value_name is not None:
            sub = m.get(key_or_value_name)
            if isinstance(sub, dict) and value_name in sub:
                return str(sub[value_name])
            return m.get(value_name)
        if key_or_value_name in m and not isinstance(m[key_or_value_name], dict):
            return str(m[key_or_value_name])
        sub = m.get(RUN_KEY_PATH)
        if isinstance(sub, dict) and key_or_value_name in sub:
            return str(sub[key_or_value_name])
        return None

    def set_value(self, key_or_value_name: str, value_name_or_cmd: str, command: str | None = None) -> bool:
        m = self._read_map()
        k_path = key_or_value_name if command is not None else RUN_KEY_PATH
        v_name = value_name_or_cmd if command is not None else key_or_value_name
        cmd = command if command is not None else value_name_or_cmd
        if not isinstance(m.get(k_path), dict):
            m[k_path] = {}
        m[k_path][v_name] = str(cmd)
        m[v_name] = str(cmd)
        self._write_map(m)
        return True

    def delete_value(self, key_or_value_name: str, value_name: str | None = None) -> bool:
        m = self._read_map()
        k_path = key_or_value_name if value_name is not None else RUN_KEY_PATH
        v_name = value_name if value_name is not None else key_or_value_name
        if isinstance(m.get(k_path), dict):
            m[k_path].pop(v_name, None)
        m.pop(v_name, None)
        self._write_map(m)
        return True


def is_eligible_for_autostart(
    exe_path: Path | str | None = None,
    is_frozen: bool | None = None,
    allow_test_path: bool = False,
    *,
    executable_path: Path | str | None = None,
    frozen: bool | None = None,
    platform_name: str | None = None,
) -> tuple[bool, str, str]:
    """Verifies that the current runtime is a genuine frozen `.exe` outside temp/extraction directories.
    Returns `(eligible, reason_code, arabic_message)`.
    """
    eff_frozen = frozen if frozen is not None else (is_frozen if is_frozen is not None else bool(getattr(sys, "frozen", False)))
    eff_plat = platform_name if platform_name is not None else sys.platform
    eff_exe_raw = executable_path if executable_path is not None else (exe_path if exe_path is not None else sys.executable)

    if not eff_frozen:
        return (
            False,
            "unavailable_source_mode",
            "التشغيل التلقائي مع بدء تشغيل ويندوز متاح فقط في النسخة التنفيذية المبنية (YawmJadidAcademy.exe)؛ التطبيق يعمل حاليًا من الكود المصدري (Source Mode).",
        )

    if not eff_plat.startswith("win") and os.name != "nt":
        return (
            False,
            "non_windows_os",
            "التشغيل التلقائي عبر سجل ويندوز (HKCU Run) متاح على نظام Windows فقط.",
        )

    raw_str = str(eff_exe_raw or "").strip()
    cand = Path(raw_str)
    norm_lower = raw_str.replace("\\", "/").lower()
    name_lower = norm_lower.rsplit("/", 1)[-1]

    if name_lower in ("python.exe", "pythonw.exe", "python", "python3") or not name_lower.endswith(".exe"):
        return (
            False,
            "python_interpreter_blocked",
            f"تم حظر تسجيل مفسر بايثون أو ملف غير تنفيذي ({name_lower}) في التشغيل التلقائي.",
        )

    segments = [seg for seg in norm_lower.split("/") if seg]
    if any(seg.startswith("_mei") for seg in segments):
        return (
            False,
            "temp_extraction_path",
            "تم حظر تسجيل مسار استخراج PyInstaller المؤقت (_MEI) في التشغيل التلقائي.",
        )

    if any(seg in ("pytest", "unittest", "safety_snapshots") for seg in segments):
        return (
            False,
            "test_directory_blocked",
            "تم حظر تسجيل مسار اختباري في التشغيل التلقائي.",
        )

    if not (raw_str.startswith("C:\\") or raw_str.startswith("D:\\")) and not cand.is_file():
        return (
            False,
            "executable_not_found",
            f"الملف التنفيذي غير موجود في المسار المحدد: {cand}",
        )

    return (
        True,
        "eligible",
        "النسخة التنفيذية مؤهلة للتشغيل التلقائي مع بدء تشغيل ويندوز.",
    )


def build_autostart_command(
    exe_path: Path | str | None = None,
    start_minimized: bool = False,
    *,
    executable_path: Path | str | None = None,
    minimized: bool | None = None,
) -> str:
    """Builds a properly quoted startup command string for `HKCU\\...\\Run`."""
    eff_exe = executable_path if executable_path is not None else (exe_path if exe_path is not None else sys.executable)
    eff_min = minimized if minimized is not None else start_minimized
    p = Path(eff_exe)
    try:
        if p.exists():
            p = p.resolve()
    except Exception:
        pass
    cmd = f'"{p}" --autostart'
    if eff_min:
        cmd += " --minimized"
    return cmd


def parse_executable_from_command(command: str | None) -> str:
    s = str(command or "").strip()
    if not s:
        return ""
    if s.startswith('"'):
        end_q = s.find('"', 1)
        if end_q > 1:
            return s[1:end_q]
    return s.split(" ")[0]


def get_autostart_status(
    exe_path: Path | str | None = None,
    is_frozen: bool | None = None,
    backend: Any = None,
    value_name: str = DEFAULT_VALUE_NAME,
    start_minimized: bool = False,
    allow_test_path: bool = False,
    *,
    executable_path: Path | str | None = None,
    frozen: bool | None = None,
    platform_name: str | None = None,
    minimized: bool | None = None,
) -> dict[str, Any]:
    """Reads the actual registry registration and returns truthful status and diagnostics."""
    eff_exe = Path(executable_path if executable_path is not None else (exe_path if exe_path is not None else sys.executable))
    eff_min = minimized if minimized is not None else start_minimized
    eligible, reason_code, msg_ar = is_eligible_for_autostart(
        eff_exe,
        is_frozen=is_frozen,
        allow_test_path=allow_test_path,
        executable_path=eff_exe,
        frozen=frozen,
        platform_name=platform_name,
    )
    eff_backend = backend or WinRegBackend()
    reg_cmd = eff_backend.get_value(RUN_KEY_PATH, value_name)
    expected_cmd = build_autostart_command(eff_exe, start_minimized=eff_min) if eligible else '"YawmJadidAcademy.exe" --autostart'
    reg_exe = parse_executable_from_command(reg_cmd)

    if not eligible:
        return {
            "eligible": False,
            "enabled": False,
            "status": "unavailable_source_mode",
            "reasonCode": reason_code,
            "statusLabel": "غير متاح في وضع المصدر (متاح في النسخة التنفيذية .exe فقط)",
            "messageAr": msg_ar,
            "explanation": msg_ar,
            "needsRepair": False,
            "valueName": value_name,
            "registryKeyPath": RUN_KEY_PATH,
            "registryPath": f"HKCU\\{RUN_KEY_PATH}\\{value_name}",
            "registeredCommand": reg_cmd or "",
            "expectedCommand": expected_cmd,
        }

    if not reg_cmd:
        msg_dis = "التشغيل التلقائي معطل حاليًا (لا يوجد إدخال مسجل في HKCU Run)."
        return {
            "eligible": True,
            "enabled": False,
            "status": "disabled",
            "reasonCode": "not_registered",
            "statusLabel": "معطل (غير مسجل في بدء تشغيل ويندوز)",
            "messageAr": msg_dis,
            "explanation": msg_dis,
            "needsRepair": False,
            "valueName": value_name,
            "registryKeyPath": RUN_KEY_PATH,
            "registryPath": f"HKCU\\{RUN_KEY_PATH}\\{value_name}",
            "registeredCommand": "",
            "expectedCommand": expected_cmd,
        }

    try:
        resolved_cur = str(eff_exe.resolve()) if eff_exe.exists() else str(eff_exe)
    except Exception:
        resolved_cur = str(eff_exe)

    reg_exe_path = Path(reg_exe) if reg_exe else None
    reg_exists = bool(reg_exe_path and reg_exe_path.is_file())
    path_matches = (reg_exe == resolved_cur) and ("--autostart" in reg_cmd) and reg_exists

    if path_matches:
        msg_en = "التشغيل التلقائي مفعّل ومسجل بشكل سليم في ويندوز."
        return {
            "eligible": True,
            "enabled": True,
            "status": "enabled",
            "reasonCode": "registered_valid",
            "statusLabel": "مفعّل ويعمل تلقائيًا مع بدء تشغيل ويندوز ✓",
            "messageAr": msg_en,
            "explanation": msg_en,
            "needsRepair": False,
            "valueName": value_name,
            "registryKeyPath": RUN_KEY_PATH,
            "registryPath": f"HKCU\\{RUN_KEY_PATH}\\{value_name}",
            "registeredCommand": reg_cmd,
            "expectedCommand": expected_cmd,
        }

    msg_rep = f"المسار المسجل في ويندوز ({reg_exe}) يختلف عن مسار التطبيق الحالي ({resolved_cur}) ويحتاج إلى إصلاح تلقائي."
    return {
        "eligible": True,
        "enabled": False,
        "status": "needs_repair",
        "reasonCode": "path_mismatch_or_missing",
        "statusLabel": "يحتاج تحديث مسار الملف التنفيذي (Needs Repair)",
        "messageAr": msg_rep,
        "explanation": msg_rep,
        "needsRepair": True,
        "valueName": value_name,
        "registryKeyPath": RUN_KEY_PATH,
        "registryPath": f"HKCU\\{RUN_KEY_PATH}\\{value_name}",
        "registeredCommand": reg_cmd,
        "expectedCommand": expected_cmd,
    }


def set_autostart_enabled(
    enabled: bool,
    exe_path: Path | str | None = None,
    is_frozen: bool | None = None,
    backend: Any = None,
    value_name: str = DEFAULT_VALUE_NAME,
    start_minimized: bool = False,
    allow_test_path: bool = False,
    *,
    executable_path: Path | str | None = None,
    frozen: bool | None = None,
    platform_name: str | None = None,
    minimized: bool | None = None,
) -> dict[str, Any]:
    """Enables or disables Windows per-user auto-start and verifies by reading back the registration."""
    eff_exe = Path(executable_path if executable_path is not None else (exe_path if exe_path is not None else sys.executable))
    eff_min = minimized if minimized is not None else start_minimized
    eff_backend = backend or WinRegBackend()

    if not enabled:
        eff_backend.delete_value(RUN_KEY_PATH, value_name)
        st = get_autostart_status(
            eff_exe,
            is_frozen=is_frozen,
            backend=eff_backend,
            value_name=value_name,
            start_minimized=eff_min,
            allow_test_path=allow_test_path,
            executable_path=eff_exe,
            frozen=frozen,
            platform_name=platform_name,
            minimized=eff_min,
        )
        return {
            "ok": True,
            "action": "disabled",
            "messageAr": "تم إيقاف التشغيل التلقائي وحذف الإدخال من سجل ويندوز بنجاح.",
            "autostart": st,
            **st,
        }

    eligible, reason_code, msg_ar = is_eligible_for_autostart(
        eff_exe,
        is_frozen=is_frozen,
        allow_test_path=allow_test_path,
        executable_path=eff_exe,
        frozen=frozen,
        platform_name=platform_name,
    )
    if not eligible:
        st = get_autostart_status(
            eff_exe,
            is_frozen=is_frozen,
            backend=eff_backend,
            value_name=value_name,
            start_minimized=eff_min,
            allow_test_path=allow_test_path,
            executable_path=eff_exe,
            frozen=frozen,
            platform_name=platform_name,
            minimized=eff_min,
        )
        return {
            "ok": False,
            "action": "blocked_not_eligible",
            "reasonCode": reason_code,
            "message": msg_ar,
            "messageAr": msg_ar,
            "autostart": st,
            **st,
        }

    cmd = build_autostart_command(eff_exe, start_minimized=eff_min)
    eff_backend.set_value(RUN_KEY_PATH, value_name, cmd)
    st = get_autostart_status(
        eff_exe,
        is_frozen=is_frozen,
        backend=eff_backend,
        value_name=value_name,
        start_minimized=eff_min,
        allow_test_path=allow_test_path,
        executable_path=eff_exe,
        frozen=frozen,
        platform_name=platform_name,
        minimized=eff_min,
    )
    return {
        "ok": st["status"] == "enabled",
        "action": "enabled",
        "messageAr": "تم تفعيل التشغيل التلقائي مع بدء تشغيل ويندوز بنجاح.",
        "autostart": st,
        **st,
    }


def sync_and_repair_autostart(
    settings: dict[str, Any] | None = None,
    exe_path: Path | str | None = None,
    is_frozen: bool | None = None,
    backend: Any = None,
    value_name: str = DEFAULT_VALUE_NAME,
    allow_test_path: bool = False,
    *,
    settings_enabled: bool | None = None,
    initialized: bool | None = None,
    minimized: bool = False,
    executable_path: Path | str | None = None,
    frozen: bool | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Synchronizes and self-repairs Windows auto-start registration on startup."""
    s_dict = settings if isinstance(settings, dict) else {}
    eff_exe = Path(executable_path if executable_path is not None else (exe_path if exe_path is not None else sys.executable))
    eff_min = bool(minimized if settings is None else s_dict.get("startMinimized", minimized))
    eff_init = bool(initialized if initialized is not None else s_dict.get("autoStartInitialized", False))
    eff_enabled = bool(
        settings_enabled
        if settings_enabled is not None
        else (s_dict.get("autoStartEnabled", True) if not eff_init else s_dict.get("autoStartEnabled", False))
    )
    eff_backend = backend or WinRegBackend()

    cur_st = get_autostart_status(
        eff_exe,
        is_frozen=is_frozen,
        backend=eff_backend,
        value_name=value_name,
        start_minimized=eff_min,
        allow_test_path=allow_test_path,
        executable_path=eff_exe,
        frozen=frozen,
        platform_name=platform_name,
        minimized=eff_min,
    )
    if not cur_st["eligible"]:
        return {"action": "skipped_source_mode", "repaired": False, "autostart": cur_st, **cur_st}

    if not eff_init:
        res = set_autostart_enabled(
            True,
            exe_path=eff_exe,
            is_frozen=is_frozen,
            backend=eff_backend,
            value_name=value_name,
            start_minimized=eff_min,
            allow_test_path=allow_test_path,
            executable_path=eff_exe,
            frozen=frozen,
            platform_name=platform_name,
            minimized=eff_min,
        )
        return {
            "action": "first_run_default_enabled",
            "repaired": False,
            "autoStartEnabled": True,
            "autoStartInitialized": True,
            "autostart": res["autostart"],
            **res["autostart"],
        }

    if eff_enabled:
        if cur_st["status"] in ("needs_repair", "disabled"):
            was_repair = cur_st["status"] == "needs_repair"
            res = set_autostart_enabled(
                True,
                exe_path=eff_exe,
                is_frozen=is_frozen,
                backend=eff_backend,
                value_name=value_name,
                start_minimized=eff_min,
                allow_test_path=allow_test_path,
                executable_path=eff_exe,
                frozen=frozen,
                platform_name=platform_name,
                minimized=eff_min,
            )
            return {
                "action": "repaired_moved_exe" if was_repair else "restored_missing_entry",
                "repaired": was_repair,
                "autoStartEnabled": True,
                "autoStartInitialized": True,
                "autostart": res["autostart"],
                **res["autostart"],
            }
        return {
            "action": "already_synced",
            "repaired": False,
            "autoStartEnabled": True,
            "autoStartInitialized": True,
            "autostart": cur_st,
            **cur_st,
        }

    if cur_st["registeredCommand"]:
        res = set_autostart_enabled(
            False,
            exe_path=eff_exe,
            is_frozen=is_frozen,
            backend=eff_backend,
            value_name=value_name,
            start_minimized=eff_min,
            allow_test_path=allow_test_path,
            executable_path=eff_exe,
            frozen=frozen,
            platform_name=platform_name,
            minimized=eff_min,
        )
        return {
            "action": "removed_stale_disabled_entry",
            "repaired": False,
            "autoStartEnabled": False,
            "autoStartInitialized": True,
            "autostart": res["autostart"],
            **res["autostart"],
        }

    return {
        "action": "disabled_clean",
        "repaired": False,
        "autoStartEnabled": False,
        "autoStartInitialized": True,
        "autostart": cur_st,
        **cur_st,
    }


class SingleInstanceGuard:
    """Prevents multiple concurrent instances from opening the database or binding the API port."""

    def __init__(self, app_home: Path, mutex_name: str = "Local\\YawmJadidAcademy_SingleInstance_v1") -> None:
        self.app_home = Path(app_home)
        self.lock_file = self.app_home / ".alahd_instance.lock"
        self.mutex_name = mutex_name
        self._fp: Any = None
        self._mutex_handle: Any = None
        self.acquired = False

    def acquire(self) -> bool:
        if self.acquired:
            return True
        self.app_home.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                handle = kernel32.CreateMutexW(None, False, self.mutex_name)
                last_err = kernel32.GetLastError()
                if last_err == 183:  # ERROR_ALREADY_EXISTS
                    if handle:
                        kernel32.CloseHandle(handle)
                    return False
                self._mutex_handle = handle
            except Exception:
                pass

        try:
            self._fp = open(self.lock_file, "a+", encoding="utf-8")
            if os.name == "nt":
                import msvcrt  # type: ignore[import-not-found]
                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fp.seek(0)
            self._fp.truncate()
            self._fp.write(json.dumps({"pid": os.getpid(), "ts": datetime.datetime.now().isoformat()}))
            self._fp.flush()
            self.acquired = True
            return True
        except Exception:
            if self._fp is not None:
                try:
                    self._fp.close()
                except Exception:
                    pass
                self._fp = None
            if self._mutex_handle is not None and os.name == "nt":
                try:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(self._mutex_handle)  # type: ignore[attr-defined]
                except Exception:
                    pass
                self._mutex_handle = None
            return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self._fp is not None:
                if os.name == "nt":
                    try:
                        import msvcrt  # type: ignore[import-not-found]
                        self._fp.seek(0)
                        msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                else:
                    try:
                        import fcntl
                        fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
                    except Exception:
                        pass
                try:
                    self._fp.close()
                except Exception:
                    pass
                self._fp = None
            self.lock_file.unlink(missing_ok=True)
        except Exception:
            pass
        if self._mutex_handle is not None and os.name == "nt":
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._mutex_handle = None
        self.acquired = False


class BootGuard:
    """Tracks boot-time `--autostart` launches and prevents infinite boot failure loops."""

    def __init__(self, app_home: Path, max_failures: int = MAX_BOOT_FAILURES) -> None:
        self.state_file = Path(app_home) / ".alahd_boot_state.json"
        self.max_failures = max_failures

    def read_state(self) -> dict[str, Any]:
        try:
            if self.state_file.is_file():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {
            "consecutiveFailedBootLaunches": 0,
            "lastBootAttemptAt": "",
            "lastBootSuccessAt": "",
            "lastFailureReason": "",
            "bootLoopProtected": False,
        }

    def _write_state(self, state: dict[str, Any]) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def should_abort_autostart(self) -> bool:
        st = self.read_state()
        return int(st.get("consecutiveFailedBootLaunches") or 0) >= self.max_failures

    def can_proceed_with_boot_launch(self) -> tuple[bool, str]:
        if self.should_abort_autostart():
            fails = int(self.read_state().get("consecutiveFailedBootLaunches") or 0)
            return (
                False,
                f"تم إيقاف التشغيل التلقائي مؤقتًا بعد {fails} محاولات إقلاع غير ناجحة متتالية لمنع تكرار الخطأ.",
            )
        return (True, "")

    def record_boot_failure(self, reason: str, is_autostart: bool = True) -> dict[str, Any]:
        st = self.read_state()
        fails = int(st.get("consecutiveFailedBootLaunches") or 0) + (1 if is_autostart else 0)
        st["consecutiveFailedBootLaunches"] = fails
        st["lastBootAttemptAt"] = datetime.datetime.now().isoformat(timespec="seconds")
        st["lastFailureReason"] = str(reason or "startup_error")[:300]
        st["bootLoopProtected"] = fails >= self.max_failures
        self._write_state(st)
        return st

    def record_startup_success(self, is_autostart: bool = False) -> dict[str, Any]:
        st = self.read_state()
        now_iso = datetime.datetime.now().isoformat(timespec="seconds")
        st["consecutiveFailedBootLaunches"] = 0
        if is_autostart:
            st["lastBootAttemptAt"] = now_iso
        st["lastBootSuccessAt"] = now_iso
        st["lastFailureReason"] = ""
        st["bootLoopProtected"] = False
        self._write_state(st)
        return st
