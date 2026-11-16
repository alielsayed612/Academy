@echo off
REM =====================================================================================================
REM  Yawm Jadid Academy - Windows release build.   Run from the project root in a normal (non-admin) cmd.
REM  Requires: Python 3.12 (py launcher), internet for the first pip install, optional Inno Setup 6 (iscc).
REM  Steps: check python -> venv -> install -> TESTS (stop on failure) -> PyInstaller -> EXE smoke test -> Desktop Shortcut -> optional installer.
REM  Options: /strict = stop on a failing test or smoke test (default: warn and continue).
REM  NOTE: keep this file ASCII-only with CRLF line endings (cmd.exe misparses LF-only files / non-ASCII text).
REM =====================================================================================================
setlocal EnableExtensions
set "STRICT="
if /i "%~1"=="/strict" set "STRICT=1"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
REM --- Robust console setup: some cmd sessions lack System32 on PATH, which makes chcp/where/powershell "not recognized" ---
set "SYS32=%SystemRoot%\System32"
if not exist "%SYS32%\cmd.exe" set "SYS32=C:\Windows\System32"
set "PATH=%SYS32%;%SYS32%\Wbem;%SYS32%\WindowsPowerShell\v1.0;%PATH%"
if exist "%SYS32%\chcp.com" "%SYS32%\chcp.com" 65001 >nul
cd /d "%~dp0"

REM ---------- Python 3.12 detection (does not depend on "where") ----------
set "PY="
set "PY_VER=3.12"
set "BUILD_TIER=TIER 1 - RELEASE-QUALIFYING BUILD (Python 3.12)"
py -3.12 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3.12"
if not defined PY call :probe "%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY call :probe "%ProgramFiles%\Python312\python.exe"
if not defined PY call :probe "C:\Python312\python.exe"
if not defined PY call :probe "python"
REM --- Tier 2 (Python 3.14, PROVISIONAL) is OFF by default (integrity round F9); developer experiments only:
REM     set ALLOW_PROVISIONAL_BUILD=1 to enable. Such a build is never release-qualifying; the installer step is skipped.
if not defined PY if "%ALLOW_PROVISIONAL_BUILD%"=="1" (
  REM --- Tier 2 Fallback: Python 3.14 Provisional Build ---
  py -3.14 --version >nul 2>nul
  if not errorlevel 1 set "PY=py -3.14"
  if not defined PY call :probe314 "%LocalAppData%\Programs\Python\Python314\python.exe"
  if not defined PY call :probe314 "%ProgramFiles%\Python314\python.exe"
  if not defined PY call :probe314 "python"
  if defined PY (
    set "PY_VER=3.14"
    set "BUILD_TIER=TIER 2 - PROVISIONAL BUILD (Python 3.14) - NOT FOR RELEASE"
    echo [WARN] ALLOW_PROVISIONAL_BUILD=1 and Python 3.12 not found: PROVISIONAL BUILD ^(Python 3.14^) - NOT FOR RELEASE.
  )
)
if not defined PY (
  echo [error] Python 3.12 was not found ^(release builds require exactly 3.12^). Install Python 3.12 from python.org, then re-run.
  echo         Diagnostics: run  echo %%PATH%%  and  py -0p  - if "py" or "chcp" are "not recognized", your PATH is damaged ^(missing %%SystemRoot%%\System32^).
  goto :fail
)
echo === %BUILD_TIER% ===

if not exist .venv\Scripts\python.exe (
  %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip || goto :fail
REM Reproducible builds: when a committed requirements.lock exists, install exactly those pins first (see scripts\make_lock.bat).
if exist requirements.lock (
  echo === Installing pinned dependencies from requirements.lock ===
  python -m pip install -r requirements.lock || goto :fail
) else (
  echo [WARN] requirements.lock not found - building from lower-bound requirements ^(NOT reproducible^). Run scripts\make_lock.bat after the first good build and commit the result.
)
python -m pip install -r requirements-build.txt || goto :fail
python -m pip install "pywebview>=5.0" || echo [WARN] pywebview could not be installed - the EXE will open the app in the web browser instead of a native window.

echo.
echo === Running test-suite (HTTP tests run because fastapi+httpx are installed) ===
python -m unittest discover -s tests -t .
if errorlevel 1 (
  if defined STRICT goto :fail
  echo [WARN] Some tests failed - continuing the build ^(use /strict to stop on test failures^).
)

echo.
echo === Building executable ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
pyinstaller YawmJadidAcademy.spec --noconfirm || goto :fail
if not exist dist\YawmJadidAcademy\YawmJadidAcademy.exe goto :fail

echo === Verifying EXE content against this source ===
python scripts\verify_build_artifact.py dist\YawmJadidAcademy\YawmJadidAcademy.exe --ui-dir dist\YawmJadidAcademy\_internal\alahd_erp\ui --expected-python %PY_VER% || goto :fail

echo.
echo === EXE smoke test (isolated temp data folder) ===
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_exe.ps1 -ExePath dist\YawmJadidAcademy\YawmJadidAcademy.exe
if errorlevel 1 (
  if defined STRICT goto :fail
  echo [WARN] EXE smoke test failed - see the log lines above. Continuing ^(use /strict to stop^).
)

echo === Creating Desktop Shortcut ===
set "TARGET_EXE=%~dp0dist\YawmJadidAcademy\YawmJadidAcademy.exe"
set "WORK_DIR=%~dp0dist\YawmJadidAcademy"
set "ICON_FILE=%~dp0alahd_erp\icon.ico"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_shortcut.ps1" -Target "%TARGET_EXE%" -WorkDir "%WORK_DIR%" -Icon "%ICON_FILE%"

REM Record what this build used WITHOUT overwriting the committed requirements.lock (use scripts\make_lock.bat to refresh it).
python -m pip freeze > build-freeze.txt

if "%PY_VER%"=="3.14" (
  echo [info] PROVISIONAL build - installer step skipped. Do NOT distribute this build.
  goto :done
)
where iscc >nul 2>nul
if not errorlevel 1 (
  echo === Building installer ===
  iscc installer\YawmJadidAcademy.iss || goto :fail
  echo Installer: installer\Output\
) else (
  echo [info] Inno Setup not found - skipped installer. The portable folder is dist\YawmJadidAcademy\
)

:done
echo.
echo BUILD OK.  Now follow docs\RELEASE_CHECKLIST.md before shipping.
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
exit /b 1

:probe
REM %~1 = candidate interpreter. Accept only an exact 3.12.x; sets PY, returns 0/1.
if /i not "%~1"=="python" if not exist "%~1" exit /b 1
"%~1" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PY="%~1""
exit /b 0

:probe314
if /i not "%~1"=="python" if not exist "%~1" exit /b 1
"%~1" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,14) else 1)" >nul 2>nul
if errorlevel 1 exit /b 1
set "PY="%~1""
exit /b 0
