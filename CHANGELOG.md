# Changelog

## 1.2.1 - Windows launcher fixes
- Rewrote START_ACADEMY.bat / build scripts: ASCII + CRLF, optional pywebview, pre-flight check, /strict flag.
- SQLite read-only URIs built with Path.as_uri(); loopback probe bypasses system proxy.
- Removed heavy handoff/report docs. See CLAUDE_WORK_REPORT.md.

## 1.2.0 — 2026-11-14 — "Yawm Jadid" identity + production hardening
**Identity (أكاديمية يوم جديد — «يوم جديد هو عهد جديد»)**
- Name, tagline, logo (circular, transparent), banner, multi-size `icon.ico` (16–256), favicon, sage-olive/cream palette (light + dark),
  PIN-lock logo, welcome banner, Excel/HTML/certificate colours and default file-name prefixes (`YawmJadid_*`).
- Executable is now `YawmJadidAcademy.exe`. **The data folder stays `%LOCALAPPDATA%\AlAhdAcademySuite`** (`DATA_DIR_NAME`) so
  existing installs keep every record. Existing databases keep the academy name already saved in Settings.
**Security**
- F-03: the per-launch window secret is no longer accepted from the query string on API/static routes (header or httpOnly cookie
  only); `?wsec=` works on bootstrap paths only, and the UI removes it from the address bar right after load.
**Backups**
- DEFECT-B1: backups/restores no longer leave `*.part-wal`/`*.part-shm`/`-wal`/`-shm` files next to backup files; leftovers from
  <= 1.1.0 are swept on the next backup; `rotate()` removes companions of deleted backups.
**Export / Import fixes**
- Excel export (`full` and `teacher` scopes) no longer aborts when a teacher's name contains a character Excel forbids in sheet
  titles (`\ / ? * [ ] :`, e.g. «أ/ فاطمة»); such characters become `-`, edge apostrophes are dropped, valid names are unchanged.
- The import error-report workbook now writes text beginning with `=` (reasons, uploaded file name) as plain text, never a live formula.
**Build kit**: `YawmJadidAcademy.spec`, `installer/version_info.txt`, `installer/YawmJadidAcademy.iss`, `build_windows.bat`,
`requirements*.txt`, `docs/RELEASE_CHECKLIST.md`.
**Tests**: Python↔JS finance parity, WAL/backup/reset, F-03 frontend + HTTP, branding/packaging consistency, import/export (37).
**Build & Run**: `build_and_run.bat` (one click: venv → tests → PyInstaller → verify → smoke → portable zip → launch; flags `/fast` `/run` `/dev` `/nopause`).
**Session 13 (QA run: 145 engine scenarios + 67 real-browser UI checks)**
- FIX: the PIN lock screen never appeared after restarting the app (UI read `pinEnabled`, server sends `pinRequired`) — `ui/js/modules/store.js`; regression test `tests/test_pin_state_contract.py`.
- FIX: a student name > 120 chars or phone > 40 chars produced a generic "internal error"; now a clear Arabic validation message (`Store._put_student_in_tx`), and any other DB CHECK/UNIQUE violation is returned as HTTP 400 instead of 500 (`api/server.py`; needs the HTTP suite on Windows to re-confirm).
- ADD: `scripts/qa/engine_scenarios.py <root>` and `scripts/qa/ui_smoke.py <root> <outdir>` (need Playwright; UI smoke answers /api from the real Store, so it works without FastAPI).
**Known / unchanged**: finance parity divergence PARITY-1 (see docs/ISSUES.md) awaits a business decision; nothing in the
finance rules was changed.
