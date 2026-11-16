"""Central Operational Automation Engine — Yawm Jadid Academy / Yawm Jadid Academy ERP Suite (v1.2.0).

Implements:
1. Lightweight, synchronous, idempotent, loop-free Domain Event Bus (`DomainEventBus`) for single-user desktop ERP.
2. Scheduling & Time-Slot Conflict Detection (`detect_scheduling_conflicts`) for teachers and students.
3. Comprehensive Data Quality & Integrity Audit (`compute_data_quality_audit`).
4. Monthly Salary Preparation & Readiness Aggregation (`compute_salary_preparation_report`) — strictly read-only preparation without auto-approving or paying salaries.
5. Safe Preview-First Bulk Operations (`preview_bulk_operation`, `execute_bulk_operation_in_store`).
6. Multi-Entity Operational Discovery & Search (`search_operational_entities`).
"""
from __future__ import annotations

import collections
import datetime
import hashlib
import json
import logging
import re
from decimal import Decimal
from typing import Any, Callable

from .attendance_engine import (
    DATE_RE,
    MONTH_RE,
    TIME_RE,
    calculate_study_period,
    format_month_label,
    is_valid_calendar_date,
    normalize_time_str,
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
    raw_month_discount,
    resolve_actual_start_date,
    resolve_end_date,
    resolve_payment_date,
    round_half_up,
)
from .models import (
    DomainValidationError,
    clean_teacher_display_name,
    detect_duplicate_students,
    find_duplicate_teacher,
    normalize_arabic_for_comparison,
    normalize_phone_digits,
    normalize_teacher_key,
)

_LOG = logging.getLogger("alahd_erp.automation")

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset({
    "StudentCreated",
    "StudentUpdated",
    "TeacherCreated",
    "TeacherUpdated",
    "TeacherDeactivated",
    "TeacherReactivated",
    "PackageUpdated",
    "SubscriptionCreated",
    "SubscriptionUpdated",
    "ReceiptRecorded",
    "AttendanceRecorded",
    "SessionRecorded",
    "SalaryDataUpdated",
    "BackupCompleted",
    "RestoreCompleted",
    "BulkOperationExecuted",
})


class SchedulingConflictError(DomainValidationError):
    """Raised when a new session or plan conflicts with existing appointments (maps to HTTP 409)."""

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = conflicts
        first_reason = conflicts[0].get("reason", "تعارض في مواعيد الحصص") if conflicts else "تعارض في مواعيد الحصص"
        msg = f"تم اكتشاف {len(conflicts)} تعارض في المواعيد: {first_reason}"
        super().__init__(msg)


class DomainEventBus:
    """Lightweight, synchronous, idempotent, loop-guarded in-process domain event bus.

    Designed for a local single-user SQLite desktop application:
    - Deduplicates events by `idempotency_key` (bounded OrderedDict of 2,000 recent keys).
    - Prevents recursive event loops via `_active_stack` and `max_depth`.
    - Isolates handler failures so observer errors never corrupt committed data.
    """

    def __init__(self, max_history: int = 2000, max_depth: int = 2) -> None:
        self._handlers: dict[str, list[Callable[[dict[str, Any]], dict[str, Any] | None]]] = {}
        self._processed_keys: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._active_stack: list[str] = []
        self._max_history = max_history
        self._max_depth = max_depth
        self.event_log: list[dict[str, Any]] = []

    def subscribe(
        self,
        event_type: str,
        handler: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        if not event_type or not isinstance(event_type, str):
            raise ValueError(f"Unsupported domain event type: {event_type}")
        bucket = self._handlers.setdefault(event_type, [])
        if handler not in bucket:
            bucket.append(handler)

    def get_history(self) -> list[dict[str, Any]]:
        return list(self.event_log)

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Dispatches `event_type` to subscribed handlers idempotently and without recursion loops."""
        if event_type not in SUPPORTED_EVENT_TYPES and event_type not in self._handlers:
            return {"status": "ignored", "reason": "unsupported_event_type", "eventType": event_type}

        if not idempotency_key:
            raw_sig = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            digest = hashlib.sha256(f"{event_type}:{raw_sig}".encode("utf-8")).hexdigest()[:20]
            idempotency_key = f"{event_type}:{digest}"

        if idempotency_key in self._processed_keys:
            return {
                "status": "deduplicated",
                "eventType": event_type,
                "idempotencyKey": idempotency_key,
                "previousOutcome": self._processed_keys[idempotency_key],
            }

        if len(self._active_stack) >= self._max_depth or event_type in self._active_stack:
            _LOG.warning("Blocked recursive domain event loop: %s (stack=%s)", event_type, self._active_stack)
            rec_entry = {
                "status": "blocked_recursion",
                "type": event_type,
                "eventType": event_type,
                "idempotencyKey": idempotency_key,
            }
            self.event_log.append(rec_entry)
            return rec_entry

        self._active_stack.append(event_type)
        outcomes: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            event_obj = {
                "type": event_type,
                "eventType": event_type,
                "idempotencyKey": idempotency_key,
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "payload": payload,
            }
            handlers = list(self._handlers.get(event_type, [])) + list(self._handlers.get("*", []))
            for h_fn in handlers:
                try:
                    res = h_fn(event_obj)
                    if isinstance(res, dict):
                        outcomes.append(res)
                except Exception as exc:
                    _LOG.warning("Automation handler %s failed on %s: %s", getattr(h_fn, "__name__", "handler"), event_type, exc)
                    errors.append(str(exc))

            summary = {
                "status": "processed" if not errors else "partial_error",
                "eventType": event_type,
                "idempotencyKey": idempotency_key,
                "timestamp": event_obj["timestamp"],
                "handlersRun": len(handlers),
                "outcomes": outcomes,
                "errors": errors,
            }
            self._processed_keys[idempotency_key] = summary
            while len(self._processed_keys) > self._max_history:
                self._processed_keys.popitem(last=False)
            self.event_log.append(summary)
            if len(self.event_log) > 200:
                self.event_log = self.event_log[-200:]
            return summary
        finally:
            self._active_stack.pop()


# ------------------------------------------------------------------
# Task 5: Scheduling & Time-Slot Conflict Detection
# ------------------------------------------------------------------
def _time_to_minutes(time_str: str) -> int | None:
    t = normalize_time_str(time_str, default="")
    if not TIME_RE.match(t):
        return None
    hh, mm = map(int, t.split(":"))
    return hh * 60 + mm


def _format_minutes_hhmm(total_min: int) -> str:
    hh = (total_min // 60) % 24
    mm = total_min % 60
    return f"{hh:02d}:{mm:02d}"


def detect_scheduling_conflicts(
    sessions: list[dict[str, Any]],
    students: list[dict[str, Any]] | None = None,
    candidate_sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Detects confirmed teacher and student time-slot overlaps across active/scheduled sessions.

    Rules:
    - Cancelled or postponed sessions (`cancel_teacher`, `cancel_admin`, `postponed`) do not block time slots.
    - Two sessions for the same teacher on the same date with overlapping `[start, start + dur)` conflict
      UNLESS they belong to the same non-empty study group (`groupId`) or are the same session ID.
    - Two sessions for the same student on the same date with overlapping `[start, start + dur)` conflict.
    """
    stu_name_by_id: dict[str, str] = {}
    stu_teacher_by_id: dict[str, str] = {}
    for s in students or []:
        if isinstance(s, dict) and s.get("id"):
            sid = str(s["id"])
            stu_name_by_id[sid] = str(s.get("name") or sid)
            stu_teacher_by_id[sid] = str(s.get("teacher") or "").strip()

    inactive_statuses = {"cancel_teacher", "cancel_admin", "postponed"}

    def _normalize_sess(raw: dict[str, Any], is_cand: bool = False) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        status = str(raw.get("status") or "scheduled").strip()
        if status in inactive_statuses:
            return None
        sdate = str(raw.get("date") or "").strip()
        if not is_valid_calendar_date(sdate):
            return None
        start_min = _time_to_minutes(str(raw.get("time") or "16:00"))
        if start_min is None:
            return None
        try:
            dur = max(5, min(600, int(float(raw.get("dur") or 40))))
        except (ValueError, TypeError):
            dur = 40
        sid = str(raw.get("studentId") or "").strip()
        teacher = str(raw.get("teacher") or stu_teacher_by_id.get(sid, "")).strip()
        return {
            "id": str(raw.get("id") or ""),
            "studentId": sid,
            "studentName": str(raw.get("studentName") or stu_name_by_id.get(sid) or sid),
            "teacher": teacher,
            "groupId": str(raw.get("groupId") or "").strip(),
            "date": sdate,
            "time": _format_minutes_hhmm(start_min),
            "startMin": start_min,
            "endMin": start_min + dur,
            "dur": dur,
            "status": status,
            "isCandidate": is_cand,
        }

    existing_norm = [n for s in (sessions or []) if (n := _normalize_sess(s, False)) is not None]
    if candidate_sessions is not None:
        cand_norm = [n for s in (candidate_sessions or []) if (n := _normalize_sess(s, True)) is not None]
        cand_ids = {c["id"] for c in cand_norm if c["id"]}
        all_items = [e for e in existing_norm if not e["id"] or e["id"] not in cand_ids] + cand_norm
    else:
        all_items = existing_norm

    by_date: dict[str, list[dict[str, Any]]] = {}
    for item in all_items:
        by_date.setdefault(item["date"], []).append(item)

    conflicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str, str]] = set()

    for sdate, day_items in sorted(by_date.items()):
        day_items.sort(key=lambda x: (x["startMin"], x["id"]))
        n = len(day_items)
        for i in range(n):
            a = day_items[i]
            for j in range(i + 1, n):
                b = day_items[j]
                if b["startMin"] >= a["endMin"]:
                    break
                if candidate_sessions is not None and not (a["isCandidate"] or b["isCandidate"]):
                    continue
                if a["id"] and b["id"] and a["id"] == b["id"]:
                    continue

                pair_ids = tuple(sorted([a["id"] or f"idx_{i}", b["id"] or f"idx_{j}"]))

                # 1. Student double-booking conflict
                if a["studentId"] and a["studentId"] == b["studentId"]:
                    sig = ("student", sdate, f"{a['studentId']}:{pair_ids[0]}:{pair_ids[1]}")
                    if sig not in seen_pairs:
                        seen_pairs.add(sig)
                        conflicts.append({
                            "id": f"conf_stu_{sdate}_{a['studentId']}_{pair_ids[0]}",
                            "type": "student_double_booking",
                            "conflictType": "student_double_booking",
                            "severity": "critical",
                            "date": sdate,
                            "timeRange": f"{a['time']}–{_format_minutes_hhmm(a['endMin'])} ∩ {b['time']}–{_format_minutes_hhmm(b['endMin'])}",
                            "teacher": a["teacher"] or b["teacher"],
                            "studentId": a["studentId"],
                            "studentName": a["studentName"],
                            "sessionIds": [x for x in (a["id"], b["id"]) if x],
                            "reason": (
                                f"تعارض موعد للطالبة «{a['studentName']}» يوم {sdate} بين الحصة "
                                f"({a['time']} لمدة {a['dur']} د) والحصة ({b['time']} لمدة {b['dur']} د)"
                            ),
                            "deepLink": f"#/attendance?highlight={a['id'] or b['id']}",
                        })

                # 2. Teacher double-booking conflict (different students, not sharing the same study group)
                elif (
                    a["teacher"]
                    and a["teacher"] == b["teacher"]
                    and (not a["groupId"] or a["groupId"] != b["groupId"])
                ):
                    sig = ("teacher", sdate, f"{a['teacher']}:{pair_ids[0]}:{pair_ids[1]}")
                    if sig not in seen_pairs:
                        seen_pairs.add(sig)
                        conflicts.append({
                            "id": f"conf_tch_{sdate}_{a['id'] or i}_{b['id'] or j}",
                            "type": "teacher_double_booking",
                            "conflictType": "teacher_double_booking",
                            "severity": "warning",
                            "date": sdate,
                            "timeRange": f"{a['time']}–{_format_minutes_hhmm(a['endMin'])} ∩ {b['time']}–{_format_minutes_hhmm(b['endMin'])}",
                            "teacher": a["teacher"],
                            "studentId": a["studentId"],
                            "studentName": f"{a['studentName']} / {b['studentName']}",
                            "studentIds": [x for x in (a["studentId"], b["studentId"]) if x],
                            "sessionIds": [x for x in (a["id"], b["id"]) if x],
                            "reason": (
                                f"تعارض جدول للمعلمة «{a['teacher']}» يوم {sdate}: الطالبة «{a['studentName']}» "
                                f"({a['time']}، {a['dur']} د) تتداخل مع الطالبة «{b['studentName']}» ({b['time']}، {b['dur']} د)"
                            ),
                            "deepLink": f"#/attendance?highlight={a['id'] or b['id']}",
                        })

    return conflicts


# ------------------------------------------------------------------
# Task 6: Salary Preparation & Monthly Payroll Readiness Report
# ------------------------------------------------------------------
def compute_salary_preparation_report(
    state: dict[str, Any],
    month: str,
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Prepares a comprehensive, transparent monthly salary readiness report without auto-approving payouts."""
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    ref_iso = ref_dt.isoformat()
    mk = month.strip() if month and MONTH_RE.match(month.strip()) else f"{ref_dt.year:04d}-{ref_dt.month:02d}"

    y, m_num = map(int, mk.split("-"))
    prev_y, prev_m = (y - 1, 12) if m_num == 1 else (y, m_num - 1)
    prev_mk = f"{prev_y:04d}-{prev_m:02d}"

    settings = _v if isinstance(_v := state.get("settings"), dict) else {}
    pct_source = str(settings.get("pctSource") or "student")
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")
    default_pct = parse_pct_int(settings.get("defaultTeacherPct") if settings.get("defaultTeacherPct") is not None else 70)

    teachers = [str(t).strip() for t in (state.get("teachers") or []) if str(t).strip()]
    teacher_meta = _v if isinstance(_v := state.get("teacherMeta"), dict) else {}
    students = [s for s in (state.get("students") or []) if isinstance(s, dict) and str(s.get("name") or "").strip()]
    installments = [i for i in (state.get("installments") or []) if isinstance(i, dict)]
    payouts = [p for p in (state.get("payouts") or []) if isinstance(p, dict)]
    inst_map = index_installments_by_student_month(installments)

    # Track unassigned students with subscriptions in `mk`
    unassigned_students: list[dict[str, Any]] = []
    for stu in students:
        tname = str(stu.get("teacher") or "").strip()
        if not tname:
            m_obj = (stu.get("months") or {}).get(mk)
            if isinstance(m_obj, dict) and effective_month_subscription(m_obj) > 0:
                unassigned_students.append({
                    "studentId": stu.get("id", ""),
                    "studentName": stu.get("name", ""),
                    "netSubscription": effective_month_net_subscription(m_obj),
                    "reason": "طالبة لها اشتراك فعال في هذا الشهر ولكن لم تُسند إلى معلمة",
                })

    teacher_rows: list[dict[str, Any]] = []
    total_accrued = 0
    total_paid = 0
    total_remaining = 0
    warnings_list: list[dict[str, Any]] = []

    for u_item in unassigned_students:
        warnings_list.append({
            "type": "missing_teacher_reference",
            "severity": "warning",
            "teacher": "",
            "studentId": u_item["studentId"],
            "studentName": u_item["studentName"],
            "reason": f"الطالبة «{u_item['studentName']}» لها اشتراك ({u_item['netSubscription']} ج.م) بدون معلمة محددة.",
            "deepLink": f"#/students/{u_item['studentId']}?month={mk}",
        })

    for tname in teachers:
        tmeta = teacher_meta.get(tname) if isinstance(teacher_meta.get(tname), dict) else {}
        is_archived = bool(tmeta.get("archived")) or (tmeta.get("active") is False)
        t_pct = parse_pct_int(tmeta.get("pct") if tmeta.get("pct") is not None else tmeta.get("defaultPct", default_pct))

        t_students = [s for s in students if str(s.get("teacher") or "").strip() == tname]
        active_studs_mk = 0
        gross_mk = 0
        disc_mk = 0
        net_mk = 0
        coll_mk = 0
        wages_mk = 0
        custom_wage_count = 0
        uncollected_studs_count = 0
        missing_pct_students: list[str] = []
        unrenewed_from_prev: list[str] = []

        wages_prev = 0
        active_studs_prev = 0

        for stu in t_students:
            sid = str(stu.get("id") or "")
            sname = str(stu.get("name") or "")
            months_dict = stu.get("months") if isinstance(stu.get("months"), dict) else {}

            # Previous month comparison
            m_prev = months_dict.get(prev_mk)
            if isinstance(m_prev, dict):
                g_prev = effective_month_subscription(m_prev)
                if g_prev > 0:
                    active_studs_prev += 1
                w_prev = effective_month_wage(
                    stu,
                    m_prev,
                    teacher_meta=teacher_meta,
                    pct_source=pct_source,
                    wage_basis=wage_basis,
                    wage_accrual_mode=wage_accrual_mode,
                    student_month_installments=inst_map.get((sid, prev_mk)),
                    reference_date=ref_iso,
                    default_pct=default_pct,
                )
                wages_prev += w_prev
                # Check if active in prev_mk but missing in mk
                m_cur_check = months_dict.get(mk)
                if g_prev > 0 and (not isinstance(m_cur_check, dict) or effective_month_subscription(m_cur_check) <= 0):
                    unrenewed_from_prev.append(sname)

            m_cur = months_dict.get(mk)
            if not isinstance(m_cur, dict):
                continue
            g_cur = effective_month_subscription(m_cur)
            d_cur = effective_month_discount(m_cur)
            n_cur = effective_month_net_subscription(m_cur)
            stu_insts = inst_map.get((sid, mk)) or []
            c_cur = effective_month_collected(m_cur, stu_insts, reference_date=ref_iso)
            w_cur = effective_month_wage(
                stu,
                m_cur,
                teacher_meta=teacher_meta,
                pct_source=pct_source,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                student_month_installments=stu_insts,
                reference_date=ref_iso,
                default_pct=default_pct,
            )
            if g_cur <= 0 and w_cur <= 0:
                continue

            active_studs_mk += 1
            gross_mk += g_cur
            disc_mk += d_cur
            net_mk += n_cur
            coll_mk += c_cur
            wages_mk += w_cur

            if m_cur.get("customWage") not in (None, ""):
                custom_wage_count += 1
            if c_cur < n_cur:
                uncollected_studs_count += 1
            if parse_pct_int(stu.get("pct"), t_pct) == 0 and m_cur.get("customWage") in (None, ""):
                missing_pct_students.append(sname)

        paid_mk = sum(
            max(0, round_half_up(p.get("amount") or 0))
            for p in payouts
            if str(p.get("teacher") or "").strip() == tname and str(p.get("month") or "").strip() == mk
        )

        if is_archived and active_studs_mk == 0 and wages_mk == 0 and paid_mk == 0:
            continue

        rem_mk = max(0, wages_mk - paid_mk)
        overpaid_mk = max(0, paid_mk - wages_mk)

        total_accrued += wages_mk
        total_paid += paid_mk
        total_remaining += rem_mk

        delta_wage = wages_mk - wages_prev
        delta_pct = (
            round_half_up((Decimal(abs(delta_wage)) * Decimal(100)) / Decimal(wages_prev))
            if wages_prev > 0
            else (100 if wages_mk > 0 else 0)
        )
        unusual_change = bool(wages_prev > 0 and delta_pct >= 30)

        row_notes: list[str] = []
        if overpaid_mk > 0:
            msg = f"المبلغ المصروف ({paid_mk} ج.م) يتجاوز الأجر المستحق المحسوب ({wages_mk} ج.م) بمقدار {overpaid_mk} ج.م"
            row_notes.append(msg)
            warnings_list.append({
                "type": "overpaid_teacher",
                "severity": "critical",
                "teacher": tname,
                "reason": msg,
                "deepLink": f"#/payroll?month={mk}",
            })
        if unusual_change:
            dir_lbl = "زيادة" if delta_wage > 0 else "انخفاض"
            msg = f"{dir_lbl} ملحوظة بنسبة {delta_pct}% مقارنة بشهر {format_month_label(prev_mk)} ({wages_prev} ← {wages_mk} ج.م)"
            row_notes.append(msg)
            warnings_list.append({
                "type": "unusual_wage_change",
                "severity": "info",
                "teacher": tname,
                "reason": f"المعلمة «{tname}»: {msg}",
                "deepLink": f"#/payroll?month={mk}",
            })
        if missing_pct_students:
            msg = f"طالبات بنسبة 0%: {'، '.join(missing_pct_students[:3])}"
            row_notes.append(msg)
            warnings_list.append({
                "type": "zero_pct_student",
                "severity": "warning",
                "teacher": tname,
                "reason": f"المعلمة «{tname}» لديها {msg}",
                "deepLink": f"#/students?teacher={tname}",
            })
        if wage_accrual_mode == "collected" and uncollected_studs_count > 0:
            row_notes.append(f"{uncollected_studs_count} طالبة لم يُستكمل تحصيل اشتراكها (السياسة: من المحصّل فقط)")

        readiness_status = (
            "paid_in_full"
            if (wages_mk > 0 and rem_mk == 0 and overpaid_mk == 0)
            else ("overpaid" if overpaid_mk > 0 else ("ready_to_pay" if rem_mk > 0 else "no_accrual"))
        )

        mom_pct_signed = (delta_pct if delta_wage >= 0 else -delta_pct) if wages_prev > 0 else None
        ui_status = (
            "settled"
            if readiness_status == "paid_in_full"
            else ("overpaid" if readiness_status == "overpaid" else ("needs_review" if row_notes else "ready"))
        )
        teacher_rows.append({
            "teacher": tname,
            "active": not is_archived,
            "archived": is_archived,
            "configuredPct": t_pct,
            "totalAssignedStudents": len(t_students),
            "activeStudentsInMonth": active_studs_mk,
            "activeStudentCount": active_studs_mk,
            "grossSubscriptions": gross_mk,
            "discounts": disc_mk,
            "netSubscriptions": net_mk,
            "collected": coll_mk,
            "collectedAmount": coll_mk,
            "uncollected": max(0, net_mk - coll_mk),
            "customWageCount": custom_wage_count,
            "uncollectedStudentsCount": uncollected_studs_count,
            "unrenewedFromPrevCount": len(unrenewed_from_prev),
            "wagesAccrued": wages_mk,
            "accruedWage": wages_mk,
            "wagesPrevMonth": wages_prev,
            "prevAccruedWage": wages_prev,
            "wageDelta": delta_wage,
            "wageDeltaPct": delta_pct if delta_wage >= 0 else -delta_pct,
            "momChangePct": mom_pct_signed,
            "unusualChange": unusual_change,
            "wagesPaid": paid_mk,
            "paidAmount": paid_mk,
            "wagesRemaining": rem_mk,
            "remainingBalance": rem_mk,
            "overpaidAmount": overpaid_mk,
            "readinessStatus": readiness_status,
            "status": ui_status,
            "notes": row_notes,
            "warnings": row_notes,
            "missingInputs": [],
        })

    return {
        "ok": True,
        "month": mk,
        "monthLabel": format_month_label(mk),
        "prevMonth": prev_mk,
        "previousMonth": prev_mk,
        "prevMonthLabel": format_month_label(prev_mk),
        "unassignedSummary": {
            "count": len(unassigned_students),
            "netSubscriptions": sum(u["netSubscription"] for u in unassigned_students),
        },
        "calculationPolicy": {
            "pctSource": pct_source,
            "wageBasis": wage_basis,
            "wageAccrualMode": wage_accrual_mode,
            "defaultTeacherPct": default_pct,
        },
        "summary": {
            "teachersCount": len(teacher_rows),
            "totalWagesAccrued": total_accrued,
            "totalWagesPaid": total_paid,
            "totalWagesRemaining": total_remaining,
            "unassignedActiveStudentsCount": len(unassigned_students),
            "warningsCount": len(warnings_list),
        },
        "teachers": teacher_rows,
        "unassignedStudents": unassigned_students,
        "warnings": warnings_list,
    }


# ------------------------------------------------------------------
# Task 7: Comprehensive Data Quality & Integrity Audit
# ------------------------------------------------------------------
def compute_data_quality_audit(
    state: dict[str, Any],
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Audits all entities for duplicates, missing fields, broken references, orphans, invalid dates,
    suspicious receipts, scheduling conflicts, and inactive teachers assigned to new operations.
    """
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    ref_iso = ref_dt.isoformat()
    cur_mk = f"{ref_dt.year:04d}-{ref_dt.month:02d}"

    teachers = [str(t).strip() for t in (state.get("teachers") or []) if str(t).strip()]
    teacher_set = set(teachers)
    teacher_meta = _v if isinstance(_v := state.get("teacherMeta"), dict) else {}
    students = [s for s in (state.get("students") or []) if isinstance(s, dict)]
    installments = [i for i in (state.get("installments") or []) if isinstance(i, dict)]
    payouts = [p for p in (state.get("payouts") or []) if isinstance(p, dict)]
    sessions = [s for s in (state.get("sessions") or []) if isinstance(s, dict)]
    plans = [p for p in (state.get("plans") or []) if isinstance(p, dict)]
    groups = [g for g in (state.get("groups") or []) if isinstance(g, dict)]
    memorization = [m for m in (state.get("memorization") or []) if isinstance(m, dict)]
    madrasa_enrollments = [e for e in (state.get("madrasaEnrollments") or []) if isinstance(e, dict)]
    certificates = [c for c in (state.get("certificates") or []) if isinstance(c, dict)]

    issues: list[dict[str, Any]] = []

    # 1. Duplicate students (by name+phone AND by normalized multi-word name)
    dup_students = detect_duplicate_students(students)
    seen_dup_names: set[str] = set()
    for d in dup_students:
        recs = d.get("records") or []
        codes = " ، ".join(str(r.get("code") or r.get("id") or "") for r in recs)
        first_id = str(recs[0].get("id") or "") if recs else ""
        seen_dup_names.add(normalize_arabic_for_comparison(d.get("name", "")))
        issues.append({
            "id": f"dq_dup_stu_{first_id}",
            "category": "duplicate_student",
            "severity": "critical",
            "title": f"تكرار بيانات طالبة: {d['name']}",
            "explanation": f"الاسم «{d['name']}» ورقم الهاتف ({d['phone']}) مكرران في {d['count']} سجلات ({codes}).",
            "suggestion": "راجع سجل الطالبة المكرر وتأكد مما إذا كان يمثل نفس الطالبة قبل تحديثه أو حذفه يدويًا.",
            "deepLink": f"#/students?search={d['name']}",
        })
    by_norm_name: dict[str, list[dict[str, Any]]] = {}
    for stu in students:
        nm_norm = normalize_arabic_for_comparison(stu.get("name") or "")
        if len(nm_norm.split()) >= 2:
            by_norm_name.setdefault(nm_norm, []).append(stu)
    for nm_norm, recs in by_norm_name.items():
        if len(recs) > 1 and nm_norm not in seen_dup_names:
            first_id = str(recs[0].get("id") or "")
            disp_name = str(recs[0].get("name") or "")
            codes = " ، ".join(str(r.get("code") or r.get("id") or "") for r in recs)
            issues.append({
                "id": f"dq_dup_name_{first_id}",
                "category": "duplicate_student",
                "severity": "warning",
                "title": f"تشابه/تكرار اسم طالبة: {disp_name}",
                "explanation": f"الاسم «{disp_name}» مكرر في {len(recs)} سجلات ({codes}).",
                "suggestion": "راجع سجلات الطالبة للتأكد من عدم تكرار تسجيلها.",
                "deepLink": f"#/students?search={disp_name}",
            })

    # 2. Potential duplicate teachers (by normalized Arabic key)
    seen_t_keys: dict[str, str] = {}
    for tname in teachers:
        tkey = normalize_teacher_key(tname)
        if len(tkey) >= 2:
            if tkey in seen_t_keys and seen_t_keys[tkey] != tname:
                first_t = seen_t_keys[tkey]
                issues.append({
                    "id": f"dq_dup_tch_{tkey}",
                    "category": "duplicate_teachers",
                    "severity": "warning",
                    "title": f"تشابه إملائي بين معلمتين: «{first_t}» و«{tname}»",
                    "explanation": f"الاسمان يتطابقان بعد التطبيع الإملائي ({tkey}).",
                    "suggestion": "تحقق من شاشة المعلمات؛ إذا كانتا نفس المعلمة يمكنك إعادة إسناد طالباتها ثم حذف السجل الإضافي.",
                    "deepLink": "#/teachers",
                })
            else:
                seen_t_keys[tkey] = tname

    # 3. Student-level checks: missing info, inconsistent codes, broken teacher refs, inactive teacher on new ops, invalid dates
    stu_ids: set[str] = set()
    codes_map: dict[str, list[str]] = {}
    independent_dates_count = 0

    for stu in students:
        sid = str(stu.get("id") or "").strip()
        sname = str(stu.get("name") or "").strip()
        scode = str(stu.get("code") or "").strip()
        sphone = str(stu.get("phone") or "").strip()
        steacher = str(stu.get("teacher") or "").strip()
        stype = str(stu.get("type") or "").strip()
        if sid:
            stu_ids.add(sid)
        if scode:
            codes_map.setdefault(scode.upper(), []).append(sname or sid)

        phone_digits = re.sub(r"\D", "", sphone)
        if len(phone_digits) < 8:
            issues.append({
                "id": f"dq_missing_phone_{sid}",
                "category": "missing_info",
                "severity": "info",
                "title": f"رقم هاتف غير مكتمل: {sname}",
                "explanation": f"الطالبة «{sname}» لا يوجد لها رقم هاتف صالح ({sphone or 'فارغ'}).",
                "suggestion": "أضف رقم الهاتف لتفعيل تنبيهات واتساب ومنع التكرار.",
                "deepLink": f"#/students/{sid}?highlight={sid}",
            })
        if not scode:
            issues.append({
                "id": f"dq_missing_code_{sid}",
                "category": "missing_info",
                "severity": "info",
                "title": f"كود طالبة مفقود: {sname}",
                "explanation": f"الطالبة «{sname}» بدون كود تعريفي (STU-XXXX).",
                "suggestion": "استخدم أداة التصحيح الجماعي لتوليد الأكواد المفقودة تلقائيًا.",
                "deepLink": f"#/students/{sid}?highlight={sid}",
            })
        if steacher and steacher not in teacher_set:
            issues.append({
                "id": f"dq_broken_tch_{sid}",
                "category": "broken_references",
                "severity": "critical",
                "title": f"مرجع معلمة غير موجود: {steacher}",
                "explanation": f"الطالبة «{sname}» مسندة إلى المعلمة «{steacher}» غير الموجودة في جدول المعلمات.",
                "suggestion": "أضف المعلمة في قسم المعلمات أو أعد إسناد الطالبة إلى معلمة نشطة.",
                "deepLink": f"#/students/{sid}?highlight={sid}",
            })

        tmeta = teacher_meta.get(steacher) if isinstance(teacher_meta.get(steacher), dict) else {}
        t_is_inactive = bool(tmeta.get("archived")) or (tmeta.get("active") is False)

        months_dict = stu.get("months") if isinstance(stu.get("months"), dict) else {}
        for mk, m in sorted(months_dict.items()):
            if not isinstance(m, dict):
                continue
            pay_dt = resolve_payment_date(m)
            start_dt = resolve_actual_start_date(m)
            end_dt = resolve_end_date(m)
            if pay_dt or start_dt:
                independent_dates_count += 1
            gross_val = effective_month_subscription(m)
            raw_disc = raw_month_discount(m)

            if pay_dt and not is_valid_calendar_date(pay_dt):
                issues.append({
                    "id": f"dq_baddate_pay_{sid}_{mk}",
                    "category": "invalid_dates",
                    "severity": "critical",
                    "title": f"تاريخ دفع غير صالح: {sname} ({mk})",
                    "explanation": f"تاريخ الدفع المسجل ({pay_dt}) ليس تاريخًا تقويميًا صالحًا.",
                    "suggestion": "صحح تاريخ الدفع في جدول الطالبات.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })
            if start_dt and not is_valid_calendar_date(start_dt):
                issues.append({
                    "id": f"dq_baddate_start_{sid}_{mk}",
                    "category": "invalid_dates",
                    "severity": "critical",
                    "title": f"تاريخ بداية غير صالح: {sname} ({mk})",
                    "explanation": f"تاريخ البداية الفعلي ({start_dt}) ليس تاريخًا تقويميًا صالحًا.",
                    "suggestion": "صحح تاريخ البداية الفعلي في جدول الطالبات.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })
            if start_dt and end_dt and is_valid_calendar_date(start_dt) and is_valid_calendar_date(end_dt) and end_dt < start_dt:
                issues.append({
                    "id": f"dq_date_order_{sid}_{mk}",
                    "category": "invalid_dates",
                    "severity": "critical",
                    "title": f"تاريخ النهاية يسبق تاريخ البداية: {sname} ({mk})",
                    "explanation": f"تاريخ النهاية ({end_dt}) أقدم من تاريخ البداية الفعلي ({start_dt}).",
                    "suggestion": "أعد حساب تاريخ النهاية من تاريخ البداية الفعلي.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })
            if raw_disc > gross_val:
                issues.append({
                    "id": f"dq_disc_exceeds_{sid}_{mk}",
                    "category": "invalid_dates",
                    "severity": "critical",
                    "title": f"الخصم يتجاوز قيمة الاشتراك: {sname} ({mk})",
                    "explanation": f"الخصم ({raw_disc} ج.م) أكبر من قيمة الاشتراك ({gross_val} ج.م).",
                    "suggestion": "عدّل قيمة الخصم أو الاشتراك للطالبة.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })
            if gross_val > 0 and not start_dt:
                issues.append({
                    "id": f"dq_missing_start_{sid}_{mk}",
                    "category": "missing_info",
                    "severity": "warning",
                    "title": f"اشتراك بدون تاريخ بداية فعلي: {sname} ({mk})",
                    "explanation": f"الطالبة «{sname}» لها اشتراك ({gross_val} ج.م) لشهر {mk} بدون تاريخ بداية فعلي.",
                    "suggestion": "حدد تاريخ البداية الفعلي لحساب فترة الدراسة وجدولة الحصص.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })
            if t_is_inactive and gross_val > 0 and mk >= cur_mk:
                issues.append({
                    "id": f"dq_inactive_tch_sub_{sid}_{mk}",
                    "category": "inactive_entity_in_new_ops",
                    "severity": "warning",
                    "title": f"معلمة غير نشطة مسندة لاشتراك حالي/قادم: {sname}",
                    "explanation": f"الطالبة «{sname}» لديها اشتراك في شهر {mk} مع المعلمة المؤرشفة/غير النشطة «{steacher}».",
                    "suggestion": "أعد إسناد الطالبة إلى معلمة نشطة أو أعد تفعيل المعلمة من قسم المعلمات.",
                    "deepLink": f"#/students/{sid}?month={mk}&highlight={sid}",
                })

    # 4. Inconsistent / duplicate student codes
    for code_val, names_list in sorted(codes_map.items()):
        if len(names_list) > 1:
            issues.append({
                "id": f"dq_dup_code_{code_val}",
                "category": "inconsistent_identifiers",
                "severity": "warning",
                "title": f"كود طالبة مكرر ({code_val})",
                "explanation": f"الكود «{code_val}» مستخدم لأكثر من طالبة: {' ، '.join(names_list[:4])}.",
                "suggestion": "خصص كودًا فريدًا لكل طالبة لمنع التباس المطابقة عند استيراد الإكسيل.",
                "deepLink": f"#/students?search={code_val}",
            })

    # 5. Orphaned records & suspicious duplicate receipts
    rcp_no_map: dict[str, list[str]] = {}
    rcp_sig_map: dict[tuple[str, str, int, str, str, str], list[str]] = {}
    for inst in installments:
        iid = str(inst.get("id") or "")
        sid = str(inst.get("studentId") or "").strip()
        rno = str(inst.get("receiptNo") or "").strip()
        mk = str(inst.get("month") or "").strip()
        amt = round_half_up(inst.get("amount") or 0)
        pdt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
        method = str(inst.get("method") or "").strip()
        note = str(inst.get("note") or "").strip()

        if sid and sid not in stu_ids:
            issues.append({
                "id": f"dq_orphan_inst_{iid}",
                "category": "orphaned_records",
                "severity": "critical",
                "title": f"إيصال سداد يتيم ({rno or iid})",
                "explanation": f"الإيصال يشير إلى معرف طالبة غير موجود ({sid}).",
                "suggestion": "اربط الإيصال بالطالبة الصحيحة أو احذفه بعد المراجعة.",
                "deepLink": "#/collections",
            })
        if rno:
            rcp_no_map.setdefault(rno.upper(), []).append(iid)
        # Ignore auto-migrated legacy receipts when checking duplicate manual receipts
        if not iid.startswith("inst_mig_"):
            sig = (sid, mk, amt, pdt, method, note)
            rcp_sig_map.setdefault(sig, []).append(rno or iid)

    for rno, iids in sorted(rcp_no_map.items()):
        if len(iids) > 1:
            issues.append({
                "id": f"dq_dup_rcpno_{rno}",
                "category": "inconsistent_identifiers",
                "severity": "warning",
                "title": f"رقم إيصال مكرر ({rno})",
                "explanation": f"رقم الإيصال «{rno}» مسجل في {len(iids)} إيصالات مختلفة.",
                "suggestion": "راجع جدول الإيصالات في قسم التحصيل للتأكد من عدم تكرار تسجيل نفس الدفعة.",
                "deepLink": "#/collections",
            })

    for (sid, mk, amt, pdt, method, _), rnos in rcp_sig_map.items():
        if len(rnos) > 1:
            issues.append({
                "id": f"dq_susp_rcp_{sid}_{mk}_{pdt}_{amt}",
                "category": "suspicious_duplicate_receipts",
                "severity": "warning",
                "title": f"إيصالات متطابقة محتملة التكرار ({amt} ج.م — {pdt})",
                "explanation": f"يوجد {len(rnos)} إيصالات بنفس الطالبة والشهر والمبلغ والتاريخ والوسيلة ({' ، '.join(rnos)}).",
                "suggestion": "تحقق مما إذا كانت دفعتين حقيقيتين في نفس اليوم أم تسجيلًا مكررًا بالخطأ.",
                "deepLink": "#/collections",
            })

    # Check orphaned sessions, plans, memorization, madrasa, certificates, and inactive teachers in future sessions
    for coll, lbl, link in (
        (sessions, "حصة دراسية", "#/attendance"),
        (plans, "خطة حضور", "#/attendance"),
        (memorization, "سجل تسميع", "#/memorization"),
        (madrasa_enrollments, "انتساب مدرسة إيمانية", "#/madrasa"),
        (certificates, "شهادة تقدير", "#/certificates"),
    ):
        for item in coll:
            xid = str(item.get("id") or "")
            sid = str(item.get("studentId") or "").strip()
            if sid and sid not in stu_ids:
                issues.append({
                    "id": f"dq_orphan_{lbl}_{xid}",
                    "category": "orphaned_records",
                    "severity": "warning",
                    "title": f"{lbl} يشير لطالبة غير موجودة ({sid})",
                    "explanation": f"السجل ({xid}) مرتبط بمعرف طالبة غير موجود حاليًا.",
                    "suggestion": "راجع السجل في الشاشة المعنية.",
                    "deepLink": link,
                })

    for sess in sessions:
        if sess.get("status") == "scheduled" and str(sess.get("date") or "") >= ref_iso:
            st_tch = str(sess.get("teacher") or "").strip()
            tmeta = teacher_meta.get(st_tch) if isinstance(teacher_meta.get(st_tch), dict) else {}
            if st_tch and (bool(tmeta.get("archived")) or tmeta.get("active") is False):
                issues.append({
                    "id": f"dq_inactive_tch_sess_{sess.get('id', '')}",
                    "category": "inactive_entity_in_new_ops",
                    "severity": "warning",
                    "title": f"حصة مجدولة قادمة مع معلمة غير نشطة ({st_tch})",
                    "explanation": f"الحصة بتاريخ {sess.get('date')} مجدولة مع المعلمة غير النشطة «{st_tch}».",
                    "suggestion": "انقل الطالبة وحصصها المجدولة إلى معلمة نشطة.",
                    "deepLink": f"#/attendance?highlight={sess.get('id', '')}",
                })

    # 6. Scheduling conflicts
    sched_conflicts = detect_scheduling_conflicts(sessions, students)
    for sc in sched_conflicts:
        issues.append({
            "id": f"dq_{sc['id']}",
            "category": "conflicting_assignments",
            "severity": sc.get("severity", "warning"),
            "title": "تعارض في مواعيد الحصص",
            "explanation": sc["reason"],
            "suggestion": "عدّل وقت الحصة أو تاريخها لمنع التداخل الزمني.",
            "deepLink": sc["deepLink"],
        })

    by_category: dict[str, list[dict[str, Any]]] = {}
    for iss in issues:
        iss.setdefault("detail", iss.get("explanation", ""))
        by_category.setdefault(iss["category"], []).append(iss)

    crit_cnt = sum(1 for i in issues if i["severity"] == "critical")
    warn_cnt = sum(1 for i in issues if i["severity"] == "warning")
    info_cnt = sum(1 for i in issues if i["severity"] == "info")

    return {
        "ok": True,
        "referenceDate": ref_iso,
        "summary": {
            "totalStudents": len(students),
            "totalTeachers": len(teachers),
            "independentDatesCount": independent_dates_count,
            "totalIssues": len(issues),
            "criticalCount": crit_cnt,
            "warningCount": warn_cnt,
            "infoCount": info_cnt,
            "critical": crit_cnt,
            "warning": warn_cnt,
            "info": info_cnt,
            "duplicateStudentsCount": len(dup_students),
            "schedulingConflictsCount": len(sched_conflicts),
            "isHealthy": crit_cnt == 0 and warn_cnt == 0,
        },
        "issues": issues,
        "byCategory": by_category,
    }


# ------------------------------------------------------------------
# Task 10: Safe Preview-First Bulk Operations
# ------------------------------------------------------------------
SUPPORTED_BULK_OPERATIONS: frozenset[str] = frozenset({
    "assign_teacher",
    "assign_package",
    "set_actual_start_date",
    "update_session_status",
    "normalize_phones_and_codes",
})


def preview_bulk_operation(
    state: dict[str, Any],
    operation: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Validates and previews a bulk operation without modifying the database."""
    op = str(operation or "").strip().lower()
    if op not in SUPPORTED_BULK_OPERATIONS:
        raise DomainValidationError(f"عملية جماعية غير مدعومة: {operation}")
    if not isinstance(params, dict):
        raise DomainValidationError("معاملات العملية الجماعية غير صالحة")

    students = [s for s in (state.get("students") or []) if isinstance(s, dict)]
    teachers = [str(t).strip() for t in (state.get("teachers") or []) if str(t).strip()]
    teacher_meta = _v if isinstance(_v := state.get("teacherMeta"), dict) else {}
    sessions = [s for s in (state.get("sessions") or []) if isinstance(s, dict)]
    closed_months = {
        (c.get("month") if isinstance(c, dict) else str(c))
        for c in (state.get("closedMonths") or [])
    }
    settings = _v if isinstance(_v := state.get("settings"), dict) else {}
    def_dur = int(settings.get("defaultDurationDays") or 30)

    target_ids = {str(x).strip() for x in (params.get("studentIds") or []) if str(x).strip()}
    source_teacher = clean_teacher_display_name(params.get("sourceTeacher") or "")

    affected: list[dict[str, Any]] = []

    if op == "assign_teacher":
        new_teacher = clean_teacher_display_name(params.get("newTeacher") or params.get("targetTeacher") or params.get("teacher") or "")
        if not new_teacher or new_teacher not in teachers:
            raise DomainValidationError(f"المعلمة المستهدفة غير موجودة: {new_teacher}")
        tmeta = teacher_meta.get(new_teacher) if isinstance(teacher_meta.get(new_teacher), dict) else {}
        if bool(tmeta.get("archived")) or tmeta.get("active") is False:
            raise DomainValidationError(f"لا يمكن النقل الجماعي إلى معلمة غير نشطة/مؤرشفة: {new_teacher}")
        sync_pct = bool(params.get("syncTeacherPct", True))
        target_pct = parse_pct_int(tmeta.get("pct") if tmeta.get("pct") is not None else 70)

        for stu in students:
            sid = str(stu.get("id") or "")
            cur_t = str(stu.get("teacher") or "").strip()
            if target_ids and sid not in target_ids:
                continue
            if source_teacher and cur_t != source_teacher:
                continue
            if not target_ids and not source_teacher:
                raise DomainValidationError("يرجى تحديد الطالبات أو المعلمة المصدر للنقل الجماعي")
            if cur_t == new_teacher and (not sync_pct or parse_pct_int(stu.get("pct"), 70) == target_pct):
                continue
            affected.append({
                "id": sid,
                "name": str(stu.get("name") or ""),
                "code": str(stu.get("code") or ""),
                "before": {"teacher": cur_t or "بدون معلمة", "pct": parse_pct_int(stu.get("pct"), 70)},
                "after": {
                    "teacher": new_teacher,
                    "pct": target_pct if sync_pct else parse_pct_int(stu.get("pct"), 70),
                },
            })

    elif op == "assign_package":
        new_pkg = clean_teacher_display_name(params.get("newPackage") or params.get("package") or params.get("type") or "")
        if not new_pkg:
            raise DomainValidationError("اسم الباقة المستهدفة مطلوب")
        if not target_ids and not source_teacher:
            raise DomainValidationError("يرجى تحديد الطالبات لتحديث الباقة")
        for stu in students:
            sid = str(stu.get("id") or "")
            cur_t = str(stu.get("teacher") or "").strip()
            if target_ids and sid not in target_ids:
                continue
            if source_teacher and cur_t != source_teacher:
                continue
            cur_pkg = str(stu.get("type") or "").strip()
            if cur_pkg == new_pkg:
                continue
            affected.append({
                "id": sid,
                "name": str(stu.get("name") or ""),
                "code": str(stu.get("code") or ""),
                "before": {"type": cur_pkg or "—"},
                "after": {"type": new_pkg},
            })

    elif op == "set_actual_start_date":
        mk = str(params.get("month") or "").strip()
        start_dt = str(params.get("actualStartDate") or "").strip()
        if not MONTH_RE.match(mk):
            raise DomainValidationError("شهر الاشتراك غير صالح (YYYY-MM)")
        if mk in closed_months:
            raise DomainValidationError(f"الشهر {mk} مغلق مالياً؛ لا يمكن تعديله")
        if not is_valid_calendar_date(start_dt):
            raise DomainValidationError(f"تاريخ البداية الفعلي غير صالح: {start_dt}")
        only_missing = bool(params.get("onlyMissing", True))
        s_date_obj = parse_iso_date(start_dt)
        calc_end = (s_date_obj + datetime.timedelta(days=def_dur)).isoformat() if s_date_obj else ""

        for stu in students:
            sid = str(stu.get("id") or "")
            cur_t = str(stu.get("teacher") or "").strip()
            if target_ids and sid not in target_ids:
                continue
            if source_teacher and cur_t != source_teacher:
                continue
            m = (stu.get("months") or {}).get(mk)
            if not isinstance(m, dict) or effective_month_subscription(m) <= 0:
                continue
            cur_start = resolve_actual_start_date(m)
            cur_end = resolve_end_date(m)
            if only_missing and cur_start:
                continue
            if cur_start == start_dt:
                continue
            affected.append({
                "id": sid,
                "name": str(stu.get("name") or ""),
                "code": str(stu.get("code") or ""),
                "month": mk,
                "before": {"actualStartDate": cur_start or "غير محدد", "endDate": cur_end or "—"},
                "after": {"actualStartDate": start_dt, "endDate": cur_end if (cur_end and cur_end >= start_dt) else calc_end},
            })

    elif op == "update_session_status":
        new_status = str(params.get("status") or "").strip()
        valid_statuses = {"scheduled", "done", "absent", "excused", "late", "cancel_teacher", "cancel_admin", "postponed", "makeup"}
        if new_status not in valid_statuses:
            raise DomainValidationError(f"حالة الحصة غير صالحة: {new_status}")
        sess_ids = {str(x).strip() for x in (params.get("sessionIds") or []) if str(x).strip()}
        filter_date = str(params.get("date") or "").strip()
        if not sess_ids and not filter_date:
            raise DomainValidationError("يرجى تحديد الحصص أو التاريخ لتحديث الحالة الجماعي")
        for sess in sessions:
            xid = str(sess.get("id") or "")
            if sess_ids and xid not in sess_ids:
                continue
            if filter_date and str(sess.get("date") or "") != filter_date:
                continue
            if source_teacher and str(sess.get("teacher") or "") != source_teacher:
                continue
            cur_st = str(sess.get("status") or "scheduled")
            if cur_st == new_status:
                continue
            affected.append({
                "id": xid,
                "studentId": str(sess.get("studentId") or ""),
                "date": str(sess.get("date") or ""),
                "teacher": str(sess.get("teacher") or ""),
                "before": {"status": cur_st},
                "after": {"status": new_status},
            })

    elif op == "normalize_phones_and_codes":
        existing_codes = {str(s.get("code") or "").strip().upper() for s in students if str(s.get("code") or "").strip()}
        next_num = len(students) + 1
        for stu in students:
            sid = str(stu.get("id") or "")
            if target_ids and sid not in target_ids:
                continue
            cur_name = str(stu.get("name") or "")
            norm_name = re.sub(r"\s+", " ", cur_name).strip()
            cur_phone = str(stu.get("phone") or "").strip()
            norm_phone = normalize_phone_digits(cur_phone) if cur_phone else ""
            cur_code = str(stu.get("code") or "").strip()
            new_code = cur_code.upper()
            m_code = re.match(r"^STU[-_\s]*(\d+)$", new_code, re.IGNORECASE)
            if m_code:
                new_code = f"STU-{int(m_code.group(1)):04d}"
            elif not new_code:
                while f"STU-{next_num:04d}" in existing_codes:
                    next_num += 1
                new_code = f"STU-{next_num:04d}"
                existing_codes.add(new_code)
                next_num += 1
            if (norm_phone and norm_phone != cur_phone) or new_code != cur_code or norm_name != cur_name:
                affected.append({
                    "id": sid,
                    "name": norm_name,
                    "before": {"name": cur_name, "phone": cur_phone, "code": cur_code},
                    "after": {"name": norm_name, "phone": norm_phone or cur_phone, "code": new_code},
                })

    raw_sig = json.dumps({"op": op, "params": params, "affected": affected}, ensure_ascii=False, sort_keys=True)
    preview_hash = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()
    target_total = len(target_ids) if target_ids else len(students)
    preview_rows = [
        {
            "id": r.get("id", ""),
            "name": r.get("name") or r.get("id", ""),
            "before": json.dumps(r.get("before", {}), ensure_ascii=False),
            "after": json.dumps(r.get("after", {}), ensure_ascii=False),
        }
        for r in affected
    ]

    return {
        "ok": True,
        "operation": op,
        "targetCount": target_total,
        "affectedCount": len(affected),
        "unchangedCount": max(0, target_total - len(affected)),
        "affectedRecords": affected,
        "previewRows": preview_rows,
        "summaryText": f"العملية ({op}): ستؤثر على {len(affected)} سجل من أصل {target_total}.",
        "previewHash": preview_hash,
    }


# ------------------------------------------------------------------
# Task 12: Multi-Entity Operational Search
# ------------------------------------------------------------------
def search_operational_entities(
    state: dict[str, Any],
    query: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Searches students, teachers, receipts, transactions, sessions, and packages by normalized Arabic/ASCII query."""
    q_raw = str(query or "").strip()
    if not q_raw:
        return {"ok": True, "query": "", "totalMatches": 0, "results": [], "byType": {}}

    q_lower = q_raw.lower()
    q_norm = normalize_arabic_for_comparison(q_raw)
    cap = max(1, min(200, int(limit or 30)))

    def _matches(*fields: Any) -> bool:
        for f in fields:
            s = str(f or "").strip()
            if not s:
                continue
            if q_lower in s.lower() or (q_norm and q_norm in normalize_arabic_for_comparison(s)):
                return True
        return False

    stu_by_id = {str(s.get("id") or ""): s for s in (state.get("students") or []) if isinstance(s, dict)}
    results: list[dict[str, Any]] = []

    for stu in state.get("students") or []:
        if not isinstance(stu, dict):
            continue
        if _matches(stu.get("name"), stu.get("code"), stu.get("phone"), stu.get("teacher"), stu.get("type")):
            sid = str(stu.get("id") or "")
            results.append({
                "entityType": "student",
                "id": sid,
                "title": f"👩‍🎓 {stu.get('name', '')} ({stu.get('code', '')})",
                "subtitle": f"المعلمة: {stu.get('teacher') or 'بدون معلمة'} | الباقة: {stu.get('type') or '—'} | الهاتف: {stu.get('phone') or '—'}",
                "deepLink": f"#/students/{sid}?highlight={sid}",
            })
            if len(results) >= cap:
                break

    for tname in state.get("teachers") or []:
        t_str = str(tname or "").strip()
        tmeta = (state.get("teacherMeta") or {}).get(t_str) or {}
        if _matches(t_str, tmeta.get("specialization"), tmeta.get("phone"), tmeta.get("notes")):
            results.append({
                "entityType": "teacher",
                "id": t_str,
                "title": f"👩‍🏫 المعلمة: {t_str}",
                "subtitle": f"النسبة: {tmeta.get('pct', 70)}% | الحالة: {'غير نشطة/مؤرشفة' if tmeta.get('archived') else 'نشطة'}",
                "deepLink": f"#/students?teacher={t_str}",
            })

    for inst in state.get("installments") or []:
        if not isinstance(inst, dict):
            continue
        st_obj = stu_by_id.get(str(inst.get("studentId") or "")) or {}
        if _matches(inst.get("receiptNo"), inst.get("paymentDate"), inst.get("method"), inst.get("note"), st_obj.get("name")):
            iid = str(inst.get("id") or "")
            results.append({
                "entityType": "receipt",
                "id": iid,
                "title": f"🧾 إيصال {inst.get('receiptNo', '')} — {st_obj.get('name', inst.get('studentId', ''))}",
                "subtitle": f"المبلغ: {inst.get('amount', 0)} ج.م | تاريخ الدفع: {inst.get('paymentDate', '')} | الشهر: {inst.get('month', '')}",
                "deepLink": f"#/collections?highlight={iid}",
            })
            if len(results) >= cap:
                break

    for tx in state.get("transactions") or []:
        if not isinstance(tx, dict):
            continue
        if _matches(tx.get("category"), tx.get("note"), tx.get("method"), tx.get("date")):
            tid = str(tx.get("id") or "")
            results.append({
                "entityType": "transaction",
                "id": tid,
                "title": f"🏦 قيد خزينة: {tx.get('category', '')} ({tx.get('amount', 0)} ج.م)",
                "subtitle": f"التاريخ: {tx.get('date', '')} | الوسيلة: {tx.get('method', '')} | {tx.get('note', '')}",
                "deepLink": f"#/accounting?highlight={tid}",
            })
            if len(results) >= cap:
                break

    for sess in state.get("sessions") or []:
        if not isinstance(sess, dict):
            continue
        st_obj = stu_by_id.get(str(sess.get("studentId") or "")) or {}
        if _matches(sess.get("date"), sess.get("time"), sess.get("teacher"), sess.get("notes"), st_obj.get("name")):
            xid = str(sess.get("id") or "")
            results.append({
                "entityType": "session",
                "id": xid,
                "title": f"🗓️ حصة: {st_obj.get('name', '')} ({sess.get('date', '')} {sess.get('time', '')})",
                "subtitle": f"المعلمة: {sess.get('teacher', '')} | الحالة: {sess.get('status', '')}",
                "deepLink": f"#/attendance?highlight={xid}",
            })
            if len(results) >= cap:
                break

    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in results[:cap]:
        by_type.setdefault(r["entityType"], []).append(r)
    groups = {
        "students": by_type.get("student", []),
        "teachers": by_type.get("teacher", []),
        "receipts": by_type.get("receipt", []),
        "transactions": by_type.get("transaction", []),
        "sessions": by_type.get("session", []),
    }

    return {
        "ok": True,
        "query": q_raw,
        "total": len(results[:cap]),
        "totalMatches": len(results[:cap]),
        "results": results[:cap],
        "byType": by_type,
        "groups": groups,
    }
