@echo off
REM ============================================================================
REM  Yawm Jadid Academy - one-click launcher (runs from source, no EXE build).
REM  First run : creates .venv and installs libraries (internet needed once).
REM  Next runs : opens the app directly, fully offline.
REM  Needs     : Python 3.12 or newer from python.org (tick "Add python.exe to PATH").
REM  Your data : %LOCALAPPDATA%\AlAhdAcademySuite   (outside this folder)
REM  NOTE      : keep this file ASCII-only with CRLF line endings (cmd.exe breaks otherwise).
REM ============================================================================
setlocal EnableExtensions
set "SYS32=%SystemRoot%\System32"
if not exist "%SYS32%\cmd.exe" set "SYS32=C:\Windows\System32"
set "PATH=%SYS32%;%SYS32%\Wbem;%SYS32%\WindowsPowerShell\v1.0;%PATH%"
cd /d "%~dp0"
title Yawm Jadid Academy
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

if not exist "alahd_erp\main.py" (
  echo [ERROR] alahd_erp\main.py not found. Extract the whole ZIP first, then run this file from the extracted folder.
  goto :fail
)

REM ---------- find Python >= 3.12 ----------
set "PY="
py -3.12 -c "import sys" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>nul && set "PY=py -3.13"
if not defined PY py -3.14 -c "import sys" >nul 2>nul && set "PY=py -3.14"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info>=(3,12) else 1)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo [ERROR] Python 3.12 or newer was not found.
  echo         Install it from https://www.python.org/downloads/windows/
  echo         and tick "Add python.exe to PATH", then run this file again.
  goto :fail
)

REM ---------- virtual environment (recreated if broken) ----------
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul || rmdir /s /q ".venv"
)
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating environment ...
  if exist ".venv" rmdir /s /q ".venv"
  %PY% -m venv .venv || goto :fail_setup
)

REM ---------- libraries ----------
if not exist ".venv\.deps_ok" (
  echo [2/3] Installing libraries ^(internet needed once^) ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>nul
  ".venv\Scripts\python.exe" -m pip install -r requirements-core.txt || goto :fail_setup
  REM pywebview (native window) is optional: if it cannot be installed the app opens in your browser instead.
  ".venv\Scripts\python.exe" -m pip install "pywebview>=5.0" || echo [WARN] pywebview not installed - the app will open in your web browser instead.
  echo ok> ".venv\.deps_ok"
)

REM ---------- pre-flight: fail loudly here instead of silently in the background ----------
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, pydantic, openpyxl, alahd_erp.main" || (
  echo.
  echo [ERROR] The application failed to load ^(details above^).
  echo         Deleting the .venv folder and running this file again usually fixes broken installs.
  goto :fail
)

echo [3/3] Starting Yawm Jadid Academy ...
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m alahd_erp.main
) else (
  start "" ".venv\Scripts\python.exe" -m alahd_erp.main
)
echo If no window appears within a minute, check: %LOCALAPPDATA%\AlAhdAcademySuite\logs\alahd_erp.log
timeout /t 4 /nobreak >nul 2>nul
exit /b 0

:fail_setup
echo.
echo [ERROR] Setup failed. Check your internet connection and try again.
:fail
echo.
echo Setup/launch failed - read the messages above.
pause
exit /b 1
