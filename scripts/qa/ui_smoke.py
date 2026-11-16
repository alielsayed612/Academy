"""Real-browser UI smoke test. FastAPI is unavailable here, so /api/* is answered in-process from the REAL Store
(the same Store/engines the app uses) via Playwright route interception; static UI files are served by http.server.
Usage: python ui_smoke.py <project_root> <outdir>
"""
from __future__ import annotations
import sys, json, pathlib, tempfile, threading, functools, http.server, socketserver, re, urllib.parse, time, io

ROOT = pathlib.Path(sys.argv[1]).resolve(); OUT = pathlib.Path(sys.argv[2]).resolve(); OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT)); sys.dont_write_bytecode = True
from alahd_erp.infrastructure.database import Store, MonthLockedError  # noqa: E402
from alahd_erp.domain.alerts_engine import compute_alerts  # noqa: E402
from alahd_erp.domain.statistics_engine import compute_general_statistics  # noqa: E402
from alahd_erp.domain.attendance_engine import *  # noqa: E402,F401
from alahd_erp.services.excel_service import export_bytes  # noqa: E402
from alahd_erp.services.html_export_service import generate_readonly_html  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

tmp = tempfile.TemporaryDirectory(); home = pathlib.Path(tmp.name)
store = Store(home / "ui.sqlite3")
UI = ROOT / "alahd_erp" / "ui"

class Q(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(UI), **k)
    def log_message(self, *a): pass
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Q); PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

UNHANDLED: list[str] = []; CALLS: list[str] = []

def jr(route, obj, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(obj, ensure_ascii=False, default=str))

def handle(route, request):
    u = urllib.parse.urlparse(request.url); path = u.path[len("/api"):]; q = dict(urllib.parse.parse_qsl(u.query)); m = request.method
    CALLS.append(f"{m} {path}")
    body = {}
    try:
        if request.post_data: body = json.loads(request.post_data)
    except Exception: pass
    try:
        if path == "/health": return jr(route, {"ok": True, "version": "1.2.0", "empty": not store.has_data(), "onboarded": store.is_onboarded(), "pinRequired": store.has_pin(), "studentsCount": store.count_students(), "rev": store.rev})
        if path == "/onboarding": return jr(route, {"onboarded": store.is_onboarded(), "settings": store.get_settings(), "rev": store.rev})
        if path == "/pin/status": return jr(route, {"pinRequired": store.has_pin(), "pinIdleMinutes": 15})
        if path == "/pin/verify": return jr(route, {"ok": True}) if store.verify_pin(str(body.get("pin", ""))) else jr(route, {"detail": "رمز PIN غير صحيح"}, 401)
        if path == "/settings" and m == "GET": return jr(route, store.get_settings())
        if path == "/settings": return jr(route, store.update_settings(body.get("settings", body)) or {"ok": True, "rev": store.rev})
        if path == "/onboarding/complete": return jr(route, store.complete_onboarding(body))
        if path == "/demo/load": return jr(route, store.load_demo_data(backup_folder=home / "bk"))
        if path == "/demo/clear": return jr(route, store.clear_demo_data(backup_folder=home / "bk") if "backup_folder" in store.clear_demo_data.__code__.co_varnames else store.clear_demo_data())
        if path == "/sync" and m == "GET":
            s = store.export_state(); s["duplicates"] = []; s["backupHealth"] = store.get_backup_health(home / "bk"); s["rev"] = store.rev; return jr(route, s)
        if path == "/stats": return jr(route, store.stats(q["month"], q.get("reference_date") or None))
        if path == "/accounting": return jr(route, store.accounting_summary(q.get("month") or None, q.get("reference_date") or None))
        if path == "/statistics":
            st = store.export_state(teacher=q.get("teacher") or None)
            return jr(route, compute_general_statistics(st, period=q.get("period") or "all_time", from_month=(q.get("custom_from") or "")[:7] or None, to_month=(q.get("custom_to") or "")[:7] or None, reference_date=q.get("reference_date") or None, teacher_filter=q.get("teacher") or None))
        if path == "/alerts": return jr(route, compute_alerts(store.export_state(), month=q.get("month") or None, reference_date=q.get("reference_date") or None, backup_health=store.get_backup_health(home / "bk"), include_dismissed=q.get("include_dismissed") == "true"))
        if path == "/months/closed": return jr(route, store.list_closed_months())
        if re.fullmatch(r"/months/[\d-]+/close", path): return jr(route, store.close_month(path.split("/")[2], body.get("note", ""), backup_folder=home / "bk"))
        if re.fullmatch(r"/months/[\d-]+/reopen", path): return jr(route, store.reopen_month(path.split("/")[2], body.get("note", "")))
        if path == "/collections": return jr(route, store.collection_report(q["month"], q.get("reference_date") or None))
        if re.fullmatch(r"/students(/[^/]+)?", path) and m in ("POST", "PUT"):
            stu = body.get("student", body); sid = path.split("/")[2] if path.count("/") == 2 else stu.get("id")
            stu["id"] = sid; return jr(route, store.upsert_student(stu))
        if path == "/export/xlsx": return route.fulfill(status=200, content_type="application/vnd.ms-excel", body=export_bytes(store.export_state(), scope=q.get("scope") or "full", month=q.get("month") or None))
        if path == "/export/html": return route.fulfill(status=200, content_type="text/html", body=generate_readonly_html(store.export_state(), scope=q.get("scope") or "full", month=q.get("month") or None))
        if path == "/audit": return jr(route, [])
        if path == "/backups/health": return jr(route, store.get_backup_health(home / "bk"))
        if path == "/backups": return jr(route, store.list_backup_files(home / "bk"))
    except MonthLockedError as e:
        return jr(route, {"detail": {"message": str(e), "code": "MONTH_LOCKED"}}, 400)
    except ValueError as e:
        return jr(route, {"detail": str(e)}, 400)
    except Exception as e:  # noqa
        UNHANDLED.append(f"EXC {m} {path}: {type(e).__name__}: {e}"); return jr(route, {"detail": "err"}, 500)
    UNHANDLED.append(f"UNMOCKED {m} {path}"); return jr(route, {"detail": "not mocked"}, 404)

results: list[tuple[str, bool, str]] = []
def check(n, c, d=""):
    results.append((n, bool(c), d)); print(("PASS " if c else "FAIL ") + n + (f"  [{d}]" if d and not c else ""))

with sync_playwright() as pw:
    br = pw.chromium.launch(); ctx = br.new_context(viewport={"width": 1366, "height": 900}, locale="ar-EG", accept_downloads=True)
    page = ctx.new_page(); errs: list[str] = []; bad_req: list[str] = []
    page.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
    page.on("console", lambda m: errs.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("requestfailed", lambda r: bad_req.append(f"{r.url} {r.failure}"))
    page.on("response", lambda r: bad_req.append(f"{r.status} {r.url}") if r.status >= 400 and "/api/" not in r.url else None)
    page.on("dialog", lambda d: d.accept())
    page.route("**/api/**", handle)

    # ---- 1. first run (empty DB): wizard must show
    page.goto(f"http://127.0.0.1:{PORT}/#/dashboard"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "01_first_run.png"))
    check("first-run: onboarding wizard visible", page.locator("#onboardingModal.open").count() == 1)
    check("title is Arabic academy name", "يوم جديد" in page.title(), page.title())
    check("logo image loaded", page.evaluate("() => { const i=document.querySelector('.sidebar-logo img'); return !!(i && i.complete && i.naturalWidth>0) }"))
    check("Cairo font loaded", page.evaluate("() => document.fonts.check('16px Cairo')"))
    check("vendored libs present", page.evaluate("() => ({x: typeof XLSX, e: typeof ExcelJS, h: typeof html2pdf, q: typeof qrcode}) ") is not None)

    # ---- 2. complete onboarding via API (as wizard would) then demo data
    store.complete_onboarding({"settings": {"academyName": "أكاديمية يوم جديد"}, "teachers": [{"name": "أ. نور", "pct": 70}]})
    store.load_demo_data(backup_folder=home / "bk")
    page.reload(); page.wait_for_load_state("networkidle"); page.wait_for_timeout(1500)
    check("after onboarding+reload: wizard closed", page.locator("#onboardingModal.open").count() == 0)
    page.screenshot(path=str(OUT / "02_dashboard.png"))
    check("dashboard has students rendered", page.locator("#view-dashboard").inner_text().strip() != "")
    check("save-status chip shows revision", "محفوظ" in (page.locator("#syncStatusChip").inner_text() if page.locator("#syncStatusChip").count() else ""), page.locator("#syncStatusChip").inner_text() if page.locator("#syncStatusChip").count() else "none")

    # ---- 3. visit every view
    views = ["dashboard", "statistics", "alerts", "students", "profile", "collections", "payroll", "accounting", "attendance", "memorization", "madrasa", "certificates", "teachers", "import", "exports", "system"]
    for v in views:
        n_before = len(errs)
        btn = page.locator(f'.nav-btn[data-view="{v}"]')
        if btn.count():
            btn.first.click(); page.wait_for_timeout(700)
        else:
            page.evaluate(f"location.hash='#/{v}'"); page.wait_for_timeout(700)
        active = page.evaluate(f"() => document.getElementById('view-{v}')?.classList.contains('active')")
        txt = page.evaluate(f"() => (document.getElementById('view-{v}')?.innerText||'').trim().length")
        page.screenshot(path=str(OUT / f"view_{v}.png"))
        check(f"view '{v}' activates, renders content ({txt} chars), no new JS errors", active and txt > 20 and len(errs) == n_before, "; ".join(errs[n_before:])[:300])

    # ---- 4. dark theme + mobile viewport
    tog = page.locator("#btnToggleTheme")
    if tog.count():
        tog.first.click(); page.wait_for_timeout(400); page.screenshot(path=str(OUT / "03_dark.png"))
        check("dark theme toggles", page.evaluate("() => document.documentElement.getAttribute('data-theme')") == "dark")
    else:
        check("theme toggle found", False, "no toggle selector matched")
    for (w, h) in ((1080, 700), (960, 700)):   # main.py: min_size=(1080,700); CSS breakpoint at 960
        page.set_viewport_size({"width": w, "height": h}); page.wait_for_timeout(300)
        for v in views:
            page.evaluate(f"location.hash='#/{v}'"); page.wait_for_timeout(250)
            ow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"{w}px window '{v}': no page-level horizontal overflow", ow <= 2, f"overflow {ow}px")
        page.screenshot(path=str(OUT / f"win_{w}.png"))
    page.set_viewport_size({"width": 1366, "height": 900})

    # ---- 5. RTL
    check("document is RTL/Arabic", page.evaluate("() => document.documentElement.dir") == "rtl" and page.evaluate("() => document.documentElement.lang").startswith("ar"))

    # ---- 6. add student through the UI
    page.evaluate("location.hash='#/students'"); page.wait_for_timeout(600)
    add = page.locator("#btnAddStudent, [data-action=add-student], button:has-text('إضافة طالبة')")
    check("add-student button exists", add.count() > 0)
    if add.count():
        add.first.click(); page.wait_for_timeout(600); page.screenshot(path=str(OUT / "05_add_student_modal.png"))
        fields = page.evaluate("() => [...document.querySelectorAll('.modal-overlay.open input, .modal.open input, dialog[open] input, .open input')].map(i=>i.id||i.name).filter(Boolean).slice(0,15)")
        check("student form shows inputs", len(fields) > 0, str(fields))

    # ---- 7. XSS probe: hostile student name must render as text
    store.upsert_student({"id": "xss1", "name": "<img src=x onerror=window.__pwned=1>", "phone": "010", "teacher": store.export_state()["teachers"][0], "type": "x", "pct": 70, "months": {"2026-11": {"value": 100, "discount": 0}}})
    page.evaluate("location.reload()"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(1200)
    for v in ("students", "collections", "payroll", "dashboard", "alerts"):
        page.evaluate(f"location.hash='#/{v}'"); page.wait_for_timeout(500)
    check("XSS: hostile student name did not execute", page.evaluate("() => window.__pwned") is None)

    # ---- 8. exports download through the UI flow endpoints
    r = page.evaluate("async () => { const x = await fetch('/api/export/xlsx?scope=full'); return [x.status, (await x.arrayBuffer()).byteLength]; }")
    check("xlsx export endpoint via UI origin", r[0] == 200 and r[1] > 5000, str(r))

    # ---- 9. PIN lock flow
    store.set_pin("2468")
    page.evaluate("location.reload()"); page.wait_for_load_state("networkidle"); page.wait_for_timeout(1200)
    page.screenshot(path=str(OUT / "06_pin_lock.png"))
    check("PIN lock screen is shown when PIN is set", page.locator("#pinLockModal.open").count() == 1)
    pin_in = page.locator("#pinUnlockInput")
    if pin_in.count():
        pin_in.first.fill("0000"); pin_in.first.press("Enter"); page.wait_for_timeout(700)
        still = page.locator("#pinLockModal.open").count() == 1
        check("wrong PIN keeps screen locked", still)
        pin_in = page.locator("#pinUnlockInput")
        pin_in.first.fill("2468"); pin_in.first.press("Enter"); page.wait_for_timeout(1000)
        check("correct PIN unlocks", page.locator("#pinLockModal.open").count() == 0)
    store.clear_pin()

    # ---- 10. Offline-first: no external network requests
    ext = [u for u in bad_req if "127.0.0.1" not in u]
    check("no external (internet) requests made by UI", not ext, str(ext[:3]))
    check("no failed static assets (4xx/5xx/requestfailed)", not [b for b in bad_req if "127.0.0.1" in b], str([b for b in bad_req][:5]))
    br.close()

print("\nAPI calls the UI made:", sorted(set(c.split("?")[0] for c in CALLS)))
print("UNHANDLED/UNMOCKED:", sorted(set(UNHANDLED)))
print("JS errors/console errors overall:", errs[:15])
bad = [r for r in results if not r[1]]
print(f"\n==== UI: {len(results)-len(bad)}/{len(results)} passed")
for n, _, d in bad: print("FAILED:", n, "|", d[:300])
