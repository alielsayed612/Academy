"""General Statistics Engine (Phase 4).

Computes academy-wide KPIs, monthly trend series, and multi-dimensional breakdowns
with drill-down deep links for any period:
  - 'all_time' (default)
  - 'this_month'
  - 'last_3'
  - 'last_6'
  - 'this_year'
  - 'custom' (from_month..to_month or start_date..end_date)

Empty-state safe: never produces NaN or division-by-zero errors.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from .attendance_engine import (
    MONTH_RE,
    calculate_study_period,
    format_month_label,
    parse_iso_date,
)
from .finance_engine import (
    effective_month_collected,
    effective_month_discount,
    effective_month_net_subscription,
    effective_month_subscription,
    effective_month_wage,
    index_installments_by_student_month,
    parse_pct_int,
    resolve_actual_start_date,
    resolve_end_date,
    round_half_up,
)


def _discover_all_months(state: dict[str, Any], ref_dt: datetime.date) -> list[str]:
    keys: set[str] = set()
    for stu in state.get("students") or []:
        if isinstance(stu, dict) and isinstance(stu.get("months"), dict):
            for mk in stu["months"].keys():
                if MONTH_RE.match(str(mk)):
                    keys.add(str(mk))
    for inst in state.get("installments") or []:
        if isinstance(inst, dict):
            mk = str(inst.get("month") or "")
            if MONTH_RE.match(mk):
                keys.add(mk)
    for po in state.get("payouts") or []:
        if isinstance(po, dict):
            mk = str(po.get("month") or "")
            if MONTH_RE.match(mk):
                keys.add(mk)
    for tx in state.get("transactions") or []:
        if isinstance(tx, dict):
            mk = str(tx.get("month") or str(tx.get("date") or "")[:7])
            if MONTH_RE.match(mk):
                keys.add(mk)
    if not keys:
        keys.add(f"{ref_dt.year:04d}-{ref_dt.month:02d}")
    return sorted(keys)


def _resolve_target_months(
    all_months: list[str],
    period: str,
    ref_dt: datetime.date,
    from_month: str = "",
    to_month: str = "",
) -> list[str]:
    cur_mk = f"{ref_dt.year:04d}-{ref_dt.month:02d}"
    p = (period or "all_time").strip().lower()

    if p == "this_month":
        target_m = from_month if (from_month and MONTH_RE.match(from_month)) else cur_mk
        return [target_m]

    if p in ("last_3", "last_6"):
        count = 3 if p == "last_3" else 6
        result = []
        base_total = ref_dt.year * 12 + (ref_dt.month - 1)
        for offset in range(-(count - 1), 1):
            tot = base_total + offset
            result.append(f"{tot // 12:04d}-{(tot % 12) + 1:02d}")
        return result

    if p == "this_year":
        yr_prefix = f"{ref_dt.year:04d}-"
        yr_months = [m for m in all_months if m.startswith(yr_prefix)]
        if not yr_months:
            yr_months = [f"{ref_dt.year:04d}-{m:02d}" for m in range(1, 13)]
        return sorted(set(yr_months))

    if p == "custom":
        fm = from_month if MONTH_RE.match(str(from_month or "")) else ""
        tm = to_month if MONTH_RE.match(str(to_month or "")) else ""
        if fm and tm and fm <= tm:
            sy, sm = map(int, fm.split("-"))
            ey, em = map(int, tm.split("-"))
            res: list[str] = []
            cur_tot = sy * 12 + (sm - 1)
            end_tot = ey * 12 + (em - 1)
            while cur_tot <= end_tot and len(res) <= 120:
                res.append(f"{cur_tot // 12:04d}-{(cur_tot % 12) + 1:02d}")
                cur_tot += 1
            return res
        return [m for m in all_months if (not fm or m >= fm) and (not tm or m <= tm)]

    # Default: all_time
    return all_months


def compute_general_statistics(
    state: dict[str, Any],
    period: str = "all_time",
    from_month: str = "",
    to_month: str = "",
    reference_date: str | None = None,
    start_date: str | None = "",
    end_date: str | None = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Computes comprehensive general statistics, trends, and breakdowns for the selected period."""
    from_month = str(from_month or (start_date[:7] if start_date else ""))
    to_month = str(to_month or (end_date[:7] if end_date else ""))
    settings = _v if isinstance(_v := state.get("settings"), dict) else {}
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    ref_iso = ref_dt.isoformat()

    pct_source = str(settings.get("pctSource") or "student")
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")
    default_pct = int(_n if (_n := settings.get("defaultTeacherPct")) is not None else 70)
    duration_days = int(settings.get("defaultDurationDays") or 30)
    soon_days = int(_n if (_n := settings.get("expiringSoonDays")) is not None else 5)

    teacher_filter = str(kwargs.get("teacher_filter") or "").strip()
    students = [s for s in (state.get("students") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
    if teacher_filter:
        if teacher_filter == "__unassigned__":
            students = [s for s in students if not str(s.get("teacher") or "").strip()]
        else:
            students = [s for s in students if str(s.get("teacher") or "").strip() == teacher_filter]
    filtered_student_ids = {str(s.get("id") or "") for s in students if s.get("id")}

    installments = [
        i for i in (state.get("installments") or [])
        if isinstance(i, dict) and (not teacher_filter or str(i.get("studentId") or "") in filtered_student_ids)
    ]
    payouts = [
        p for p in (state.get("payouts") or [])
        if isinstance(p, dict) and (
            not teacher_filter
            or (teacher_filter != "__unassigned__" and str(p.get("teacher") or "").strip() == teacher_filter)
        )
    ]
    transactions = [t for t in (state.get("transactions") or []) if isinstance(t, dict)]
    sessions = [
        s for s in (state.get("sessions") or [])
        if isinstance(s, dict) and (
            not teacher_filter
            or str(s.get("teacher") or "").strip() == teacher_filter
            or str(s.get("studentId") or "") in filtered_student_ids
        )
    ]
    memorization = [
        m for m in (state.get("memorization") or [])
        if isinstance(m, dict) and (
            not teacher_filter
            or str(m.get("teacher") or "").strip() == teacher_filter
            or str(m.get("studentId") or "") in filtered_student_ids
        )
    ]
    madrasa_batches = [b for b in (state.get("madrasaBatches") or []) if isinstance(b, dict)]
    madrasa_enrollments = [e for e in (state.get("madrasaEnrollments") or []) if isinstance(e, dict)]
    teacher_meta = _v if isinstance(_v := state.get("teacherMeta"), dict) else {}

    all_months = _discover_all_months(state, ref_dt)
    target_months = _resolve_target_months(all_months, period, ref_dt, from_month=from_month, to_month=to_month)
    target_months_set = set(target_months)
    inst_map = index_installments_by_student_month(installments)

    # Track KPIs & Breakdowns
    active_student_ids: set[str] = set()
    new_student_ids: set[str] = set()
    churned_student_ids: set[str] = set()
    retained_student_ids: set[str] = set()
    eligible_retention_ids: set[str] = set()

    total_gross = 0
    total_discounts = 0
    total_net = 0
    total_collected = 0
    total_wages_accrued = 0

    by_month_map: dict[str, dict[str, Any]] = {
        mk: {
            "month": mk,
            "label": format_month_label(mk),
            "activeStudents": 0,
            "grossSubscriptions": 0,
            "discounts": 0,
            "revenue": 0,
            "netSubscriptions": 0,
            "collected": 0,
            "collectedAccrual": 0,
            "collectedCash": 0,
            "uncollected": 0,
            "wages": 0,
            "profit": 0,
            "academyNet": 0,
            "collectionRatePct": 0,
            "deepLink": f"#/students?month={mk}",
        }
        for mk in target_months
    }

    by_teacher_map: dict[str, dict[str, Any]] = {}
    for tname in state.get("teachers") or []:
        t_clean = str(tname).strip()
        if t_clean and (not teacher_filter or t_clean == teacher_filter):
            by_teacher_map[t_clean] = {
                "teacher": t_clean,
                "studentsIds": set(),
                "revenue": 0,
                "collected": 0,
                "uncollected": 0,
                "wages": 0,
                "paidWages": 0,
            }

    by_package_map: dict[str, dict[str, Any]] = {}

    last_month_in_period = max(target_months) if target_months else ""

    for stu in students:
        sid = str(stu.get("id") or "")
        tname = str(stu.get("teacher") or "").strip() or "بدون معلمة"
        pkg = str(stu.get("type") or "").strip() or "غير محدد"
        months_dict = _v if isinstance(_v := stu.get("months"), dict) else {}

        # Determine student's earliest active month overall
        stu_active_mks = sorted(
            mk for mk, m in months_dict.items()
            if isinstance(m, dict) and effective_month_subscription(m) > 0
        )
        earliest_mk = stu_active_mks[0] if stu_active_mks else ""
        latest_mk = stu_active_mks[-1] if stu_active_mks else ""

        in_period_active = False

        for mk in target_months:
            m = months_dict.get(mk)
            if not isinstance(m, dict):
                continue
            gross_sub = effective_month_subscription(m)
            disc = effective_month_discount(m)
            net_sub = effective_month_net_subscription(m)
            stu_insts = inst_map.get((sid, mk))
            wage = effective_month_wage(
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
            if gross_sub <= 0 and wage <= 0:
                continue

            coll = effective_month_collected(m, stu_insts, reference_date=ref_iso)
            if gross_sub > 0:
                in_period_active = True
                active_student_ids.add(sid)
                by_month_map[mk]["activeStudents"] += 1

            total_gross += gross_sub
            total_discounts += disc
            total_net += net_sub
            total_collected += coll
            total_wages_accrued += wage

            m_bucket = by_month_map[mk]
            m_bucket["grossSubscriptions"] += gross_sub
            m_bucket["discounts"] += disc
            m_bucket["revenue"] += net_sub
            m_bucket["netSubscriptions"] = m_bucket["revenue"]
            m_bucket["collected"] += coll
            m_bucket["collectedAccrual"] = m_bucket["collected"]
            m_bucket["collectedCash"] = m_bucket["collected"]
            m_bucket["uncollected"] += max(0, net_sub - coll)
            m_bucket["wages"] += wage
            m_bucket["profit"] = m_bucket["revenue"] - m_bucket["wages"]
            m_bucket["academyNet"] = m_bucket["profit"]
            m_bucket["collectionRatePct"] = round_half_up((Decimal(m_bucket["collected"]) * Decimal(100)) / Decimal(m_bucket["revenue"])) if m_bucket["revenue"] > 0 else 0

            t_bucket = by_teacher_map.setdefault(
                tname,
                {
                    "teacher": tname,
                    "studentsIds": set(),
                    "revenue": 0,
                    "collected": 0,
                    "uncollected": 0,
                    "wages": 0,
                    "paidWages": 0,
                },
            )
            if gross_sub > 0:
                t_bucket["studentsIds"].add(sid)
            t_bucket["revenue"] += net_sub
            t_bucket["collected"] += coll
            t_bucket["uncollected"] += max(0, net_sub - coll)
            t_bucket["wages"] += wage

            p_bucket = by_package_map.setdefault(
                pkg,
                {
                    "package": pkg,
                    "studentsIds": set(),
                    "revenue": 0,
                    "collected": 0,
                },
            )
            if gross_sub > 0:
                p_bucket["studentsIds"].add(sid)
            p_bucket["revenue"] += net_sub
            p_bucket["collected"] += coll

        if in_period_active and earliest_mk in target_months_set:
            new_student_ids.add(sid)

        # Check churn / expired status
        is_expired_latest = False
        if latest_mk and latest_mk in months_dict:
            m_latest = months_dict[latest_mk]
            sp = calculate_study_period(
                resolve_actual_start_date(m_latest),
                resolve_end_date(m_latest),
                duration_days=duration_days,
                reference_date=ref_iso,
                expiring_soon_days=soon_days,
            )
            if sp["status"] == "expired":
                is_expired_latest = True

        if stu_active_mks:
            eligible_retention_ids.add(sid)
            if (last_month_in_period and latest_mk < last_month_in_period) or is_expired_latest:
                churned_student_ids.add(sid)
            else:
                retained_student_ids.add(sid)

    # Salary payouts & transactions in period
    total_wages_paid = 0
    for po in payouts:
        mk = str(po.get("month") or "")
        if mk in target_months_set:
            amt = max(0, round_half_up(po.get("amount") or 0))
            total_wages_paid += amt
            tname = str(po.get("teacher") or "").strip() or "بدون معلمة"
            if tname in by_teacher_map:
                by_teacher_map[tname]["paidWages"] += amt

    extra_revenue = 0
    operating_expense = 0
    for tx in transactions:
        mk = str(tx.get("month") or str(tx.get("date") or "")[:7])
        if mk in target_months_set:
            amt = max(0, round_half_up(tx.get("amount") or 0))
            if tx.get("type") == "revenue":
                extra_revenue += amt
            elif tx.get("type") == "expense":
                operating_expense += amt

    # Payment method breakdown from receipts in target months
    by_method_map: dict[str, dict[str, Any]] = {}
    for inst in installments:
        mk = str(inst.get("month") or "")
        pay_dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
        if mk in target_months_set and (not pay_dt or pay_dt <= ref_iso):
            method = str(inst.get("method") or "نقدي").strip() or "نقدي"
            amt = max(0, round_half_up(inst.get("amount") or 0))
            mb = by_method_map.setdefault(method, {"method": method, "receiptsCount": 0, "amount": 0})
            mb["receiptsCount"] += 1
            mb["amount"] += amt

    # Attendance breakdown
    status_labels = {
        "done": "حضور مكتمل",
        "scheduled": "مجدولة",
        "absent": "غياب طالبة",
        "excused": "اعتذار مسبق",
        "late": "تأخر",
        "cancel_teacher": "إلغاء معلمة",
        "cancel_admin": "إلغاء إداري",
        "postponed": "مؤجلة",
        "makeup": "حصة تعويضية",
    }
    att_counts: dict[str, int] = {k: 0 for k in status_labels}
    compensated_count = 0
    for sess in sessions:
        sdate = str(sess.get("date") or "")
        if sdate[:7] in target_months_set or period == "all_time":
            st = str(sess.get("status") or "scheduled")
            att_counts[st] = att_counts.get(st, 0) + 1
            if sess.get("compensatedFor"):
                compensated_count += 1

    # Memorization summary
    mem_in_period = [
        m for m in memorization
        if (str(m.get("date") or "")[:7] in target_months_set or period == "all_time")
    ]
    total_pages = sum(float(m.get("pages") or 0) for m in mem_in_period)
    scores = [float(m["score"]) for m in mem_in_period if m.get("score") is not None and m.get("score") != ""]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
    mem_tracks: dict[str, dict[str, Any]] = {}
    for m in mem_in_period:
        tr = str(m.get("track") or "حفظ جديد")
        tb = mem_tracks.setdefault(tr, {"track": tr, "count": 0, "pages": 0.0})
        tb["count"] += 1
        tb["pages"] = round(tb["pages"] + float(m.get("pages") or 0), 1)

    # Madrasa summary
    mad_fees = sum(max(0, round_half_up(e.get("feePaid") or 0)) for e in madrasa_enrollments)
    streaks = [int(e.get("streak") or 0) for e in madrasa_enrollments]
    avg_streak = round(sum(streaks) / len(streaks), 1) if streaks else 0.0
    max_streak = max(streaks) if streaks else 0

    active_count = len(active_student_ids)
    outstanding = max(0, total_net - total_collected)
    collection_rate = (
        round_half_up((Decimal(total_collected) * Decimal(100)) / Decimal(total_net))
        if total_net > 0
        else 0
    )
    avg_rev_per_student = (
        round_half_up(Decimal(total_net) / Decimal(active_count))
        if active_count > 0
        else 0
    )
    retention_rate = (
        round_half_up((Decimal(len(retained_student_ids)) * Decimal(100)) / Decimal(len(eligible_retention_ids)))
        if eligible_retention_ids
        else 0
    )
    net_operating_profit = (total_net - total_wages_accrued) + extra_revenue - operating_expense

    teacher_breakdown = []
    for tb in by_teacher_map.values():
        s_cnt = len(tb["studentsIds"])
        rev = tb["revenue"]
        coll = tb["collected"]
        rate = round_half_up((Decimal(coll) * Decimal(100)) / Decimal(rev)) if rev > 0 else 0
        t_param = "__unassigned__" if tb["teacher"] == "بدون معلمة" else tb["teacher"]
        t_meta_obj = (teacher_meta or {}).get(tb["teacher"]) if isinstance(teacher_meta, dict) else {}
        t_pct = parse_pct_int((t_meta_obj or {}).get("pct", (t_meta_obj or {}).get("defaultPct", default_pct)))
        unpaid_bal = max(0, tb["wages"] - tb["paidWages"])
        teacher_breakdown.append({
            "teacher": tb["teacher"],
            "teacherKey": t_param,
            "pct": t_pct,
            "studentsCount": s_cnt,
            "activeInPeriod": s_cnt,
            "revenue": rev,
            "netSubscriptions": rev,
            "collected": coll,
            "uncollected": tb["uncollected"],
            "wages": tb["wages"],
            "wagesAccrued": tb["wages"],
            "paidWages": tb["paidWages"],
            "wagesPaid": tb["paidWages"],
            "salariesPaid": tb["paidWages"],
            "unpaidBalance": unpaid_bal,
            "wagesUnpaid": unpaid_bal,
            "academyNet": rev - tb["wages"],
            "collectionRatePct": rate,
            "deepLink": f"#/students?teacher={t_param}",
        })
    teacher_breakdown.sort(key=lambda x: (x["revenue"], x["studentsCount"]), reverse=True)

    package_breakdown = []
    for pb in by_package_map.values():
        pkg_rev = pb["revenue"]
        share_pct = round_half_up((Decimal(pkg_rev) * Decimal(100)) / Decimal(total_net)) if total_net > 0 else 0
        package_breakdown.append({
            "package": pb["package"],
            "studentsCount": len(pb["studentsIds"]),
            "revenue": pkg_rev,
            "netRevenue": pkg_rev,
            "netSubscriptions": pkg_rev,
            "collected": pb["collected"],
            "uncollected": max(0, pkg_rev - pb["collected"]),
            "sharePct": share_pct,
            "deepLink": f"#/students?type={pb['package']}",
        })
    package_breakdown.sort(key=lambda x: x["revenue"], reverse=True)

    method_breakdown = [
        {
            "method": mb["method"],
            "receiptsCount": mb["receiptsCount"],
            "count": mb["receiptsCount"],
            "amount": mb["amount"],
            "deepLink": f"#/collections?method={mb['method']}",
        }
        for mb in sorted(by_method_map.values(), key=lambda x: x["amount"], reverse=True)
    ]

    attendance_breakdown = [
        {
            "status": st,
            "label": lbl,
            "count": att_counts.get(st, 0),
            "deepLink": f"#/attendance?status={st}",
        }
        for st, lbl in status_labels.items()
    ]
    done_cnt = att_counts.get("done", 0) + att_counts.get("makeup", 0) + att_counts.get("late", 0)
    denom_cnt = done_cnt + att_counts.get("absent", 0) + att_counts.get("excused", 0)
    att_rate = round_half_up((Decimal(done_cnt) * Decimal(100)) / Decimal(denom_cnt)) if denom_cnt > 0 else 100

    is_empty = (
        len(students) == 0
        and len(installments) == 0
        and len(transactions) == 0
        and len(sessions) == 0
    )

    return {
        "period": period,
        "fromMonth": target_months[0] if target_months else "",
        "toMonth": target_months[-1] if target_months else "",
        "months": target_months,
        "isEmpty": is_empty,
        "kpis": {
            "totalRegisteredStudents": len(students),
            "totalStudents": len(students),
            "activeStudents": active_count,
            "newStudents": len(new_student_ids),
            "churnedStudents": len(churned_student_ids),
            "totalSubscriptionsGross": total_gross,
            "grossSubscriptions": total_gross,
            "totalDiscounts": total_discounts,
            "discounts": total_discounts,
            "totalSubscriptionsNet": total_net,
            "netSubscriptions": total_net,
            "totalCollected": total_collected,
            "collectedTotal": total_collected,
            "totalOutstanding": outstanding,
            "uncollectedTotal": outstanding,
            "collectionRatePct": collection_rate,
            "teacherWagesAccrued": total_wages_accrued,
            "teacherWagesPaid": total_wages_paid,
            "teacherWagesRemaining": max(0, total_wages_accrued - total_wages_paid),
            "teacherWagesUnpaid": max(0, total_wages_accrued - total_wages_paid),
            "extraRevenue": extra_revenue,
            "operatingExpense": operating_expense,
            "netOperatingProfit": net_operating_profit,
            "academyNetProfit": net_operating_profit,
            "avgRevenuePerStudent": avg_rev_per_student,
            "retentionRatePct": retention_rate,
        },
        "byMonth": [by_month_map[mk] for mk in target_months],
        "monthlyTrends": [by_month_map[mk] for mk in target_months],
        "monthlyTrend": [by_month_map[mk] for mk in target_months],
        "students": {
            "total": len(students),
            "activeStudy": active_count,
            "newInPeriod": len(new_student_ids),
            "expiredStudy": len(churned_student_ids),
            "retentionRatePct": retention_rate,
        },
        "finance": {
            "grossSubscriptions": total_gross,
            "discounts": total_discounts,
            "netSubscriptions": total_net,
            "collectedAccrual": total_collected,
            "collectedCash": total_collected,
            "collectionRatePct": collection_rate,
            "outstandingReceivables": outstanding,
            "avgRevenuePerActiveStudent": avg_rev_per_student,
            "teacherWagesAccrued": total_wages_accrued,
            "teacherSalariesPaid": total_wages_paid,
            "teacherUnpaidBalance": max(0, total_wages_accrued - total_wages_paid),
            "netOperatingProfit": net_operating_profit,
            "netCashBalance": (total_collected + extra_revenue) - (total_wages_paid + operating_expense),
        },
        "teachers": {
            "total": len(teacher_breakdown),
        },
        "sessions": {
            "totalSessions": sum(att_counts.values()),
            "attendanceRatePct": att_rate,
            "done": att_counts.get("done", 0),
            "doneCount": att_counts.get("done", 0),
            "absent": att_counts.get("absent", 0),
            "absentCount": att_counts.get("absent", 0),
            "cancelTeacherCount": att_counts.get("cancel_teacher", 0),
            "makeupCount": att_counts.get("makeup", 0),
            "uncompensatedCancellations": max(0, att_counts.get("cancel_teacher", 0) - compensated_count),
        },
        "attendance": {
            "totalSessions": sum(att_counts.values()),
            "attendanceRatePct": att_rate,
            "done": att_counts.get("done", 0),
            "doneCount": att_counts.get("done", 0),
            "absent": att_counts.get("absent", 0),
            "absentCount": att_counts.get("absent", 0),
            "cancelTeacherCount": att_counts.get("cancel_teacher", 0),
            "makeupCount": att_counts.get("makeup", 0),
            "uncompensatedCancellations": max(0, att_counts.get("cancel_teacher", 0) - compensated_count),
        },
        "memorization": {
            "totalLogs": len(mem_in_period),
            "totalPages": round(total_pages, 1),
            "avgScore": avg_score,
        },
        "madrasa": {
            "batchesCount": len(madrasa_batches),
            "enrollmentsCount": len(madrasa_enrollments),
            "totalFeesPaid": mad_fees,
            "avgStreak": avg_streak,
            "maxStreak": max_streak,
        },
        "byTeacher": teacher_breakdown,
        "byPackage": package_breakdown,
        "byPaymentMethod": method_breakdown,
        "breakdowns": {
            "byTeacher": teacher_breakdown,
            "byPackage": package_breakdown,
            "byPaymentMethod": method_breakdown,
            "byAttendanceStatus": attendance_breakdown,
            "compensatedSessionsCount": compensated_count,
            "memorizationSummary": {
                "totalLogs": len(mem_in_period),
                "totalPages": round(total_pages, 1),
                "avgScore": avg_score,
                "byTrack": list(mem_tracks.values()),
                "deepLink": "#/memorization",
            },
            "madrasaSummary": {
                "batchesCount": len(madrasa_batches),
                "enrolledStudents": len(madrasa_enrollments),
                "totalFeesPaid": mad_fees,
                "avgStreak": avg_streak,
                "maxStreak": max_streak,
                "deepLink": "#/madrasa",
            },
        },
    }
