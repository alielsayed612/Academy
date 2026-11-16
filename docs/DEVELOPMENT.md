# Development & Build Guide
Target: Windows 10/11, Python 3.12. Dev tools are never bundled into the EXE and the app never needs internet at runtime.

## Dependencies
Runtime (from the real imports in `alahd_erp/`): `fastapi`, `uvicorn`, `pydantic`, `pywebview`, `openpyxl`. Extras: `dev` = `ruff`, `mypy`, `httpx`; `build` = `pyinstaller`
(`requirements.txt` / `requirements-build.txt` carry lower bounds only). Tests are plain `unittest`; only `test_http_api.py` / `test_http_integrity.py` need `fastapi` + `httpx`
(skipped without them) and the JS-backed tests use Node when present.
Versions are **unpinned**. After the first good build on the Windows machine: `pip freeze > requirements.lock` and commit it (NOT DONE, issue DEP-1).

## Setup (networked dev machine)
```
python -m venv .venv && .venv\Scripts\activate
pip install -e .[dev,build]
```

## Commands and last verified status (details: `TESTING.md`, `ISSUES.md`)
| Task | Command | Status |
|---|---|---|
| Tests | `python -X dev -W default -m unittest discover -s tests -t .` | 307 OK, 55 skipped, 7 expected failures (Session 11, no fastapi/httpx) |
| Lint | `ruff check .` | 0 findings at the end of Session 7 (not re-run in Session 8: tool not installed) |
| Types | `mypy` (domain + config) and `mypy alahd_erp` | 0 errors at the end of Session 7 (same caveat) |
| Package metadata/wheel | `python -m pip wheel . --no-deps -w dist` | verified once offline with `--no-build-isolation` (Phase 0, v1.1.0) |
| Route table | `python scripts/route_inventory.py > routes.md` | works (AST based) |
| Recovery archive | `scripts/make_checkpoint.sh <label>` | zip + `.sha256` + `unzip -t` verification into `CHECKPOINTS/` (git-ignored) |
| Dashboard converter | `python -m alahd_erp.tools.excel_converter <xlsx> -o <dir> [--paid none\|full]` | verified on the real file (Session 7) |
| Run from source | `python -m alahd_erp.main` | NOT VERIFIED |
| Windows EXE | `build_windows.bat` (tests → `pyinstaller YawmJadidAcademy.spec --noconfirm` → Inno Setup if present) | NOT VERIFIED (never built on Windows) |

Env vars: `ALAHD_ERP_HOME` (data dir), `ALAHD_ERP_PORT` (default 8890), `ALAHD_ERP_NO_UI`. Production data lives in `%LOCALAPPDATA%\AlAhdAcademySuite` (never rename); tests use temporary directories.

## Rules
Never test against the production database. Run the full suite before and after every change. Explore the real behaviour first, then pin it in a test; an
`expectedFailure` must be confirmed to fail for the documented reason. After every session update `docs/ISSUES.md` and `docs/IMPLEMENTATION_LOG.md`, and ship a new
zip with its `.sha256`. Keep CI lint/mypy advisory until CI has run once (CI-1).
