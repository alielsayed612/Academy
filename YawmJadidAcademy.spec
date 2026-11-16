# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows one-folder build.   Build:  pyinstaller YawmJadidAcademy.spec --noconfirm
# v1.2.0 hardening (session 9): + webview/clr_loader data+DLLs, anyio backends. After building run scripts/smoke_exe.ps1.
# config.resolve_ui_dir() looks in BUNDLE_DIR/"alahd_erp"/"ui" (3rd candidate), hence the datas target below.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


def _safe(fn, pkg):
    """Defensive collection: a missing optional package must not abort the whole build."""
    try:
        return fn(pkg)
    except Exception as exc:  # noqa: BLE001
        print(f"[spec] skipped {fn.__name__}({pkg!r}): {exc}")
        return []


hidden = (
    collect_submodules("uvicorn")          # uvicorn loads loops/protocols/lifespan dynamically
    + _safe(collect_submodules, "webview")  # pywebview selects its GUI backend at runtime
    + _safe(collect_submodules, "anyio")   # anyio picks its asyncio backend via importlib
    + _safe(collect_submodules, "clr_loader")  # pywebview/WinForms -> pythonnet -> clr_loader (Windows)
    + [
        "openpyxl",
        "clr",
        "winreg",
        "msvcrt",
        "alahd_erp.infrastructure.autostart",
        "alahd_erp.domain.settings_registry",
        "alahd_erp.domain.demo_cleaner",
        "alahd_erp.domain.academy_data_loader",
    ]
)
# pywebview ships WebView2 / .NET DLLs as package data; pythonnet + clr_loader ship native loaders.
extra_datas = (
    _safe(collect_data_files, "webview")
    + _safe(collect_data_files, "clr_loader")
    + _safe(collect_data_files, "pythonnet")
)
extra_bins = _safe(collect_dynamic_libs, "webview") + _safe(collect_dynamic_libs, "clr_loader")

a = Analysis(
    ["alahd_erp/main.py"],
    pathex=["."],
    binaries=extra_bins,
    datas=[("alahd_erp/ui", "alahd_erp/ui")] + extra_datas,   # UI assets (vendored Cairo font + JS libs, offline-first) + webview/.NET data
    hiddenimports=hidden,
    excludes=["tkinter", "tests", "mypy", "ruff"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="YawmJadidAcademy",                     # == alahd_erp.EXECUTABLE_NAME
    console=False,
    icon="alahd_erp/icon.ico",
    version="installer/version_info.txt",        # File properties shown in Explorer > Properties > Details
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="YawmJadidAcademy", upx=False)
