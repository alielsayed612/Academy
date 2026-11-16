# Release checklist — Yawm Jadid Academy 1.2.0 (Windows 10/11)

Nothing below has been executed by the authoring sandbox (Linux, no Windows). Tick each item on a clean Windows machine/VM
**and** on a machine that already has 1.1.0 data.

## A. Build
- [ ] (Session 11) `python scripts\verify_build_artifact.py dist\YawmJadidAcademy\YawmJadidAcademy.exe --ui-dir dist\YawmJadidAcademy\_internal\alahd_erp\ui` prints no `[FAIL]` (runs automatically in `build_windows.bat` and CI). Run with `--strict` once to see whether any check is `BLOCKED` on the real one-folder layout (PYZ location is assumed from PyInstaller 6 behaviour, unverified).
- [ ] Never ship an EXE that failed that check, and never ship any EXE built before this one: the supplied `AlAhdAcademySuite.exe` (v1.0.0) embeds real-looking student data (PRIV-1).
- [ ] (جديد، جلسة 9) `scripts\smoke_exe.ps1` بيتنفذ تلقائي من `build_windows.bat` بعد البناء، وبيتأكد إن الـ EXE بيشتغل ويخدم الواجهة و`/api/health` وبينشئ الداتابيس في فولدر مؤقت معزول. كمان job اسمه `build-smoke` في GitHub Actions بيبني الـ EXE ويرفعه كـ artifact. **الاتنين لسه ما اتشغّلوش على ويندوز.**
- [ ] `build_windows.bat` finishes with `BUILD OK` (this also runs the full test-suite *with* HTTP tests — expect 0 skipped except the Node-less ones).
- [ ] `dist\YawmJadidAcademy\YawmJadidAcademy.exe` exists; Explorer → Properties → Details shows product "Yawm Jadid Academy 1.2.0.0"; the icon is the olive/cream logo.
- [ ] Installer `installer\Output\YawmJadidAcademy-Setup-1.2.0.exe` created (needs Inno Setup 6).
- [ ] `requirements.lock` committed.

## B. First run (clean machine, no data)
- [ ] App window opens (title "أكاديمية يوم جديد — v1.2.0"), logo in sidebar, banner on the welcome card, no console window.
- [ ] Setup wizard shows academy name "أكاديمية يوم جديد"; change it, save, reopen → persists.
- [ ] Light/Dark theme toggle both look right; PIN lock screen shows the logo.
- [ ] Browser address bar / history never contains `wsec` (F-03) when the fallback browser mode is used.

## C. Core flows
- [ ] Add teacher → add student with a month, discount, payment date, actual start date → add receipt.
- [ ] Close a month → editing that month is refused with the Arabic "month locked" message; reopen → editable.
- [ ] Excel export, HTML read-only export, receipt, certificate PNG: Arabic text correct, colours are the sage palette, file names start with `YawmJadid_`.
- [ ] Import template download → fill → import → numbers match.
- [ ] Manual backup → file appears **alone** in the backups folder (no `.part-wal` / `.part-shm`) → restore → data identical.
- [ ] Reset application → a `safety_before_reset_app` snapshot exists → restore it → data back.

## D. Upgrade / data safety (most important)
- [ ] Install 1.1.0, create data, install 1.2.0 over it: all students, receipts, settings, closed months intact; academy name unchanged (user's saved value).
- [ ] `%LOCALAPPDATA%\AlAhdAcademySuite` still the data folder; uninstall 1.2.0 → the folder and database are still there.
- [ ] Second launch while running focuses/reuses the running instance (no second server, no data corruption).

## E. Known open items (not blockers unless the owner says so)
- PARITY-1 / PARITY-7 finance decisions; PIN is a UI lock (F-11); re-run the HTTP suite (HTTP-1); logo resolution (ASSET-1). Full list: `ISSUES.md`.
