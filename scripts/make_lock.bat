@echo off
REM Generates requirements.lock on the Windows build machine (Python 3.12 only) - run AFTER a successful build_windows.bat.
REM The lock must be created on Windows/3.12 because wheels (pywebview, pythonnet, ...) are platform specific.
REM Then commit requirements.lock; build_windows.bat installs from it automatically.
setlocal
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (
  echo [error] .venv not found - run build_windows.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat || exit /b 1
python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)" || (
  echo [error] The venv is not Python 3.12 - the lock would not be release-qualifying.
  exit /b 1
)
python -m pip freeze --exclude-editable > requirements.lock || exit /b 1
echo requirements.lock written. Review and commit it.
exit /b 0
