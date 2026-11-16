"""Smart Attendance, Dynamic Months & Study Period Engine.

Strict Business Rule:
All study period durations, subscription end-date calculations, elapsed/remaining study days,
and attendance session projections are anchored strictly to `actualStartDate` (the date the student
officially started attending classes), NEVER to `paymentDate` (the date fees were paid).
"""
from __future__ import annotations

import datetime
import re
import secrets
from typing import Any

from .finance_engine import resolve_actual_start_date, resolve_end_date, resolve_payment_date

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

ARABIC_MONTH_NAMES = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}


def format_month_label(month_key: str) -> str:
    """Formats 'YYYY-MM' into Arabic month label e.g. 'نوفمبر 2026'."""
    if not MONTH_RE.match(str(month_key or "")):
        return str(month_key or "")
    y_str, m_str = str(month_key).split("-")
    return f"{ARABIC_MONTH_NAMES.get(int(m_str), m_str)} {y_str}"


def generate_dynamic_months(
    reference_date: str | None = None,
    past_months: int = 6,
    future_months: int = 6,
    extra_months: list[str] | set[str] | None = None,
) -> list[dict[str, str]]:
    """Generates a sorted list of {'key': 'YYYY-MM', 'label': '...'} dynamically around
    `reference_date` (or today's date), merged with any month keys that exist in data.
    """
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    base_y, base_m = ref_dt.year, ref_dt.month
    keys: set[str] = set()
    for offset in range(-max(0, int(past_months)), max(0, int(future_months)) + 1):
        total_m = (base_y * 12 + (base_m - 1)) + offset
        y = total_m // 12
        m = (total_m % 12) + 1
        keys.add(f"{y:04d}-{m:02d}")
    for extra in extra_months or []:
        if extra and MONTH_RE.match(str(extra)):
            keys.add(str(extra))
    sorted_keys = sorted(keys)
    return [{"key": k, "label": format_month_label(k)} for k in sorted_keys]


def normalize_time_str(raw_time: Any, default: str = "16:00") -> str:
    """Normalizes a time string such as '9:00' or '16:00:00' into 'HH:MM' format."""
    s = str(raw_time or "").strip()
    if not s:
        return default
    parts = s.split(":")
    if len(parts) in (2, 3):
        try:
            hh = int(parts[0])
            mm = int(parts[1])
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return f"{hh:02d}:{mm:02d}"
        except ValueError:
            pass
    return s


def is_valid_calendar_date(iso_str: str) -> bool:
    if not iso_str or not DATE_RE.match(str(iso_str)):
        return False
    try:
        y, m, d = map(int, str(iso_str).split("-"))
        datetime.date(y, m, d)
        return True
    except ValueError:
        return False


def parse_iso_date(iso_str: str) -> datetime.date | None:
    if not is_valid_calendar_date(iso_str):
        return None
    y, m, d = map(int, str(iso_str).split("-"))
    return datetime.date(y, m, d)


def calculate_study_period(
    actual_start_date: str | None,
    end_date: str | None = None,
    duration_days: int = 30,
    reference_date: str | None = None,
    expiring_soon_days: int = 5,
) -> dict[str, Any]:
    """Calculates subscription duration and study-related periods strictly from `actualStartDate`.
    Never uses `paymentDate`.
    """
    start_dt = parse_iso_date(actual_start_date or "")
    end_dt = parse_iso_date(end_date or "")
    ref_dt = parse_iso_date(reference_date or "") or datetime.date.today()
    soon_threshold = max(0, int(expiring_soon_days if expiring_soon_days is not None else 5))

    if not start_dt and not end_dt:
        return {
            "actualStartDate": "",
            "calculatedEndDate": "",
            "durationDays": 0,
            "elapsedDays": 0,
            "daysUntilStart": 0,
            "remainingDays": None,
            "status": "unscheduled",
        }

    if start_dt and not end_dt:
        dur = max(1, int(duration_days or 30))
        end_dt = start_dt + datetime.timedelta(days=dur)

    if start_dt and end_dt:
        total_days = max(0, (end_dt - start_dt).days)
    else:
        total_days = max(1, int(duration_days or 30))

    if start_dt and ref_dt < start_dt:
        days_until_start = (start_dt - ref_dt).days
        remaining = (end_dt - ref_dt).days if end_dt else total_days
        return {
            "actualStartDate": start_dt.isoformat(),
            "calculatedEndDate": end_dt.isoformat() if end_dt else "",
            "durationDays": total_days,
            "elapsedDays": 0,
            "daysUntilStart": days_until_start,
            "remainingDays": remaining,
            "status": "not_started",
        }

    elapsed = (ref_dt - start_dt).days if start_dt else 0
    remaining = (end_dt - ref_dt).days if end_dt else 0

    if remaining < 0:
        status = "expired"
    elif remaining <= soon_threshold:
        status = "expiring_soon"
    else:
        status = "active"

    return {
        "actualStartDate": start_dt.isoformat() if start_dt else "",
        "calculatedEndDate": end_dt.isoformat() if end_dt else "",
        "durationDays": total_days,
        "elapsedDays": max(0, elapsed),
        "daysUntilStart": 0,
        "remainingDays": remaining,
        "status": status,
    }


def evaluate_student_month_dates(
    month_entry: dict[str, Any] | None,
    reference_date: str | None = None,
    default_duration_days: int = 30,
    expiring_soon_days: int = 5,
    student_month_installments: list[dict[str, Any]] | None = None,
    duration_days: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Evaluates both financial payment date and study period dates independently for a month entry."""
    eff_duration = duration_days if duration_days is not None else default_duration_days
    pay_date = resolve_payment_date(month_entry, student_month_installments, reference_date=reference_date)
    actual_start = resolve_actual_start_date(month_entry)
    explicit_end = resolve_end_date(month_entry)
    study_info = calculate_study_period(
        actual_start_date=actual_start,
        end_date=explicit_end,
        duration_days=eff_duration,
        reference_date=reference_date,
        expiring_soon_days=expiring_soon_days,
    )
    return {
        "paymentDate": pay_date,
        "actualStartDate": actual_start,
        "endDate": explicit_end or study_info["calculatedEndDate"],
        "isPaidByDate": bool(pay_date),
        "paidInAdvance": bool(pay_date and actual_start and pay_date < actual_start),
        "paidAfterStart": bool(pay_date and actual_start and pay_date > actual_start),
        "studyPeriod": study_info,
    }


def project_smart_attendance_dates(
    start_iso: str,
    days_of_week: list[int],
    total_sessions: int,
    end_limit_iso: str | None = None,
) -> dict[str, Any]:
    """Projects actual class session dates onto the calendar starting from `start_iso` (Actual Start Date)
    and selected days of the week (0=Sunday .. 6=Saturday).
    """
    if (
        not is_valid_calendar_date(str(start_iso or ""))
        or not days_of_week
        or not isinstance(total_sessions, (int, float))
        or int(total_sessions) <= 0
    ):
        return {"dates": [], "actualStartDate": str(start_iso or ""), "endDate": "", "truncated": False}

    total_sessions = int(total_sessions)
    valid_days = {int(d) for d in days_of_week if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}
    if not valid_days:
        return {"dates": [], "actualStartDate": str(start_iso or ""), "endDate": "", "truncated": False}

    y, m, d = map(int, str(start_iso).split("-"))
    cur = datetime.date(y, m, d)
    limit = None
    if end_limit_iso and is_valid_calendar_date(str(end_limit_iso)):
        ly, lm, ld = map(int, str(end_limit_iso).split("-"))
        limit = datetime.date(ly, lm, ld)

    dates: list[str] = []
    guard = 0
    max_guard = total_sessions * 62 + 800
    truncated = False
    while len(dates) < total_sessions and guard < max_guard:
        guard += 1
        if limit and cur > limit:
            truncated = True
            break
        ar_day = (cur.weekday() + 1) % 7
        if ar_day in valid_days:
            dates.append(cur.isoformat())
        cur += datetime.timedelta(days=1)

    if len(dates) < total_sessions:
        truncated = True

    return {
        "dates": dates,
        "actualStartDate": str(start_iso),
        "endDate": dates[-1] if dates else "",
        "truncated": truncated,
    }


def auto_compensate_cancelled_session(
    session_id: str,
    sessions: list[dict[str, Any]],
    plans: list[dict[str, Any]],
    students: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compensates a teacher-cancelled session (`cancel_teacher`) by scheduling a makeup session
    after the student's latest scheduled session and updating the month's study `endDate` (`end`).
    Never alters `paymentDate` (`pay`).
    """
    if not session_id or not isinstance(session_id, str) or not isinstance(sessions, list):
        return {"compensated": False, "reason": "invalid_input"}

    target = next((s for s in sessions if isinstance(s, dict) and s.get("id") == session_id), None)
    if not target:
        return {"compensated": False, "reason": "session_not_found"}

    target_date = str(target.get("date") or "").strip()
    student_id = str(target.get("studentId") or "").strip()
    if not student_id:
        return {"compensated": False, "reason": "missing_student_id"}
    if not is_valid_calendar_date(target_date):
        return {"compensated": False, "reason": "missing_or_invalid_date"}

    target["status"] = "cancel_teacher"

    existing_comp = next(
        (s for s in sessions if isinstance(s, dict) and s.get("compensatedFor") == session_id),
        None,
    )
    if existing_comp:
        return {"compensated": False, "reason": "already_compensated", "newSession": existing_comp}

    plan = next(
        (p for p in (plans or []) if isinstance(p, dict) and p.get("id") == target.get("planId")),
        None,
    )
    stu_valid_dates = [
        str(s.get("date"))
        for s in sessions
        if isinstance(s, dict)
        and s.get("studentId") == student_id
        and (not target.get("planId") or s.get("planId") == target.get("planId"))
        and is_valid_calendar_date(str(s.get("date") or ""))
    ]
    max_date_str = max(stu_valid_dates) if stu_valid_dates else target_date
    y, m, d = map(int, max_date_str.split("-"))
    next_start = (datetime.date(y, m, d) + datetime.timedelta(days=1)).isoformat()

    days_of_week: list[int] = []
    if plan and isinstance(plan.get("days"), list):
        days_of_week = [int(x) for x in plan["days"] if isinstance(x, int) and 0 <= x <= 6]
    if not days_of_week:
        ty, tm, td = map(int, target_date.split("-"))
        days_of_week = [(datetime.date(ty, tm, td).weekday() + 1) % 7]

    proj = project_smart_attendance_dates(next_start, days_of_week, 1)
    if not proj["dates"]:
        return {"compensated": False, "reason": "projection_failed"}

    new_date = proj["dates"][0]
    dur_val = target.get("dur")
    try:
        dur_int = int(float(dur_val)) if dur_val is not None else 40
        if dur_int <= 0 or dur_int > 600:
            dur_int = 40
    except (ValueError, TypeError):
        dur_int = 40

    time_str = str(target.get("time") or "16:00").strip()
    if not TIME_RE.match(time_str):
        time_str = "16:00"

    new_sess = {
        "id": "comp_" + secrets.token_hex(4),
        "planId": str(target.get("planId") or ""),
        "groupId": str(target.get("groupId") or ""),
        "studentId": student_id,
        "teacher": str(target.get("teacher") or ""),
        "date": new_date,
        "time": time_str,
        "dur": dur_int,
        "status": "scheduled",
        "compensatedFor": session_id,
        "notes": f"حصة تعويضية تلقائية عن حصة {target_date} (إلغاء المعلمة)",
        "createdAt": datetime.date.today().isoformat(),
        "updatedAt": datetime.date.today().isoformat(),
    }
    sessions.append(new_sess)

    new_end_date = new_date
    month_key = target_date[:7]
    if students and MONTH_RE.match(month_key):
        stu = next((st for st in students if isinstance(st, dict) and st.get("id") == student_id), None)
        if stu and isinstance(stu.get("months"), dict):
            if month_key in stu["months"] and isinstance(stu["months"][month_key], dict):
                cur_end = resolve_end_date(stu["months"][month_key])
                if not cur_end or new_date > cur_end:
                    stu["months"][month_key]["end"] = new_date
                    stu["months"][month_key]["endDate"] = new_date
                    new_end_date = new_date

    return {"compensated": True, "newSession": new_sess, "newEndDate": new_end_date, "monthKey": month_key}
