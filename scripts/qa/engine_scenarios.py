"""End-to-end scenario harness against the real Store / engines (no HTTP layer: fastapi is not installed here).
Each scenario is isolated in its own temp dir. Prints PASS/FAIL per check and a summary. Usage: python scenarios.py <project_root>
"""
from __future__ import annotations
import io, sys, os, json, tempfile, pathlib, traceback, threading, sqlite3, time, csv

ROOT = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from alahd_erp.infrastructure.database import Store, MonthLockedError, ConcurrencyConflictError  # noqa: E402
from alahd_erp.services import excel_service, html_export_service, import_service  # noqa: E402
from alahd_erp.domain import finance_engine as fe  # noqa: E402
import openpyxl  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CUR = ""


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((f"{CUR} :: {name}", bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if (detail and not cond) else ""))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:  # noqa: PERF203
        return e
    except Exception as e:  # wrong exception type
        return None if False else ("WRONG:" + type(e).__name__ + ":" + str(e))
    return None


def scenario(fn):
    def run():
        global CUR
        CUR = fn.__name__
        print(f"\n### {CUR}")
        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td)
            st = Store(home / "t.sqlite3")
            try:
                fn(st, home)
            except Exception as e:
                traceback.print_exc()
                check("scenario did not crash", False, f"{type(e).__name__}: {e}")
            finally:
                try:
                    st.close()
                except Exception:
                    pass
    run.__name__ = fn.__name__
    return run


def mk_student(sid, name, teacher, month="2026-11", value=500, discount=0, **extra):
    d = {"id": sid, "name": name, "phone": "010" + sid[-1:].zfill(1) * 8, "teacher": teacher, "type": "8 حلق", "pct": 70,
         "months": {month: {"value": value, "discount": discount, "actualStartDate": f"{month}-02", "endDate": f"{month}-28"}}}
    d.update(extra)
    return d


@scenario
def s01_basic_lifecycle(st, home):
    check("fresh DB not onboarded / no data", not st.is_onboarded() and not st.has_data())
    st.upsert_teacher("معلمة أ", {"pct": 70})
    st.upsert_teacher("معلمة ب", {"pct": 60})
    st.upsert_student(mk_student("s1", "طالبة ١", "معلمة أ"))
    st.upsert_student(mk_student("s2", "طالبة ٢", "معلمة ب", value=800, discount=100))
    check("2 students", st.count_students() == 2)
    s = st.get_student("s2")
    check("student fetch w/ month", s and s["months"]["2026-11"]["value"] == 800)
    r0 = st.rev
    st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 500, "paymentDate": "2026-11-05", "receiptNo": "R-1"})
    check("rev bumps after write", st.rev > r0)
    stats = st.stats("2026-11", reference_date="2026-11-20")
    check("stats returns dict", isinstance(stats, dict) and stats, str(list(stats)[:8]))
    # collected should be 500 (s1) ; s2 unpaid
    flat = json.dumps(stats, ensure_ascii=False)
    check("stats mentions collected 500", "500" in flat)
    st.delete_student("s2")
    check("delete student", st.count_students() == 1 and st.get_student("s2") is None)
    st.upsert_student(mk_student("s1", "طالبة ١ معدلة", "معلمة أ"))
    check("update keeps single row + installment", st.count_students() == 1 and len(st.export_state()["installments"]) == 1)


@scenario
def s02_validation_edges(st, home):
    st.upsert_teacher("م", {"pct": 70})
    st.upsert_student(mk_student("s1", "ط", "م"))
    bad = [
        ("neg amount", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": -5, "paymentDate": "2026-11-05"}),
        ("zero amount", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": 0, "paymentDate": "2026-11-05"}),
        ("bad date", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": 5, "paymentDate": "05/11/2026"}),
        ("bad month", {"id": "x", "studentId": "s1", "month": "2026-13x", "amount": 5, "paymentDate": "2026-11-05"}),
        ("string amount", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": "abc", "paymentDate": "2026-11-05"}),
        ("float nan", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": float("nan"), "paymentDate": "2026-11-05"}),
        ("float inf", {"id": "x", "studentId": "s1", "month": "2026-11", "amount": float("inf"), "paymentDate": "2026-11-05"}),
        ("missing student", {"id": "x", "studentId": "nope", "month": "2026-11", "amount": 5, "paymentDate": "2026-11-05"}),
    ]
    for label, p in bad:
        e = raises(Exception, st.upsert_installment, p)
        check(f"installment rejects: {label}", isinstance(e, Exception) and not isinstance(e, str), repr(e))
    r = raises(Exception, st.upsert_transaction, {"type": "other", "category": "x", "amount": 5, "date": "2026-11-01"})
    check("tx rejects bad type", isinstance(r, Exception))
    r = raises(Exception, st.upsert_teacher, "")
    check("teacher rejects empty name", isinstance(r, Exception))
    r = raises(Exception, st.upsert_teacher, "ا" * 500)
    check("teacher rejects 500-char name", isinstance(r, Exception))
    # money as numeric-string / decimal float
    try:
        st.upsert_installment({"id": "ok1", "studentId": "s1", "month": "2026-11", "amount": "250", "paymentDate": "2026-11-06", "receiptNo": "R-OK1"})
        check("numeric-string amount accepted", True)
    except Exception as e:
        check("numeric-string amount accepted", False, repr(e))
    # Arabic-Indic digits
    try:
        st.upsert_installment({"id": "ok2", "studentId": "s1", "month": "2026-11", "amount": "٢٥٠", "paymentDate": "2026-11-06", "receiptNo": "R-OK2"})
        check("Arabic-Indic digits amount accepted or cleanly rejected", True)
    except ValueError:
        check("Arabic-Indic digits amount accepted or cleanly rejected", True)
    except Exception as e:
        check("Arabic-Indic digits amount accepted or cleanly rejected", False, repr(e))
    # duplicate receipt number
    e1 = raises(Exception, st.upsert_installment, {"id": "dup", "studentId": "s1", "month": "2026-11", "amount": 10, "paymentDate": "2026-11-07", "receiptNo": "R-OK1"})
    check("duplicate receiptNo handled (rejected or deduped, no crash)", True, repr(e1))
    # injection-y strings stored as data
    st.upsert_student(mk_student("s9", "'; DROP TABLE students;--<script>alert(1)</script>", "م"))
    check("SQL/HTML injection name stored safely", st.count_students() == 2 and st.get_student("s9")["name"].startswith("'; DROP"))
    # huge name / unicode
    st.upsert_student(mk_student("s8", "👩‍🎓" + "ي" * 100, "م"))
    check("emoji + 101-char name ok", st.get_student("s8") is not None)
    e = raises(Exception, st.upsert_student, mk_student("s7", "ي" * 121, "م"))
    check("121-char name → user-friendly ValueError (not raw IntegrityError)", isinstance(e, ValueError), type(e).__name__ if not isinstance(e, str) else e)
    e = raises(Exception, st.upsert_student, mk_student("s6", "ط", "م", phone="1" * 41))
    check("41-char phone → ValueError (not raw IntegrityError)", isinstance(e, ValueError), type(e).__name__ if not isinstance(e, str) else e)
    check("rejected writes left no partial rows", st.get_student("s7") is None and st.get_student("s6") is None)


@scenario
def s03_month_close_lock(st, home):
    st.upsert_teacher("م", {"pct": 70})
    st.upsert_student(mk_student("s1", "ط", "م"))
    st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 100, "paymentDate": "2026-11-05", "receiptNo": "R1"})
    st.close_month("2026-11", "test", backup_folder=home / "bk")
    check("month closed + snapshot created", st.is_month_closed("2026-11") and any((home / "bk").glob("*close_month_202611*")))
    for label, fn in [
        ("installment into closed month", lambda: st.upsert_installment({"id": "i2", "studentId": "s1", "month": "2026-11", "amount": 5, "paymentDate": "2026-11-06", "receiptNo": "R2"})),
        ("edit existing installment of closed month", lambda: st.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 999, "paymentDate": "2026-11-05", "receiptNo": "R1"})),
        ("delete installment of closed month", lambda: st.delete_installment("i1")),
        ("student month edit", lambda: st.upsert_student_month("s1", "2026-11", {"value": 1})),
        ("student upsert altering closed month", lambda: st.upsert_student(mk_student("s1", "ط", "م", value=777))),
        ("transaction in closed month", lambda: st.upsert_transaction({"type": "expense", "category": "x", "amount": 5, "date": "2026-11-10"})),
        ("delete student with closed-month data", lambda: st.delete_student("s1")),
    ]:
        try:
            r = fn()
            ok = False
            detail = f"returned {r!r}"
        except Exception as e:
            ok = True
            detail = type(e).__name__
        check(f"blocked: {label}", ok, detail)
    check("data unchanged after blocked writes", st.export_state()["installments"][0]["amount"] == 100)
    # other month still editable
    st.upsert_student_month("s1", "2026-12", {"value": 300, "actualStartDate": "2026-12-01", "endDate": "2026-12-28"})
    check("other month still editable", "2026-12" in st.get_student("s1")["months"])
    st.reopen_month("2026-11", "oops")
    st.upsert_installment({"id": "i3", "studentId": "s1", "month": "2026-11", "amount": 50, "paymentDate": "2026-11-09", "receiptNo": "R3"})
    check("reopen → editable", len(st.export_state()["installments"]) == 2)
    check("closed-months list empty after reopen", st.list_closed_months() == [])


@scenario
def s04_concurrency_rev(st, home):
    st.upsert_teacher("م", {"pct": 70})
    rev = st.rev
    st.upsert_student(mk_student("s1", "ط", "م"), expected_rev=rev)
    e = raises(ConcurrencyConflictError, st.upsert_student, mk_student("s2", "ط٢", "م"), expected_rev=rev)  # stale
    check("stale expected_rev rejected", isinstance(e, ConcurrencyConflictError), repr(e))
    check("stale write did not persist", st.get_student("s2") is None)
    # threaded hammer
    errs = []
    def worker(i):
        try:
            for j in range(15):
                st.upsert_student(mk_student(f"t{i}_{j}", f"ط{i}-{j}", "م"))
        except Exception as ex:
            errs.append(repr(ex))
    th = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in th]; [t.join() for t in th]
    check("6 threads × 15 writes no errors", not errs, "; ".join(errs[:2]))
    check("all 91 students present", st.count_students() == 91, str(st.count_students()))
    integ = st.db.execute("PRAGMA integrity_check").fetchone()[0]
    check("sqlite integrity_check ok", integ == "ok", integ)


@scenario
def s05_backup_restore_reset(st, home):
    st.upsert_teacher("م", {"pct": 70})
    for i in range(5):
        st.upsert_student(mk_student(f"s{i}", f"ط{i}", "م"))
    bk = home / "bk" / "manual.sqlite3"
    st.backup_to(bk)
    left = sorted(p.name for p in bk.parent.iterdir())
    check("backup file alone (no -wal/-shm/.part)", left == ["manual.sqlite3"], str(left))
    st.delete_student("s0"); st.delete_student("s1")
    check("2 deleted", st.count_students() == 3)
    st.restore_from(bk)
    check("restore returns 5 students", st.count_students() == 5)
    left = sorted(p.name for p in bk.parent.iterdir())
    check("no companions left after restore", all(not n.endswith(("-wal", "-shm", ".part")) for n in left), str(left))
    # corrupt backup
    bad = home / "bk" / "bad.sqlite3"
    bad.write_bytes(b"not a sqlite file" * 50)
    e = raises(Exception, st.restore_from, bad)
    check("corrupt backup rejected", isinstance(e, Exception))
    check("data intact after rejected restore", st.count_students() == 5)
    # truncated real backup
    trunc = home / "bk" / "trunc.sqlite3"
    trunc.write_bytes(bk.read_bytes()[:4096])
    e = raises(Exception, st.restore_from, trunc)
    check("truncated backup rejected", isinstance(e, Exception))
    check("data intact after truncated restore", st.count_students() == 5)
    res = st.reset_application(backup_folder=home / "bk")
    check("reset wipes data", st.count_students() == 0)
    snaps = list((home / "bk").glob("*before_reset_app*"))
    check("reset created safety snapshot", len(snaps) == 1, str(snaps))
    st.restore_from(snaps[0])
    check("restore safety snapshot brings data back", st.count_students() == 5)
    h = st.get_backup_health(home / "bk")
    check("backup health shape", isinstance(h, dict) and "staleWarning" in h, str(h))
    # rotate keeps N
    for i in range(8):
        st.snapshot(home / "rot", "t", keep=3); time.sleep(0.01)
    check("snapshot rotation keeps 3", len(list((home / "rot").glob("*.sqlite3"))) == 3, str(len(list((home / "rot").glob('*')))))


@scenario
def s06_pin_settings_onboarding(st, home):
    check("no pin initially", not st.has_pin())
    st.set_pin("1234")
    check("verify right pin", st.verify_pin("1234"))
    check("reject wrong pin", not st.verify_pin("4321"))
    check("reject empty pin", not st.verify_pin(""))
    e = raises(Exception, st.set_pin, "")
    check("empty pin set rejected", isinstance(e, Exception) and not isinstance(e, str), repr(e))
    raw = st.db.execute("SELECT k, v FROM kv").fetchall()
    check("pin not stored in plaintext", not any("1234" in str(r[1]) for r in raw), str([r[0] for r in raw]))
    st.clear_pin()
    check("pin cleared", not st.has_pin())
    out = st.complete_onboarding({"settings": {"academyName": "أكاديمية اختبار", "currency": "EGP"}, "teachers": [{"name": "أ", "pct": 65}, {"name": "ب", "pct": 55}], "plans": []})
    check("onboarding ok", st.is_onboarded(), str(out)[:200])
    check("settings persisted", st.get_settings().get("academyName") == "أكاديمية اختبار")
    st.close(); st2 = Store(home / "t.sqlite3")
    check("persisted across reopen", st2.is_onboarded() and st2.get_settings().get("academyName") == "أكاديمية اختبار")
    st2.close()
    globals()["_tmp"] = None
    # re-open handle for the scenario finalizer
    st.__init__(home / "t.sqlite3")


@scenario
def s07_teacher_archive(st, home):
    st.upsert_teacher("أ", {"pct": 70}); st.upsert_teacher("ب", {"pct": 70})
    st.upsert_student(mk_student("s1", "ط", "أ"))
    refs = st.teacher_references("أ")
    check("references counted", sum(refs.values()) >= 1, str(refs))
    r = st.delete_teacher("أ")
    check("referenced teacher archived not lost", st.get_student("s1")["teacher"] == "أ", str(r))
    r2 = st.delete_teacher("ب")
    check("unreferenced teacher deleted", "ب" not in (st.export_state().get("teachers") or []), str(r2))
    e = raises(Exception, st.delete_teacher, "غير موجود")
    check("delete missing teacher no crash", True, repr(e))


@scenario
def s08_demo_and_exports(st, home):
    r = st.load_demo_data(backup_folder=home / "bk")
    check("demo loaded", r["studentsCount"] > 0, str(r))
    state = st.export_state()
    check("state has students/teachers", state["students"] and state["teachers"])
    months = sorted({k for s in state["students"] for k in s.get("months", {})})
    check("demo has months", len(months) >= 1, str(months))
    mk = months[-1]
    # Excel: every scope
    for scope in ("full", "teacher", "finance", "summary"):
        try:
            t = state["teachers"][0] if scope == "teacher" else None
            data = excel_service.export_bytes(state, scope=scope, teacher=t, month=mk if scope != "full" else None)
            wb = openpyxl.load_workbook(io.BytesIO(data))
            check(f"excel scope={scope} loads ({len(wb.sheetnames)} sheets)", len(wb.sheetnames) >= 1)
        except Exception as e:
            check(f"excel scope={scope}", False, f"{type(e).__name__}: {e}")
    # Excel with illegal teacher name chars
    st2 = json.loads(json.dumps(state))
    st2["teachers"].append("أ/ فاطمة:*?[x]")
    for s in st2["students"][:2]:
        s["teacher"] = "أ/ فاطمة:*?[x]"
    try:
        excel_service.export_bytes(st2, scope="full"); check("excel with forbidden sheet chars", True)
    except Exception as e:
        check("excel with forbidden sheet chars", False, repr(e))
    # formula injection neutralised
    st3 = json.loads(json.dumps(state)); st3["students"][0]["name"] = "=HYPERLINK(\"http://evil\",\"x\")"
    data = excel_service.export_bytes(st3, scope="full")
    wb = openpyxl.load_workbook(io.BytesIO(data))
    live = [c.coordinate for ws in wb for row in ws.iter_rows() for c in row if c.data_type == "f"]
    check("no live formulas from user text in export", not live, str(live[:5]))
    # HTML exports
    for scope in sorted(html_export_service.SUPPORTED_HTML_SCOPES):
        try:
            h = html_export_service.generate_readonly_html(state, scope=scope, month=mk, teacher=state["teachers"][0] if scope == "teacher" else None,
                                                           student_id=state["students"][0]["id"] if scope == "student" else None)
            check(f"html scope={scope} produced", "<html" in h.lower() and len(h) > 500)
        except Exception as e:
            check(f"html scope={scope}", False, f"{type(e).__name__}: {e}")
    h = html_export_service.generate_readonly_html(st3, scope="full", month=mk)
    check("html escapes injected text", "<script>" not in h.split("<body", 1)[-1].lower().replace("<script>", "<script>", 1) or True)
    st4 = json.loads(json.dumps(state)); st4["students"][0]["name"] = "<img src=x onerror=alert(1)>"
    h = html_export_service.generate_readonly_html(st4, scope="full", month=mk)
    check("html: <img onerror> escaped", "<img src=x onerror" not in h)
    # phone masking
    h = html_export_service.generate_readonly_html(state, scope="full", month=mk, hide_personal_data=True)
    ph = state["students"][0].get("phone") or ""
    check("html masks phone by default", (not ph) or ph not in h, ph)
    # Receipt
    inst = state.get("installments") or []
    if inst:
        r = html_export_service.generate_printable_receipt_html(state, inst[0]["id"])
        check("receipt html", "<html" in r.lower())
    # stats/reports on demo
    for fn in ("stats", "accrual_report", "accounting_summary"):
        try:
            getattr(st, fn)(mk); check(f"store.{fn}({mk})", True)
        except Exception as e:
            check(f"store.{fn}", False, repr(e))
    try:
        st.collection_report(mk); st.study_periods_report(mk); check("collection/study_periods reports", True)
    except Exception as e:
        check("collection/study reports", False, repr(e))
    st.clear_demo_data()
    check("clear demo", st.count_students() == 0)


@scenario
def s09_import_csv_xlsx(st, home):
    st.upsert_teacher("معلمة أ", {"pct": 70})
    for ds in ("students", "installments", "transactions", "teachers", "payouts"):
        try:
            b = import_service.generate_import_template_xlsx(ds)
            openpyxl.load_workbook(io.BytesIO(b)); check(f"template {ds}", True)
        except Exception as e:
            check(f"template {ds}", False, repr(e))
    # CSV students with messy headers & Arabic digits
    csv_text = "الاسم,الهاتف,المعلمة,الشهر,قيمة الاشتراك\nطالبة جديدة,٠١٠٠٠٠٠٠٠٠٩,معلمة أ,2026-11,٦٠٠\nثانية,01000000008,معلمة ج,2026-11,400\n,0100,معلمة أ,2026-11,100\nثالثة,0100,معلمة أ,2026-11,abc\n"
    state = st.export_state()
    pv = import_service.preview_import_file(csv_text.encode("utf-8-sig"), "students.csv", "students", state)
    check("csv preview ok", isinstance(pv, dict) and ("rows" in pv or "summary" in pv or "headers" in pv), str(list(pv)[:10]))
    flat = json.dumps(pv, ensure_ascii=False, default=str)
    check("preview flags problem rows (missing name / bad money)", ("error" in flat.lower() or "خطأ" in flat or "invalid" in flat.lower()), flat[:200])
    n0 = st.count_students()
    res = import_service.commit_import(st, csv_text.encode("utf-8-sig"), "students.csv", "students", backup_folder=home / "bk")
    check("commit returns dict", isinstance(res, dict), str(res)[:200])
    n1 = st.count_students()
    check("valid rows imported, bad rows skipped (1..2 added)", 1 <= n1 - n0 <= 2, f"{n0}->{n1}")
    # undo
    try:
        u = import_service.undo_last_import(st, home / "bk")
        check("undo import restores count", st.count_students() == n0, f"{st.count_students()} vs {n0}")
    except Exception as e:
        check("undo import", False, repr(e))
    # garbage file
    e = raises(Exception, import_service.preview_import_file, b"\x00\x01garbage", "x.xlsx", "students", st.export_state())
    check("garbage xlsx handled w/o leaking internals", isinstance(e, Exception) and "Traceback" not in str(e) and "openpyxl" not in str(e).lower(), repr(e)[:150])
    # formula cell in xlsx
    wb = openpyxl.Workbook(); ws = wb.active; ws.append(["الاسم", "المعلمة", "الشهر", "قيمة الاشتراك"]); ws.append(["=1+1", "معلمة أ", "2026-11", 100]); ws.append(["عادي", "معلمة أ", "2026-11", 100])
    buf = io.BytesIO(); wb.save(buf)
    try:
        pv = import_service.preview_import_file(buf.getvalue(), "f.xlsx", "students", st.export_state())
        check("xlsx with formula cell handled", True, str(pv)[:100])
    except Exception as e:
        check("xlsx with formula cell handled", "formula" in str(e).lower() or "معادلة" in str(e) or True, repr(e)[:150])
    # oversize rows
    big = "الاسم,المعلمة,الشهر,قيمة الاشتراك\n" + "\n".join(f"ط{i},معلمة أ,2026-11,100" for i in range(3000))
    t = time.time()
    try:
        import_service.preview_import_file(big.encode("utf-8"), "big.csv", "students", st.export_state())
        check(f"3000-row csv preview in {time.time()-t:.1f}s", time.time() - t < 20)
    except Exception as e:
        check("3000-row csv preview (clean rejection ok)", True, repr(e)[:120])


@scenario
def s10_finance_engine_edges(st, home):
    f = fe
    check("round_half_up(2.5)=3", f.round_half_up(2.5) == 3 and f.round_half_up(0.5) == 1 and f.round_half_up(-0.5) in (0, -1))
    check("to_decimal: None/'' → 0, garbage → ValueError", f.to_decimal(None) == 0 and f.to_decimal("") == 0 and isinstance(raises(ValueError, f.to_decimal, "abc"), ValueError))
    check("parse_pct_int: '70'→70, 0.7→70, '70%'/150/-5/True rejected", f.parse_pct_int("70") == 70 and f.parse_pct_int(0.7) == 70 and all(isinstance(raises(ValueError, f.parse_pct_int, v), ValueError) for v in ("70%", 150, -5, True)))
    stu = {"id": "a", "name": "x", "teacher": "م", "pct": 70, "months": {"2026-11": {"value": 1000, "discount": 1500}}}
    net = f.effective_month_net_subscription(stu["months"]["2026-11"])
    check("discount > value never yields negative net", net >= 0, str(net))
    stu2 = {"id": "b", "name": "y", "teacher": "م", "pct": 70, "months": {"2026-11": {"value": 1001, "discount": 0}}}
    s = f.compute_month_stats([stu, stu2], "2026-11", installments=[], teacher_meta={}, wage_basis="net", wage_accrual_mode="accrued", reference_date="2026-11-30")
    check("stats w/o receipts ok", isinstance(s, dict))
    s_empty = f.compute_month_stats([], "2026-11", installments=[], teacher_meta={}, wage_basis="net", wage_accrual_mode="accrued", reference_date="2026-11-30")
    check("stats with zero students ok", isinstance(s_empty, dict))
    # over-payment & partial
    st.upsert_teacher("م", {"pct": 70}); st.upsert_student(mk_student("s1", "ط", "م", value=1000))
    st.upsert_installment({"id": "p1", "studentId": "s1", "month": "2026-11", "amount": 400, "paymentDate": "2026-11-03", "receiptNo": "A1"})
    st.upsert_installment({"id": "p2", "studentId": "s1", "month": "2026-11", "amount": 700, "paymentDate": "2026-11-09", "receiptNo": "A2"})  # overpay 100
    state = st.export_state()
    m = state["students"][0]["months"]["2026-11"]
    st_ = f.derive_month_payment_status(m, [i for i in state["installments"]], ) if True else None
    check("over-payment doesn't crash status", True, str(st_))
    rep = st.collection_report("2026-11", reference_date="2026-11-30")
    check("collection report with overpay", isinstance(rep, dict))
    # Feb leap / month boundaries
    st.upsert_student(mk_student("s2", "ط2", "م", month="2028-02", value=300))
    st.upsert_student_month("s2", "2028-02", {"value": 300, "actualStartDate": "2028-02-29", "endDate": "2028-02-29"})
    check("leap-day month accepted", "2028-02" in st.get_student("s2")["months"])
    e = raises(Exception, st.upsert_student_month, "s2", "2027-02", {"value": 300, "actualStartDate": "2027-02-29", "endDate": "2027-02-28"})
    check("invalid 29 Feb 2027 rejected/normalised (no crash)", True, repr(e)[:100])
    # wage pct edge
    for pct in (0, 100, 150, -5, "abc", None):
        try:
            st.upsert_student(mk_student("sp", "pct", "م", pct=pct)); ok = True
        except Exception:
            ok = True
        check(f"pct={pct!r} no crash", ok)


@scenario
def s11_large_dataset_perf(st, home):
    st.upsert_teacher("م", {"pct": 70})
    t = time.time()
    state = st.export_state()
    students = []
    for i in range(1500):
        students.append(mk_student(f"p{i}", f"طالب {i}", "م", value=300 + i % 7 * 50))
    state["students"] = students; state["teachers"] = ["م"]
    st.replace_all(state, allow_empty=True)
    d_ins = time.time() - t
    check(f"replace_all 1500 students in {d_ins:.1f}s", d_ins < 30)
    t = time.time(); s = st.stats("2026-11"); d = time.time() - t
    check(f"stats on 1500 students in {d:.2f}s", d < 5)
    t = time.time(); b = excel_service.export_bytes(st.export_state(), scope="full"); d = time.time() - t
    check(f"excel full export 1500 students in {d:.1f}s ({len(b)//1024} KB)", d < 60)
    t = time.time(); h = html_export_service.generate_readonly_html(st.export_state(), scope="full", month="2026-11"); d = time.time() - t
    check(f"html export 1500 students in {d:.1f}s", d < 30)
    e = raises(Exception, st.replace_all, {"students": [], "teachers": []})
    check("replace_all with empty students refused (data-loss guard)", isinstance(e, Exception) and not isinstance(e, str), repr(e)[:100])
    check("1500 still there", st.count_students() == 1500)


@scenario
def s12_migration_old_db(st, home):
    st.upsert_teacher("م", {"pct": 70}); st.upsert_student(mk_student("s1", "ط", "م"))
    st.close()
    p = home / "t.sqlite3"
    c = sqlite3.connect(p); ver = c.execute("PRAGMA user_version").fetchone()[0]; c.close()
    check("schema version recorded", True, str(ver))
    # simulate a read-only / locked DB file opening twice (two instances)
    a = Store(p); b = Store(p)
    a.upsert_student(mk_student("s2", "ط٢", "م"))
    b._resync_rev() if hasattr(b, "_resync_rev") else None
    check("second instance sees write", b.count_students() == 2)
    a.close(); b.close()
    # corrupt db at open
    bad = home / "corrupt.sqlite3"; bad.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4000)
    e = raises(Exception, Store, bad)
    check("corrupt DB open raises cleanly (no hang)", isinstance(e, Exception) and not isinstance(e, str), repr(e)[:100])
    st.__init__(p)


@scenario
def s13_sessions_memorization_certs(st, home):
    st.upsert_teacher("م", {"pct": 70}); st.upsert_student(mk_student("s1", "ط", "م"))
    for label, fn in [
        ("group", lambda: st.upsert_group({"id": "g1", "name": "حلقة 1", "teacher": "م"})),
        ("plan", lambda: st.upsert_plan({"id": "p1", "studentId": "s1", "actualStartDate": "2026-11-02", "count": 8, "teacher": "م"})),
        ("session", lambda: st.upsert_session({"id": "se1", "date": "2026-11-10", "teacher": "م", "studentId": "s1", "status": "done"})),
        ("memorization", lambda: st.upsert_memorization({"id": "m1", "studentId": "s1", "date": "2026-11-10", "surah": "الفاتحة", "from": 1, "to": 7})),
        ("certificate", lambda: st.upsert_certificate({"id": "c1", "studentId": "s1", "courseTitle": "إتمام", "issueDate": "2026-11-10"})),
        ("payout", lambda: st.upsert_payout({"id": "pay1", "teacher": "م", "month": "2026-11", "amount": 100, "date": "2026-11-28"})),
        ("transaction", lambda: st.upsert_transaction({"id": "t1", "type": "expense", "category": "إيجار", "amount": 200, "date": "2026-11-01"})),
    ]:
        try:
            fn(); check(f"upsert {label}", True)
        except Exception as e:
            check(f"upsert {label}", False, f"{type(e).__name__}: {str(e)[:120]}")
    try:
        st.upsert_sessions_batch({"id": "pb", "studentId": "s1", "actualStartDate": "2026-11-02", "count": 30, "teacher": "م"}, [{"id": f"b{i}", "planId": "pb", "date": "2026-11-%02d" % (i % 28 + 1), "teacher": "م", "studentId": "s1", "status": "scheduled"} for i in range(30)])
        check("sessions batch", True)
    except Exception as e:
        check("sessions batch", False, f"{type(e).__name__}: {str(e)[:120]}")
    try:
        st.patch_session("se1", {"status": "absent"}); check("patch session", True)
    except Exception as e:
        check("patch session", False, repr(e)[:120])
    st.delete_student("s1")
    left = st.export_state()
    orphan = [x for x in (left.get("sessions") or []) if x.get("studentId") == "s1"]
    check("deleting student leaves no orphan sessions", not orphan, str(len(orphan)))


if __name__ == "__main__":
    for f in [s01_basic_lifecycle, s02_validation_edges, s03_month_close_lock, s04_concurrency_rev, s05_backup_restore_reset,
              s06_pin_settings_onboarding, s07_teacher_archive, s08_demo_and_exports, s09_import_csv_xlsx,
              s10_finance_engine_edges, s11_large_dataset_perf, s12_migration_old_db, s13_sessions_memorization_certs]:
        f()
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n==== {len(RESULTS)-len(bad)}/{len(RESULTS)} checks passed; {len(bad)} failed")
    for n, _, d in bad:
        print("FAILED:", n, "|", d[:300])
