# Testing

Merges the old root `TESTING_REPORT.md`, `docs/TESTING_REPORT.md` and `docs/API_TESTING.md`. Only results that were actually produced are recorded.
(The old `API_TESTING.md` also contained an unrendered template placeholder where the list of uncovered routes should have been; the real list is in §5.)

## 1. How to run
```
python -X dev -W default -m unittest discover -s tests -t .      # whole suite, stdlib unittest only
python -m unittest tests.test_http_api tests.test_http_integrity -v   # HTTP layer only (needs fastapi + httpx)
```
Full setup on a networked machine: `python -m venv .venv`, activate, `pip install -e .[dev]` (fastapi, uvicorn, pydantic, pywebview, openpyxl + ruff, mypy, httpx).
Optional: `ALAHD_DASHBOARD_XLSX=<path to Academy_Management_Dashboard*.xlsx>` enables the real-file converter test.
`ALAHD_SRC_ROOT=<other checkout>` runs the same tests against another copy of the code (used to prove a regression test fails on the original code).
Every test uses a temporary home/DB; the production database is never touched. Node is needed for the JS-backed tests (they skip without it; Node 22 was used).

## 2. Current result (Session 8, Linux sandbox, Python 3.12.3, Node 22, openpyxl 3.1.5, **no fastapi/httpx/ruff/mypy**)
`python -X dev -W default -m unittest discover -s tests -t .` → **Ran 307 tests, OK (skipped=55, expected failures=7)** (Session 11: +16 `test_build_artifact_verifier`), no warnings.
- **55 skipped = 44 (`test_http_api`) + 10 (`test_http_integrity`) + 1 (real-file converter test, no `ALAHD_DASHBOARD_XLSX`).** Skips are *not* passes.
- **7 expected failures** = PARITY-1…6 and the PARITY-7 defect in `test_finance_parity.py` — documented divergences awaiting owner decisions (`ISSUES.md` §1).
  If one becomes an "unexpected success", someone fixed the cause: remove its `@unittest.expectedFailure`.
- Not run in this sandbox: HTTP suite, ruff, mypy, JS beyond the Node harnesses, app launch, PyWebView, PyInstaller, Windows, GitHub Actions.

## 3. Test modules
| Module | Tests | Covers |
|---|---|---|
| `test_store` | 15 | Store CRUD, rev concurrency, migrations basics |
| `test_finance_engine` | 16 | Python finance rules |
| `test_teacher_pct` | 7 | wage-pct precedence / alias sync |
| `test_finance_parity` | 17 | Python ↔ JS parity (real `finance-engine.js` in Node via `tests/js/parity_runner.js`); 6 documented divergences + PARITY-7 (7 expected failures) |
| `test_data_integrity` | 15 | closed-month freeze, teacher archive-not-delete, empty-collection guard |
| `test_backup_wal` | 25 | WAL-aware backup, reset/pre-restore snapshots, rotation, mirror folder, no orphan `-wal/-shm` |
| `test_restore_safety` | 7 | incompatible/corrupt backup restore → rollback (F-01) |
| `test_import` | 18 | importer core |
| `test_import_export` | 38 | templates, Arabic headers/Indic digits, formula rows, bad files, modes, closed-month atomicity, error report, all export scopes with hostile text, workbook totals == `compute_accounting_summary`, IMPORT-2 regression |
| `test_excel` | 6 | Excel export basics |
| `test_excel_converter` | 18 | dashboard converter (includes the real-file test, skipped without `ALAHD_DASHBOARD_XLSX`) |
| `test_html_export` | 6 | read-only HTML export, phone masking, filename sanitising, escaping |
| `test_security_utils` | 14 | pure helpers behind the server hardening |
| `test_frontend_security` | 21 | `dom.js` escaping, `csv.js` injection, static sink scan, offline-only assets, F-03 `api.js` URL stripping (real modules in Node) |
| `test_branding` | 14 | identity/version/packaging consistency, data-folder safety (`DATA_DIR_NAME`) |
| `test_http_api` | 44 (skipped here) | FastAPI `TestClient` suite, see §4 |
| `test_http_integrity` | 10 (skipped here) | status mapping of `MonthLockedError`/`EmptyCollectionError`, month-lock writes leave `rev`/state/audit untouched |

JS harnesses: `tests/js/parity_runner.js`, `tests/js/f03_harness.mjs`. Shared helpers: `tests/_helpers.py` (`StoreTestCase`, `ROOT`).

## 4. HTTP suite
`test_http_api.py` builds `create_app()` on a temporary home. **It was executed once, in Session 3: 44/44 OK** (fastapi 0.162.7, httpx 0.28.1; one wrong test
assumption fixed — `/api/stats` requires `month`; the app was right). Since then it was edited (F-03: `/api/health?wsec=` → 401) and `test_http_integrity.py` was
added, **neither has been run**. First step on a machine with the dependencies: run both modules; read the route before blaming the code.

| Class | Cases |
|---|---|
| Authentication | no/wrong/empty/truncated secret → 401 on read, write, delete; header and cookie accepted; `?wsec=` accepted only on bootstrap paths (F-03); bootstrap page leaks nothing; httpOnly + SameSite cookie; security headers; unknown `/api/*` → 404 JSON |
| OriginAndHost | foreign / `null` / https / wrong-port Origin → 403 and no data change; own or missing Origin allowed; foreign Host (DNS rebinding) → 403; non-loopback client → 403 |
| PinGuard | set/verify, 5 failures → 429 even for the right PIN, PIN never echoed, short/long PIN → 400, change/clear need the current PIN |
| ValidationAndErrors | malformed/non-object JSON → 400 without leaking; invalid pct → 400 and nothing saved; bad `scope`/`month`/`dataset` → 400; bad / stale `X-Client-Rev` → 400 / 409; reset needs the confirmation word; unexpected exception → generic 500 |
| UploadsAndSizes | body > 25 MB → 413; empty → 400; bad base64 → 400; garbage file with traversal filename never 500; garbage multipart → 400 |
| BackupAndRestore | manual backup is a real SQLite file; traversal/newline/foreign names → 400; missing → 404; junk or incompatible backup → 400 and data kept; valid round-trip |
| FinancialIntegrityThroughApi | `pct`↔`defaultPct` sync; pct stays an integer; `/api/sync` round-trip keeps students and `teacherMeta`; `/api/accounting` and `/api/stats` deterministic |
| XssThroughExports | hostile teacher name stored verbatim, escaped in HTML exports |
| RouteInventory | walks the real route table: every `/api/*` route answers 401 without the secret |

## 5. Routes with no HTTP test (per the Phase 1 route inventory, 43 of 71)
`GET /api/alerts`, `POST /api/alerts/dismiss`, `POST /api/alerts/restore`, `POST /api/attendance/auto-compensate`, `POST /api/attendance/project-dates`, `GET /api/audit`,
`POST /api/certificates`, `DELETE /api/certificates/{cert_id}`, `POST /api/demo/clear`, `GET /api/duplicates`, `GET /api/export/receipt-html`, `POST /api/groups`,
`DELETE /api/groups/{group_id}`, `POST /api/import/commit`, `POST /api/import/undo`, `DELETE /api/installments/{inst_id}`, `PUT /api/kv/{key}`,
`POST /api/madrasa/enrollments`, `DELETE /api/madrasa/enrollments/{enr_id}`, `POST /api/memorization`, `DELETE /api/memorization/{mem_id}`, `GET /api/months/closed`,
`POST /api/months/{month_key}/close`, `POST /api/months/{month_key}/reopen`, `GET /api/onboarding`, `POST /api/onboarding`, `POST /api/payouts`, `DELETE /api/payouts/{payout_id}`,
`POST /api/plans`, `DELETE /api/plans/{plan_id}`, `GET /api/reports/accrual`, `GET /api/reports/collections`, `GET /api/reports/study-periods`, `POST /api/sessions`,
`POST /api/sessions/batch`, `DELETE /api/sessions/{session_id}`, `GET /api/statistics`, `PATCH /api/students/{student_id}/months/{month_key}`,
`DELETE /api/students/{student_id}/months/{month_key}`, `POST /api/study-period/calculate`, `DELETE /api/teachers/{teacher_name}`, `POST /api/transactions`, `DELETE /api/transactions/{tx_id}`.
Caveat: `test_http_integrity.py` (never run) later added coverage of the month-lock write paths; these routes are exercised at Store level by the other modules, and `RouteInventory`
still proves they reject unauthenticated calls.

## 6. History (test count after each session)
| Moment | Command result |
|---|---|
| Original code | 68 OK |
| Phase 0 | 70 OK (+2 offline-asset tests) |
| Phase 1 | 149 OK, 44 skipped (HTTP suite written, not runnable offline) |
| Session 3 | 164 OK, 0 skipped (deps installed from PyPI; HTTP 44/44 executed) |
| Session 4 | 210, 54 skipped, 7 expected failures (parity, backup, HTTP integrity written) |
| Session 5 | 233, 54 skipped, 6 expected failures (DEFECT-B1 fixed → its test became a normal test) |
| Session 6 | 270 OK, 54 skipped, 6 expected failures (import/export tests, EXPORT-1, IMPORT-1) |
| Session 7 | 289 → 291 OK, 55 skipped, 7 expected failures (converter, IMPORT-2, PARITY-7, Ruff/Mypy to 0) |
| Session 8 | 291 OK, 55 skipped, 7 expected failures — unchanged by the file consolidation (4 small test modules merged, none lost) |
| Session 11 | 307 OK, 55 skipped, 7 expected failures (+16 build-artifact verifier tests; baseline 291/55/7 reproduced first; HTTP suite NOT run: no fastapi/httpx offline) |

Reproduction notes kept from earlier sessions: F-01 was reproduced on the original code (`restore` raised `OperationalError`, afterwards `export_state()` raised `no such column: pos`);
the new restore tests fail 3 of 7 on the original code; EXPORT-1 / IMPORT-1 / IMPORT-2 regression tests fail on the original 1.2.0 code and pass after the fixes.
