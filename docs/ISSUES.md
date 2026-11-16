# Issue register (single source of truth)

Replaces the old `OPEN_ISSUES.md` (its status column had gone stale: it still listed F-03, Ruff/Mypy and the dashboard importer as open).
Statuses: **FIXED AND VERIFIED** · **FIXED, NOT FULLY VERIFIED** · **OPEN** · **DECISION NEEDED** (owner's call; nothing is changed until then) · **INFO** (observed, deliberately unchanged).
"Verified" means a test that was executed in the sandbox; HTTP-layer verification is marked separately because the HTTP suite needs `fastapi` + `httpx`.
State as of Session 8 (v1.2.0, **never built or run on Windows**).

## 0. Release-blocking findings on the supplied artifact (Session 11)
Evidence: `scripts/verify_build_artifact.py` run on the supplied `AlAhdAcademySuite.exe` (sha256 `cf8e2932…46aa2`, 39,169,119 bytes; extracted from `files_of_the_project_1.7z`, CRC verified) against the v1.2.0 source (`alahd_erp_v1_2_0_ready.zip`, sha256 matches its `.sha256` file). Full output in `docs/TEST_RESULTS.md`.

| ID | Severity | Finding | Status |
|---|---|---|---|
| PRIV-1 | **P0 for distribution** | The supplied EXE embeds `ui/assets/seed_state.json` (63 named students, 63 phone numbers, 62 Egyptian-mobile format) + `ui/js/seed-data.js`: almost certainly real personal data, probably of minors, extractable by anyone holding the file. The v1.2.0 *source* has no such file (`demo_data.py` is synthetic: «طالبة تجريبية N», `0100000000N`). | **OPEN – do not distribute that EXE.** Owner must confirm whether the data is real and handle it per their own policy. New guard: `verify_build_artifact.py` check D1 fails any build carrying seed/db/env/backup payloads. |
| BUILD-1 | **P1** | The supplied EXE is **not** built from the v1.2.0 source: bundled `alahd_erp.__version__` = 1.0.0 («Al-Ahd Academy ERP Suite», PE timestamp 2026-11-12); 8 PYZ entries missing (`api.security_utils`, `domain.alerts_engine`, `domain.demo_data`, `domain.statistics_engine`, `services.html_export_service`, `services.import_service`, `tools`, `tools.excel_converter`); 7 UI files differ and 9 `js/modules/*` are absent. It therefore lacks the Phase-1 security and Phase-2 integrity fixes. | **OPEN – needs a Windows rebuild** (`build_windows.bat`). Cannot be done on Linux; no Windows/PyInstaller here. Never patch the EXE. |
| BUILD-2 | P2 | That EXE bundles Python **3.14** (project targets 3.12: `build_windows.bat`, CI) and unrelated packages (lxml, opentelemetry-api, attrs, click, websockets, markupsafe, pythonnet) → built from a polluted global environment, not a clean venv. `pyproject.toml` has `requires-python = ">=3.12"` (permits 3.14, untested with pywebview/pythonnet). | Mitigated in tooling: verifier check A2 fails a non-3.12 runtime; the .bat already builds in a fresh `.venv`. Consider `<3.14` in `requires-python` after the first good Windows build. |
| DOC-1 | P3 | `HANDOFF.md`/`README.md` still said "never built/HTTP run once" while `IMPLEMENTATION_LOG.md` (earlier "Session 10") claims a full HTTP run (291 OK, 1 skipped) and a Linux PyInstaller smoke. Session 11 could not reproduce either (no fastapi/PyInstaller/network) and did not re-verify them. | Documented here; HANDOFF updated. |

## 1. Decisions needed from the owner
| ID | Severity | Issue | Options / proposal |
|---|---|---|---|
| PARITY-1 | MEDIUM, financial | Receipts array present + a student has **no receipt** that month + the month has a `paymentDate`: Python collects 500 (wage 350 in `collected` mode), JS collects 0. Python falls back to the legacy rule (`inst_map.get()` → None); JS treats receipts as the only truth (`instMap[k] \|\| []`). Pinned by `tests/test_finance_parity.py` (expectedFailure). | Which side is right? After the decision fix the other engine and delete the `expectedFailure`. Related: the Store's legacy migration already turns dated months into paid receipts (see INFO below). |
| PARITY-7 | MEDIUM, financial | `Store.stats` / `accrual_report` / `accounting_summary` ignore `settings.pctSource` and `settings.defaultTeacherPct`, while the UI engine and `alerts_engine` read them. Measured with pctSource=teacher, default 60, T1 meta 80, T2 none: server wages **800** vs UI **960**; netOperatingProfit **900** vs **740**; `salaryRemaining` also differs. With default settings (student / 70) there is no difference. Pinned by `Parity7` in `tests/test_finance_parity.py`. | Proposed fix: pass `pct_source=settings.pctSource, default_pct=settings.defaultTeacherPct` in those 3 methods (plus the Excel/HTML export paths that use them), then remove the `expectedFailure`. |
| F-11 | design | The PIN is a UI lock only; the server does not ask for it. The owner wrote "F-03 … والـ PIN" without saying what they meant. | Keep as a UI lock, or enforce on the server (changes the contract: every data route needs a session token). **Ask before touching.** |
| LATENT-1 | LOW | `database.py::export_state` does `out["dynamicMonths"] = generate_dynamic_months(out)`: the whole state is passed as `reference_date` (works by accident: falls back to today ±6 months, ignores months present in the data). Nothing reads `dynamicMonths`. Likely intent: `extra_months=` the data's months. | Changing it changes JSON content → needs approval. |
| PARITY-2..6 | LOW | Remaining Python↔JS differences, all `expectedFailure` in `tests/test_finance_parity.py`: (2) non-numeric money text — Python raises `ValueError`, JS treats as 0; (3) Arabic-Indic digits — Python accepts, JS rejects (the UI normalises before saving); (4) `null` receipts argument — Python = not passed, JS `null` ≠ `undefined` (`app.js` sends `undefined`, no UI effect); (5) invalid reference date — `"2026-02-30"` excludes a 03-01 receipt in Python, JS ignores the reference; (6) student with blank name — Python counts, JS skips (legacy/imported data only). | Decide per item or accept as-is. When an engine is changed the matching `expectedFailure` must be removed (an "unexpected success" is the signal). |

## 2. Open (not waiting on a decision)
| ID | Severity | Issue |
|---|---|---|
| WIN-1 | HIGH for release | Nothing has run on Windows: `build_windows.bat`, PyInstaller spec (hidden imports, `resolve_ui_dir` third candidate), WebView2/PyWebView, Inno installer, upgrade from 1.1.0. Follow `RELEASE_CHECKLIST.md`. **Do not say the program was "built" before this.** |
| HTTP-1 | MEDIUM (FIXED & VERIFIED in Session 12) | The HTTP suite last ran in Session 3 (44/44 OK with fastapi 0.162.7 / httpx 0.28.1). Since then: `test_http_integrity.py` (10 tests) was written but **never run**; the F-03 HTTP test edit (`/api/health?wsec=` → 401) never ran; the 69 `raise … from exc` edits in `server.py` are proven only by AST-equivalence + `py_compile`. Run `pip install fastapi httpx` and the full suite; treat the first failure as a possible wrong test assumption before blaming the code. |
| HTTP-2 | LOW | 43 of 71 routes had no HTTP test in the Phase 1 inventory (list in `TESTING.md`); `test_http_integrity.py` later added month-lock/empty-collection mapping coverage. Every `/api/*` route is still proven to reject unauthenticated calls (`RouteInventory`). |
| F-09 | LOW (FIXED & VERIFIED in Session 12) | Chunked / unbounded request bodies: only `Content-Length` > 25 MB is rejected early. Needs an ASGI-level cap. |
| F-13 | LOW (FIXED & VERIFIED in Session 12) | `/backups/health` route referenced but does not exist (`get_backup_health` exists in the Store). |
| F-15 | LOW (FIXED & VERIFIED in Session 12) | Mirror-folder (`extraBackupFolder`) copy failure is silent in the UI; `get_backup_health` reports `extraFolderReachable=False`. |
| SEC-1 | LOW (FIXED & VERIFIED in Session 12) | Residual `ValueError` message exposure on some 400 responses. |
| DEP-1 | LOW | Dependencies are unpinned; no `requirements.lock` (create it on the Windows build machine after the first good build). |
| CI-1 | LOW | `ci.yml` lint/mypy steps are still `continue-on-error`; Ruff and Mypy are now at 0 locally, so they could be made blocking (not done: CI itself was never run). |
| ASSET-1 | LOW | Logo source is only 256 px (upscaled to 512 with LANCZOS + sharpen). Replace `ui/assets/logo.png` with a high-res transparent original and regenerate `icon.ico`/favicon. The banner image contains `@zaytoon_18` baked in. |
| UI-1 | COSMETIC (FIXED & VERIFIED in Session 12) | The blue accounting-rule card on the dashboard is too light in dark mode. |
| VIS-1 | LOW | Excel export with a teacher name containing `/` was not opened in real Excel (openpyxl accepts the sanitised title; Excel's own limits were applied from documentation). |

## 3. Fixed and verified
| ID | What | Evidence |
|---|---|---|
| TOOL-1 (Session 11) | No automated guard tied a built EXE to its source (how BUILD-1/PRIV-1 went unnoticed). Added `scripts/verify_build_artifact.py` (stdlib-only, static PyInstaller parser: x64, Python 3.12, module set, version, UI byte-hashes incl. one-dir layout, forbidden payload) + wired into `build_windows.bat` and CI `build-smoke`. | `tests/test_build_artifact_verifier.py` (16; mutation-checked: 6 fail when the checks are disabled); run on the real supplied EXE → 5 FAIL, as independently confirmed by manual TOC/PYZ inspection. **Static only; not a runtime proof; the CI/bat wiring has not run on Windows.** |
| F-01 (HIGH) | Restoring an integrity-valid but schema-incompatible backup left the live DB unusable. Now verified + automatic revert to the `pre_restore` snapshot; incompatible backup → 400. | `test_restore_safety.py` (3 of 7 fail on the old code) + HTTP (Session 3) |
| F-02, F-04, F-06, F-07, F-08, F-10 | `server.py` hardening: no `str(exc)` leaks + generic JSON 500, `[::1]` Host parsing, robust multipart + filename sanitising, `fullmatch` on backup names, lock around the PIN counter, error-report type check, early 413. | `test_security_utils.py`, HTTP 44/44 (Session 3) |
| F-05 | CSV fallback export formula/quote injection (`ui/js/modules/csv.js`). | `test_frontend_security.py::CsvInjection` runs the real module in Node |
| F-03 | Window secret no longer accepted from the query string except on bootstrap paths; `api.js` captures it once and strips it with `history.replaceState` keeping other params and the hash. | Frontend half: `test_frontend_security.py::WsecIsStrippedFromUrl` (real `api.js` in Node). **HTTP half not run** (HTTP-1) |
| DEFECT-B1 | Backups/restores left `*.part-wal` / `*.part-shm` (and `-wal/-shm` beside restore sources). Fixed; legacy leftovers swept; `rotate()` removes companions. | `test_backup_wal.py` |
| Closed-month freeze, teacher archive-not-delete, empty-collection guard | Phase 2 integrity items. | `test_data_integrity.py` (15) at Store level |
| EXPORT-1 (HIGH usability) | `scope=full\|teacher` export crashed on teacher names containing `\ / ? * [ ] :` (e.g. «أ/ فاطمة») → `excel_service._safe_sheet_title`. | `test_import_export.py::ExportSafety` (fails on the original 1.2.0 code) |
| IMPORT-1 (LOW security) | The import error-report workbook wrote `=…` text (reason, uploaded file name) as live formulas. | `ErrorReport.test_text_is_sanitised_no_live_formulas` |
| IMPORT-2 (HIGH, silent data loss) | `commit_import(students)`: a NEW student on several rows (one per month) kept only the last month. Fixed in `import_service.py`. | `MultiMonthNewStudent` (fails on the original code) |
| Dashboard importer | Importer rejected the real dashboard (formulas, per-month sub-columns) → `alahd_erp/tools/excel_converter.py` (see ARCHITECTURE §6). | `test_excel_converter.py` (18) + real file |
| Ruff 88/89 → 0, Mypy 35 → 0 (58 → 0 on `mypy alahd_erp`) | Behaviour-neutral: 69× B904 `raise … from exc`, 16 unused imports, 3 dead assignments, annotations, 3 `# type: ignore`. AST of `server.py` identical before/after (ignoring `Raise.cause` and handler names). | Re-run both tools when available; in Session 8 they were not installed (no network) |

## 4. Observed, deliberately unchanged (INFO)
- `skip_duplicates` and `add_only` behave identically; `update_existing` re-importing an identical file counts 1 committed row and bumps `rev` without changing data; a formula row yields two error entries (cell column + «معادلة»).
- The importer fills an empty fee from the package price whenever the month has any date; the converter therefore drops fee-less months that only have dates (reported as `DATE_WITHOUT_FEE`).
- The Store's legacy migration marks every month with a fee and a `paymentDate` as fully paid (`RCP-MIG-…`), even with an empty paid amount (importing the real dashboard: 62 months / 14,845 EGP). Related to PARITY-1.
- `MonthLockedError` maps to **HTTP 400**, not 423 (its docstring says 400/423; 423 is never sent). Documented contract, unchanged.
- `/api/sync` takes a `before_shrink` safety snapshot even if the write is then rejected with `EmptyCollectionError`; repeated `students:[]` requests can rotate out the 20 snapshots (the rotation is shared by all tags).
- The Excel «صافي الأكاديمية» (subscriptions − wages) is a different quantity from the engine's `netOperatingProfit` (which also adds extra revenue/expenses). Intentional or not is a business call.
- `validate_state` leaves `allowEmptyCollections` in the state and `replace_all` ignores it; only the literal `allow_empty=True` opens the guard.

## Integrity Round updates (2026-11-15)
- F6 Excel stray-cell/zip-bomb/giant-merge: FIXED & VERIFIED. F7 foreign-401 instance detection: FIXED & VERIFIED. F9 3.14 fallback now opt-in: FIXED (static; Windows NOT RUN). F10 docs drift: FIXED.
- OPEN owner decisions with numeric evidence (`reports/integrity_round/decision_evidence.json`): closed-month freeze (wages 350->200 after close), receipt numbering/uniqueness (duplicate RCP-000000 and duplicate manual numbers accepted), PARITY-7 (1213 vs 1216), `/api/demo/load` guard.
- F8 (`timeout.unref` on Node 22): NOT REPRODUCED here.
