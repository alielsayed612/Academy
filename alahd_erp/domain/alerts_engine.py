"""Detailed Alerts & Notifications Engine with Deep-Link Navigation (Phase 5).

Computes all 16 alert categories server-side, grouped by severity ('critical', 'warning', 'info'),
with exact affected records, deep links (hash routing), WhatsApp reminder links, quick actions,
and dismiss/snooze state management.
"""
from __future__ import annotations

import datetime
import urllib.parse
from typing import Any

from .attendance_engine import (
    MONTH_RE,
    calculate_study_period,
    format_month_label,
    parse_iso_date,
)
from .finance_engine import (
    effective_month_collected,
    effective_month_net_subscription,
    effective_month_subscription,
    effective_month_wage,
    index_installments_by_student_month,
    raw_month_discount,
    resolve_actual_start_date,
    resolve_end_date,
    resolve_payment_date,
    round_half_up,
)


def _wa_link(phone: str, message: str) -> str:
    import re
    digits = re.sub(r"\D", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("01") and len(digits) == 11:
        digits = "2" + digits
    elif digits.startswith("00"):
        digits = digits[2:]
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{digits}?text={encoded}"


def compute_alerts(
    state: dict[str, Any],
    reference_date: str | None = None,
    backup_status: dict[str, Any] | None = None,
    include_dismissed: bool = False,
    month: str | None = None,
    backup_health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Computes all active and dismissed alerts across the academy."""
    if backup_status is None and backup_health is not None:
        backup_status = backup_health
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    ref_iso = ref_dt.isoformat()
    cur_mk = month.strip() if month and MONTH_RE.match(month.strip()) else f"{ref_dt.year:04d}-{ref_dt.month:02d}"

    settings = _v if isinstance(_v := state.get("settings"), dict) else {}
    alerts_cfg = _v if isinstance(_v := settings.get("alertsConfig"), dict) else {}
    duration_days = int(settings.get("defaultDurationDays") or 30)
    soon_days = int(_n if (_n := settings.get("expiringSoonDays")) is not None else 5)
    consec_abs_threshold = int(settings.get("consecutiveAbsenceThreshold") or 2)
    pct_source = str(settings.get("pctSource") or "student")
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")
    default_pct = int(_n if (_n := settings.get("defaultTeacherPct")) is not None else 70)
    academy_name = str(settings.get("academyName") or "أكاديمية يوم جديد")

    alerts_state = _v if isinstance(_v := state.get("alertsState"), dict) else {}
    dismissed_map = _v if isinstance(_v := alerts_state.get("dismissed"), dict) else {}
    read_map = _v if isinstance(_v := alerts_state.get("read"), dict) else {}
    import_warnings = _v if isinstance(_v := alerts_state.get("importWarnings"), list) else []
    raw_cm = state.get("closedMonths") or []
    if isinstance(raw_cm, dict):
        closed_months = set(raw_cm.keys())
    else:
        closed_months = {(c.get("month") if isinstance(c, dict) else str(c)) for c in raw_cm}

    students = [s for s in (state.get("students") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
    installments = [i for i in (state.get("installments") or []) if isinstance(i, dict)]
    payouts = [p for p in (state.get("payouts") or []) if isinstance(p, dict)]
    sessions = [s for s in (state.get("sessions") or []) if isinstance(s, dict)]
    teacher_meta = _v if isinstance(_v := state.get("teacherMeta"), dict) else {}
    inst_map = index_installments_by_student_month(installments)

    def is_enabled(alert_type: str) -> bool:
        val = alerts_cfg.get(alert_type)
        if isinstance(val, bool):
            return val
        if isinstance(val, dict):
            return bool(val.get("enabled", True))
        return True

    # Buckets for student/month-level alerts
    unpaid_items: list[dict[str, Any]] = []
    overdue_items: list[dict[str, Any]] = []
    expiring_soon_items: list[dict[str, Any]] = []
    expired_no_renewal_items: list[dict[str, Any]] = []
    missing_start_items: list[dict[str, Any]] = []
    not_started_items: list[dict[str, Any]] = []
    advance_not_started_items: list[dict[str, Any]] = []
    inconsistent_data_items: list[dict[str, Any]] = []
    missing_phone_items: list[dict[str, Any]] = []

    active_months_seen: set[str] = set()
    wages_by_teacher_month: dict[tuple[str, str], int] = {}

    for stu in students:
        sid = str(stu.get("id") or "")
        sname = str(stu.get("name") or "")
        tname = str(stu.get("teacher") or "")
        phone = str(stu.get("phone") or "").strip()
        months_dict = _v if isinstance(_v := stu.get("months"), dict) else {}

        # Phone check
        import re
        phone_digits = re.sub(r"\D", "", phone)
        if len(phone_digits) < 8:
            missing_phone_items.append({
                "id": f"phone_{sid}",
                "studentId": sid,
                "studentName": sname,
                "teacher": tname,
                "month": cur_mk,
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": "رقم الهاتف غير مسجل أو غير مكتمل",
                "deepLink": f"#/students/{sid}?highlight={sid}",
                "quickActions": [{"type": "edit_student", "label": "تعديل بيانات الطالبة", "studentId": sid}],
            })

        stu_active_mks = sorted(
            mk for mk, m in months_dict.items()
            if isinstance(m, dict) and effective_month_subscription(m) > 0
        )

        for mk, m in sorted(months_dict.items()):
            if not isinstance(m, dict) or not MONTH_RE.match(str(mk)):
                continue
            gross_sub = effective_month_subscription(m)
            raw_disc = raw_month_discount(m)
            net_sub = effective_month_net_subscription(m)
            stu_insts = inst_map.get((sid, mk)) or []
            coll = effective_month_collected(m, stu_insts, reference_date=ref_iso)
            rem = max(0, net_sub - coll)
            pay_dt = resolve_payment_date(m, stu_insts, reference_date=ref_iso)
            start_dt = resolve_actual_start_date(m)
            end_dt = resolve_end_date(m)

            if gross_sub > 0:
                active_months_seen.add(mk)

            # Inconsistent discount or date ordering
            if raw_disc > gross_sub or (start_dt and end_dt and end_dt < start_dt):
                reason_txt = (
                    f"الخصم ({raw_disc}) أكبر من قيمة الاشتراك ({gross_sub})"
                    if raw_disc > gross_sub
                    else f"تاريخ النهاية ({end_dt}) يسبق تاريخ البداية الفعلي ({start_dt})"
                )
                inconsistent_data_items.append({
                    "id": f"inconsistent_{sid}_{mk}",
                    "studentId": sid,
                    "studentName": sname,
                    "teacher": tname,
                    "month": mk,
                    "amount": gross_sub,
                    "paymentDate": pay_dt,
                    "actualStartDate": start_dt,
                    "reason": reason_txt,
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                    "quickActions": [{"type": "edit_student", "label": "تصحيح البيانات", "studentId": sid, "month": mk}],
                })

            if gross_sub <= 0:
                continue

            # Teacher wages tracking
            w = effective_month_wage(
                stu,
                m,
                teacher_meta=teacher_meta,
                pct_source=pct_source,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                student_month_installments=stu_insts,
                reference_date=ref_iso,
                default_pct=default_pct,
            )
            if tname and w > 0:
                wages_by_teacher_month[(tname, mk)] = wages_by_teacher_month.get((tname, mk), 0) + w

            # Unpaid or partial
            if rem > 0:
                wa_msg = (
                    f"السلام عليكم ورحمة الله وبركاته، تذكير لطيف من {academy_name} بخصوص اشتراك الطالبة "
                    f"{sname} لشهر {format_month_label(mk)} (المتبقي: {rem} ج.م). بارك الله فيكم."
                )
                wa_url = _wa_link(phone, wa_msg)
                actions = [
                    {
                        "type": "record_payment",
                        "label": "💳 تسجيل دفعة",
                        "studentId": sid,
                        "month": mk,
                        "amount": rem,
                    }
                ]
                if wa_url:
                    actions.append({"type": "whatsapp", "label": "💬 تذكير واتساب", "url": wa_url})

                item_payload = {
                    "id": f"unpaid_{sid}_{mk}",
                    "studentId": sid,
                    "studentName": sname,
                    "teacher": tname,
                    "month": mk,
                    "amount": rem,
                    "netSubscription": net_sub,
                    "collected": coll,
                    "paymentDate": pay_dt,
                    "actualStartDate": start_dt,
                    "reason": f"متبقي {rem} ج.م من أصل {net_sub} ج.م ({'مسدد جزئيًا' if coll > 0 else 'غير مسدد'})",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                    "quickActions": actions,
                }
                unpaid_items.append(item_payload)

                # Overdue if study already started (start_dt <= ref_iso) or month < cur_mk
                if (start_dt and start_dt <= ref_iso) or mk < cur_mk:
                    overdue_items.append({
                        **item_payload,
                        "id": f"overdue_{sid}_{mk}",
                        "reason": f"اشتراك متأخر السداد ({rem} ج.م) بعد بدء الدراسة ({start_dt or mk})",
                    })

            # Missing actualStartDate
            if not start_dt:
                missing_start_items.append({
                    "id": f"nostart_{sid}_{mk}",
                    "studentId": sid,
                    "studentName": sname,
                    "teacher": tname,
                    "month": mk,
                    "amount": net_sub,
                    "paymentDate": pay_dt,
                    "actualStartDate": "",
                    "reason": f"اشتراك مسجل لشهر {format_month_label(mk)} بدون تحديد تاريخ البداية الفعلي للدراسة",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                    "quickActions": [{"type": "edit_student", "label": "📅 تحديد تاريخ البداية", "studentId": sid, "month": mk}],
                })
            else:
                sp = calculate_study_period(
                    start_dt,
                    end_dt,
                    duration_days=duration_days,
                    reference_date=ref_iso,
                    expiring_soon_days=soon_days,
                )
                calc_end = sp["calculatedEndDate"]
                if sp["status"] == "not_started":
                    not_started_items.append({
                        "id": f"notstarted_{sid}_{mk}",
                        "studentId": sid,
                        "studentName": sname,
                        "teacher": tname,
                        "month": mk,
                        "amount": net_sub,
                        "paymentDate": pay_dt,
                        "actualStartDate": start_dt,
                        "endDate": calc_end,
                        "reason": f"تبدأ الدراسة بعد {sp.get('daysUntilStart', 0)} يوم ({start_dt})",
                        "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                        "quickActions": [{"type": "open_profile", "label": "عرض الملف", "studentId": sid}],
                    })
                    if coll > 0 and pay_dt and pay_dt < start_dt:
                        advance_not_started_items.append({
                            "id": f"adv_notstarted_{sid}_{mk}",
                            "studentId": sid,
                            "studentName": sname,
                            "teacher": tname,
                            "month": mk,
                            "amount": coll,
                            "paymentDate": pay_dt,
                            "actualStartDate": start_dt,
                            "endDate": calc_end,
                            "reason": f"مسددة مقدمًا بتاريخ {pay_dt} وتبدأ الدراسة فعليًا بتاريخ {start_dt}",
                            "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                            "quickActions": [{"type": "open_profile", "label": "عرض الملف", "studentId": sid}],
                        })
                elif sp["status"] == "expiring_soon":
                    rem_days = sp.get("remainingDays", 0)
                    wa_msg = (
                        f"السلام عليكم ورحمة الله وبركاته، نحيطكم علمًا من {academy_name} بأن دورة الطالبة "
                        f"{sname} ستنتهي بتاريخ {calc_end} (متبقي {rem_days} أيام)."
                    )
                    wa_url = _wa_link(phone, wa_msg)
                    q_acts = [{"type": "open_profile", "label": "تجديد الاشتراك", "studentId": sid, "month": mk}]
                    if wa_url:
                        q_acts.append({"type": "whatsapp", "label": "💬 إشعار واتساب", "url": wa_url})
                    expiring_soon_items.append({
                        "id": f"expiring_{sid}_{mk}",
                        "studentId": sid,
                        "studentName": sname,
                        "teacher": tname,
                        "month": mk,
                        "amount": net_sub,
                        "paymentDate": pay_dt,
                        "actualStartDate": start_dt,
                        "endDate": calc_end,
                        "reason": f"ينتهي الاشتراك خلال {rem_days} أيام (بتاريخ {calc_end})",
                        "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                        "quickActions": q_acts,
                    })
                elif sp["status"] == "expired":
                    has_later = any(later_mk > mk for later_mk in stu_active_mks)
                    if not has_later:
                        expired_no_renewal_items.append({
                            "id": f"expired_{sid}_{mk}",
                            "studentId": sid,
                            "studentName": sname,
                            "teacher": tname,
                            "month": mk,
                            "amount": net_sub,
                            "paymentDate": pay_dt,
                            "actualStartDate": start_dt,
                            "endDate": calc_end,
                            "reason": f"انتهت فترة الدراسة بتاريخ {calc_end} دون تجديد لشهر لاحق",
                            "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                            "quickActions": [{"type": "open_profile", "label": "تجديد الاشتراك", "studentId": sid}],
                        })

    # Uncompensated teacher cancellations & consecutive absences
    compensated_ids = {str(s.get("compensatedFor") or "") for s in sessions if s.get("compensatedFor")}
    stu_by_id = {str(s.get("id") or ""): s for s in students}
    uncomp_sessions: list[dict[str, Any]] = []
    sessions_by_student: dict[str, list[dict[str, Any]]] = {}

    for sess in sessions:
        sid = str(sess.get("studentId") or "")
        sessions_by_student.setdefault(sid, []).append(sess)
        xid = str(sess.get("id") or "")
        if sess.get("status") == "cancel_teacher" and xid not in compensated_ids:
            stu = stu_by_id.get(sid) or {}
            sdate = str(sess.get("date") or "")
            uncomp_sessions.append({
                "id": f"uncomp_{xid}",
                "sessionId": xid,
                "studentId": sid,
                "studentName": str(stu.get("name") or ""),
                "teacher": str(sess.get("teacher") or stu.get("teacher") or ""),
                "month": sdate[:7],
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": sdate,
                "reason": f"حصة ملغاة من المعلمة بتاريخ {sdate} لم يتم جدولة تعويض لها بعد",
                "deepLink": f"#/attendance?highlight={xid}",
                "quickActions": [
                    {"type": "compensate_session", "label": "⚡ تعويض تلقائي", "sessionId": xid}
                ],
            })

    consec_absence_items: list[dict[str, Any]] = []
    for sid, stu_sess_list in sessions_by_student.items():
        sorted_sess = sorted(stu_sess_list, key=lambda x: str(x.get("date") or ""))
        streak = 0
        max_streak = 0
        last_abs_date = ""
        for s_item in sorted_sess:
            if s_item.get("status") == "absent":
                streak += 1
                last_abs_date = str(s_item.get("date") or "")
                if streak > max_streak:
                    max_streak = streak
            elif s_item.get("status") in ("done", "makeup"):
                streak = 0
        if max_streak >= consec_abs_threshold:
            stu = stu_by_id.get(sid) or {}
            consec_absence_items.append({
                "id": f"abs_{sid}",
                "studentId": sid,
                "studentName": str(stu.get("name") or ""),
                "teacher": str(stu.get("teacher") or ""),
                "month": last_abs_date[:7] if len(last_abs_date) >= 7 else cur_mk,
                "amount": max_streak,
                "paymentDate": "",
                "actualStartDate": last_abs_date,
                "reason": f"غياب متتالي لعدد {max_streak} حصص (آخر غياب: {last_abs_date})",
                "deepLink": f"#/attendance?studentId={sid}&highlight={sid}",
                "quickActions": [{"type": "open_profile", "label": "عرض ملف الطالبة", "studentId": sid}],
            })

    # Teacher wages accrued vs paid
    paid_by_teacher_month: dict[tuple[str, str], int] = {}
    for po in payouts:
        tname = str(po.get("teacher") or "").strip()
        mk = str(po.get("month") or "").strip()
        amt = max(0, round_half_up(po.get("amount") or 0))
        if tname and mk:
            paid_by_teacher_month[(tname, mk)] = paid_by_teacher_month.get((tname, mk), 0) + amt

    unpaid_wage_items: list[dict[str, Any]] = []
    for (tname, mk), accrued in sorted(wages_by_teacher_month.items()):
        paid = paid_by_teacher_month.get((tname, mk), 0)
        rem_wage = max(0, accrued - paid)
        if rem_wage > 0:
            unpaid_wage_items.append({
                "id": f"wage_{tname}_{mk}",
                "studentId": "",
                "studentName": tname,
                "teacher": tname,
                "month": mk,
                "amount": rem_wage,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": f"مستحقات المعلمة لشهر {format_month_label(mk)}: {accrued} ج.م (المصروف: {paid} ج.م، المتبقي: {rem_wage} ج.م)",
                "deepLink": f"#/payroll?month={mk}&teacher={tname}&highlight={tname}",
                "quickActions": [
                    {"type": "record_payout", "label": "💵 صرف الراتب", "teacher": tname, "month": mk, "amount": rem_wage}
                ],
            })

    # Duplicate students
    from .models import detect_duplicate_students
    dups = detect_duplicate_students(students)
    dup_items: list[dict[str, Any]] = []
    for d in dups:
        first_rec = d["records"][0]
        sid = str(first_rec.get("id") or "")
        dup_items.append({
            "id": f"dup_{sid}",
            "studentId": sid,
            "studentName": d["name"],
            "teacher": str(first_rec.get("teacher") or ""),
            "month": cur_mk,
            "amount": d["count"],
            "paymentDate": "",
            "actualStartDate": "",
            "reason": f"تكرار الاسم ورقم الهاتف ({d['phone']}) في {d['count']} سجلات",
            "deepLink": f"#/students?search={d['name']}&highlight={sid}",
            "quickActions": [{"type": "open_profile", "label": "مراجعة السجل", "studentId": sid}],
        })

    # Unclosed past months
    unclosed_month_items: list[dict[str, Any]] = []
    for mk in sorted(active_months_seen):
        if mk < cur_mk and mk not in closed_months:
            unclosed_month_items.append({
                "id": f"unclosed_{mk}",
                "studentId": "",
                "studentName": format_month_label(mk),
                "teacher": "",
                "month": mk,
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": f"الشهر المنصرم ({format_month_label(mk)}) لم يتم إقفاله محاسبيًا بعد",
                "deepLink": f"#/system?month={mk}&highlight=month-lock",
                "quickActions": [{"type": "close_month", "label": "🔒 إقفال الشهر", "month": mk}],
            })

    # Backup status alert (Task 11)
    backup_items: list[dict[str, Any]] = []
    if backup_status:
        age_days = backup_status.get("daysSinceLastBackup")
        if age_days is None:
            age_days = backup_status.get("ageDays", backup_status.get("lastBackupAgeDays"))
        if backup_status.get("backupTooOld") or backup_status.get("staleWarning"):
            backup_items.append({
                "id": "backup_old",
                "studentId": "",
                "studentName": "النسخ الاحتياطي التلقائي",
                "teacher": "",
                "month": cur_mk,
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": (
                    f"مضى {age_days} يومًا منذ آخر نسخة احتياطية"
                    if age_days is not None
                    else "لم يتم إنشاء نسخة احتياطية حديثة بعد"
                ),
                "deepLink": "#/system?highlight=backups",
                "quickActions": [{"type": "create_backup", "label": "💾 نسخ احتياطي الآن"}],
            })
        elif backup_status.get("missingExpectedBackup") or (len(students) > 0 and backup_status.get("count", 1) == 0):
            backup_items.append({
                "id": "backup_missing",
                "studentId": "",
                "studentName": "لا توجد نسخة احتياطية محفوظة",
                "teacher": "",
                "month": cur_mk,
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": f"توجد {len(students)} طالبة مسجلة دون أخذ نسخة احتياطية في مجلد النسخ الاحتياطية بعد",
                "deepLink": "#/system?highlight=backups",
                "quickActions": [{"type": "create_backup", "label": "💾 نسخ احتياطي الآن"}],
            })
        extra_unreach = bool(
            backup_status.get("extraFolderUnreachable")
            or (backup_status.get("extraFolderConfigured") and not backup_status.get("extraFolderReachable", True))
        )
        if extra_unreach:
            backup_items.append({
                "id": "backup_extra_unreachable",
                "studentId": "",
                "studentName": "مجلد النسخ الاحتياطي الإضافي",
                "teacher": "",
                "month": cur_mk,
                "amount": 0,
                "paymentDate": "",
                "actualStartDate": "",
                "reason": f"تعذّر الوصول إلى مسار النسخ الإضافي: {backup_status.get('extraBackupFolder') or backup_status.get('extraFolder') or ''}",
                "deepLink": "#/system?highlight=backups",
                "quickActions": [],
            })

    # Scheduling conflicts (Task 5)
    from .automation_engine import detect_scheduling_conflicts
    sched_conflicts = detect_scheduling_conflicts(sessions, students)
    scheduling_conflict_items: list[dict[str, Any]] = []
    for sc in sched_conflicts:
        scheduling_conflict_items.append({
            "id": sc["id"],
            "studentId": sc.get("studentId", ""),
            "studentName": sc.get("studentName", ""),
            "teacher": sc.get("teacher", ""),
            "month": str(sc.get("date") or "")[:7] or cur_mk,
            "amount": 0,
            "paymentDate": "",
            "actualStartDate": sc.get("date", ""),
            "reason": sc["reason"],
            "deepLink": sc["deepLink"],
            "quickActions": [],
        })

    # Import warnings pending review
    import_warning_items: list[dict[str, Any]] = []
    for idx, iw in enumerate(import_warnings):
        if not isinstance(iw, dict):
            continue
        import_warning_items.append({
            "id": str(iw.get("id") or f"imp_warn_{idx+1}"),
            "studentId": str(iw.get("studentId") or ""),
            "studentName": str(iw.get("title") or "ملاحظة استيراد Excel"),
            "teacher": "",
            "month": cur_mk,
            "amount": int(iw.get("count") or 0),
            "paymentDate": "",
            "actualStartDate": "",
            "reason": str(iw.get("reason") or "توجد سجلات مستوردة تحتاج إلى مراجعة"),
            "deepLink": "#/import?highlight=history",
            "quickActions": [],
        })

    categories = [
        (
            "overdue_payments",
            "critical",
            "🚨 اشتراكات متأخرة السداد (Overdue Payments)",
            "طالبات بدأن الدراسة فعليًا أو انتهى شهرهن دون استكمال سداد الرسوم",
            overdue_items,
            "#/collections?timing=unpaid",
        ),
        (
            "inconsistent_discount_or_data",
            "critical",
            "⚠️ تعارض في البيانات أو الخصومات",
            "سجلات يتجاوز فيها الخصم قيمة الاشتراك أو يسبق تاريخ النهاية تاريخ البداية الفعلي",
            inconsistent_data_items,
            "#/students",
        ),
        (
            "duplicate_students",
            "critical",
            "👥 تكرار طالبات (نفس الاسم ورقم الهاتف)",
            "سجلات مكررة تحتاج إلى دمج أو مراجعة",
            dup_items,
            "#/system?highlight=duplicates",
        ),
        (
            "backup_status_warning",
            "critical",
            "🛡️ تنبيهات النسخ الاحتياطي وأمان البيانات",
            "تأخر النسخ الاحتياطي الدوري أو تعذّر الوصول إلى المجلد الخارجي الإضافي",
            backup_items,
            "#/system?highlight=backups",
        ),
        (
            "unpaid_or_partial",
            "warning",
            "💳 اشتراكات غير مسددة أو مسددة جزئيًا",
            "جميع الاشتراكات التي عليها مبالغ متبقية للتحصيل",
            unpaid_items,
            "#/collections",
        ),
        (
            "expiring_soon",
            "warning",
            f"⏳ اشتراكات قريبة الانتهاء (خلال {soon_days} أيام)",
            "طالبات تقترب دورتهن الدراسية من الانتهاء بناءً على تاريخ البداية الفعلي",
            expiring_soon_items,
            "#/students?studyStatus=expiring_soon",
        ),
        (
            "expired_no_renewal",
            "warning",
            "⌛ اشتراكات منتهية بدون تجديد",
            "طالبات انتهت فترة دراستهن الفعلية ولم يُسجل لهن اشتراك في شهر لاحق",
            expired_no_renewal_items,
            "#/students?studyStatus=expired",
        ),
        (
            "missing_actual_start",
            "warning",
            "📅 اشتراكات بدون تاريخ بداية فعلي (Actual Start Date)",
            "طالبات لهن قيمة اشتراك مسجلة ولكن لم يُحدد لهن تاريخ بدء الدراسة الفعلي",
            missing_start_items,
            "#/students",
        ),
        (
            "scheduling_conflict",
            "warning",
            "🗓️ تعارض مواعيد الحصص والجداول",
            "تداخل زمني بين حصتين لنفس المعلمة أو الطالبة في نفس اليوم والوقت",
            scheduling_conflict_items,
            "#/attendance",
        ),
        (
            "uncompensated_cancel_teacher",
            "warning",
            "🔄 حصص إلغاء معلمة غير معوضة",
            "حصص ألغتها المعلمة وتنتظر جدولة حصة تعويضية وتمديد تاريخ النهاية",
            uncomp_sessions,
            "#/attendance?status=cancel_teacher",
        ),
        (
            "consecutive_absences",
            "warning",
            f"📉 غياب متتالي للطالبات ({consec_abs_threshold}+ حصص)",
            "طالبات تغيبن لعدد متتالي من الحصص ويحتجن للمتابعة",
            consec_absence_items,
            "#/attendance?status=absent",
        ),
        (
            "unpaid_teacher_wages",
            "warning",
            "💰 رواتب معلمات مستحقة لم تُصرف بالكامل",
            "معلمات لهن أجور مستحقة عن أشهر دراسية لم يُستكمل صرفها بعد",
            unpaid_wage_items,
            "#/payroll",
        ),
        (
            "unclosed_past_month",
            "info",
            "🔒 أشهر منصرمة غير مقفلة محاسبيًا",
            "أشهر سابقة انتهت ويمكن إقفالها لمنع التعديل غير المقصود",
            unclosed_month_items,
            "#/system?highlight=month-lock",
        ),
        (
            "not_yet_started",
            "info",
            "🌱 طالبات لم تبدأ دراستهن بعد (تاريخ بداية مستقبلي)",
            "طالبات مسجلات بتاريخ بداية فعلي قادم",
            not_started_items,
            "#/students?studyStatus=not_started",
        ),
        (
            "paid_in_advance_not_started",
            "info",
            "✨ دفع مسبق قبل تاريخ البداية الفعلي",
            "طالبات سددن الرسوم مقدمًا قبل موعد بدء الدراسة الفعلي",
            advance_not_started_items,
            "#/collections?timing=paid_in_advance",
        ),
        (
            "invalid_or_missing_phone",
            "info",
            "📱 طالبات بدون رقم هاتف صالح",
            "سجلات ينقصها رقم هاتف للتواصل عبر واتساب",
            missing_phone_items,
            "#/students",
        ),
        (
            "import_warnings_pending",
            "info",
            "📥 ملاحظات استيراد Excel قيد المراجعة",
            "تحذيرات مسجلة من آخر عملية استيراد",
            import_warning_items,
            "#/import",
        ),
    ]

    active_alerts: list[dict[str, Any]] = []
    dismissed_alerts: list[dict[str, Any]] = []

    for cat_key, severity, title, description, items, default_link in categories:
        if not is_enabled(cat_key) or not items:
            continue

        visible_items = []
        cat_dismiss_info = dismissed_map.get(cat_key)
        cat_is_dismissed = False
        if isinstance(cat_dismiss_info, dict):
            exp = str(cat_dismiss_info.get("expiresAt") or "")
            if not exp or exp > ref_iso:
                cat_is_dismissed = True

        for it in items:
            item_key = f"{cat_key}:{it['id']}"
            it_dismiss = dismissed_map.get(item_key)
            it_is_dismissed = False
            if isinstance(it_dismiss, dict):
                exp = str(it_dismiss.get("expiresAt") or "")
                if not exp or exp > ref_iso:
                    it_is_dismissed = True
            is_read = bool("__all__" in read_map or item_key in read_map or cat_key in read_map or it.get("id") in read_map)
            it_copy = dict(
                it,
                alertKey=item_key,
                category=cat_key,
                dismissed=cat_is_dismissed or it_is_dismissed,
                read=is_read,
                timestamp=ref_iso,
            )
            if cat_is_dismissed or it_is_dismissed:
                if include_dismissed:
                    visible_items.append(it_copy)
            else:
                visible_items.append(it_copy)

        alert_obj = {
            "key": cat_key,
            "severity": severity,
            "title": title,
            "description": description,
            "count": len(visible_items) if visible_items else len(items),
            "deepLink": visible_items[0]["deepLink"] if visible_items else default_link,
            "items": visible_items if visible_items else items,
            "dismissed": cat_is_dismissed or len(visible_items) == 0,
            "dismissInfo": cat_dismiss_info if isinstance(cat_dismiss_info, dict) else None,
        }

        if alert_obj["dismissed"]:
            dismissed_alerts.append(alert_obj)
            if include_dismissed:
                active_alerts.append(alert_obj)
        else:
            active_alerts.append(alert_obj)

    by_severity = {
        "critical": [a for a in active_alerts if a["severity"] == "critical" and not a["dismissed"]],
        "warning": [a for a in active_alerts if a["severity"] == "warning" and not a["dismissed"]],
        "info": [a for a in active_alerts if a["severity"] == "info" and not a["dismissed"]],
    }
    total_active_categories = sum(len(v) for v in by_severity.values())
    total_active_items = sum(a["count"] for v in by_severity.values() for a in v)

    crit_cnt = sum(a["count"] for a in by_severity["critical"])
    warn_cnt = sum(a["count"] for a in by_severity["warning"])
    info_cnt = sum(a["count"] for a in by_severity["info"])
    flat_items: list[dict[str, Any]] = []
    for a in active_alerts:
        for it in a.get("items") or []:
            it_flat = dict(it)
            it_flat.setdefault("id", it_flat.get("alertKey") or f"{a['key']}:{it.get('id', '')}")
            it_flat.setdefault("severity", a["severity"])
            it_flat.setdefault("category", a["key"])
            it_flat.setdefault("title", it.get("title") or a["title"])
            it_flat.setdefault("detail", it.get("detail") or it.get("subtitle") or a["description"])
            it_flat.setdefault("deepLink", it.get("deepLink") or a["deepLink"])
            flat_items.append(it_flat)

    return {
        "referenceDate": ref_iso,
        "totalCategories": total_active_categories,
        "totalItems": total_active_items,
        "countsBySeverity": {
            "critical": crit_cnt,
            "warning": warn_cnt,
            "info": info_cnt,
        },
        "counts": {
            "total": total_active_items,
            "unread": sum(1 for x in flat_items if not x.get("dismissed") and not x.get("read")),
            "critical": crit_cnt,
            "warning": warn_cnt,
            "info": info_cnt,
            "dismissed": len(dismissed_alerts),
        },
        "groups": by_severity,
        "alerts": active_alerts,
        "items": flat_items,
        "dismissedAlerts": dismissed_alerts,
    }
