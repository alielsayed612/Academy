# Architecture — Yawm Jadid Academy Suite (code name `alahd_erp`)

Version 1.2.0. This file is the stable description of *what the system is*. For *where the work stands* read
[`../README.md`](../README.md); for known problems read [`ISSUES.md`](ISSUES.md).

## 1. What it is
Offline, single-user Windows desktop ERP for an Arabic Quran academy: students, monthly subscriptions, receipts/installments,
teacher wages and payouts, expenses/revenue, attendance/sessions, memorization logs, madrasa batches, certificates, alerts,
statistics, Excel/HTML import-export, backups.
Stack: Python 3.12, FastAPI + uvicorn (loopback only), SQLite (WAL), vanilla ES-module JS, PyWebView, PyInstaller. Arabic RTL, Cairo font vendored.
Product name «أكاديمية يوم جديد»; the legacy internal names (`alahd_erp`, data folder `AlAhdAcademySuite`, `DB_FILENAME`) are **frozen** so
existing installs keep their data.

## 2. Repository layout
```
alahd_erp/                    the application package
  main.py                     startup: home/port, single-instance probe (/api/health), uvicorn thread, pywebview window
                              (browser fallback), periodic backup thread + exit backup
  config.py                   paths/env/logging; resolve_ui_dir handles frozen builds (sys._MEIPASS)
  api/server.py               all 103 route registrations (verified by tests/test_docs_in_sync.py), security middleware, upload parsing, error mapping
  api/security_utils.py       pure helpers: Host parsing, multipart parser, filename sanitising, backup-name check
  infrastructure/database.py  Store: schema v3, migrations, CRUD, backup/restore, PIN, audit, rev concurrency
  domain/                     finance_engine (money rules), attendance_engine, statistics_engine, alerts_engine, models, demo_data
  services/                   import_service (CSV/XLSX preview/commit/undo), excel_service, html_export_service
  tools/excel_converter.py    dashboard workbook -> import-ready workbooks (CLI, see §6)
  ui/                         index.html, css/design-system.css, js/app.js, js/finance-engine.js (JS mirror of money logic),
                              js/modules/* (api, store, router, dom, csv, alerts, statistics, import_view, wizard_and_system),
                              vendor/ (fonts, xlsx, exceljs, html2pdf, qrcode), assets/ (logo, banner, favicon)
tests/                        unittest suite + tests/js/ Node harnesses            -> see TESTING.md
scripts/                      dev helpers: route_inventory.py (route table from the AST), make_checkpoint.sh (verified recovery zip)
installer/, YawmJadidAcademy.spec, build_windows.bat, requirements*.txt   Windows build kit (never run on Windows yet)
docs/                         this documentation set
```

## 3. Runtime architecture
Startup → config → `create_app(store, ui_dir, secret)` → uvicorn on `127.0.0.1:8890` → window opens carrying a per-launch secret.
The frontend loads the whole state with `GET /api/sync` and mutates through REST routes or `POST /api/sync`, always sending
`X-Client-Rev`; the server answers **409 `CONCURRENCY_CONFLICT`** on a stale rev.

Security layers: loopback-only client check, Host/Origin allow-lists, mandatory
`X-Window-Secret` (header or httpOnly SameSite=strict cookie; `?wsec=` accepted **only** on the bootstrap paths `/`, `/index.html`,
`/favicon.ico`, `/assets/favicon.png`, and the UI strips it from the address bar), salted `scrypt` PIN (UI lock — the server does not
enforce it, F-11), HTML escaping server and client side, XLSX/CSV formula neutralisation, safety snapshots.

Error mapping (`_handle_domain_or_store_error`): `ConcurrencyConflictError`→409, `MonthLockedError`→**400** `{code:"MONTH_LOCKED", month}`
(not 423), `EmptyCollectionError`/`DomainValidationError`/`ValueError`→400 (Arabic message), `KeyError`→404; anything else is logged and
returned as a generic JSON 500. An incompatible backup restore returns 400.

Finance is computed twice on purpose: Python (`domain/finance_engine.py`) for server reports/exports, JavaScript (`ui/js/finance-engine.js`)
for on-screen numbers. Where they differ see PARITY-1..7 in [`ISSUES.md`](ISSUES.md).

## 4. Database
Schema v3 via `PRAGMA user_version`; WAL, `synchronous=FULL`, foreign keys ON, `busy_timeout`. Tables: teachers(name PK, meta JSON), students,
months(PK student_id+month_key), transactions, installments, salaries, study_groups, attendance_plans, sessions, memorization_logs,
madrasa_batches/enrollments/logs, certificates, closed_months, import_batches, kv, audit. Teacher FKs use `ON UPDATE CASCADE`; student children
`ON DELETE CASCADE`; salaries cascade on teacher delete (teachers are archived, not deleted, by the Store). Money columns are INTEGER;
**`students.pct` is REAL (0..100)** (changing it needs approval).
Backups: integrity-checked copies taken through a connection (`db.backup`), rotation auto 48 / daily 60 / manual 30 / safety 30, optional mirror
folder (`extraBackupFolder`; a failed mirror copy is silent, F-15). Safety snapshots are taken before restore, reset and shrinking syncs.
Backups/restores leave no `-wal/-shm/.part-*` companions (DEFECT-B1, fixed; leftovers from ≤1.1.0 are swept on the next backup).

## 5. Financial business rules (verified from `domain/finance_engine.py` and its tests)
- Money = integer EGP. `round_half_up` = Decimal quantize to 1 with `ROUND_HALF_UP`; inputs are first snapped to 6 decimals.
- Percent is an integer 0..100; `normalize_pct_decimal` → Decimal fraction (6 dp).
- **Wage pct precedence** (`resolve_wage_pct`): if setting `pctSource == "teacher"` and `teacherMeta[t].pct` exists → it; else the student's `pct`
  if set; else teacher `pct`; else `default_pct` (70). Default `pctSource` is `"student"` (allowed: `student|teacher`).
  `teacherMeta[t].pct` is the key that is read; `defaultPct` is mirrored by `_sync_pct_aliases`.
- **Monthly wage** (`effective_month_wage`): `customWage` overrides everything (in `collected` mode only if something was collected); otherwise base =
  collected amount (`wage_accrual_mode="collected"`) or gross/net subscription (`"accrued"`, per `wage_basis` `net|gross`); wage =
  `round_half_up(base × pct)`, never negative; `wage_basis` is ignored in `collected` mode.
- Net subscription = value − discount, used consistently (arrears never include a discount). Payment status is derived only from receipts
  (`installments`); a `paymentDate` alone does not mean paid in the engine; future-dated receipts are not collected. `paymentDate` ≠ `actualStartDate`.
- Legacy migration quirk (INFO): on save, the Store turns any month that has a fee **and** a `paymentDate` into a fully-paid receipt (`RCP-MIG-…`).
- Not documented in depth: `compute_collection_report`, `compute_accrual_month_report`, the legacy-payment migration internals.

## 6. Dashboard → import converter (`alahd_erp/tools/excel_converter.py`)
`python -m alahd_erp.tools.excel_converter <Academy_Management_Dashboard.xlsx> -o <out_dir> [--paid none|full]` writes
`import_students.xlsx`, `import_teachers.xlsx`, `conversion_issues.xlsx`. It never modifies the source and adds no route/UI/schema.
- Reads with `data_only=True` (the importer opens workbooks with `data_only=False`, which turns every formula cell into an error — that was the
  original "150 errors"). It warns when a formula has no stored value or is `#REF!`/`#DIV/0!`.
- Detects the month blocks from the sheet header, converts wide → long (one row per student × month), ignores the formula "teacher wage"
  columns and the totals row.
- Verified on the real file (63 students, 12 months): month totals 9,445 / 6,125 equal the «ملخص» sheet; importer preview 0 errors; commit into a
  temporary Store reproduces the totals. The real file is **not** shipped in the repo; set `ALAHD_DASHBOARD_XLSX=<path>` to run that test.
- Data issues in the user's file are reported, not fixed (probable duplicate students by phone, end-date before payment-date, missing package/teacher,
  27 of 69 payment dates equal to the file's own date 2026-11-11). Importing needs «auto-create teachers/packages» enabled in the students import.

## 7. Constraints that must hold
Offline-first (no CDN, no cloud); keep API routes and JSON shapes; money = integer EGP; do not change finance rules, schema, column types, routes or
JSON shapes without the owner's explicit approval; never change `DATA_DIR_NAME` or `DB_FILENAME`.
