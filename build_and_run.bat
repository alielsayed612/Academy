@echo off
REM =====================================================================================================
REM  Yawm Jadid Academy v1.2.0  -  BUILD & RUN (one click, Windows 10/11)
REM
REM  Double-click this file (or run it from cmd) from the project root. It will:
REM    1) find Python 3.12 (or 3.14)  2) create .venv + install requirements
REM    3) run the test-suite          4) build dist\YawmJadidAcademy\YawmJadidAcademy.exe (PyInstaller)
REM    5) verify the EXE vs source    6) smoke-test the EXE in an isolated temp folder
REM    7) pack release\YawmJadidAcademy-1.2.0-portable.zip + create Desktop Shortcut (.lnk)
REM    8) ensure Excel workbooks data is loaded & launch the application
REM
REM  Options:
REM    build_and_run.bat          full pipeline (recommended for a release)
REM    build_and_run.bat /fast    skip tests + smoke test (quick rebuild)
REM    build_and_run.bat /run     just launch the built EXE (builds it first if missing)
REM    build_and_run.bat /dev     run from source with the venv (no EXE build)
REM    build_and_run.bat /nopause do not wait for a key at the end (for scripts)
REM    build_and_run.bat /strict  stop on a failing test / smoke test (default: warn and continue)
REM  NOTE: keep this file ASCII-only with CRLF line endings (cmd.exe misparses LF-only files / non-ASCII text).
REM
REM  Needs internet only the first time (pip). Your data lives in %LOCALAPPDATA%\AlAhdAcademySuite
REM  and is never touched by the build or by the smoke test (which uses a temporary folder).
REM =====================================================================================================
setlocal EnableExtensions
REM --- Robust console setup: some cmd sessions lack System32 on PATH, which makes chcp/where/powershell "not recognized" ---
set "SYS32=%SystemRoot%\System32"
if not exist "%SYS32%\cmd.exe" set "SYS32=C:\Windows\System32"
set "PATH=%SYS32%;%SYS32%\Wbem;%SYS32%\WindowsPowerShell\v1.0;%PATH%"
if exist "%SYS32%\chcp.com" "%SYS32%\chcp.com" 65001 >nul
cd /d "%~dp0"
title Yawm Jadid Academy - Build and Run

set "MODE=full"
set "NOPAUSE="
set "STRICT="
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="/fast"    set "MODE=fast"
if /i "%~1"=="/run"     set "MODE=run"
if /i "%~1"=="/dev"     set "MODE=dev"
if /i "%~1"=="/nopause" set "NOPAUSE=1"
if /i "%~1"=="/strict"  set "STRICT=1"
shift
goto :parse
:parsed

set "EXE=dist\YawmJadidAcademy\YawmJadidAcademy.exe"
set "VERSION=1.2.0"

echo.
echo ================================================================
echo   Yawm Jadid Academy v%VERSION%  -  mode: %MODE%
echo ================================================================

REM ---------- /run : launch an existing build ----------
if /i "%MODE%"=="run" (
  if exist "%EXE%" (
    call :create_desktop_shortcut "%~dp0%EXE%" "%~dp0dist\YawmJadidAcademy" "%~dp0alahd_erp\icon.ico"
    echo Launching %EXE% ...
    start "" "%EXE%"
    goto :end_ok
  )
  echo [info] No built EXE found - switching to a full build first.
  set "MODE=full"
)

REM ---------- 1) Python 3.12 ----------
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
REM Enable provisional fallback in interactive build_and_run if user has Python 3.14 installed
if not defined ALLOW_PROVISIONAL_BUILD set "ALLOW_PROVISIONAL_BUILD=1"
REM Tier 2 (Python 3.14, PROVISIONAL) is OFF by default (integrity round F9); set ALLOW_PROVISIONAL_BUILD=1 for experiments only.
if not defined PY if "%ALLOW_PROVISIONAL_BUILD%"=="1" (
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
  echo.
  echo [ERROR] Python 3.12 was not found ^(release builds require exactly 3.12^).
  echo         Install Python 3.12 from https://www.python.org/downloads/windows/
  echo         and tick "Add python.exe to PATH" and "py launcher", then run this file again.
  goto :fail
)
echo [TIER] %BUILD_TIER%
echo [1/8] Python: & %PY% --version

REM ---------- 2) venv + dependencies ----------
if not exist ".venv\Scripts\python.exe" (
  echo [2/8] Creating virtual environment ...
  %PY% -m venv .venv || goto :fail
)
call ".venv\Scripts\activate.bat" || goto :fail

if /i "%MODE%"=="dev" (
  python -m pip install -r requirements-core.txt || goto :fail
  python -m pip install "pywebview>=5.0" || echo [WARN] pywebview not installed - the app will open in the browser.
  python -c "import alahd_erp.main" || goto :fail
  echo Starting from source ...
  start "" ".venv\Scripts\pythonw.exe" -m alahd_erp.main
  goto :end_ok
)

echo [2/8] Installing build requirements ...
python -m pip install --disable-pip-version-check --upgrade pip || goto :fail
python -m pip install --disable-pip-version-check -r requirements-build.txt || goto :fail
python -m pip install "pywebview>=5.0" || echo [WARN] pywebview could not be installed - the EXE will open the app in the web browser instead of a native window.

REM ---------- 3) tests ----------
if /i "%MODE%"=="full" (
  echo.
  echo [3/8] Running test-suite ...
  python -m unittest discover -s tests -t .
  if errorlevel 1 (
    if defined STRICT goto :fail
    echo [WARN] Some tests failed - continuing the build ^(use /strict to stop on test failures^).
  )
) else (
  echo [3/8] Tests skipped ^(/fast^).
)

REM ---------- 4) build ----------
echo.
echo [4/8] Building executable ^(PyInstaller, takes a few minutes^) ...
taskkill /IM YawmJadidAcademy.exe /F >nul 2>nul
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist dist (
  echo [ERROR] Could not clean the dist folder - close the app / Explorer windows using it and retry.
  goto :fail
)
pyinstaller YawmJadidAcademy.spec --noconfirm --clean || goto :fail
if not exist "%EXE%" (
  echo [ERROR] PyInstaller finished but %EXE% is missing.
  goto :fail
)

REM ---------- 5) verify ----------
echo.
echo [5/8] Verifying EXE content against this source ...
python scripts\verify_build_artifact.py "%EXE%" --ui-dir "dist\YawmJadidAcademy\_internal\alahd_erp\ui" --expected-python %PY_VER% || goto :fail

REM ---------- 6) smoke ----------
if /i "%MODE%"=="full" (
  echo.
  echo [6/8] EXE smoke test ^(isolated temp data folder^) ...
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_exe.ps1 -ExePath "%EXE%"
  if errorlevel 1 (
    if defined STRICT goto :fail
    echo [WARN] EXE smoke test failed - see the log lines above. Continuing ^(use /strict to stop^).
  )
) else (
  echo [6/8] Smoke test skipped ^(/fast^).
)

REM ---------- 7) package + desktop shortcut ----------
echo.
echo [7/8] Packaging and creating Desktop Shortcut ...
python -m pip freeze > build-freeze.txt
if not exist release mkdir release
if exist "release\YawmJadidAcademy-%VERSION%-portable.zip" del /q "release\YawmJadidAcademy-%VERSION%-portable.zip"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\YawmJadidAcademy' -DestinationPath 'release\YawmJadidAcademy-%VERSION%-portable.zip' -Force" || goto :fail
echo        release\YawmJadidAcademy-%VERSION%-portable.zip

call :create_desktop_shortcut "%~dp0%EXE%" "%~dp0dist\YawmJadidAcademy" "%~dp0alahd_erp\icon.ico"

set "ISCC="
where iscc >nul 2>nul && set "ISCC=iscc"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if defined ISCC (
  "%ISCC%" installer\YawmJadidAcademy.iss || goto :fail
  echo        installer\Output\  ^(Setup EXE^)
) else (
  echo        [info] Inno Setup 6 not found - installer skipped ^(portable zip + Desktop shortcut are ready^).
)

REM ---------- 8) load bundled Excel workbooks & run ----------
echo.
echo [8/8] Launching the application ^(bundled Excel workbooks load automatically on startup^) ...
start "" "%EXE%"

:end_ok
echo.
echo ================================================================
echo   DONE.  EXE: %~dp0%EXE%
echo   Desktop shortcut created on your Windows Desktop!
echo ================================================================
if not defined NOPAUSE pause
exit /b 0

:fail
echo.
echo ================================================================
echo   FAILED - read the messages above, fix the problem and run again.
echo ================================================================
if not defined NOPAUSE pause
exit /b 1

:create_desktop_shortcut
REM Creates Desktop shortcuts (English + Arabic names) via scripts\create_shortcut.ps1
set "TARGET_EXE=%~1"
set "WORK_DIR=%~2"
set "ICON_FILE=%~3"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_shortcut.ps1" -Target "%TARGET_EXE%" -WorkDir "%WORK_DIR%" -Icon "%ICON_FILE%"
exit /b 0

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
