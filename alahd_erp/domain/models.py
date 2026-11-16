"""Domain Models, Validation Rules, Normalization, and Duplicate Detection.

Enforces Clean Architecture domain invariants:
1. `paymentDate` (Payment Date) and `actualStartDate` (Actual Start Date) are strictly separate,
   independent fields on both student profiles and monthly subscription entries.
2. Neither field is ever auto-copied from or assumed equal to the other.
3. Study period ordering checks `endDate >= actualStartDate` when both are set; `paymentDate`
   can occur before `actualStartDate` (advance payment), on `actualStartDate`, or after
   `actualStartDate` / `endDate` (deferred payment).
4. Percentages are validated and stored as integer percent (0..100).
"""
from __future__ import annotations

import math
import re
from typing import Any

from .attendance_engine import MONTH_RE, TIME_RE, is_valid_calendar_date, normalize_time_str
from .finance_engine import (
    parse_pct_int,
    resolve_actual_start_date,
    resolve_end_date,
    resolve_payment_date,
    round_half_up,
)

MAX_STUDENTS = 20000
_NUM_FIELDS_M = ("value", "partialPaid", "customWage", "discount")

ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

DEFAULT_SETTINGS: dict[str, Any] = {
    "onboardingCompleted": False,
    "academyName": "أكاديمية يوم جديد",
    "academySubtitle": "نظام إدارة الأكاديمية الموحد",
    "logoDataUri": "",
    "currency": "ج.م",
    "defaultDurationDays": 30,
    "expiringSoonDays": 5,
    "defaultTeacherPct": 70,
    "pctSource": "student",
    "wageBasis": "net",
    "wageAccrualMode": "accrued",
    "monthsRangePast": 6,
    "monthsRangeFuture": 6,
    "extraBackupFolder": "",
    "backupWarnDays": 3,
    "consecutiveAbsenceThreshold": 2,
    "idleLockMinutes": 15,
    "pinEnabled": False,
    "isDemoData": False,
    "alertsConfig": {},
    "defaultLandingView": "dashboard",
    "defaultPageSize": 50,
    "useArabicDigits": False,
    "autoStartEnabled": False,
    "autoStartInitialized": False,
    "startMinimized": False,
    "theme": "light",
    "fontScale": "md",
    "tableDensity": "comfortable",
    "tableZebraStripes": True,
    "sidebarCompact": False,
    "showDashAlertsStrip": True,
    "studentCodePrefix": "STD-",
    "requireStudentPhone": False,
    "defaultStudentViewMode": "table",
    "studentStatuses": ["نشطة", "متوقفة مؤقتاً", "منقطعة", "خريجة"],
    "defaultSessionDurationMin": 40,
    "defaultPlanSessionCount": 8,
    "defaultSessionStartTime": "16:00",
    "defaultStatsPeriod": "all_time",
    "defaultCollectionBasis": "cash",
    "hidePersonalDataByDefault": True,
    "defaultImportMode": "update_existing",
    "defaultAutoCreateTeachers": True,
    "defaultAutoCreatePackages": True,
}


class DomainValidationError(ValueError):
    """Raised when domain validation fails (maps to HTTP 400)."""


class DuplicateTeacherError(DomainValidationError):
    """Raised when a teacher name conflicts with an existing teacher (maps to HTTP 409)."""

    def __init__(self, candidate_name: str, existing_name: str, match_type: str = "normalized") -> None:
        self.candidate_name = candidate_name
        self.existing_name = existing_name
        self.match_type = match_type
        if match_type == "exact":
            msg = f"المعلمة «{existing_name}» مسجلة بالفعل في النظام."
        else:
            msg = (
                f"الاسم المدخل «{candidate_name}» يتطابق بعد التطبيع الإملائي مع المعلمة المسجلة "
                f"بالفعل «{existing_name}». يرجى اختيار المعلمة الموجودة أو تأكيد الإضافة المتعمدة."
            )
        super().__init__(msg)


def _bad(msg: str) -> None:
    raise DomainValidationError(msg)


def _is_num(v: Any) -> bool:
    if v is None or v == "" or isinstance(v, bool):
        return not isinstance(v, bool)
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _sid(v: Any, msg: str, max_len: int = 64) -> str:
    if not isinstance(v, str) or not v.strip() or len(v) > max_len:
        _bad(msg)
    return v.strip()


def normalize_digits_to_ascii(text: str) -> str:
    """Converts Arabic-Indic and Eastern Arabic-Indic digits to ASCII digits 0..9."""
    return str(text or "").translate(ARABIC_DIGITS_MAP)


def normalize_arabic_for_comparison(text: str) -> str:
    """Normalizes Arabic letters (أ/إ/آ -> ا, ى -> ي, ة -> ه, removes tashkeel) strictly for comparison."""
    s = normalize_digits_to_ascii(str(text or "")).strip()
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s)
    s = re.sub(r"[أإآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


_TEACHER_HONORIFIC_RE = re.compile(
    r"^(?:أ\s*/\s*|أ\s*\.\s*|ا\s*/\s*|ا\s*\.\s*|أستاذة\s+|استاذة\s+|أستاذه\s+|استاذه\s+|المعلمة\s+|معلمة\s+|معلمه\s+|الشيخة\s+|شيخة\s+|مس\s+)+",
    re.UNICODE,
)


def clean_teacher_display_name(name: Any) -> str:
    """Collapses leading, trailing, and repeated internal whitespace while preserving original display text."""
    return re.sub(r"\s+", " ", str(name or "").strip())


def normalize_teacher_key(name: Any) -> str:
    """Normalizes a teacher name for safe duplicate detection (honorifics, tashkeel, Alef/Ta-Marbuta/Ya, compound spacing)."""
    s = clean_teacher_display_name(name)
    if not s:
        return ""
    s_no_diac = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s).strip()
    stripped_hon = _TEACHER_HONORIFIC_RE.sub("", s_no_diac).strip()
    if stripped_hon:
        s_no_diac = stripped_hon
    norm = normalize_arabic_for_comparison(s_no_diac)
    norm = re.sub(r"\bعبد\s+ال", "عبدال", norm)
    norm = re.sub(r"[/._\-]+", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


def find_duplicate_teacher(
    candidate_name: Any,
    existing_teachers: Any,
    exclude_name: str | None = None,
) -> dict[str, str] | None:
    """Checks if `candidate_name` matches any teacher in `existing_teachers` (exact or Arabic-normalized)."""
    cand_clean = clean_teacher_display_name(candidate_name)
    if not cand_clean:
        return None
    excl_clean = clean_teacher_display_name(exclude_name) if exclude_name else ""
    teachers_iter = list(existing_teachers or [])

    # Pass 1: exact whitespace-normalized or ASCII case-insensitive match
    for t in teachers_iter:
        t_str = str(t or "")
        t_clean = clean_teacher_display_name(t_str)
        if not t_clean:
            continue
        if excl_clean and t_clean == excl_clean:
            continue
        if t_clean == cand_clean or t_clean.lower() == cand_clean.lower():
            return {
                "existingName": t_str.strip() or t_clean,
                "matchType": "exact",
                "normalizedKey": normalize_teacher_key(cand_clean),
            }

    # Pass 2: normalized Arabic / honorific / compound-name match (only when normalized key has >= 2 chars)
    cand_key = normalize_teacher_key(cand_clean)
    if len(cand_key) >= 2:
        for t in teachers_iter:
            t_str = str(t or "")
            t_clean = clean_teacher_display_name(t_str)
            if not t_clean:
                continue
            if excl_clean and t_clean == excl_clean:
                continue
            t_key = normalize_teacher_key(t_clean)
            if t_key and t_key == cand_key:
                return {
                    "existingName": t_str.strip() or t_clean,
                    "matchType": "normalized",
                    "normalizedKey": cand_key,
                }
    return None


def normalize_phone_digits(phone: str) -> str:
    """Normalizes Egyptian/international phone digits for duplicate comparison."""
    s = normalize_digits_to_ascii(str(phone or "")).strip()
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if digits.startswith("00201") and len(digits) == 14:
        digits = "0" + digits[4:]
    elif digits.startswith("201") and len(digits) == 12:
        digits = "0" + digits[2:]
    elif digits.startswith("1") and len(digits) == 10 and digits[1] in ("0", "1", "2", "5"):
        digits = "0" + digits
    return digits or s


def detect_duplicate_students(students: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detects duplicate students sharing (normalized Arabic name + normalized phone)."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for idx, s in enumerate(students or []):
        if not isinstance(s, dict):
            continue
        raw_name = re.sub(r"\s+", " ", str(s.get("name") or "").strip())
        if not raw_name:
            continue
        norm_name = normalize_arabic_for_comparison(raw_name)
        phone = normalize_phone_digits(str(s.get("phone") or ""))
        key = (norm_name, phone)
        by_key.setdefault(key, []).append({
            "index": idx,
            "id": s.get("id", ""),
            "name": raw_name,
            "phone": s.get("phone", ""),
            "teacher": s.get("teacher", ""),
        })

    duplicates = []
    for (_, phone), items in by_key.items():
        if len(items) > 1:
            duplicates.append({
                "name": items[0]["name"],
                "phone": items[0]["phone"],
                "normalizedPhone": phone,
                "count": len(items),
                "records": items,
            })
    return duplicates


def validate_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validates and merges user settings with DEFAULT_SETTINGS."""
    merged = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return merged

    if "onboardingCompleted" in raw or "onboarded" in raw:
        val = bool(raw.get("onboardingCompleted") or raw.get("onboarded"))
        merged["onboardingCompleted"] = val
        merged["onboarded"] = val
    if "academyName" in raw and isinstance(raw["academyName"], str) and raw["academyName"].strip():
        merged["academyName"] = raw["academyName"].strip()[:120]
    if "logoDataUri" in raw and isinstance(raw["logoDataUri"], str):
        if len(raw["logoDataUri"]) <= 2_000_000:
            merged["logoDataUri"] = raw["logoDataUri"]
    if "currency" in raw and isinstance(raw["currency"], str) and raw["currency"].strip():
        merged["currency"] = raw["currency"].strip()[:20]

    if "academySubtitle" in raw and isinstance(raw["academySubtitle"], str):
        merged["academySubtitle"] = raw["academySubtitle"].strip()[:160]
    if "logoDataUrl" in raw and "logoDataUri" not in raw and isinstance(raw["logoDataUrl"], str):
        if len(raw["logoDataUrl"]) <= 2_000_000:
            merged["logoDataUri"] = raw["logoDataUrl"]
    if "currencyLabel" in raw and "currency" not in raw and isinstance(raw["currencyLabel"], str) and raw["currencyLabel"].strip():
        merged["currency"] = raw["currencyLabel"].strip()[:20]
    if "pinIdleMinutes" in raw and "idleLockMinutes" not in raw:
        raw = dict(raw, idleLockMinutes=raw["pinIdleMinutes"])

    for int_key, min_v, max_v, def_v in (
        ("defaultDurationDays", 1, 365, 30),
        ("expiringSoonDays", 0, 90, 5),
        ("defaultTeacherPct", 0, 100, 70),
        ("monthsRangePast", 1, 36, 6),
        ("monthsRangeFuture", 1, 36, 6),
        ("backupWarnDays", 1, 90, 3),
        ("consecutiveAbsenceThreshold", 1, 30, 2),
        ("idleLockMinutes", 0, 480, 15),
        ("defaultPageSize", 10, 500, 50),
        ("defaultSessionDurationMin", 10, 300, 40),
        ("defaultPlanSessionCount", 1, 100, 8),
    ):
        if int_key in raw and raw[int_key] is not None and raw[int_key] != "":
            try:
                v = round_half_up(raw[int_key])
                if not (min_v <= v <= max_v):
                    _bad(f"{int_key} must be between {min_v} and {max_v}")
                merged[int_key] = v
            except ValueError as exc:
                _bad(f"invalid {int_key}: {exc}")

    if "pctSource" in raw:
        if raw["pctSource"] not in ("student", "teacher"):
            _bad("pctSource must be 'student' or 'teacher'")
        merged["pctSource"] = raw["pctSource"]

    if "wageBasis" in raw:
        if raw["wageBasis"] not in ("net", "gross"):
            _bad("wageBasis must be 'net' or 'gross'")
        merged["wageBasis"] = raw["wageBasis"]

    if "wageAccrualMode" in raw:
        if raw["wageAccrualMode"] not in ("accrued", "collected"):
            _bad("wageAccrualMode must be 'accrued' or 'collected'")
        merged["wageAccrualMode"] = raw["wageAccrualMode"]

    for enum_key, allowed_vals in (
        ("defaultLandingView", ("dashboard", "students", "attendance", "memorization", "teachers", "accounting", "statistics", "alerts")),
        ("theme", ("light", "dark", "sepia")),
        ("fontScale", ("sm", "md", "lg", "xl")),
        ("tableDensity", ("compact", "comfortable", "spacious")),
        ("defaultStudentViewMode", ("table", "cards")),
        ("defaultStatsPeriod", ("all_time", "current_month", "last_3_months", "current_year")),
        ("defaultCollectionBasis", ("cash", "accrual")),
        ("defaultImportMode", ("update_existing", "append_only", "skip_existing")),
    ):
        if enum_key in raw and raw[enum_key] is not None and raw[enum_key] != "":
            val_s = str(raw[enum_key]).strip()
            if val_s not in allowed_vals:
                _bad(f"{enum_key} must be one of {allowed_vals}")
            merged[enum_key] = val_s

    if "studentCodePrefix" in raw and isinstance(raw["studentCodePrefix"], str):
        merged["studentCodePrefix"] = raw["studentCodePrefix"].strip()[:20]

    if "defaultSessionStartTime" in raw and raw["defaultSessionStartTime"]:
        norm_t = normalize_time_str(str(raw["defaultSessionStartTime"]))
        if not TIME_RE.match(norm_t):
            _bad("invalid defaultSessionStartTime (expected HH:MM)")
        merged["defaultSessionStartTime"] = norm_t

    if "studentStatuses" in raw and isinstance(raw["studentStatuses"], list):
        clean_statuses = []
        for st in raw["studentStatuses"][:25]:
            st_s = str(st or "").strip()[:64]
            if st_s and st_s not in clean_statuses:
                clean_statuses.append(st_s)
        if clean_statuses:
            merged["studentStatuses"] = clean_statuses

    for bool_key in (
        "useArabicDigits",
        "autoStartEnabled",
        "autoStartInitialized",
        "startMinimized",
        "tableZebraStripes",
        "sidebarCompact",
        "showDashAlertsStrip",
        "requireStudentPhone",
        "hidePersonalDataByDefault",
        "defaultAutoCreateTeachers",
        "defaultAutoCreatePackages",
        "pinEnabled",
        "isDemoData",
    ):
        if bool_key in raw:
            merged[bool_key] = bool(raw[bool_key])

    if "isDemoLoaded" in raw:
        merged["isDemoLoaded"] = bool(raw["isDemoLoaded"])

    if "extraBackupFolder" in raw and isinstance(raw["extraBackupFolder"], str):
        merged["extraBackupFolder"] = raw["extraBackupFolder"].strip()[:500]

    if "alertsConfig" in raw and isinstance(raw["alertsConfig"], dict):
        merged["alertsConfig"] = dict(raw["alertsConfig"])

    return merged


def validate_month_entry(
    mk: str,
    m: Any,
    student_id: str = "",
    allow_legacy_dates: bool = False,
) -> dict[str, Any]:
    """Validates a single student's monthly subscription record with independent
    `paymentDate` and `actualStartDate` fields.
    """
    if not MONTH_RE.match(str(mk)) or not isinstance(m, dict):
        _bad(f"invalid month entry {mk} for {student_id}")
    if not all(_is_num(m.get(f)) for f in _NUM_FIELDS_M):
        _bad(f"invalid number in {mk} for {student_id}")

    val_raw = m.get("value")
    val_int = None
    if val_raw is not None and val_raw != "":
        val_f = float(val_raw)
        if val_f < 0:
            _bad(f"negative subscription value not allowed in {mk} for {student_id}")
        val_int = round_half_up(val_raw)

    pp_raw = m.get("partialPaid")
    if pp_raw is not None and pp_raw != "":
        pp_f = float(pp_raw)
        if pp_f < 0:
            _bad(f"negative partialPaid not allowed in {mk} for {student_id}")

    cw_raw = m.get("customWage")
    if cw_raw is not None and cw_raw != "":
        cw_f = float(cw_raw)
        if cw_f < 0:
            _bad(f"negative customWage not allowed in {mk} for {student_id}")

    disc_raw = m.get("discount")
    if disc_raw is not None and disc_raw != "":
        disc_f = float(disc_raw)
        if disc_f < 0:
            _bad(f"negative discount not allowed in {mk} for {student_id}")
        disc_int = round_half_up(disc_raw)
        if disc_int > 0 and (val_int is None or disc_int > val_int):
            _bad(f"discount ({disc_int}) cannot exceed subscription value ({val_int or 0}) in {mk} for {student_id}")

    pay_str = resolve_payment_date(m)
    actual_start_str = resolve_actual_start_date(m)
    end_str = resolve_end_date(m)

    if pay_str and not is_valid_calendar_date(pay_str):
        _bad(f"invalid payment date {pay_str} in {mk} for {student_id}")
    if actual_start_str and not is_valid_calendar_date(actual_start_str):
        _bad(f"invalid actual start date {actual_start_str} in {mk} for {student_id}")
    if end_str and not is_valid_calendar_date(end_str):
        _bad(f"invalid end date {end_str} in {mk} for {student_id}")

    # Study period invariant: endDate cannot precede actualStartDate (when actualStartDate is set).
    if actual_start_str and end_str and end_str < actual_start_str and not allow_legacy_dates:
        _bad(
            f"end date ({end_str}) cannot be before actual start date ({actual_start_str}) in {mk} for {student_id}"
        )

    if "pct" in m and m.get("pct") is not None and m.get("pct") != "":
        if not _is_num(m.get("pct")):
            _bad(f"invalid pct in {mk} for {student_id}")
        try:
            parse_pct_int(m.get("pct"), default=0)
        except ValueError as exc:
            _bad(f"pct out of valid range (0..100%) in {mk} for {student_id}: {exc}")

    inv_note = str(m.get("invoiceNote") or "")
    if len(inv_note) > 1000:
        _bad(f"invoiceNote too long in {mk} for {student_id}")

    return m


def validate_student_record(
    s: Any,
    known_teachers: set[str] | None = None,
    allow_legacy_dates: bool = False,
) -> dict[str, Any]:
    """Validates a student record including independent student-level and month-level
    `paymentDate` and `actualStartDate` fields, and integer percentage (0..100).
    """
    if not isinstance(s, dict):
        _bad("invalid student")
    sid = _sid(s.get("id"), "invalid student id", max_len=64)
    raw_name = s.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip() or len(raw_name.strip()) > 120:
        _bad(f"invalid student name for {sid}")

    phone = str(s.get("phone") or "")
    if len(phone) > 40:
        _bad(f"phone too long for {sid}")

    tname = str(s.get("teacher") or "").strip()
    if len(tname) > 120:
        _bad(f"teacher name too long for {sid}")
    if tname and known_teachers is not None and tname not in known_teachers:
        _bad(f"unknown teacher '{tname}' for student {sid}")

    stype = str(s.get("type") or "")
    if len(stype) > 120:
        _bad(f"subscription type too long for {sid}")

    sched = str(s.get("schedule") or "")
    if len(sched) > 200:
        _bad(f"schedule too long for {sid}")

    if not _is_num(s.get("pct")):
        _bad("invalid pct for " + sid)
    pct_raw = s.get("pct")
    if pct_raw is not None and pct_raw != "":
        try:
            s["pct"] = parse_pct_int(pct_raw, default=70)
        except ValueError as exc:
            _bad(f"pct out of valid range (0..100%) for {sid}: {exc}")

    # Optional profile-level paymentDate and actualStartDate (strictly independent)
    stu_pay_date = str(s.get("paymentDate") or "").strip()
    stu_actual_start = str(s.get("actualStartDate") or "").strip()
    if stu_pay_date and not is_valid_calendar_date(stu_pay_date):
        _bad(f"invalid student paymentDate {stu_pay_date} for {sid}")
    if stu_actual_start and not is_valid_calendar_date(stu_actual_start):
        _bad(f"invalid student actualStartDate {stu_actual_start} for {sid}")

    months = s.get("months", {})
    if not isinstance(months, dict):
        _bad("invalid months")
    for mk, m in months.items():
        validate_month_entry(str(mk), m, student_id=sid, allow_legacy_dates=allow_legacy_dates)

    return s


def validate_state(
    d: Any,
    allow_legacy_dates: bool = False,
    existing_teachers: set[str] | None = None,
) -> dict[str, Any]:
    """Validates the full application state payload prior to any database transaction."""
    if not isinstance(d, dict) or not isinstance(d.get("students"), list):
        _bad("invalid payload: students[] required")
    if len(d["students"]) > MAX_STUDENTS:
        raise OverflowError("too many students")

    known_teachers: set[str] | None = None
    if "teachers" in d:
        if not isinstance(d["teachers"], list) or not all(
            isinstance(t, str) and 1 <= len(t.strip()) <= 120 for t in d["teachers"]
        ):
            _bad("invalid teachers")
        known_teachers = {t.strip() for t in d["teachers"]}
    elif existing_teachers:
        known_teachers = set(existing_teachers)

    if "teacherMeta" in d:
        if not isinstance(d["teacherMeta"], dict):
            _bad("invalid teacherMeta")
        for tname, meta in d["teacherMeta"].items():
            if not isinstance(tname, str) or not isinstance(meta, dict):
                _bad("invalid teacherMeta entry")
            if "pct" in meta and meta["pct"] is not None and meta["pct"] != "":
                if not _is_num(meta["pct"]):
                    _bad(f"invalid default percentage for teacher {tname}")
                try:
                    meta["pct"] = parse_pct_int(meta["pct"], default=70)
                except ValueError:
                    _bad(f"invalid default percentage for teacher {tname}")

    ids: set[str] = set()
    for s in d["students"]:
        validate_student_record(s, known_teachers=known_teachers, allow_legacy_dates=allow_legacy_dates)
        if s["id"] in ids:
            _bad("duplicate student id: " + s["id"])
        ids.add(s["id"])

    payouts = d.get("payouts")
    if payouts is not None:
        if not isinstance(payouts, list) or len(payouts) > 20000:
            _bad("invalid payouts")
        for p in payouts:
            if not isinstance(p, dict) or not isinstance(p.get("teacher"), str) or not p.get("teacher", "").strip():
                _bad("invalid payout entry")
            if known_teachers is not None and p["teacher"].strip() not in known_teachers:
                _bad(f"payout references unknown teacher: {p['teacher']}")
            if not MONTH_RE.match(str(p.get("month") or "")) or not _is_num(p.get("amount")):
                _bad("invalid payout entry")
            if p.get("amount") is None or p.get("amount") == "" or float(p["amount"]) <= 0:
                _bad("payout amount must be positive")

    sess_status = {
        "scheduled", "done", "absent", "excused", "late",
        "cancel_teacher", "cancel_admin", "postponed", "makeup",
    }

    groups = d.get("groups")
    if groups is not None:
        if not isinstance(groups, list) or len(groups) > 5000:
            _bad("invalid groups")
        for g in groups:
            if not isinstance(g, dict):
                _bad("invalid group entry")
            _sid(g.get("id"), "invalid group entry")
            if not isinstance(g.get("name"), str) or not g.get("name", "").strip() or len(g.get("name", "")) > 120:
                _bad("invalid group entry")
            days = g.get("days")
            if not isinstance(days, list) or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6 for x in days
            ):
                _bad("invalid group days")
            if g.get("time"):
                norm_gtime = normalize_time_str(g.get("time"), default="")
                if not TIME_RE.match(norm_gtime):
                    _bad("invalid group time")
                g["time"] = norm_gtime
            if not _is_num(g.get("capacity")) or (
                g.get("capacity") is not None and g.get("capacity") != "" and float(g["capacity"]) < 0
            ):
                _bad("invalid group capacity")

    sessions = d.get("sessions")
    if sessions is not None:
        if not isinstance(sessions, list) or len(sessions) > 60000:
            _bad("invalid sessions")
        for s in sessions:
            if not isinstance(s, dict):
                _bad("invalid session entry")
            _sid(s.get("id"), "invalid session entry")
            _sid(s.get("studentId"), "invalid session entry")
            if not isinstance(s.get("teacher"), str):
                _bad("invalid session entry")
            if not is_valid_calendar_date(str(s.get("date") or "")):
                _bad("invalid session date")
            norm_stime = normalize_time_str(s.get("time"), default="")
            if not TIME_RE.match(norm_stime):
                _bad("invalid session time")
            s["time"] = norm_stime
            if not _is_num(s.get("dur")) or (
                s.get("dur") is not None and (float(s["dur"]) <= 0 or float(s["dur"]) > 600)
            ):
                _bad("invalid session duration")
            if s.get("status", "scheduled") not in sess_status:
                _bad("invalid session status")

    plans = d.get("plans")
    if plans is not None:
        if not isinstance(plans, list) or len(plans) > 20000:
            _bad("invalid plans")
        for p in plans:
            if not isinstance(p, dict):
                _bad("invalid plan entry")
            _sid(p.get("id"), "invalid plan entry")
            _sid(p.get("studentId"), "invalid plan entry")
            if not isinstance(p.get("days"), list) or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 6 for x in p["days"]
            ):
                _bad("invalid plan days")
            if not _is_num(p.get("count")) or (
                p.get("count") is not None and not (0 < float(p["count"]) <= 500)
            ):
                _bad("invalid plan count")
            start_val = str(p.get("actualStartDate") or p.get("startDate") or "")
            if not is_valid_calendar_date(start_val):
                _bad("invalid plan startDate")

    alerts_state = d.get("alertsState")
    if alerts_state is not None:
        if not isinstance(alerts_state, dict) or not isinstance(alerts_state.get("dismissed", {}), dict):
            _bad("invalid alertsState")
        if len(alerts_state.get("dismissed", {})) > 50000:
            _bad("invalid alertsState")

    transactions = d.get("transactions")
    if transactions is not None:
        if not isinstance(transactions, list) or len(transactions) > 50000:
            _bad("invalid transactions")
        for tx in transactions:
            if not isinstance(tx, dict):
                _bad("invalid transaction entry")
            _sid(tx.get("id"), "invalid transaction id")
            if tx.get("type") not in ("revenue", "expense"):
                _bad("invalid transaction type")
            if (
                not isinstance(tx.get("category"), str)
                or not tx.get("category", "").strip()
                or len(tx.get("category", "")) > 120
            ):
                _bad("invalid transaction category")
            if (
                not _is_num(tx.get("amount"))
                or tx.get("amount") is None
                or tx.get("amount") == ""
                or float(tx["amount"]) < 0
            ):
                _bad("invalid transaction amount")
            if not is_valid_calendar_date(str(tx.get("date") or "")):
                _bad("invalid transaction date")
            if tx.get("month") and not MONTH_RE.match(str(tx.get("month"))):
                _bad("invalid transaction month")

    installments = d.get("installments")
    if installments is not None:
        if not isinstance(installments, list) or len(installments) > 50000:
            _bad("invalid installments")
        for inst in installments:
            if not isinstance(inst, dict):
                _bad("invalid installment entry")
            _sid(inst.get("id"), "invalid installment id")
            _sid(inst.get("studentId"), "invalid installment studentId")
            if (
                not isinstance(inst.get("receiptNo"), str)
                or not inst.get("receiptNo", "").strip()
                or len(inst.get("receiptNo", "")) > 64
            ):
                _bad("invalid installment receiptNo")
            if not MONTH_RE.match(str(inst.get("month") or "")):
                _bad("invalid installment month")
            if (
                not _is_num(inst.get("amount"))
                or inst.get("amount") is None
                or inst.get("amount") == ""
                or float(inst["amount"]) <= 0
            ):
                _bad("invalid installment amount")
            pay_dt = str(inst.get("paymentDate") or inst.get("date") or "")
            if not is_valid_calendar_date(pay_dt):
                _bad("invalid installment date")

    memorization = d.get("memorization")
    if memorization is not None:
        if not isinstance(memorization, list) or len(memorization) > 60000:
            _bad("invalid memorization")
        for mem in memorization:
            if not isinstance(mem, dict):
                _bad("invalid memorization entry")
            _sid(mem.get("id"), "invalid memorization id")
            _sid(mem.get("studentId"), "invalid memorization studentId")
            if not is_valid_calendar_date(str(mem.get("date") or "")):
                _bad("invalid memorization date")
            if (
                not isinstance(mem.get("track"), str)
                or not mem.get("track", "").strip()
                or len(mem.get("track", "")) > 120
            ):
                _bad("invalid memorization track")
            if not _is_num(mem.get("pages")) or (mem.get("pages") is not None and float(mem["pages"]) < 0):
                _bad("invalid memorization pages")
            if not _is_num(mem.get("score")) or (
                mem.get("score") is not None and not (0 <= float(mem["score"]) <= 100)
            ):
                _bad("invalid memorization score")

    batches = d.get("madrasaBatches")
    if batches is not None:
        if not isinstance(batches, list) or len(batches) > 2000:
            _bad("invalid madrasaBatches")
        for b in batches:
            if not isinstance(b, dict):
                _bad("invalid batch entry")
            _sid(b.get("id"), "invalid batch id")
            if not isinstance(b.get("name"), str) or not b.get("name", "").strip() or len(b.get("name", "")) > 120:
                _bad("invalid batch name")
            if not _is_num(b.get("fee")) or (b.get("fee") is not None and float(b["fee"]) < 0):
                _bad("invalid batch fee")

    enrollments = d.get("madrasaEnrollments")
    if enrollments is not None:
        if not isinstance(enrollments, list) or len(enrollments) > 50000:
            _bad("invalid madrasaEnrollments")
        for e in enrollments:
            if not isinstance(e, dict):
                _bad("invalid enrollment entry")
            _sid(e.get("id"), "invalid enrollment id")
            _sid(e.get("batchId"), "invalid enrollment batchId")
            _sid(e.get("studentId"), "invalid enrollment studentId")
            if not _is_num(e.get("feePaid")) or (
                e.get("feePaid") is not None and e.get("feePaid") != "" and float(e["feePaid"]) < 0
            ):
                _bad("invalid enrollment feePaid")

    mlogs = d.get("madrasaLogs")
    if mlogs is not None:
        if not isinstance(mlogs, list) or len(mlogs) > 100000:
            _bad("invalid madrasaLogs")
        for lg in mlogs:
            if not isinstance(lg, dict):
                _bad("invalid madrasaLog entry")
            _sid(lg.get("id"), "invalid madrasaLog id")
            _sid(lg.get("batchId"), "invalid madrasaLog batchId")
            _sid(lg.get("studentId"), "invalid madrasaLog studentId")
            if not is_valid_calendar_date(str(lg.get("date") or "")):
                _bad("invalid madrasaLog date")

    certs = d.get("certificates")
    if certs is not None:
        if not isinstance(certs, list) or len(certs) > 50000:
            _bad("invalid certificates")
        for c in certs:
            if not isinstance(c, dict):
                _bad("invalid certificate entry")
            _sid(c.get("id"), "invalid certificate id")
            _sid(c.get("studentId"), "invalid certificate studentId")
            if not isinstance(c.get("courseTitle"), str) or not c.get("courseTitle", "").strip():
                _bad("invalid certificate courseTitle")

    if "settings" in d and isinstance(d["settings"], dict):
        d["settings"] = validate_settings(d["settings"])

    return d
