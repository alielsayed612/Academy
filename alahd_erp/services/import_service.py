"""Safe Excel & CSV Import Service — Yawm Jadid Academy Suite (v1.2.0).

Implements Phase 1 & Phase 6 requirements:
- Supports 6 datasets: `teachers`, `packages`, `students`, `receipts`, `transactions`, `salaries`.
- Strict validation of `dataset`, `mode`, and explicitly selected `sheet_name` (no silent fallbacks).
- Generates downloadable Arabic `.xlsx` template workbooks with example rows and instructions.
- Safe read-only parsing of `.xlsx` and `.csv` in memory (no macros/formulas executed; formula cells
  starting with `=` are rejected; size limit enforced).
- Auto-detects header row (handling leading title/merged rows and 2-row exported headers), maps Arabic &
  English header variants, reuses saved mappings safely, and validates custom user column mappings.
- Normalizes Arabic-Indic digits, spaces, Arabic letters (for comparison), Egyptian phone numbers,
  multi-format dates (ISO, DD/MM/YYYY, Excel serial numbers, Arabic month names), and Decimal money.
- Keeps `paymentDate` and `actualStartDate` strictly independent (never copies one into the other).
- Safe student matching with ambiguity & identity-conflict detection (never overwrites the wrong student).
- Granular `autoCreateTeachers` / `autoCreatePackages` / `autoCreateRefs` with Arabic spelling normalization
  and explicit warnings when new entities will be created.
- Dry-run preview (`new` / `will_update` / `duplicate` / `invalid`) + downloadable `.xlsx` error report.
- Transactional commit with file-hash verification, verified safety snapshot, idempotency, batch tracking, and undo support.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import re
import secrets
import zipfile
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..domain.attendance_engine import MONTH_RE
from ..domain.finance_engine import parse_pct_int, round_half_up
from ..domain.models import (
    normalize_arabic_for_comparison,
    normalize_digits_to_ascii,
    normalize_phone_digits,
)

MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10 MB

# Excel (.xlsx) structural limits. The byte cap above does not protect against a tiny file that declares a huge sheet
# (a single far-away cell such as XFD1048576, a giant merged range, or a zip-bomb). All limits are module constants so
# tests/ops can tune them.
MAX_XLSX_UNCOMPRESSED_BYTES = 60 * 1024 * 1024   # total uncompressed size of all zip entries
MAX_XLSX_ENTRIES = 500                            # zip entries
MAX_SHEET_ROWS = 20_000                           # rows that actually carry data
MAX_SHEET_COLS = 200                              # columns that actually carry data
MAX_SHEET_AREA = 1_000_000                        # rows * cols iterated
MAX_MERGED_AREA = 100_000                         # cells in one merged range / in all merged ranges of a sheet

SUPPORTED_DATASETS = ("teachers", "packages", "students", "receipts", "transactions", "salaries")
SUPPORTED_MODES = ("add_only", "skip_duplicates", "update_existing")

ARABIC_MONTH_NAMES = {
    "يناير": 1, "كانون الثاني": 1, "jan": 1, "january": 1,
    "فبراير": 2, "شباط": 2, "feb": 2, "february": 2,
    "مارس": 3, "آذار": 3, "اذار": 3, "mar": 3, "march": 3,
    "أبريل": 4, "ابريل": 4, "نيسان": 4, "apr": 4, "april": 4,
    "مايو": 5, "أيار": 5, "ايار": 5, "may": 5,
    "يونيو": 6, "حزيران": 6, "jun": 6, "june": 6,
    "يوليو": 7, "تموز": 7, "jul": 7, "july": 7,
    "أغسطس": 8, "اغسطس": 8, "آب": 8, "اب": 8, "aug": 8, "august": 8,
    "سبتمبر": 9, "أيلول": 9, "ايلول": 9, "sep": 9, "september": 9,
    "أكتوبر": 10, "اكتوبر": 10, "تشرين الأول": 10, "oct": 10, "october": 10,
    "نوفمبر": 11, "تشرين الثاني": 11, "nov": 11, "november": 11,
    "ديسمبر": 12, "كانون الأول": 12, "dec": 12, "december": 12,
}

# Canonical field specifications per dataset (for UI mapping controls and validation)
DATASET_FIELD_SPECS: dict[str, list[dict[str, Any]]] = {
    "teachers": [
        {"field": "name", "label": "اسم المعلمة", "required": True},
        {"field": "phone", "label": "رقم الهاتف", "required": False},
        {"field": "specialty", "label": "التخصص", "required": False},
        {"field": "defaultPct", "label": "نسبة المعلمة", "required": False},
        {"field": "notes", "label": "ملاحظات", "required": False},
    ],
    "packages": [
        {"field": "name", "label": "نوع الاشتراك / الباقة", "required": True},
        {"field": "price", "label": "قيمة الاشتراك / السعر", "required": True},
    ],
    "students": [
        {"field": "code", "label": "كود الطالبة", "required": False},
        {"field": "name", "label": "اسم الطالبة", "required": True},
        {"field": "phone", "label": "رقم التواصل", "required": False},
        {"field": "teacher", "label": "اسم المعلمة", "required": False},
        {"field": "type", "label": "نوع الاشتراك", "required": False},
        {"field": "pct", "label": "نسبة المعلمة", "required": False},
        {"field": "schedule", "label": "المواعيد", "required": False},
        {"field": "month", "label": "الشهر", "required": False},
        {"field": "value", "label": "قيمة الاشتراك", "required": False},
        {"field": "discount", "label": "الخصم", "required": False},
        {"field": "customWage", "label": "أجر المعلمة (مخصص)", "required": False},
        {"field": "paidAmount", "label": "المبلغ المسدد", "required": False},
        {"field": "paymentDate", "label": "تاريخ الدفع", "required": False},
        {"field": "actualStartDate", "label": "تاريخ البداية الفعلي", "required": False},
        {"field": "endDate", "label": "تاريخ النهاية", "required": False},
        {"field": "method", "label": "طريقة الدفع", "required": False},
        {"field": "adminStatus", "label": "حالة الطالبة", "required": False},
        {"field": "note", "label": "ملاحظات", "required": False},
    ],
    "receipts": [
        {"field": "receiptNo", "label": "رقم الإيصال", "required": False},
        {"field": "studentCode", "label": "كود الطالبة", "required": False},
        {"field": "studentName", "label": "اسم الطالبة", "required": False},
        {"field": "phone", "label": "رقم الهاتف", "required": False},
        {"field": "month", "label": "الشهر", "required": False},
        {"field": "amount", "label": "المبلغ المسدد", "required": True},
        {"field": "paymentDate", "label": "تاريخ الدفع", "required": True},
        {"field": "method", "label": "طريقة الدفع", "required": False},
        {"field": "note", "label": "ملاحظة", "required": False},
    ],
    "transactions": [
        {"field": "type", "label": "نوع الحركة (إيراد/مصروف)", "required": False},
        {"field": "category", "label": "التصنيف / البند", "required": True},
        {"field": "amount", "label": "المبلغ", "required": True},
        {"field": "date", "label": "التاريخ", "required": True},
        {"field": "month", "label": "الشهر", "required": False},
        {"field": "method", "label": "وسيلة الدفع", "required": False},
        {"field": "note", "label": "البيان / ملاحظة", "required": False},
    ],
    "salaries": [
        {"field": "teacher", "label": "اسم المعلمة", "required": True},
        {"field": "month", "label": "الشهر", "required": False},
        {"field": "amount", "label": "المبلغ المصروف", "required": True},
        {"field": "date", "label": "تاريخ الصرف", "required": False},
        {"field": "note", "label": "ملاحظة", "required": False},
    ],
}

# Canonical field aliases per dataset
HEADER_SYNONYMS: dict[str, dict[str, tuple[str, ...]]] = {
    "teachers": {
        "name": ("اسم المعلمة", "المعلمة", "الاسم", "teacher", "teacher_name", "name"),
        "phone": ("رقم الهاتف", "الهاتف", "رقم التواصل", "جوال", "موبايل", "phone", "mobile"),
        "specialty": ("التخصص", "المسار", "specialty", "track"),
        "defaultPct": ("النسبة", "نسبة المعلمة", "النسبة الافتراضية", "نسبة الأجر", "pct", "percentage", "default_pct"),
        "notes": ("ملاحظات", "ملاحظة", "notes", "note"),
    },
    "packages": {
        "name": ("اسم الباقة", "نوع الاشتراك", "الباقة", "الاشتراك", "package", "type", "name"),
        "price": ("السعر", "قيمة الاشتراك", "الرسوم", "المبلغ", "price", "value", "amount", "fee"),
    },
    "students": {
        "code": ("كود الطالبة", "الكود", "الرقم الكودي", "معرف الطالبة", "code", "student_code", "id"),
        "name": ("اسم الطالبة", "اسم الطالب", "الطالبة", "الطالب", "الاسم", "name", "student_name", "student"),
        "phone": ("رقم التواصل", "رقم الهاتف", "الهاتف", "الموبايل", "الجوال", "واتساب", "phone", "mobile"),
        "teacher": ("اسم المعلمة", "المعلمة", "معلمة الحلقة", "teacher", "teacher_name"),
        "type": ("نوع الاشتراك", "الباقة", "اسم الباقة", "الاشتراك", "type", "package", "subscription_type"),
        "pct": ("نسبة المعلمة", "النسبة", "pct", "teacher_pct"),
        "schedule": ("المواعيد", "الجدول", "أيام الحضور", "schedule", "days"),
        "month": ("الشهر", "شهر الاشتراك", "تاريخ التسجيل", "شهر التسجيل", "month", "month_key"),
        "value": ("قيمة الاشتراك", "الاشتراك الشهري", "الرسوم", "صافي الاشتراك", "value", "subscription_value", "fee"),
        "discount": ("الخصم", "قيمة الخصم", "discount"),
        "customWage": ("أجر المعلمة", "اجر المعلمة", "مستحق المعلمة", "أجر مخصص للمعلمة", "مرتب المعلمة", "custom_wage", "customwage", "wage", "teacher_wage"),
        "paidAmount": ("المبلغ المسدد", "المدفوع", "المحصّل", "المسدد", "paid", "paid_amount", "collected"),
        "paymentDate": ("تاريخ الدفع", "تاريخ السداد", "payment_date", "paymentdate", "pay_date"),
        "actualStartDate": ("تاريخ البداية الفعلي", "تاريخ البداية", "تاريخ بدء الدراسة", "actual_start_date", "actualstartdate", "start_date"),
        "endDate": ("تاريخ النهاية", "تاريخ انتهاء الدورة", "end_date", "enddate"),
        "method": ("طريقة الدفع", "وسيلة الدفع", "method", "payment_method"),
        "adminStatus": ("حالة الطالبة", "الحالة", "حالة الاشتراك", "status", "student_status", "admin_status"),
        "note": ("ملاحظات", "ملاحظة", "بيان", "note", "notes"),
    },
    "receipts": {
        "receiptNo": ("رقم الإيصال", "الإيصال", "كود الإيصال", "receipt_no", "receipt", "receiptno"),
        "studentCode": ("كود الطالبة", "الكود", "student_code", "code"),
        "studentName": ("اسم الطالبة", "الطالبة", "الاسم", "student_name", "student", "name"),
        "phone": ("رقم الهاتف", "رقم التواصل", "الهاتف", "phone"),
        "month": ("الشهر", "شهر الاشتراك", "month", "month_key"),
        "amount": ("المبلغ", "المبلغ المسدد", "المدفوع", "القيمة", "amount", "paid"),
        "paymentDate": ("تاريخ الدفع", "تاريخ السداد", "التاريخ", "payment_date", "pay_date", "date"),
        "method": ("طريقة الدفع", "وسيلة الدفع", "method"),
        "note": ("ملاحظة", "ملاحظات", "بيان", "note", "notes"),
    },
    "transactions": {
        "type": ("النوع", "نوع الحركة", "إيراد/مصروف", "type", "tx_type"),
        "category": ("التصنيف", "البند", "الفئة", "category", "item"),
        "amount": ("المبلغ", "القيمة", "amount", "value"),
        "date": ("التاريخ", "تاريخ الحركة", "date", "tx_date"),
        "month": ("الشهر", "month", "month_key"),
        "method": ("وسيلة الدفع", "طريقة الدفع", "method"),
        "note": ("البيان", "ملاحظة", "ملاحظات", "note", "notes"),
    },
    "salaries": {
        "teacher": ("اسم المعلمة", "المعلمة", "teacher", "teacher_name", "name"),
        "month": ("الشهر", "شهر الراتب", "month", "month_key"),
        "amount": ("المبلغ", "المبلغ المصروف", "المصروف", "الراتب", "amount", "salary", "paid"),
        "date": ("تاريخ الصرف", "التاريخ", "تاريخ الدفع", "date", "pay_date"),
        "note": ("ملاحظة", "ملاحظات", "بيان", "note", "notes"),
    },
}


def validate_dataset(dataset: Any) -> str:
    """Validates and normalizes the requested import dataset name. Raises ValueError if unsupported."""
    ds = str(dataset or "").strip().lower()
    if ds not in SUPPORTED_DATASETS:
        raise ValueError(
            f"نوع البيانات غير مدعوم للاستيراد: «{dataset}» (الأنواع المدعومة: {', '.join(SUPPORTED_DATASETS)})"
        )
    return ds


def validate_import_mode(mode: Any) -> str:
    """Validates and normalizes the requested import mode. Raises ValueError if invalid."""
    m = str(mode or "").strip().lower()
    if m not in SUPPORTED_MODES:
        raise ValueError(
            f"وضع الاستيراد غير صالح: «{mode}» (الأوضاع المدعومة: {', '.join(SUPPORTED_MODES)})"
        )
    return m


def _field_label(dataset: str, field: str) -> str:
    for spec in DATASET_FIELD_SPECS.get(dataset, ()):
        if spec["field"] == field:
            return str(spec["label"])
    return field


def _coerce_subtypes_dict(raw_subtypes: Any) -> dict[str, int]:
    """Normalizes `state['subTypes']` into `{package_name: integer_price}` whether stored as dict or list."""
    out: dict[str, int] = {}
    if isinstance(raw_subtypes, dict):
        for k, v in raw_subtypes.items():
            name = str(k or "").strip()
            if not name:
                continue
            try:
                out[name] = max(0, round_half_up(v or 0))
            except Exception:
                out[name] = 0
    elif isinstance(raw_subtypes, list):
        for item in raw_subtypes:
            if isinstance(item, str):
                if "|" in item:
                    parts = [p.strip() for p in item.split("|", 1)]
                    if parts[0]:
                        try:
                            out[parts[0]] = max(0, round_half_up(parts[1]))
                        except Exception:
                            out[parts[0]] = 0
                elif item.strip():
                    out[item.strip()] = 0
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("type") or "").strip()
                if name:
                    try:
                        out[name] = max(0, round_half_up(item.get("price") or item.get("value") or 0))
                    except Exception:
                        out[name] = 0
    return out


def _normalize_arabic_entity_key(text: str) -> str:
    """Normalizes Arabic names including compound spacing (`عبد الرحمن` -> `عبدالرحمن`) for comparison."""
    s = normalize_arabic_for_comparison(text)
    s = re.sub(r"\b(عبد|ابو|ام)\s+ال", r"\1ال", s)
    s = re.sub(r"\b(عبد|ابو|ام)\s+", r"\1", s)
    return s


def _strip_teacher_honorific(norm_text: str) -> str:
    """Strips optional honorific prefix (`أ/`, `أ.`, `أستاذة`, `المعلمة`) when followed by a real name (>= 2 chars)."""
    s = norm_text.strip()
    m = re.match(r"^(?:[اأ]\s*[/.\-]\s*|ال?معلم[هة]\s+|ال?[اأ]ستاذ[هة]\s+|مس\s+|شيخ[هة]?\s+)(.+)$", s)
    if m:
        rest = m.group(1).strip()
        if len(rest) >= 2:
            return rest
    return s


def _match_or_warn_reference_name(
    raw_name: str,
    existing_norm_map: dict[str, str],
    seen_new_norm_map: dict[str, str],
    is_teacher: bool = True,
) -> tuple[str, bool, str | None]:
    """Resolves `raw_name` against existing or already-queued entities.

    Returns `(canonical_name, exists_in_db, warning_msg)`.
    """
    clean = re.sub(r"\s+", " ", str(raw_name or "").strip())
    if not clean:
        return "", False, None

    norm = normalize_arabic_for_comparison(clean)
    compound_norm = _normalize_arabic_entity_key(clean)
    stripped_norm = _strip_teacher_honorific(compound_norm) if is_teacher else compound_norm

    # 1. Exact normalized match in existing DB
    if norm in existing_norm_map:
        canon = existing_norm_map[norm]
        warn = None
        if canon != clean:
            label = "المعلمة" if is_teacher else "الباقة"
            warn = f"تم توحيد اسم {label} «{clean}» مع المسجل «{canon}» (تطابق إملائي)."
        return canon, True, warn

    # 2. Compound/honorific-normalized match in existing DB
    for ex_norm, ex_canon in existing_norm_map.items():
        ex_comp = _normalize_arabic_entity_key(ex_canon)
        ex_strip = _strip_teacher_honorific(ex_comp) if is_teacher else ex_comp
        if compound_norm == ex_comp or (len(stripped_norm) >= 3 and stripped_norm == ex_strip):
            label = "المعلمة" if is_teacher else "الباقة"
            return (
                ex_canon,
                True,
                f"تم ربط {label} «{clean}» بـ «{ex_canon}» المسجلة مسبقًا لمنع تكرار الأسماء المتشابهة.",
            )

    # 3. Match against new entity already queued earlier in the same import file
    for new_key in (norm, compound_norm, stripped_norm):
        if new_key and new_key in seen_new_norm_map:
            canon = seen_new_norm_map[new_key]
            warn = None
            if canon != clean:
                label = "المعلمة" if is_teacher else "الباقة"
                warn = f"تم توحيد اسم {label} الجديد «{clean}» مع «{canon}» الوارد في صف سابق بنفس الملف."
            return canon, False, warn

    # 4. Check for close fuzzy similarity with existing DB entities (to warn about possible typos)
    similar_existing = None
    for ex_norm, ex_canon in existing_norm_map.items():
        ex_strip = _strip_teacher_honorific(_normalize_arabic_entity_key(ex_canon)) if is_teacher else _normalize_arabic_entity_key(ex_canon)
        if len(stripped_norm) >= 4 and len(ex_strip) >= 4:
            # Do not flag numbered/lettered variants like "معلمة أ" vs "معلمة ب" or "8 حصص" vs "12 حصة"
            nums_a = re.findall(r"\d+", stripped_norm)
            nums_b = re.findall(r"\d+", ex_strip)
            if nums_a != nums_b:
                continue
            tokens_a = stripped_norm.split()
            tokens_b = ex_strip.split()
            if len(tokens_a) >= 2 and len(tokens_b) >= 2 and tokens_a[:-1] == tokens_b[:-1] and len(tokens_a[-1]) == 1 and len(tokens_b[-1]) == 1:
                continue
            ratio = SequenceMatcher(None, stripped_norm, ex_strip).ratio()
            if ratio >= 0.84:
                similar_existing = ex_canon
                break

    seen_new_norm_map[norm] = clean
    seen_new_norm_map[compound_norm] = clean
    if len(stripped_norm) >= 3:
        seen_new_norm_map[stripped_norm] = clean

    label = "معلمة جديدة" if is_teacher else "باقة اشتراك جديدة"
    if similar_existing:
        return (
            clean,
            False,
            f"تنبيه إنشاء تلقائي: سيتم إنشاء {label} باسم «{clean}» (توجد حاليًا «{similar_existing}» باسم مشابه — تأكد من عدم وجود خطأ إملائي).",
        )
    return clean, False, f"تنبيه إنشاء تلقائي: سيتم إنشاء {label} باسم «{clean}»."


def _clean_header(h: Any) -> str:
    s = normalize_digits_to_ascii(str(h or "")).strip().lower()
    s = re.sub(r"[\s_\-\(\)\[\]/\\:]+", " ", s).strip()
    return normalize_arabic_for_comparison(s)


def auto_map_headers(dataset: str, headers: list[str]) -> dict[str, str]:
    """Maps canonical field names to column headers found in the sheet."""
    syns = HEADER_SYNONYMS.get(dataset) or {}
    cleaned_cols = [(col, _clean_header(col)) for col in headers if str(col or "").strip()]
    mapping: dict[str, str] = {}
    used_cols: set[str] = set()

    # Exact match pass
    for field, aliases in syns.items():
        norm_aliases = {_clean_header(a) for a in aliases} | {_clean_header(field)}
        for orig_col, clean_col in cleaned_cols:
            if orig_col in used_cols:
                continue
            if clean_col in norm_aliases:
                mapping[field] = orig_col
                used_cols.add(orig_col)
                break

    # Substring match pass for remaining unmapped fields
    for field, aliases in syns.items():
        if field in mapping:
            continue
        norm_alias_list = [_clean_header(a) for a in aliases if len(_clean_header(a)) >= 3]
        for orig_col, clean_col in cleaned_cols:
            if orig_col in used_cols:
                continue
            if any(na in clean_col for na in norm_alias_list):
                mapping[field] = orig_col
                used_cols.add(orig_col)
                break

    return mapping


def resolve_and_validate_column_mapping(
    dataset: str,
    headers: list[str],
    saved_mapping: dict[str, str] | None = None,
    user_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Resolves `(effective_mapping, auto_mapping, mapping_errors)` for `dataset` and `headers`.

    Supports:
    - Automatic synonym detection (`auto_map_headers`).
    - Safe reuse of `saved_mapping` for unmapped fields whose target header exists in `headers` and is unused.
    - Explicit `user_overrides` (`{field: header_name}` or `{field: "__none__"}` to unmap, or `{col_idx_or_header: field}`).
    - Validation of unknown fields, non-existent headers, and duplicate column assignments.
    """
    valid_fields = set((HEADER_SYNONYMS.get(dataset) or {}).keys())
    auto_mapped = auto_map_headers(dataset, headers)
    effective = dict(auto_mapped)
    mapping_errors: list[str] = []

    # Reuse saved mapping safely for fields not already auto-mapped
    if isinstance(saved_mapping, dict) and headers:
        used_cols = set(effective.values())
        for f_key, col_name in saved_mapping.items():
            if f_key in valid_fields and f_key not in effective and isinstance(col_name, str) and col_name in headers and col_name not in used_cols:
                effective[f_key] = col_name
                used_cols.add(col_name)

    if user_overrides and isinstance(user_overrides, dict):
        explicit_field_targets: dict[str, str] = {}
        for raw_k, raw_v in user_overrides.items():
            k = str(raw_k or "").strip()
            v = "" if raw_v is None else str(raw_v).strip()
            if not k:
                continue

            # Case 1: canonical field -> column header (or __none__ / empty to unmap)
            if k in valid_fields:
                if v in ("", "__none__", "__unmapped__"):
                    effective.pop(k, None)
                    continue
                if v not in headers:
                    mapping_errors.append(
                        f"العمود المحدد «{v}» للحقل «{_field_label(dataset, k)}» غير موجود في أعمدة الملف."
                    )
                    continue
                explicit_field_targets[k] = v
            # Case 2: column index or header name -> canonical field
            elif (k in headers) or (k.isdigit() and 0 <= int(k) < len(headers)):
                col_hdr = k if k in headers else headers[int(k)]
                if v in ("", "__none__", "__unmapped__", "غير مرتبط"):
                    for f_existing, c_existing in list(effective.items()):
                        if c_existing == col_hdr:
                            effective.pop(f_existing, None)
                    continue
                if v not in valid_fields:
                    mapping_errors.append(
                        f"الحقل «{v}» المحدد للعمود «{col_hdr}» غير صالح لنوع البيانات «{dataset}»."
                    )
                    continue
                explicit_field_targets[v] = col_hdr
            else:
                mapping_errors.append(
                    f"حقل المطابقة «{k}» غير معروف لنوع البيانات «{dataset}»."
                )

        # Check if user explicitly assigned the same column to multiple fields
        by_col: dict[str, list[str]] = {}
        for f_k, col_v in explicit_field_targets.items():
            by_col.setdefault(col_v, []).append(f_k)
        for col_v, f_list in by_col.items():
            if len(f_list) > 1:
                labels = "، ".join(f"«{_field_label(dataset, f)}»" for f in f_list)
                mapping_errors.append(
                    f"تعارض في مطابقة الأعمدة: العمود «{col_v}» مربوط بأكثر من حقل في نفس الوقت ({labels})."
                )

        # Apply valid explicit assignments, displacing earlier auto-mapped fields that used the same column
        for f_k, col_v in explicit_field_targets.items():
            for other_f, other_col in list(effective.items()):
                if other_f != f_k and other_col == col_v and other_f not in explicit_field_targets:
                    effective.pop(other_f, None)
            effective[f_k] = col_v

    return effective, auto_mapped, mapping_errors


def parse_flexible_date(val: Any) -> str | None:
    """Parses various date formats into ISO `YYYY-MM-DD`.
    Returns `""` if empty, or `None` if invalid.
    """
    if val is None or val == "" or isinstance(val, bool):
        return ""
    if isinstance(val, datetime.datetime):
        return val.date().isoformat()
    if isinstance(val, datetime.date):
        return val.isoformat()
    if isinstance(val, (int, float)):
        # Excel serial date (1900 date system: serial 1 = 1899-12-31, with Lotus 1-2-3 leap year bug)
        serial = int(float(val))
        if 20000 <= serial <= 80000:
            base = datetime.date(1899, 12, 30)
            dt = base + datetime.timedelta(days=serial)
            return dt.isoformat()
        return None

    s = normalize_digits_to_ascii(str(val)).strip()
    if not s or s in ("—", "-", "غير مسدد", "لا يوجد", "none", "null"):
        return ""

    # Strip time part if present (e.g. "2026-11-05 00:00:00")
    if " " in s and re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d", s):
        s = s.split(" ")[0]
    if "T" in s and re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        s = s.split("T")[0]

    # YYYY-MM-DD or YYYY/M/D
    m_iso = re.match(r"^(\d{4})[-/\.](\d{1,2})[-/\.](\d{1,2})$", s)
    if m_iso:
        y, mo, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None

    # DD/MM/YYYY or DD-MM-YYYY
    m_dmy = re.match(r"^(\d{1,2})[-/\.](\d{1,2})[-/\.](\d{4})$", s)
    if m_dmy:
        d, mo, y = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        if mo > 12 and d <= 12:
            d, mo = mo, d
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            return None

    # Arabic month name with day & year e.g. "5 نوفمبر 2026" or "نوفمبر 2026"
    s_clean = re.sub(r"[,،\-]+", " ", s).strip()
    for m_name, m_num in ARABIC_MONTH_NAMES.items():
        if m_name in s_clean.lower():
            nums = [int(x) for x in re.findall(r"\d+", s_clean)]
            year = next((n for n in nums if 2000 <= n <= 2100), None)
            day = next((n for n in nums if 1 <= n <= 31), 1)
            if year:
                try:
                    return datetime.date(year, m_num, day).isoformat()
                except ValueError:
                    return None
    return None


def parse_flexible_month(val: Any, default_month: str = "") -> str | None:
    """Parses a month value (`YYYY-MM`, `MM/YYYY`, `نوفمبر 2026`, or full date) into `YYYY-MM`."""
    if val is None or val == "" or isinstance(val, bool):
        return default_month or datetime.date.today().strftime("%Y-%m")
    if isinstance(val, (datetime.date, datetime.datetime)):
        return f"{val.year:04d}-{val.month:02d}"
    s = normalize_digits_to_ascii(str(val)).strip()
    if not s:
        return default_month or datetime.date.today().strftime("%Y-%m")
    if MONTH_RE.match(s):
        return s
    m_ym = re.match(r"^(\d{4})[-/\.](\d{1,2})$", s)
    if m_ym:
        y, mo = int(m_ym.group(1)), int(m_ym.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    m_my = re.match(r"^(\d{1,2})[-/\.](\d{4})$", s)
    if m_my:
        mo, y = int(m_my.group(1)), int(m_my.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}"
    m_sh = re.match(r"^شهر\s*(\d{1,2})(?:\s*[-/]\s*(\d{4}))?$", s)
    if m_sh:
        mo = int(m_sh.group(1))
        y_str = m_sh.group(2)
        if 1 <= mo <= 12:
            if y_str and 2000 <= int(y_str) <= 2100:
                y = int(y_str)
            elif default_month and re.match(r"^\d{4}", str(default_month)):
                y = int(str(default_month)[:4])
            else:
                y = datetime.date.today().year
            return f"{y:04d}-{mo:02d}"
    for m_name, m_num in ARABIC_MONTH_NAMES.items():
        if m_name in s.lower():
            nums = [int(x) for x in re.findall(r"\d+", s)]
            year = next((n for n in nums if 2000 <= n <= 2100), datetime.date.today().year)
            return f"{year:04d}-{m_num:02d}"
    dt = parse_flexible_date(val)
    if dt and len(dt) >= 7:
        return dt[:7]
    return None


def parse_flexible_money(val: Any, allow_empty: bool = True) -> int | None:
    """Parses a currency/numeric string into integer EGP via Decimal + ROUND_HALF_UP."""
    if val is None or val == "" or isinstance(val, bool):
        return 0 if allow_empty else None
    if isinstance(val, (int, float, Decimal)):
        try:
            res = round_half_up(val)
            return res if res >= 0 else None
        except Exception:
            return None
    s = normalize_digits_to_ascii(str(val)).strip()
    if not s or s in ("—", "-", "مجاني", "إعفاء"):
        return 0 if allow_empty else None
    # Remove currency labels and thousands separators
    s = re.sub(r"(ج\.?م\.?|جنيه|egp|le|,|٬|\s)+", "", s, flags=re.IGNORECASE)
    try:
        res = round_half_up(Decimal(s))
        return res if res >= 0 else None
    except Exception:
        return None


def generate_import_template_xlsx(dataset: str) -> bytes:
    """Generates a downloadable Arabic `.xlsx` template workbook with example rows and instructions."""
    ds = validate_dataset(dataset)
    wb = Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True

    head_fill = PatternFill("solid", fgColor="134E4A")
    head_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    cell_font = Font(name="Calibri", size=10.5)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    today_iso = datetime.date.today().isoformat()
    cur_mk = today_iso[:7]

    templates: dict[str, tuple[str, list[str], list[list[Any]]]] = {
        "teachers": (
            "قالب المعلمات",
            ["اسم المعلمة", "رقم الهاتف", "التخصص", "نسبة المعلمة", "ملاحظات"],
            [
                ["معلمة تجريبية أ", "01000000011", "حفظ وتجويد", 70, "حلقة الفترة المسائية"],
                ["معلمة تجريبية ب", "01000000012", "تأسيس وتلقين", 75, "حلقة الفترة الصباحية"],
            ],
        ),
        "packages": (
            "قالب باقات الاشتراك",
            ["نوع الاشتراك", "قيمة الاشتراك"],
            [
                ["٨ حصص شهريًا (٤٠ دقيقة)", 250],
                ["١٢ حصة شهريًا (٤٠ دقيقة)", 350],
                ["حلقة جماعية (٨ حصص)", 150],
            ],
        ),
        "students": (
            "قالب الطالبات والاشتراكات",
            [
                "كود الطالبة",
                "اسم الطالبة",
                "رقم التواصل",
                "اسم المعلمة",
                "نوع الاشتراك",
                "نسبة المعلمة",
                "المواعيد",
                "الشهر",
                "قيمة الاشتراك",
                "الخصم",
                "المبلغ المسدد",
                "تاريخ الدفع",
                "تاريخ البداية الفعلي",
                "تاريخ النهاية",
                "طريقة الدفع",
                "ملاحظات",
            ],
            [
                [
                    "STU-001",
                    "طالبة تجريبية ١",
                    "01000000001",
                    "معلمة تجريبية أ",
                    "٨ حصص شهريًا (٤٠ دقيقة)",
                    70,
                    "الأحد والأربعاء 4 عصراً",
                    cur_mk,
                    250,
                    0,
                    250,
                    today_iso,
                    today_iso,
                    "",
                    "فودافون كاش",
                    "تاريخ الدفع مستقل تمامًا عن تاريخ البداية الفعلي",
                ],
                [
                    "STU-002",
                    "طالبة تجريبية ٢",
                    "01000000002",
                    "معلمة تجريبية ب",
                    "١٢ حصة شهريًا (٤٠ دقيقة)",
                    70,
                    "الإثنين والخميس 6 مساءً",
                    cur_mk,
                    350,
                    50,
                    150,
                    today_iso,
                    "",
                    "",
                    "إنستا باي",
                    "دفع مسبق جزئي ولم تبدأ الدراسة بعد",
                ],
            ],
        ),
        "receipts": (
            "قالب إيصالات السداد",
            ["رقم الإيصال", "كود الطالبة", "اسم الطالبة", "رقم الهاتف", "الشهر", "المبلغ المسدد", "تاريخ الدفع", "طريقة الدفع", "ملاحظة"],
            [
                ["RCP-1001", "STU-001", "طالبة تجريبية ١", "01000000001", cur_mk, 250, today_iso, "فودافون كاش", "سداد كامل"],
                ["RCP-1002", "STU-002", "طالبة تجريبية ٢", "01000000002", cur_mk, 150, today_iso, "إنستا باي", "دفعة أولى"],
            ],
        ),
        "transactions": (
            "قالب حركات الخزينة",
            ["النوع", "التصنيف", "المبلغ", "التاريخ", "الشهر", "وسيلة الدفع", "البيان"],
            [
                ["إيراد", "تبرعات ودعم أنشطة", 500, today_iso, cur_mk, "فودافون كاش", "مساهمة جوائز المسابقة"],
                ["مصروف", "إعلانات وتسويق", 200, today_iso, cur_mk, "إنستا باي", "حملة بداية الشهر"],
            ],
        ),
        "salaries": (
            "قالب صرف الرواتب",
            ["اسم المعلمة", "الشهر", "المبلغ", "تاريخ الصرف", "ملاحظة"],
            [
                ["معلمة تجريبية أ", cur_mk, 300, today_iso, "دفعة تحت الحساب"],
            ],
        ),
    }

    sheet_title, headers, example_rows = templates[ds]
    ws.title = sheet_title[:31]

    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=1, column=ci, value=h)
        c.fill = head_fill
        c.font = head_font
        c.alignment = center
        ws.column_dimensions[get_column_letter(ci)].width = max(16, len(h) + 5)
    ws.row_dimensions[1].height = 28

    for ri, row_vals in enumerate(example_rows, start=2):
        for ci, v in enumerate(row_vals, start=1):
            c = ws.cell(row=ri, column=ci, value=v)
            c.font = cell_font
            c.alignment = center
        ws.row_dimensions[ri].height = 22

    # Instructions sheet
    ws_inst = wb.create_sheet("تعليمات الاستيراد")
    ws_inst.sheet_view.rightToLeft = True
    ws_inst.column_dimensions["A"].width = 28
    ws_inst.column_dimensions["B"].width = 75
    notes = [
        ("القواعد العامة للاستيراد", "يدعم النظام ملفات Excel (.xlsx) وملفات (.csv) بدون أي اتصال بالإنترنت."),
        ("استقلالية التواريخ الصارمة", "عمود «تاريخ الدفع» (مالي) مستقل تمامًا عن عمود «تاريخ البداية الفعلي» (دراسي). لا يقوم النظام بنسخ أحدهما إلى الآخر أبداً."),
        ("الأرقام والهواتف", "يتم تحويل الأرقام العربية (٠١٢٣٤٥٦٧٨٩) تلقائيًا، وتنسيق أرقام الهواتف المصرية تلقائيًا."),
        ("النسب المئوية", "تُكتب النسبة كرقم صحيح من 0 إلى 100 (مثال: 70 تعني 70%، و1 تعني 1%)."),
        ("الخصومات والإيصالات", "صافي الاشتراك = قيمة الاشتراك ناقص الخصم. عند إدخال «المبلغ المسدد» و«تاريخ الدفع» يتم إنشاء إيصال رسمي تلقائيًا."),
        ("الأمان", "لا يُسمح بمعادلات Excel التي تبدأ بـ (=) أو وحدات الماكرو؛ أدخل القيم النهائية مباشرة."),
    ]
    for ri, (k, v) in enumerate(notes, start=1):
        c1 = ws_inst.cell(row=ri, column=1, value=k)
        c2 = ws_inst.cell(row=ri, column=2, value=v)
        c1.font = Font(name="Calibri", size=11, bold=True, color="134E4A")
        c2.font = cell_font

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def generate_import_error_report_xlsx(errors: list[dict[str, Any]], filename: str = "") -> bytes:
    """Generates a downloadable `.xlsx` report detailing row numbers, columns, and error reasons in Arabic."""
    wb = Workbook()
    ws = wb.active
    ws.title = "تقرير أخطاء الاستيراد"
    ws.sheet_view.rightToLeft = True

    head_fill = PatternFill("solid", fgColor="991B1B")
    head_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    cell_font = Font(name="Calibri", size=10.5)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    right = Alignment(horizontal="right", vertical="center", wrap_text=True)

    headers = ["رقم الصف في الملف", "العمود / الحقل", "سبب الخطأ أو التنبيه", "اسم الملف"]
    widths = [18, 24, 55, 28]
    for ci, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=1, column=ci, value=h)
        c.fill = head_fill
        c.font = head_font
        c.alignment = center
        ws.column_dimensions[get_column_letter(ci)].width = w
    ws.row_dimensions[1].height = 26

    for ri, err in enumerate(errors or [], start=2):
        ws.cell(row=ri, column=1, value=err.get("row", "")).alignment = center
        ws.cell(row=ri, column=2, value=err.get("column", "")).alignment = center
        c3 = ws.cell(row=ri, column=3, value=err.get("reason", ""))
        c3.alignment = right
        c3.font = cell_font
        ws.cell(row=ri, column=4, value=filename or "").alignment = center
        ws.row_dimensions[ri].height = 22

    # Reasons/columns/filename can echo uploaded content (e.g. a file named `=evil.xlsx`); openpyxl would store any
    # text starting with `=` as a live formula, so force them to plain text (same rule as excel_service export).
    for row in ws.iter_rows():
        for c in row:
            if c.data_type == "f":
                c.data_type = "s"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_MERGE_REF_RE = re.compile(rb'<(?:[A-Za-z0-9_]+:)?mergeCell\b[^>]*?\bref="([A-Za-z]+)(\d+)(?::([A-Za-z]+)(\d+))?"')


def _col_to_idx(letters: bytes) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ch - 64)
    return n


def _guard_xlsx_container(file_bytes: bytes) -> None:
    """Rejects zip-bombs and giant merged ranges BEFORE openpyxl parses (and expands) the workbook."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except Exception as exc:
        raise ValueError("ملف Excel غير صالح أو تالف؛ تأكد من سلامة الملف وصيغته (.xlsx)") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_XLSX_ENTRIES:
            raise ValueError("ملف Excel يحتوي على عدد غير معتاد من المكوّنات الداخلية؛ تم رفضه لحمايتك")
        if sum(i.file_size for i in infos) > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise ValueError("حجم محتوى ملف Excel بعد فك الضغط كبير جدًا؛ تم رفضه (قد يكون ملفًا ضارًا أو تالفًا)")
        for info in infos:
            name = info.filename.lower()
            if not (name.startswith("xl/worksheets/") and name.endswith(".xml")):
                continue
            total_merged = 0
            with zf.open(info) as fh:
                data = fh.read(MAX_XLSX_UNCOMPRESSED_BYTES + 1)
            if len(data) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("حجم ورقة Excel بعد فك الضغط كبير جدًا؛ تم رفضه")
            for m in _MERGE_REF_RE.finditer(data):
                c1, r1, c2, r2 = m.group(1), int(m.group(2)), m.group(3) or m.group(1), int(m.group(4) or m.group(2))
                area = (abs(r2 - r1) + 1) * (abs(_col_to_idx(c2) - _col_to_idx(c1)) + 1)
                total_merged += area
                if area > MAX_MERGED_AREA or total_merged > MAX_MERGED_AREA:
                    raise ValueError("ملف Excel يحتوي على دمج خلايا ضخم غير معتاد؛ ألغِ الدمج الزائد وأعد المحاولة")


def _sheet_data_bounds(ws: Any) -> tuple[int, int]:
    """(max_row, max_col) of cells that really carry a value, ignoring empty styled cells far from the data."""
    max_r = max_c = 0
    for (r, c), cell in getattr(ws, "_cells", {}).items():
        v = getattr(cell, "value", None)
        if v is None or (isinstance(v, str) and v == ""):
            continue
        if r > max_r:
            max_r = r
        if c > max_c:
            max_c = c
    return max_r, max_c


def _extract_tabular_rows_from_file(
    file_bytes: bytes,
    filename: str,
    dataset: str,
    sheet_name: str | None = None,
) -> tuple[str, list[str], list[str], list[tuple[int, dict[str, Any]]], list[dict[str, Any]]]:
    """Parses `.xlsx` or `.csv` bytes into `(selected_sheet, all_sheets, headers, rows_with_rownum, formula_errors)`."""
    ds = validate_dataset(dataset)
    if not file_bytes:
        raise ValueError("الملف المرفوع فارغ")
    if len(file_bytes) > MAX_IMPORT_BYTES:
        raise ValueError(f"حجم الملف يتجاوز الحد الأقصى المسموح به ({MAX_IMPORT_BYTES // (1024 * 1024)} ميجابايت)")

    ext = Path(filename or "").suffix.lower()
    if ext not in (".xlsx", ".csv"):
        raise ValueError("صيغة الملف غير مدعومة؛ يُرجى رفع ملف Excel (.xlsx) أو (.csv) فقط")

    formula_errors: list[dict[str, Any]] = []
    req_sheet = str(sheet_name or "").strip()

    if ext == ".csv":
        if req_sheet and req_sheet != "CSV":
            raise ValueError(f"ورقة العمل المحددة «{req_sheet}» غير موجودة في ملف CSV")
        # Reject binary files disguised as CSV (e.g. ZIP/XLSX/PNG/SQLite or arbitrary binary with null bytes)
        if file_bytes.startswith((b"PK\x03\x04", b"SQLite format 3", b"\x89PNG", b"%PDF", b"\xd0\xcf\x11\xe0")):
            raise ValueError("ملف CSV غير صالح (يبدو أنه ملف ثنائي بامتداد خاطئ)")
        text = ""
        if file_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                text = file_bytes.decode("utf-16")
            except UnicodeDecodeError as exc:
                raise ValueError("تعذّر قراءة ترميز ملف CSV (UTF-16 غير صالح)") from exc
        else:
            if b"\x00" in file_bytes:
                raise ValueError("ملف CSV تالف أو يحتوي على بايتات ثنائية غير صالحة")
            for enc in ("utf-8-sig", "utf-8", "cp1256", "windows-1256"):
                try:
                    text = file_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
        if not text or not text.strip():
            raise ValueError("تعذّر قراءة ترميز ملف CSV أو الملف فارغ")
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", text):
            raise ValueError("ملف CSV يحتوي على رموز تحكم ثنائية غير صالحة")

        # Auto-detect CSV delimiter (comma, semicolon, or tab) from the first non-empty line
        first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
        delim = ","
        if first_line.count(";") > first_line.count(",") and first_line.count(";") >= 1:
            delim = ";"
        elif first_line.count("\t") > first_line.count(",") and first_line.count("\t") >= 1:
            delim = "\t"

        reader = list(csv.reader(io.StringIO(text), delimiter=delim))
        raw_grid = []
        for r_idx, row in enumerate(reader, start=1):
            row_vals = []
            for c_idx, cell_val in enumerate(row, start=1):
                if isinstance(cell_val, str) and cell_val.strip().startswith("="):
                    formula_errors.append({
                        "row": r_idx,
                        "column": get_column_letter(c_idx),
                        "reason": "لا يُسمح بمعادلات Excel أو الماكرو؛ يرجى إدخال القيم النهائية كأرقام أو نصوص مباشرة.",
                    })
                    row_vals.append("__FORMULA_ERROR__")
                else:
                    row_vals.append(cell_val)
            raw_grid.append(row_vals)
        sheets = ["CSV"]
        chosen_sheet = "CSV"
    else:
        _guard_xlsx_container(file_bytes)
        try:
            wb = load_workbook(io.BytesIO(file_bytes), read_only=False, data_only=False)
        except Exception as exc:
            raise ValueError("ملف Excel غير صالح أو تالف؛ تأكد من سلامة الملف وصيغته (.xlsx)") from exc

        all_wb_sheets = list(wb.sheetnames)
        sheets = [s for s in all_wb_sheets if s != "تعليمات الاستيراد"] or all_wb_sheets
        if req_sheet:
            if req_sheet not in all_wb_sheets:
                wb.close()
                raise ValueError(
                    f"ورقة العمل المحددة «{req_sheet}» غير موجودة في ملف الإكسيل (الأوراق المتاحة: {', '.join(all_wb_sheets)})"
                )
            chosen_sheet = req_sheet
            if chosen_sheet not in sheets:
                sheets = [chosen_sheet] + sheets
        else:
            chosen_sheet = sheets[0]
            if chosen_sheet in ("الرئيسية", "الإعدادات", "لوحة التحكم", "ملخص") and len(sheets) > 1:
                preferred_by_ds = {
                    "students": ("الطالبات", "الطلاب", "سجل الطالبات"),
                    "teachers": ("المعلمات", "المعلمات والمشرفات", "الأقسام والمعلمات"),
                    "receipts": ("الاشتراكات والمدفوعات", "الإيصالات", "المدفوعات"),
                }
                for cand_s in preferred_by_ds.get(ds, ()):
                    if cand_s in sheets:
                        chosen_sheet = cand_s
                        break

        ws = wb[chosen_sheet]
        wb_val = None
        ws_val = None
        try:
            wb_val = load_workbook(io.BytesIO(file_bytes), read_only=False, data_only=True)
            if chosen_sheet in wb_val.sheetnames:
                ws_val = wb_val[chosen_sheet]
        except Exception:
            wb_val = None
            ws_val = None

        # Propagate merged header cells (both horizontal banners and 2-row vertical header merges)
        for target_ws in (ws, ws_val):
            if target_ws is None:
                continue
            merged_ranges = list(target_ws.merged_cells.ranges)
            for mrange in merged_ranges:
                top_val = target_ws.cell(row=mrange.min_row, column=mrange.min_col).value
                target_ws.unmerge_cells(str(mrange))
                if mrange.min_row == mrange.max_row:
                    for col_idx in range(mrange.min_col, mrange.max_col + 1):
                        target_ws.cell(row=mrange.min_row, column=col_idx, value=top_val)
                elif mrange.min_col == mrange.max_col and mrange.max_row == mrange.min_row + 1 and mrange.min_row <= 10:
                    if target_ws.cell(row=mrange.max_row, column=mrange.min_col).value in (None, ""):
                        target_ws.cell(row=mrange.max_row, column=mrange.min_col, value=top_val)

        # Bound the iteration. A normal sheet keeps the legacy behaviour (iterate its declared dimension); a sheet
        # whose declared dimension is huge (a stray styled cell far away) is trimmed to the cells that carry data;
        # real data beyond the limits is rejected instead of exhausting memory.
        iter_kwargs: dict[str, Any] = {}
        if (ws.max_row or 0) > MAX_SHEET_ROWS or (ws.max_column or 0) > MAX_SHEET_COLS or \
                (ws.max_row or 0) * (ws.max_column or 0) > MAX_SHEET_AREA:
            data_r, data_c = _sheet_data_bounds(ws)
            if data_r > MAX_SHEET_ROWS or data_c > MAX_SHEET_COLS or data_r * data_c > MAX_SHEET_AREA:
                wb.close()
                if wb_val:
                    wb_val.close()
                raise ValueError(
                    f"الورقة تحتوي على بيانات خارج الحدود المسموحة (الحد الأقصى {MAX_SHEET_ROWS} صف و{MAX_SHEET_COLS} عمودًا)؛ "
                    "احذف الخلايا البعيدة أو قسّم الملف وأعد المحاولة"
                )
            iter_kwargs = {"min_row": 1, "max_row": max(data_r, 1), "min_col": 1, "max_col": max(data_c, 1)}

        _safe_excel_func_re = re.compile(
            r"^=\s*(?:ROUND|SUM|SUMIF|SUMIFS|COUNT|COUNTA|COUNTIF|COUNTIFS|AVERAGE|IFERROR)\s*\(",
            re.IGNORECASE,
        )
        _round_mul_re = re.compile(
            r"^=\s*ROUND\s*\(\s*\$?([A-Za-z]+)\$?(\d+)\s*\*\s*\$?([A-Za-z]+)\$?(\d+)\s*,\s*(\d+)\s*\)\s*$",
            re.IGNORECASE,
        )
        _simple_mul_re = re.compile(
            r"^=\s*\$?([A-Za-z]+)\$?(\d+)\s*\*\s*\$?([A-Za-z]+)\$?(\d+)\s*$",
            re.IGNORECASE,
        )

        raw_grid = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=False, **iter_kwargs), start=1):
            row_vals = []
            for c_idx, cell in enumerate(row, start=1):
                val = cell.value
                if cell.data_type == "f" or (isinstance(val, str) and val.strip().startswith("=")):
                    f_str = str(val or "").strip()
                    resolved_f = None
                    is_safe_sheet_func = bool(_safe_excel_func_re.match(f_str) or _simple_mul_re.match(f_str))
                    if is_safe_sheet_func:
                        if ws_val is not None:
                            cv = ws_val.cell(row=r_idx, column=c_idx).value
                            if cv is not None and not (isinstance(cv, str) and cv.startswith("#")):
                                resolved_f = cv
                        if resolved_f is None:
                            m_rm = _round_mul_re.match(f_str) or _simple_mul_re.match(f_str)
                            if m_rm:
                                col_a = _col_to_idx(m_rm.group(1).encode("ascii"))
                                row_a = int(m_rm.group(2))
                                col_b = _col_to_idx(m_rm.group(3).encode("ascii"))
                                row_b = int(m_rm.group(4))
                                va = ws.cell(row=row_a, column=col_a).value
                                vb = ws.cell(row=row_b, column=col_b).value
                                if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                                    resolved_f = round_half_up(Decimal(str(va)) * Decimal(str(vb)))
                    if is_safe_sheet_func:
                        row_vals.append(resolved_f)
                    else:
                        formula_errors.append({
                            "row": r_idx,
                            "column": get_column_letter(c_idx),
                            "reason": "لا يُسمح بمعادلات Excel أو الماكرو؛ يرجى إدخال القيم النهائية كأرقام أو نصوص مباشرة.",
                        })
                        row_vals.append("__FORMULA_ERROR__")
                else:
                    # Convert percentage-formatted cells (e.g., 0.7 or 1.2 with number_format="0%") to integer
                    num_fmt = str(getattr(cell, "number_format", "") or "")
                    if (
                        isinstance(val, (int, float))
                        and not isinstance(val, bool)
                        and 0 < float(val) <= 10
                        and "%" in num_fmt
                    ):
                        val = round_half_up(Decimal(str(val)) * Decimal(100))
                    row_vals.append(val)
            raw_grid.append(row_vals)
        wb.close()
        if wb_val is not None:
            wb_val.close()

    if not raw_grid:
        return chosen_sheet, sheets, [], [], formula_errors

    # Auto-detect header row among first 15 non-empty rows by scoring synonym matches
    best_row_idx = 0
    best_score = -1
    for idx in range(min(15, len(raw_grid))):
        row_cells = [str(c or "").strip() for c in raw_grid[idx]]
        non_empty = [c for c in row_cells if c]
        if len(non_empty) < 1:
            continue
        mapped = auto_map_headers(ds, row_cells)
        score = len(mapped) * 10 + len(non_empty)
        if score > best_score:
            best_score = score
            best_row_idx = idx

    raw_headers = [str(c or "").strip() for c in raw_grid[best_row_idx]]

    # Support multi-month exported workbooks (`excel_service._students_sheet`) where row `best_row_idx - 1`
    # has month banners (e.g. "نوفمبر 2026") over repeated sub-headers ["قيمة الاشتراك", "تاريخ الدفع", ...]
    if ds == "students" and best_row_idx >= 1:
        upper_row = [str(c or "").strip() for c in raw_grid[best_row_idx - 1]]
        sub_Set = {"قيمة الاشتراك", "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية", "أجر المعلمة"}
        month_groups: dict[str, dict[str, int]] = {}
        month_order: list[str] = []
        info_col_indices: list[int] = []
        for ci, h_txt in enumerate(raw_headers):
            up_txt = upper_row[ci] if ci < len(upper_row) else ""
            parsed_mk = parse_flexible_month(up_txt, default_month="") if up_txt and up_txt != h_txt else None
            if parsed_mk and h_txt in sub_Set:
                if parsed_mk not in month_groups:
                    month_groups[parsed_mk] = {}
                    month_order.append(parsed_mk)
                month_groups[parsed_mk][h_txt] = ci
            elif h_txt not in sub_Set:
                info_col_indices.append(ci)

        if month_order and any("قيمة الاشتراك" in g for g in month_groups.values()):
            info_headers = [raw_headers[i] or f"عمود_{i+1}" for i in info_col_indices]
            has_code_col = any(h in ("كود الطالبة", "الكود", "code") for h in info_headers)
            extra_code_hdr = ["كود الطالبة"] if (not has_code_col and "م" in info_headers) else []
            headers = [*extra_code_hdr, *info_headers, "الشهر", "قيمة الاشتراك", "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية", "أجر المعلمة"]
            rows_out: list[tuple[int, dict[str, Any]]] = []
            for r_idx in range(best_row_idx + 1, len(raw_grid)):
                raw_row = raw_grid[r_idx]
                non_blank = [v for v in raw_row if v is not None and str(v).strip() != ""]
                if not non_blank:
                    continue
                first_strs = [str(v or "").strip() for v in raw_row[:3] if v is not None]
                if any(s.startswith("الإجمالي") or s.startswith("الاجمالي") or s in ("الإجمالي العام", "المجموع") for s in first_strs):
                    continue
                base_dict = {
                    info_headers[idx_pos]: (raw_row[col_i] if col_i < len(raw_row) else None)
                    for idx_pos, col_i in enumerate(info_col_indices)
                }
                if extra_code_hdr:
                    seq_v = base_dict.get("م")
                    if isinstance(seq_v, (int, float)) and not isinstance(seq_v, bool) and int(seq_v) > 0:
                        base_dict["كود الطالبة"] = f"STU-{int(seq_v):03d}"
                    else:
                        base_dict["كود الطالبة"] = f"STU-{r_idx - best_row_idx:03d}"
                emitted_any = False
                for mk in month_order:
                    g = month_groups[mk]
                    v_val = raw_row[g["قيمة الاشتراك"]] if "قيمة الاشتراك" in g and g["قيمة الاشتراك"] < len(raw_row) else None
                    p_val = raw_row[g["تاريخ الدفع"]] if "تاريخ الدفع" in g and g["تاريخ الدفع"] < len(raw_row) else None
                    s_val = raw_row[g["تاريخ البداية الفعلي"]] if "تاريخ البداية الفعلي" in g and g["تاريخ البداية الفعلي"] < len(raw_row) else None
                    e_val = raw_row[g["تاريخ النهاية"]] if "تاريخ النهاية" in g and g["تاريخ النهاية"] < len(raw_row) else None
                    w_val = raw_row[g["أجر المعلمة"]] if "أجر المعلمة" in g and g["أجر المعلمة"] < len(raw_row) else None
                    if any(x is not None and str(x).strip() != "" for x in (v_val, p_val, s_val, e_val, w_val)):
                        r_copy = dict(base_dict)
                        r_copy.update({
                            "الشهر": mk,
                            "قيمة الاشتراك": v_val,
                            "تاريخ الدفع": p_val,
                            "تاريخ البداية الفعلي": s_val,
                            "تاريخ النهاية": e_val,
                            "أجر المعلمة": w_val,
                        })
                        rows_out.append((r_idx + 1, r_copy))
                        emitted_any = True
                if not emitted_any:
                    r_copy = dict(base_dict)
                    r_copy.update({
                        "الشهر": month_order[0],
                        "قيمة الاشتراك": None,
                        "تاريخ الدفع": None,
                        "تاريخ البداية الفعلي": None,
                        "تاريخ النهاية": None,
                        "أجر المعلمة": None,
                    })
                    rows_out.append((r_idx + 1, r_copy))
            return chosen_sheet, sheets, headers, rows_out, formula_errors

    headers = []
    seen_h: dict[str, int] = {}
    for ci, h in enumerate(raw_headers):
        h_clean = h or f"عمود_{ci+1}"
        if h_clean in seen_h:
            seen_h[h_clean] += 1
            h_clean = f"{h_clean}_{seen_h[h_clean]}"
        else:
            seen_h[h_clean] = 1
        headers.append(h_clean)

    rows_out = []
    for r_idx in range(best_row_idx + 1, len(raw_grid)):
        raw_row = raw_grid[r_idx]
        # Skip completely blank rows or total summary rows
        non_blank = [v for v in raw_row if v is not None and str(v).strip() != ""]
        if not non_blank:
            continue
        # Skip numbered template skeleton rows where only column 1 has a bare integer and all other cells are blank
        if (
            len(non_blank) == 1
            and raw_row[0] is not None
            and str(raw_row[0]).strip().isdigit()
            and all(v is None or str(v).strip() == "" for v in raw_row[1:])
        ):
            continue
        first_strs = [str(v or "").strip() for v in raw_row[:3] if v is not None]
        if any(
            s in ("الإجمالي", "الاجمالي", "الإجمالي العام", "المجموع", "بدون معلمة", "بدون معلمة (مجموعات)")
            or s.startswith("الإجمالي (")
            for s in first_strs
        ):
            continue
        row_dict = {headers[i]: (raw_row[i] if i < len(raw_row) else None) for i in range(len(headers))}
        # Skip dragged separator rows in student sheets where name, code, phone, teacher, and value are all blank
        if ds == "students":
            temp_map = auto_map_headers("students", headers)
            if "name" in temp_map:
                n_v = row_dict.get(temp_map["name"])
                c_v = row_dict.get(temp_map.get("code", ""))
                p_v = row_dict.get(temp_map.get("phone", ""))
                t_v = row_dict.get(temp_map.get("teacher", ""))
                v_v = row_dict.get(temp_map.get("value", ""))
                if all(x is None or str(x).strip() == "" for x in (n_v, c_v, p_v, t_v, v_v)):
                    continue
        rows_out.append((r_idx + 1, row_dict))

    return chosen_sheet, sheets, headers, rows_out, formula_errors


def _match_student_record(
    raw_code: str,
    raw_name: str,
    norm_name: str,
    norm_phone: str,
    stu_list_by_code: dict[str, list[dict[str, Any]]],
    stu_list_by_name_phone: dict[tuple[str, str], list[dict[str, Any]]],
    stu_list_by_name: dict[str, list[dict[str, Any]]],
    stu_list_by_phone: dict[str, list[dict[str, Any]]],
    for_receipt: bool = False,
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    """Safely matches an imported student or receipt row against existing students in the database.

    Returns `(matched_student_or_None, conflict_or_ambiguity_errors, matching_warnings)`.
    Never guesses when multiple existing students match or when identity fields conflict.
    """
    errors: list[str] = []
    warnings: list[str] = []
    code_key = raw_code.lower().strip()

    # 1. Explicit Student Code provided
    if code_key and code_key in stu_list_by_code:
        code_matches = stu_list_by_code[code_key]
        if len(code_matches) > 1:
            errors.append(
                f"مطابقة غامضة: يوجد أكثر من طالبة مسجلة بنفس الكود «{raw_code}» في قاعدة البيانات ({len(code_matches)} سجلات)."
            )
            return None, errors, warnings

        c_stu = code_matches[0]
        c_norm_name = normalize_arabic_for_comparison(c_stu.get("name") or "")
        c_norm_phone = normalize_phone_digits(c_stu.get("phone") or "")

        # Check if provided name conflicts with the student owning this code
        if norm_name and c_norm_name and norm_name != c_norm_name:
            errors.append(
                f"تعارض في بيانات الهوية: الكود «{raw_code}» مسجل للطالبة «{c_stu.get('name')}» وليس للطالبة «{raw_name}»."
            )
            return None, errors, warnings

        # Check if (name, phone) matches a DIFFERENT student in DB
        if norm_name and norm_phone and (norm_name, norm_phone) in stu_list_by_name_phone:
            np_matches = stu_list_by_name_phone[(norm_name, norm_phone)]
            if any(m.get("id") != c_stu.get("id") for m in np_matches):
                errors.append(
                    f"تعارض في بيانات الهوية: الكود «{raw_code}» يشير إلى طالبة مختلفة عن المطابقة للاسم ورقم الهاتف."
                )
                return None, errors, warnings

        # Check phone difference on code-matched student
        if norm_phone and c_norm_phone and norm_phone != c_norm_phone:
            if for_receipt:
                errors.append(
                    f"تعارض في بيانات الطالبة «{c_stu.get('name')}»: رقم الهاتف في الإيصال ({norm_phone}) يختلف عن الرقم المسجل ({c_norm_phone})."
                )
                return None, errors, warnings
            warnings.append(
                f"تنبيه: رقم الهاتف في الملف ({norm_phone}) يختلف عن الرقم المسجل حاليًا للطالبة «{c_stu.get('name')}» ({c_norm_phone})."
            )

        if raw_name and c_stu.get("name") and raw_name != c_stu.get("name"):
            warnings.append(
                f"تمت مطابقة الطالبة بالكود «{raw_code}» («{c_stu.get('name')}»)."
            )
        return c_stu, errors, warnings

    # 2. Code not in DB (or empty): check (norm_name, norm_phone) when phone is present
    if norm_name and norm_phone:
        np_matches = stu_list_by_name_phone.get((norm_name, norm_phone), [])
        if len(np_matches) > 1:
            errors.append(
                f"مطابقة غامضة: يوجد {len(np_matches)} طالبات مسجلات بنفس الاسم «{raw_name}» ورقم الهاتف «{norm_phone}»؛ يرجى تحديد كود الطالبة للتمييز بينهن."
            )
            return None, errors, warnings
        if len(np_matches) == 1:
            np_stu = np_matches[0]
            ex_code = normalize_digits_to_ascii(str(np_stu.get("code") or "")).strip()
            if code_key and ex_code and ex_code.lower() != code_key:
                errors.append(
                    f"تعارض في بيانات الهوية: الطالبة «{np_stu.get('name')}» مسجلة بالكود «{ex_code}» بينما الملف يحتوي على كود مختلف «{raw_code}»."
                )
                return None, errors, warnings
            if raw_name != np_stu.get("name"):
                warnings.append(
                    f"تمت مطابقة الاسم «{raw_name}» مع الطالبة المسجلة «{np_stu.get('name')}» بعد تطبيع الحروف العربية."
                )
            return np_stu, errors, warnings

        # (norm_name, norm_phone) not found — check if norm_name exists in DB with a different or empty phone
        name_matches = stu_list_by_name.get(norm_name, [])
        if name_matches:
            if for_receipt:
                # For a receipt, if phone was provided and conflicts with the existing student's phone, do NOT guess!
                with_phones = [normalize_phone_digits(m.get("phone") or "") for m in name_matches if normalize_phone_digits(m.get("phone") or "")]
                if with_phones:
                    errors.append(
                        f"تعارض في بيانات الطالبة «{raw_name}»: رقم الهاتف في الملف ({norm_phone}) لا يطابق الرقم المسجل ({'، '.join(with_phones)})."
                    )
                    return None, errors, warnings
                if len(name_matches) > 1:
                    errors.append(
                        f"مطابقة غامضة: يوجد {len(name_matches)} طالبات مسجلات بنفس الاسم «{raw_name}»؛ يرجى تحديد كود الطالبة."
                    )
                    return None, errors, warnings
                warnings.append(f"تمت مطابقة الطالبة «{name_matches[0].get('name')}» بالاسم.")
                return name_matches[0], errors, warnings
            else:
                # For student import: if an existing student has the exact same name and NO phone in DB (and no conflicting code),
                # check if multiple exist or if they have a different phone.
                phoneless = [
                    m for m in name_matches
                    if not normalize_phone_digits(m.get("phone") or "")
                    and (not code_key or not str(m.get("code") or "").strip())
                ]
                with_other_phone = [
                    m for m in name_matches
                    if normalize_phone_digits(m.get("phone") or "")
                    and normalize_phone_digits(m.get("phone") or "") != norm_phone
                ]
                if len(name_matches) > 1 and phoneless:
                    errors.append(
                        f"مطابقة غامضة: يوجد أكثر من طالبة مسجلة بنفس الاسم «{raw_name}» في قاعدة البيانات؛ يرجى تحديد كود الطالبة."
                    )
                    return None, errors, warnings
                if with_other_phone:
                    other_phones = "، ".join(normalize_phone_digits(m.get("phone") or "") for m in with_other_phone)
                    warnings.append(
                        f"تنبيه تشابه أسماء: توجد طالبة أخرى مسجلة بنفس الاسم «{with_other_phone[0].get('name')}» ولكن برقم هاتف مختلف ({other_phones})؛ سيتم التعامل مع هذا السجل كطالبة مستقلة."
                    )

        # Check if phone is shared with another existing student of a different name
        phone_owners = stu_list_by_phone.get(norm_phone, [])
        if phone_owners and not for_receipt:
            owner_names = "، ".join(str(m.get("name") or "") for m in phone_owners[:2])
            warnings.append(
                f"تنبيه: رقم الهاتف ({norm_phone}) مسجل لطالبة أخرى («{owner_names}»)."
            )
        return None, errors, warnings

    # 3. Name-only fallback (when norm_phone is empty and code did not match)
    if norm_name and not norm_phone:
        name_matches = stu_list_by_name.get(norm_name, [])
        if len(name_matches) > 1:
            errors.append(
                f"مطابقة غامضة: يوجد {len(name_matches)} طالبات مسجلات بنفس الاسم «{raw_name}» وبدون كود أو رقم هاتف في الملف للتمييز بينهن."
            )
            return None, errors, warnings
        if len(name_matches) == 1:
            n_stu = name_matches[0]
            ex_code = normalize_digits_to_ascii(str(n_stu.get("code") or "")).strip()
            if code_key and ex_code and ex_code.lower() != code_key:
                errors.append(
                    f"تعارض في بيانات الهوية: الطالبة «{n_stu.get('name')}» مسجلة بالكود «{ex_code}» بينما الملف يحتوي على كود مختلف «{raw_code}»."
                )
                return None, errors, warnings
            warnings.append(
                f"تمت المطابقة بالاسم فقط مع الطالبة «{n_stu.get('name')}» لعدم وجود كود أو رقم هاتف في الملف."
            )
            return n_stu, errors, warnings

    return None, errors, warnings


def preview_import_file(
    file_bytes: bytes,
    filename: str,
    dataset: str,
    state: dict[str, Any],
    column_mapping: dict[str, Any] | None = None,
    mode: str = "update_existing",
    auto_create_refs: bool = True,
    sheet_name: str | None = None,
    default_month: str = "",
    auto_create_teachers: bool | None = None,
    auto_create_packages: bool | None = None,
) -> dict[str, Any]:
    """Performs a non-destructive dry-run preview of an uploaded `.xlsx` or `.csv` file."""
    ds = validate_dataset(dataset)
    imp_mode = validate_import_mode(mode)
    eff_auto_teachers = bool(auto_create_refs) if auto_create_teachers is None else bool(auto_create_teachers)
    eff_auto_packages = bool(auto_create_refs) if auto_create_packages is None else bool(auto_create_packages)
    eff_auto_refs = eff_auto_teachers and eff_auto_packages

    chosen_sheet, sheets, headers, raw_rows, formula_errors = _extract_tabular_rows_from_file(
        file_bytes, filename, ds, sheet_name=sheet_name
    )

    saved_mappings = (state.get("columnMappings") or {}).get(ds) if isinstance(state.get("columnMappings"), dict) else None
    effective_mapping, auto_mapped, mapping_errors = resolve_and_validate_column_mapping(
        ds, headers, saved_mapping=saved_mappings, user_overrides=column_mapping
    )

    errors: list[dict[str, Any]] = list(formula_errors)
    formula_row_nums = {e["row"] for e in formula_errors}

    for me in mapping_errors:
        errors.append({
            "row": 1,
            "column": "مطابقة الأعمدة",
            "reason": me,
        })

    # Check required columns for dataset
    required_by_ds = {
        "teachers": ("name",),
        "packages": ("name", "price"),
        "students": ("name",),
        "receipts": ("amount", "paymentDate"),
        "transactions": ("category", "amount", "date"),
        "salaries": ("teacher", "amount"),
    }
    missing_req = [req for req in required_by_ds.get(ds, ()) if req not in effective_mapping]
    if ds == "receipts" and "studentName" not in effective_mapping and "studentCode" not in effective_mapping:
        missing_req.append("studentName")

    missing_req_details = [
        {"field": mr, "label": _field_label(ds, mr)}
        for mr in missing_req
    ]
    if missing_req and raw_rows:
        ar_names = {
            "name": "الاسم",
            "price": "قيمة الاشتراك / السعر",
            "amount": "المبلغ",
            "paymentDate": "تاريخ الدفع",
            "category": "التصنيف / البند",
            "date": "التاريخ",
            "teacher": "اسم المعلمة",
            "studentName": "اسم الطالبة / كود الطالبة",
        }
        for mr in missing_req:
            lbl = ar_names.get(mr) or _field_label(ds, mr)
            errors.append({
                "row": 1,
                "column": lbl,
                "reason": f"العمود الأساسي «{lbl}» غير موجود أو غير مربوط في الملف.",
            })

    # Existing state lookup maps
    existing_teachers = {normalize_arabic_for_comparison(t): t for t in (state.get("teachers") or []) if str(t or "").strip()}
    subtypes_dict = _coerce_subtypes_dict(state.get("subTypes"))
    existing_subtypes = {normalize_arabic_for_comparison(k): k for k in subtypes_dict.keys()}
    existing_students = state.get("students") or []

    stu_list_by_code: dict[str, list[dict[str, Any]]] = {}
    stu_list_by_name_phone: dict[tuple[str, str], list[dict[str, Any]]] = {}
    stu_list_by_name: dict[str, list[dict[str, Any]]] = {}
    stu_list_by_phone: dict[str, list[dict[str, Any]]] = {}

    for s in existing_students:
        if not isinstance(s, dict):
            continue
        code_k = normalize_digits_to_ascii(str(s.get("code") or "")).strip().lower()
        if code_k:
            stu_list_by_code.setdefault(code_k, []).append(s)
        nk = normalize_arabic_for_comparison(s.get("name") or "")
        pk = normalize_phone_digits(s.get("phone") or "")
        if nk:
            stu_list_by_name.setdefault(nk, []).append(s)
            if pk:
                stu_list_by_name_phone.setdefault((nk, pk), []).append(s)
        if pk:
            stu_list_by_phone.setdefault(pk, []).append(s)

    existing_receipts = {
        normalize_digits_to_ascii(str(inst.get("receiptNo") or "")).strip().lower(): inst
        for inst in (state.get("installments") or [])
        if isinstance(inst, dict) and inst.get("receiptNo")
    }

    settings = state.get("settings") or {}
    default_pct = parse_pct_int(settings.get("defaultTeacherPct", 70))

    preview_rows: list[dict[str, Any]] = []
    seen_in_file: set[str] = set()
    file_code_owners: dict[str, tuple[str, str, int]] = {}
    file_name_identities: dict[str, set[str]] = {}
    seen_new_teachers: dict[str, str] = {}
    seen_new_packages: dict[str, str] = {}
    new_teachers_to_create: list[str] = []
    new_packages_to_create: list[str] = []

    new_count = update_count = dup_count = invalid_count = 0

    def _get(row_d: dict[str, Any], field: str) -> Any:
        col = effective_mapping.get(field)
        return row_d.get(col) if col else None

    def _append_row(
        r_num: int,
        st: str,
        d_obj: dict[str, Any],
        errs: list[str],
        warns: list[str] | None = None,
    ) -> None:
        w_list = list(warns or [])
        p_idx = len(preview_rows)
        preview_rows.append({
            "previewIndex": p_idx,
            "row": r_num,
            "rowIndex": r_num,
            "status": st,
            "data": d_obj,
            "normalized": d_obj,
            "errors": errs,
            "warnings": w_list,
            "canForceImport": True,
        })

    for row_num, rdict in raw_rows:
        row_errs: list[dict[str, Any]] = []
        row_warns: list[str] = []

        if row_num in formula_row_nums or any(v == "__FORMULA_ERROR__" for v in rdict.values()):
            row_errs.append({
                "row": row_num,
                "column": "معادلة",
                "reason": "يحتوي الصف على معادلة Excel (=) غير مسموح بها",
            })

        if missing_req or mapping_errors:
            invalid_count += 1
            _append_row(
                row_num,
                "invalid",
                {},
                [e["reason"] for e in errors if e["row"] == 1],
                [],
            )
            continue

        if ds == "teachers":
            raw_name = str(_get(rdict, "name") or "").strip()
            norm_name = normalize_arabic_for_comparison(raw_name)
            phone = normalize_phone_digits(str(_get(rdict, "phone") or ""))
            specialty = str(_get(rdict, "specialty") or "حفظ وتجويد").strip()
            raw_pct = _get(rdict, "defaultPct")
            try:
                pct = parse_pct_int(raw_pct if (raw_pct is not None and raw_pct != "") else default_pct)
            except ValueError:
                pct = default_pct
                row_errs.append({"row": row_num, "column": "نسبة المعلمة", "reason": "نسبة المعلمة يجب أن تكون بين 0 و 100"})
            notes = str(_get(rdict, "notes") or "").strip()

            if not raw_name:
                row_errs.append({"row": row_num, "column": "اسم المعلمة", "reason": "اسم المعلمة مطلوب"})

            canon_t, exists, t_warn = _match_or_warn_reference_name(
                raw_name, existing_teachers, seen_new_teachers, is_teacher=True
            )
            if t_warn and exists:
                row_warns.append(t_warn)

            file_key = f"teacher:{normalize_arabic_for_comparison(canon_t or raw_name)}"
            if norm_name and file_key in seen_in_file:
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    {"name": raw_name, "phone": phone, "specialty": specialty, "defaultPct": pct},
                    ["صف مكرر لنفس المعلمة داخل نفس الملف"],
                    row_warns,
                )
                continue
            if norm_name:
                seen_in_file.add(file_key)

            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(row_num, "invalid", {"name": raw_name}, [e["reason"] for e in row_errs], row_warns)
                continue

            status = "new"
            if exists:
                if imp_mode == "update_existing":
                    status = "will_update"
                    update_count += 1
                else:
                    status = "duplicate"
                    dup_count += 1
            else:
                new_count += 1

            _append_row(
                row_num,
                status,
                {
                    "name": canon_t or raw_name,
                    "phone": phone,
                    "specialty": specialty,
                    "defaultPct": pct,
                    "notes": notes,
                },
                [],
                row_warns,
            )

        elif ds == "packages":
            raw_name = str(_get(rdict, "name") or "").strip()
            norm_name = normalize_arabic_for_comparison(raw_name)
            price = parse_flexible_money(_get(rdict, "price"), allow_empty=False)
            if not raw_name:
                row_errs.append({"row": row_num, "column": "نوع الاشتراك", "reason": "اسم الباقة / الاشتراك مطلوب"})
            if price is None:
                row_errs.append({"row": row_num, "column": "قيمة الاشتراك", "reason": "سعر الباقة غير صالح"})

            canon_pkg, exists, pkg_warn = _match_or_warn_reference_name(
                raw_name, existing_subtypes, seen_new_packages, is_teacher=False
            )
            if pkg_warn and exists:
                row_warns.append(pkg_warn)

            file_key = f"pkg:{normalize_arabic_for_comparison(canon_pkg or raw_name)}"
            if norm_name and file_key in seen_in_file:
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    {"name": raw_name, "price": price or 0},
                    ["باقة مكررة في الملف"],
                    row_warns,
                )
                continue
            if norm_name:
                seen_in_file.add(file_key)

            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(row_num, "invalid", {"name": raw_name}, [e["reason"] for e in row_errs], row_warns)
                continue

            if exists:
                status = "will_update" if imp_mode == "update_existing" else "duplicate"
                if status == "will_update":
                    update_count += 1
                else:
                    dup_count += 1
            else:
                status = "new"
                new_count += 1
            _append_row(
                row_num,
                status,
                {"name": canon_pkg or raw_name, "price": price or 0},
                [],
                row_warns,
            )

        elif ds == "students":
            raw_code = normalize_digits_to_ascii(str(_get(rdict, "code") or "")).strip()
            raw_name = str(_get(rdict, "name") or "").strip()
            norm_name = normalize_arabic_for_comparison(raw_name)
            raw_phone = str(_get(rdict, "phone") or "").strip()
            norm_phone = normalize_phone_digits(raw_phone)
            raw_teacher = str(_get(rdict, "teacher") or "").strip()
            raw_type = str(_get(rdict, "type") or "").strip()
            schedule = str(_get(rdict, "schedule") or "").strip()

            if not raw_name:
                row_errs.append({"row": row_num, "column": "اسم الطالبة", "reason": "اسم الطالبة مطلوب"})

            raw_pct = _get(rdict, "pct")
            try:
                pct = parse_pct_int(raw_pct if (raw_pct is not None and raw_pct != "") else default_pct)
            except ValueError:
                pct = default_pct
                row_errs.append({"row": row_num, "column": "نسبة المعلمة", "reason": "نسبة المعلمة يجب أن تكون بين 0 و 100"})

            # Resolve teacher reference & check auto_create_teachers
            canon_teacher = ""
            if raw_teacher:
                canon_teacher, t_exists, t_warn = _match_or_warn_reference_name(
                    raw_teacher, existing_teachers, seen_new_teachers, is_teacher=True
                )
                if not t_exists:
                    if not eff_auto_teachers:
                        row_errs.append({
                            "row": row_num,
                            "column": "اسم المعلمة",
                            "reason": f"المعلمة «{raw_teacher}» غير مسجلة (فعّل خيار إنشاء المعلمات تلقائيًا)",
                        })
                    else:
                        if canon_teacher not in new_teachers_to_create:
                            new_teachers_to_create.append(canon_teacher)
                        if t_warn:
                            row_warns.append(t_warn)
                elif t_warn:
                    row_warns.append(t_warn)

            # Resolve package reference & check auto_create_packages
            canon_type = ""
            if raw_type:
                canon_type, pkg_exists, pkg_warn = _match_or_warn_reference_name(
                    raw_type, existing_subtypes, seen_new_packages, is_teacher=False
                )
                if not pkg_exists:
                    if not eff_auto_packages:
                        row_errs.append({
                            "row": row_num,
                            "column": "نوع الاشتراك",
                            "reason": f"الباقة «{raw_type}» غير مسجلة (فعّل خيار إنشاء الباقات تلقائيًا)",
                        })
                    else:
                        if canon_type not in new_packages_to_create:
                            new_packages_to_create.append(canon_type)
                        if pkg_warn:
                            row_warns.append(pkg_warn)
                elif pkg_warn:
                    row_warns.append(pkg_warn)

            # Optional monthly fields — keeping paymentDate and actualStartDate strictly independent
            raw_month_cell = _get(rdict, "month")
            is_month_9_rule = str(raw_month_cell or "").strip() in ("شهر 9", "شهر ٩", "2026-09", "9/2026", "09/2026")
            mk = parse_flexible_month(raw_month_cell, default_month=default_month)
            if mk is None:
                row_errs.append({"row": row_num, "column": "الشهر", "reason": "صيغة الشهر غير صالحة"})
                mk = default_month or datetime.date.today().strftime("%Y-%m")

            note_parts: list[str] = []
            raw_note = str(_get(rdict, "note") or "").strip()
            if raw_note:
                note_parts.append(raw_note)

            val_raw = _get(rdict, "value")
            val_str = str(val_raw or "").strip()
            if val_str in ("لم يتم الدفع", "غير مسدد", "متأخر", "لم تدفع", "غير مدفوع"):
                val = 0
                note_parts.append(val_str)
                row_warns.append(f"تنبيه: قيمة الاشتراك مكتوبة «{val_str}»؛ تم تسجيلها 0 ج.م مع حفظ الملاحظة.")
            else:
                val = parse_flexible_money(val_raw, allow_empty=True) if (val_raw is not None and val_raw != "") else None
                if val_raw is not None and val_raw != "" and val is None:
                    row_errs.append({"row": row_num, "column": "قيمة الاشتراك", "reason": "قيمة الاشتراك غير صالحة"})

            disc = parse_flexible_money(_get(rdict, "discount"), allow_empty=True)
            if disc is None:
                row_errs.append({"row": row_num, "column": "الخصم", "reason": "قيمة الخصم غير صالحة"})
                disc = 0
            if val is not None and disc > val:
                row_errs.append({"row": row_num, "column": "الخصم", "reason": f"الخصم ({disc}) أكبر من قيمة الاشتراك ({val})"})

            cw_raw = _get(rdict, "customWage")
            cw_str = str(cw_raw or "").strip()
            custom_wage: int | None = None
            if cw_raw is not None and cw_str != "" and cw_str not in ("-", "—", "تلقائي"):
                if cw_str in ("تم الاستلام", "تم", "مستلم"):
                    custom_wage = round_half_up(Decimal(val or 0) * Decimal(pct) / Decimal(100)) if val is not None else 0
                    note_parts.append(f"أجر المعلمة: {cw_str}")
                    row_warns.append(f"تنبيه: حقل أجر المعلمة مكتوب «{cw_str}»؛ تم احتساب الأجر ({custom_wage} ج.م) وحفظ الملاحظة.")
                else:
                    cw_parsed = parse_flexible_money(cw_raw, allow_empty=False)
                    if cw_parsed is not None:
                        custom_wage = cw_parsed
                        if val is not None and val > 0:
                            expected_auto = round_half_up(Decimal(val - (disc or 0)) * Decimal(pct) / Decimal(100))
                            if custom_wage != expected_auto:
                                row_warns.append(
                                    f"تنبيه مرونة الأجر: أجر المعلمة في الملف ({custom_wage} ج.م) يختلف عن النسبة التلقائية ({expected_auto} ج.م)؛ سيتم اعتماد الأجر المخصص كما هو."
                                )
                    else:
                        row_warns.append(f"تنبيه: قيمة أجر المعلمة «{cw_str}» غير رقمية؛ سيُحسب الأجر من النسبة.")

            paid_raw = _get(rdict, "paidAmount")
            paid_amt = parse_flexible_money(paid_raw, allow_empty=True) if (paid_raw is not None and paid_raw != "") else 0
            if paid_raw is not None and paid_raw != "" and paid_amt is None:
                row_errs.append({"row": row_num, "column": "المبلغ المسدد", "reason": "المبلغ المسدد غير صالح"})
                paid_amt = 0

            # Strictly independent dates: never copy one into the other!
            pay_dt = parse_flexible_date(_get(rdict, "paymentDate"))
            if pay_dt is None:
                row_errs.append({"row": row_num, "column": "تاريخ الدفع", "reason": "صيغة تاريخ الدفع غير صالحة"})
                pay_dt = ""

            start_dt = parse_flexible_date(_get(rdict, "actualStartDate"))
            if start_dt is None:
                row_errs.append({"row": row_num, "column": "تاريخ البداية الفعلي", "reason": "صيغة تاريخ البداية الفعلي غير صالحة"})
                start_dt = ""

            end_dt = parse_flexible_date(_get(rdict, "endDate"))
            if end_dt is None:
                row_errs.append({"row": row_num, "column": "تاريخ النهاية", "reason": "صيغة تاريخ النهاية غير صالحة"})
                end_dt = ""

            if start_dt and end_dt and end_dt < start_dt:
                row_errs.append({"row": row_num, "column": "تاريخ النهاية", "reason": "تاريخ النهاية يسبق تاريخ البداية الفعلي"})

            raw_admin_status = str(_get(rdict, "adminStatus") or "").strip()
            is_sep_sheet_row = str(raw_month_cell or "").strip() in ("شهر 9", "شهر ٩")
            if is_sep_sheet_row:
                raw_admin_status = "منتهي الاشتراك"
                if raw_code and raw_code.isdigit():
                    raw_code = f"SEP-{int(raw_code):03d}"
                elif not raw_code:
                    raw_code = f"SEP-R{row_num:02d}"
                if canon_teacher in ("خديجة", "خديجه") and "خديجه احمد" in existing_teachers:
                    canon_teacher = existing_teachers["خديجه احمد"]
                elif canon_teacher == "هاجر" and "هاجر محمد" in existing_teachers:
                    canon_teacher = existing_teachers["هاجر محمد"]
                if not start_dt:
                    start_dt = "2026-09-01"
                if not end_dt:
                    end_dt = "2026-09-30"
                if (val or 0) > 0 and not pay_dt and not paid_amt:
                    pay_dt = "2026-09-01"
                    paid_amt = int(val or 0)
                if "اشتراك شهر 9 (منتهي الاشتراك)" not in note_parts:
                    note_parts.append("اشتراك شهر 9 (منتهي الاشتراك)")

            # Check within-file student code collision across different students
            if raw_code:
                ck = raw_code.lower()
                if ck in file_code_owners:
                    prev_name, prev_phone, prev_row = file_code_owners[ck]
                    if (norm_name and prev_name and norm_name != prev_name) or (
                        norm_phone and prev_phone and norm_phone != prev_phone
                    ):
                        row_errs.append({
                            "row": row_num,
                            "column": "كود الطالبة",
                            "reason": f"كود الطالبة «{raw_code}» مستخدم لطالبة أخرى في نفس الملف (الصف {prev_row})؛ لا يمكن تكرار الكود لطالبتين مختلفتين.",
                        })
                else:
                    file_code_owners[ck] = (norm_name, norm_phone, row_num)

            # Track within-file name identities to catch ambiguous nameless-phone rows in the same file
            if norm_name:
                ident_tok = raw_code.lower() or norm_phone
                if ident_tok:
                    file_name_identities.setdefault(norm_name, set()).add(ident_tok)
                elif len(file_name_identities.get(norm_name, set())) > 1:
                    row_errs.append({
                        "row": row_num,
                        "column": "اسم الطالبة",
                        "reason": f"مطابقة غامضة داخل الملف: الاسم «{raw_name}» مكرر لأكثر من طالبة بأكواد/هواتف مختلفة وهذا الصف بدون كود أو هاتف.",
                    })

            # Match against existing students in DB
            matched_stu = None
            if norm_name:
                if is_sep_sheet_row and raw_code.lower() in stu_list_by_code:
                    matched_stu = stu_list_by_code[raw_code.lower()][0]
                elif not is_sep_sheet_row:
                    matched_stu, match_errs, match_warns = _match_student_record(
                        raw_code=raw_code,
                        raw_name=raw_name,
                        norm_name=norm_name,
                        norm_phone=norm_phone,
                        stu_list_by_code=stu_list_by_code,
                        stu_list_by_name_phone=stu_list_by_name_phone,
                        stu_list_by_name=stu_list_by_name,
                        stu_list_by_phone=stu_list_by_phone,
                        for_receipt=False,
                    )
                    for me_txt in match_errs:
                        row_errs.append({"row": row_num, "column": "اسم الطالبة", "reason": me_txt})
                    row_warns.extend(match_warns)

            full_row_payload = {
                "matchedId": matched_stu["id"] if matched_stu else "",
                "code": raw_code or (matched_stu.get("code", "") if matched_stu else ""),
                "name": raw_name,
                "phone": norm_phone or raw_phone,
                "teacher": canon_teacher or raw_teacher,
                "type": canon_type or raw_type,
                "pct": pct,
                "schedule": schedule,
                "adminStatus": raw_admin_status or "نشطة",
                "month": mk,
                "value": val,
                "discount": disc or 0,
                "customWage": custom_wage,
                "paidAmount": paid_amt or 0,
                "paymentDate": pay_dt,
                "actualStartDate": start_dt,
                "endDate": end_dt,
                "method": str(_get(rdict, "method") or "فودافون كاش").strip(),
                "note": " — ".join(note_parts),
            }

            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(
                    row_num,
                    "invalid",
                    full_row_payload,
                    [e["reason"] for e in row_errs],
                    row_warns,
                )
                continue

            # Within-file duplicate check for the same student and month.
            # When an explicit student code (`raw_code`) is present on the row, use that code as the within-file
            # student identity so two distinct student codes sharing a name/phone (e.g. STU-011 and STU-021)
            # are preserved as separate enrollments instead of dropping one as a duplicate.
            stu_ident_keys = [f"stu:{raw_code.lower() if raw_code else (norm_name + ':' + norm_phone)}:{mk}"]
            if matched_stu and matched_stu.get("id"):
                stu_ident_keys.append(f"stu_id:{matched_stu['id']}:{mk}")
            if not raw_code and norm_name and norm_phone:
                stu_ident_keys.append(f"stu_np:{norm_name}:{norm_phone}:{mk}")

            if any(k in seen_in_file for k in stu_ident_keys):
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    full_row_payload,
                    ["صف مكرر لنفس الطالبة والشهر داخل الملف"],
                    row_warns,
                )
                continue
            for k in stu_ident_keys:
                seen_in_file.add(k)

            if matched_stu:
                if imp_mode == "update_existing":
                    status = "will_update"
                    update_count += 1
                else:
                    status = "duplicate"
                    dup_count += 1
            else:
                status = "new"
                new_count += 1

            _append_row(
                row_num,
                status,
                full_row_payload,
                [],
                row_warns,
            )

        elif ds == "receipts":
            raw_rcp = normalize_digits_to_ascii(str(_get(rdict, "receiptNo") or "")).strip()
            raw_code = normalize_digits_to_ascii(str(_get(rdict, "studentCode") or "")).strip()
            raw_name = str(_get(rdict, "studentName") or "").strip()
            norm_name = normalize_arabic_for_comparison(raw_name)
            norm_phone = normalize_phone_digits(str(_get(rdict, "phone") or ""))
            mk = parse_flexible_month(_get(rdict, "month"), default_month=default_month)
            amt = parse_flexible_money(_get(rdict, "amount"), allow_empty=False)
            pay_dt = parse_flexible_date(_get(rdict, "paymentDate"))
            method = str(_get(rdict, "method") or "فودافون كاش").strip()
            note = str(_get(rdict, "note") or "").strip()

            matched_stu, match_errs, match_warns = _match_student_record(
                raw_code=raw_code,
                raw_name=raw_name,
                norm_name=norm_name,
                norm_phone=norm_phone,
                stu_list_by_code=stu_list_by_code,
                stu_list_by_name_phone=stu_list_by_name_phone,
                stu_list_by_name=stu_list_by_name,
                stu_list_by_phone=stu_list_by_phone,
                for_receipt=True,
            )
            row_warns.extend(match_warns)
            if match_errs:
                for me_txt in match_errs:
                    row_errs.append({"row": row_num, "column": "اسم الطالبة", "reason": me_txt})
            elif not matched_stu:
                row_errs.append({
                    "row": row_num,
                    "column": "اسم الطالبة",
                    "reason": f"الطالبة «{raw_name or raw_code}» غير موجودة في قاعدة البيانات",
                })

            if amt is None or amt <= 0:
                row_errs.append({"row": row_num, "column": "المبلغ", "reason": "مبلغ الإيصال يجب أن يكون أكبر من صفر"})
            if not pay_dt:
                row_errs.append({"row": row_num, "column": "تاريخ الدفع", "reason": "تاريخ الدفع مطلوب وبصيغة صحيحة"})
            if not mk:
                row_errs.append({"row": row_num, "column": "الشهر", "reason": "شهر الاشتراك غير صالح"})
                mk = (pay_dt or datetime.date.today().isoformat())[:7]

            if not raw_rcp and matched_stu and pay_dt and amt:
                # Include note and non-default method in hash so two legitimate payments on the same day
                # with different notes or payment methods never collide on generated receipt number.
                sig_parts = f"{matched_stu['id']}:{mk}:{pay_dt}:{amt}"
                if note:
                    sig_parts += f":{note}"
                if method and method != "فودافون كاش":
                    sig_parts += f":{method}"
                digest = hashlib.sha1(sig_parts.encode("utf-8")).hexdigest()[:6].upper()
                raw_rcp = f"RCP-IMP-{digest}"

            file_key = f"rcp:{raw_rcp.lower()}"
            if raw_rcp and file_key in seen_in_file:
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    {"receiptNo": raw_rcp, "studentName": raw_name, "amount": amt or 0},
                    ["إيصال مكرر في الملف (أدخل رقم إيصال أو ملاحظة مميزة إذا كانت دفعة ثانية مستقلة بنفس اليوم والمبلغ)"],
                    row_warns,
                )
                continue
            if raw_rcp:
                seen_in_file.add(file_key)

            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(
                    row_num,
                    "invalid",
                    {"receiptNo": raw_rcp, "studentName": raw_name},
                    [e["reason"] for e in row_errs],
                    row_warns,
                )
                continue

            assert matched_stu is not None  # guaranteed: a missing student is a row error that `continue`d above
            exists = raw_rcp.lower() in existing_receipts
            if exists:
                status = "will_update" if imp_mode == "update_existing" else "duplicate"
                if status == "will_update":
                    update_count += 1
                else:
                    dup_count += 1
            else:
                status = "new"
                new_count += 1

            _append_row(
                row_num,
                status,
                {
                    "id": existing_receipts[raw_rcp.lower()]["id"] if exists else f"inst_imp_{hashlib.sha1(raw_rcp.encode('utf-8')).hexdigest()[:8]}",
                    "receiptNo": raw_rcp,
                    "studentId": matched_stu["id"],
                    "studentName": matched_stu["name"],
                    "month": mk,
                    "amount": amt,
                    "paymentDate": pay_dt,
                    "method": method,
                    "note": note,
                },
                [],
                row_warns,
            )

        elif ds == "transactions":
            raw_type = str(_get(rdict, "type") or "مصروف").strip()
            tx_type = "revenue" if ("إيراد" in raw_type or "ايراد" in raw_type or "rev" in raw_type.lower() or "+" in raw_type) else "expense"
            cat = str(_get(rdict, "category") or "").strip()
            amt = parse_flexible_money(_get(rdict, "amount"), allow_empty=False)
            dt = parse_flexible_date(_get(rdict, "date"))
            mk = parse_flexible_month(_get(rdict, "month"), default_month=(dt[:7] if dt else default_month))
            method = str(_get(rdict, "method") or "نقدي").strip()
            note = str(_get(rdict, "note") or "").strip()

            if not cat:
                row_errs.append({"row": row_num, "column": "التصنيف", "reason": "بند / تصنيف الحركة مطلوب"})
            if amt is None or amt < 0:
                row_errs.append({"row": row_num, "column": "المبلغ", "reason": "المبلغ غير صالح"})
            if not dt:
                row_errs.append({"row": row_num, "column": "التاريخ", "reason": "تاريخ الحركة غير صالح"})
            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(row_num, "invalid", {"category": cat}, [e["reason"] for e in row_errs], row_warns)
                continue

            assert dt is not None  # guaranteed: a missing/invalid date is a row error that `continue`d above
            tx_sig = f"{tx_type}:{cat}:{amt}:{dt}:{note}"
            tx_id = f"tx_imp_{hashlib.sha1(tx_sig.encode('utf-8')).hexdigest()[:8]}"
            exists = any(t.get("id") == tx_id for t in (state.get("transactions") or []))
            if tx_sig in seen_in_file or (exists and imp_mode != "update_existing"):
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    {"id": tx_id, "type": tx_type, "category": cat, "amount": amt, "date": dt},
                    ["حركة مكررة"],
                    row_warns,
                )
                continue
            seen_in_file.add(tx_sig)
            status = "will_update" if exists else "new"
            if exists:
                update_count += 1
            else:
                new_count += 1
            _append_row(
                row_num,
                status,
                {"id": tx_id, "type": tx_type, "category": cat, "amount": amt, "date": dt, "month": mk or dt[:7], "method": method, "note": note},
                [],
                row_warns,
            )

        elif ds == "salaries":
            raw_teacher = str(_get(rdict, "teacher") or "").strip()
            dt = parse_flexible_date(_get(rdict, "date")) or datetime.date.today().isoformat()
            mk = parse_flexible_month(_get(rdict, "month"), default_month=(dt[:7] if dt else default_month))
            amt = parse_flexible_money(_get(rdict, "amount"), allow_empty=False)
            note = str(_get(rdict, "note") or "").strip()

            canon_teacher = ""
            if not raw_teacher:
                row_errs.append({"row": row_num, "column": "اسم المعلمة", "reason": "اسم المعلمة مطلوب"})
            else:
                canon_teacher, t_exists, t_warn = _match_or_warn_reference_name(
                    raw_teacher, existing_teachers, seen_new_teachers, is_teacher=True
                )
                if not t_exists:
                    if not eff_auto_teachers:
                        row_errs.append({"row": row_num, "column": "اسم المعلمة", "reason": f"المعلمة «{raw_teacher}» غير مسجلة"})
                    else:
                        if canon_teacher not in new_teachers_to_create:
                            new_teachers_to_create.append(canon_teacher)
                        if t_warn:
                            row_warns.append(t_warn)
                elif t_warn:
                    row_warns.append(t_warn)

            if amt is None or amt <= 0:
                row_errs.append({"row": row_num, "column": "المبلغ", "reason": "مبلغ الصرف يجب أن يكون أكبر من صفر"})
            if not mk:
                row_errs.append({"row": row_num, "column": "الشهر", "reason": "الشهر غير صالح"})
            if row_errs:
                errors.extend(row_errs)
                invalid_count += 1
                _append_row(row_num, "invalid", {"teacher": raw_teacher}, [e["reason"] for e in row_errs], row_warns)
                continue

            tname = canon_teacher or raw_teacher
            sal_sig = f"{tname}:{mk}:{amt}:{dt}:{note}"
            sal_id = f"sal_imp_{hashlib.sha1(sal_sig.encode('utf-8')).hexdigest()[:8]}"
            exists = any(p.get("id") == sal_id for p in (state.get("payouts") or []))
            if sal_sig in seen_in_file or (exists and imp_mode != "update_existing"):
                dup_count += 1
                _append_row(
                    row_num,
                    "duplicate",
                    {"id": sal_id, "teacher": tname, "month": mk, "amount": amt, "date": dt},
                    ["صرف راتب مكرر"],
                    row_warns,
                )
                continue
            seen_in_file.add(sal_sig)
            status = "will_update" if exists else "new"
            if exists:
                update_count += 1
            else:
                new_count += 1
            _append_row(
                row_num,
                status,
                {"id": sal_id, "teacher": tname, "month": mk, "amount": amt, "date": dt, "note": note},
                [],
                row_warns,
            )

    total_rows = len(raw_rows)
    valid_count = new_count + update_count
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    # Build reverse mapping helpers for UI display
    header_to_field = {col_h: f_k for f_k, col_h in effective_mapping.items()}
    index_to_field = {str(idx): header_to_field[h] for idx, h in enumerate(headers) if h in header_to_field}

    return {
        "dataset": ds,
        "filename": filename,
        "fileHash": file_hash,
        "mode": imp_mode,
        "autoCreateRefs": eff_auto_refs,
        "autoCreateTeachers": eff_auto_teachers,
        "autoCreatePackages": eff_auto_packages,
        "detectedSheet": chosen_sheet,
        "selectedSheet": chosen_sheet,
        "sheets": sheets,
        "sheetNames": sheets,
        "headers": headers,
        "datasetFields": DATASET_FIELD_SPECS.get(ds, []),
        "autoMapping": auto_mapped,
        "columnMapping": effective_mapping,
        "headerToFieldMapping": header_to_field,
        "indexToFieldMapping": index_to_field,
        "missingRequiredFields": missing_req_details,
        "mappingErrors": mapping_errors,
        "newEntities": {
            "teachers": new_teachers_to_create,
            "packages": new_packages_to_create,
        },
        "summary": {
            "totalRows": total_rows,
            "validCount": valid_count,
            "newCount": new_count,
            "updateCount": update_count,
            "duplicateCount": dup_count,
            "invalidCount": invalid_count,
            "total": total_rows,
            "valid": valid_count,
            "new": new_count,
            "will_update": update_count,
            "duplicate": dup_count,
            "invalid": invalid_count,
            "newTeachersCount": len(new_teachers_to_create),
            "newPackagesCount": len(new_packages_to_create),
        },
        "rows": preview_rows,
        "errors": errors,
    }


def commit_import(
    store: Any,
    file_bytes: bytes,
    filename: str,
    dataset: str,
    mode: str = "update_existing",
    auto_create_refs: bool = True,
    column_mapping: dict[str, Any] | None = None,
    sheet_name: str | None = None,
    default_month: str = "",
    backup_folder: Path | None = None,
    expected_rev: int | None = None,
    auto_create_teachers: bool | None = None,
    auto_create_packages: bool | None = None,
    expected_file_hash: str | None = None,
    selected_rows: list[Any] | None = None,
    force_import: bool = False,
    row_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Executes the validated import inside a single atomic transaction after taking a verified safety snapshot.
    Supports user-controlled full (`force_import=True`) or partial (`selected_rows=[...]`) import decisions.
    """
    ds_valid = validate_dataset(dataset)
    mode_valid = validate_import_mode(mode)
    if expected_file_hash:
        actual_hash = hashlib.sha256(file_bytes or b"").hexdigest()
        if actual_hash != str(expected_file_hash).strip():
            raise ValueError("تغير محتوى الملف بعد المعاينة؛ يرجى إعادة معاينة الملف أولًا قبل الاعتماد.")

    with store.lock:
        store.check_rev(expected_rev)
        state = store.export_state()
        preview = preview_import_file(
            file_bytes=file_bytes,
            filename=filename,
            dataset=ds_valid,
            state=state,
            column_mapping=column_mapping,
            mode=mode_valid,
            auto_create_refs=auto_create_refs,
            sheet_name=sheet_name,
            default_month=default_month,
            auto_create_teachers=auto_create_teachers,
            auto_create_packages=auto_create_packages,
        )

        # Apply any inline user field edits (`row_overrides`) keyed by previewIndex (`p:0`) or row number (`2`)
        if isinstance(row_overrides, dict) and row_overrides:
            for r in preview["rows"]:
                p_key = f"p:{r.get('previewIndex')}"
                r_key = str(r.get("row"))
                ov = row_overrides.get(p_key) or row_overrides.get(str(r.get("previewIndex"))) or row_overrides.get(r_key)
                if isinstance(ov, dict):
                    d_cur = dict(r.get("data") or {})
                    for k_ov, v_ov in ov.items():
                        if k_ov in ("value", "discount", "customWage", "paidAmount", "price", "amount"):
                            d_cur[k_ov] = parse_flexible_money(v_ov, allow_empty=False) if (v_ov is not None and v_ov != "") else None
                        elif k_ov in ("pct", "defaultPct"):
                            try:
                                d_cur[k_ov] = parse_pct_int(v_ov, default=70)
                            except ValueError:
                                pass
                        else:
                            d_cur[k_ov] = v_ov
                    r["data"] = d_cur
                    r["normalized"] = d_cur

        if selected_rows is not None and isinstance(selected_rows, list):
            sel_preview_indices: set[int] = set()
            sel_sheet_rows: set[int] = set()
            for item in selected_rows:
                s_item = str(item).strip()
                if s_item.startswith("p:") and s_item[2:].isdigit():
                    sel_preview_indices.add(int(s_item[2:]))
                elif s_item.isdigit():
                    sel_sheet_rows.add(int(s_item))

            actionable = []
            for r in preview["rows"]:
                is_selected = (r.get("previewIndex") in sel_preview_indices) or (r.get("row") in sel_sheet_rows)
                if not is_selected:
                    continue
                if force_import or r["status"] in ("new", "will_update", "duplicate", "invalid"):
                    # Ensure forced invalid/duplicate rows have a fallback name/fields so commit succeeds
                    d_obj = dict(r.get("data") or {})
                    if preview["dataset"] in ("students", "teachers", "packages") and not str(d_obj.get("name") or "").strip():
                        d_obj["name"] = f"سجل مستورد (صف {r.get('row')})"
                    r["data"] = d_obj
                    actionable.append(r)
        elif force_import:
            actionable = []
            for r in preview["rows"]:
                d_obj = dict(r.get("data") or {})
                if preview["dataset"] in ("students", "teachers", "packages") and not str(d_obj.get("name") or "").strip():
                    if not any(v for k, v in d_obj.items() if k not in ("month", "pct", "method", "adminStatus")):
                        continue
                    d_obj["name"] = f"سجل مستورد (صف {r.get('row')})"
                r["data"] = d_obj
                actionable.append(r)
        else:
            actionable = [
                r for r in preview["rows"]
                if r["status"] == "new" or (r["status"] == "will_update" and preview["mode"] == "update_existing")
            ]

        added_count = sum(1 for r in actionable if r["status"] != "will_update")
        updated_count = sum(1 for r in actionable if r["status"] == "will_update")
        skipped_count = max(0, len(preview["rows"]) - len(actionable))

        if not actionable:
            return {
                "ok": True,
                "committed": 0,
                "added": 0,
                "updated": 0,
                "skipped": skipped_count,
                "duplicate": preview["summary"]["duplicateCount"],
                "invalid": preview["summary"]["invalidCount"],
                "createdTeachers": [],
                "createdPackages": [],
                "batchId": "",
                "snapshotName": "",
                "summary": {
                    **preview["summary"],
                    "committedCount": 0,
                    "addedCount": 0,
                    "updatedCount": 0,
                    "skippedCount": skipped_count,
                },
                "rev": store.rev,
            }

        # Check closed months before committing
        ds = preview["dataset"]
        for r in actionable:
            d = r["data"]
            mk = d.get("month")
            if mk and ds in ("students", "receipts", "transactions", "salaries"):
                if ds == "students" and d.get("value") is None and not d.get("paidAmount") and not d.get("paymentDate"):
                    continue
                store.assert_month_open(mk)

        snap_name = ""
        if backup_folder:
            snap_path = store.snapshot(backup_folder, f"before_import_{ds}", keep=30)
            snap_name = snap_path.name

        settings = state.get("settings") or {}
        default_pct = parse_pct_int(settings.get("defaultTeacherPct", 70))
        teachers_list = list(state.get("teachers") or [])
        teacher_meta = dict(state.get("teacherMeta") or {})
        sub_types = _coerce_subtypes_dict(state.get("subTypes"))
        students_list = list(state.get("students") or [])
        installments_list = list(state.get("installments") or [])
        transactions_list = list(state.get("transactions") or [])
        payouts_list = list(state.get("payouts") or [])

        created_teachers: list[str] = []
        created_packages: list[str] = []

        for r in actionable:
            d = r["data"]
            if ds == "teachers":
                tname = d["name"]
                if tname not in teachers_list:
                    teachers_list.append(tname)
                    created_teachers.append(tname)
                cur_m = dict(teacher_meta.get(tname) or {})
                cur_m.update({
                    "phone": d.get("phone") or cur_m.get("phone", ""),
                    "specialty": d.get("specialty") or cur_m.get("specialty", "حفظ وتجويد"),
                    "defaultPct": d.get("defaultPct", 70),
                    "pct": d.get("defaultPct", 70),  # `pct` is what wage calculations and the UI read
                    "notes": d.get("notes") or cur_m.get("notes", ""),
                    "active": True,
                })
                teacher_meta[tname] = cur_m

            elif ds == "packages":
                if d["name"] not in sub_types:
                    created_packages.append(d["name"])
                sub_types[d["name"]] = int(d["price"])

            elif ds == "students":
                tname = d.get("teacher") or ""
                if tname and tname not in teachers_list:
                    teachers_list.append(tname)
                    created_teachers.append(tname)
                    teacher_meta.setdefault(tname, {"defaultPct": d.get("pct", 70), "pct": d.get("pct", 70), "active": True})
                pkg = d.get("type") or ""
                if pkg and pkg not in sub_types:
                    created_packages.append(pkg)
                    sub_types[pkg] = int(d["value"]) if d.get("value") is not None else 0

                matched_id = d.get("matchedId") or ""
                norm_d_name = normalize_arabic_for_comparison(d["name"])
                norm_d_phone = normalize_phone_digits(d.get("phone") or "")
                norm_d_code = normalize_digits_to_ascii(str(d.get("code") or "")).strip().lower()
                if not matched_id:
                    # IMPORT-2: a second row (another month) of a student created earlier in THIS commit has no
                    # matchedId (preview ran before the student existed). Reuse that student ONLY when its code
                    # matches (or when neither row has a conflicting code).
                    derived_id = f"stu_imp_{hashlib.sha1((norm_d_name + ':' + norm_d_phone + ':' + norm_d_code).encode('utf-8')).hexdigest()[:8]}"
                    for existing_s in students_list:
                        if existing_s.get("id") == derived_id:
                            matched_id = derived_id
                            break
                        ex_code = normalize_digits_to_ascii(str(existing_s.get("code") or "")).strip().lower()
                        if norm_d_code and ex_code == norm_d_code:
                            matched_id = existing_s["id"]
                            break
                        if not norm_d_code and not ex_code and norm_d_name and norm_d_phone and (
                            normalize_arabic_for_comparison(existing_s.get("name") or "") == norm_d_name
                            and normalize_phone_digits(existing_s.get("phone") or "") == norm_d_phone
                        ):
                            matched_id = existing_s["id"]
                            break

                stu_obj = next((s for s in students_list if s.get("id") == matched_id), None) if matched_id else None
                if not stu_obj:
                    base_id = f"stu_imp_{hashlib.sha1((norm_d_name + ':' + norm_d_phone + ':' + norm_d_code).encode('utf-8')).hexdigest()[:8]}"
                    new_id = base_id
                    sfx = 2
                    existing_stu_ids = {s.get("id") for s in students_list}
                    while new_id in existing_stu_ids:
                        new_id = f"{base_id}_{sfx}"
                        sfx += 1
                    stu_obj = {
                        "id": new_id,
                        "code": d.get("code") or f"STU-{len(students_list)+1:03d}",
                        "name": d["name"],
                        "phone": d.get("phone") or "",
                        "teacher": tname,
                        "type": pkg,
                        "pct": d.get("pct", 70),
                        "schedule": d.get("schedule") or "",
                        "adminStatus": d.get("adminStatus") or "active",
                        "paymentDate": d.get("paymentDate") or "",
                        "actualStartDate": d.get("actualStartDate") or "",
                        "transferHistory": [],
                        "months": {},
                    }
                    students_list.append(stu_obj)
                else:
                    if d.get("code"):
                        stu_obj["code"] = d["code"]
                    if d.get("phone"):
                        stu_obj["phone"] = d["phone"]
                    if tname:
                        stu_obj["teacher"] = tname
                    if pkg:
                        stu_obj["type"] = pkg
                    if d.get("pct") is not None:
                        stu_obj["pct"] = d["pct"]
                    if d.get("schedule"):
                        stu_obj["schedule"] = d["schedule"]
                    if d.get("adminStatus"):
                        stu_obj["adminStatus"] = d["adminStatus"]
                    if d.get("paymentDate"):
                        stu_obj["paymentDate"] = d["paymentDate"]
                    if d.get("actualStartDate"):
                        stu_obj["actualStartDate"] = d["actualStartDate"]

                mk = d.get("month")
                has_month_payload = (
                    d.get("value") is not None
                    or d.get("customWage") is not None
                    or (d.get("discount") or 0) > 0
                    or (d.get("paidAmount") or 0) > 0
                    or bool(d.get("paymentDate"))
                    or bool(d.get("actualStartDate"))
                    or bool(d.get("endDate"))
                    or bool(d.get("note"))
                )
                if mk and has_month_payload:
                    months_map = stu_obj.setdefault("months", {})
                    m_cur = dict(months_map.get(mk) or {})
                    if d.get("value") is not None:
                        m_cur["value"] = max(0, int(d["value"]))
                    elif "value" not in m_cur and pkg in sub_types and d.get("customWage") is None:
                        m_cur["value"] = int(sub_types[pkg])
                    if d.get("discount"):
                        raw_d_int = max(0, int(d["discount"]))
                        cur_v_int = int(m_cur.get("value") or 0)
                        m_cur["discount"] = min(raw_d_int, cur_v_int) if cur_v_int >= 0 else 0
                    if d.get("customWage") is not None and d.get("customWage") != "":
                        m_cur["customWage"] = max(0, int(d["customWage"]))
                    # Keep paymentDate and actualStartDate strictly independent
                    if d.get("paymentDate"):
                        m_cur["paymentDate"] = d["paymentDate"]
                        m_cur["pay"] = d["paymentDate"]
                    if d.get("actualStartDate"):
                        m_cur["actualStartDate"] = d["actualStartDate"]
                        m_cur["startDate"] = d["actualStartDate"]
                    if d.get("endDate"):
                        m_cur["endDate"] = d["endDate"]
                        m_cur["end"] = d["endDate"]
                    if d.get("note"):
                        m_cur["invoiceNote"] = str(d["note"])[:1000]
                    months_map[mk] = m_cur

                    if (d.get("paidAmount") or 0) > 0:
                        pay_dt = d.get("paymentDate") or datetime.date.today().isoformat()
                        stu_tok = hashlib.sha1(str(stu_obj["id"]).encode("utf-8")).hexdigest()[:6].upper()
                        rcp_id = f"inst_imp_{stu_obj['id']}_{mk.replace('-', '')}"
                        rcp_no = f"RCP-IMP-{stu_tok}-{mk.replace('-', '')}"
                        existing_inst = next((i for i in installments_list if i.get("id") == rcp_id), None)
                        inst_entry = {
                            "id": rcp_id,
                            "receiptNo": rcp_no,
                            "studentId": stu_obj["id"],
                            "month": mk,
                            "amount": int(d["paidAmount"]),
                            "date": pay_dt,
                            "paymentDate": pay_dt,
                            "method": d.get("method") or "فودافون كاش",
                            "note": d.get("note") or "مستورد من Excel",
                            "createdAt": pay_dt,
                        }
                        if existing_inst:
                            existing_inst.update(inst_entry)
                        else:
                            installments_list.append(inst_entry)

            elif ds == "receipts":
                existing_inst = next((i for i in installments_list if i.get("id") == d["id"] or i.get("receiptNo") == d["receiptNo"]), None)
                inst_entry = {
                    "id": d["id"],
                    "receiptNo": d["receiptNo"],
                    "studentId": d["studentId"],
                    "month": d["month"],
                    "amount": int(d["amount"]),
                    "date": d["paymentDate"],
                    "paymentDate": d["paymentDate"],
                    "method": d.get("method") or "فودافون كاش",
                    "note": d.get("note") or "",
                    "createdAt": d["paymentDate"],
                }
                if existing_inst:
                    existing_inst.update(inst_entry)
                else:
                    installments_list.append(inst_entry)

            elif ds == "transactions":
                existing_tx = next((t for t in transactions_list if t.get("id") == d["id"]), None)
                tx_entry = {
                    "id": d["id"],
                    "type": d["type"],
                    "category": d["category"],
                    "amount": int(d["amount"]),
                    "date": d["date"],
                    "month": d["month"],
                    "method": d.get("method") or "نقدي",
                    "note": d.get("note") or "",
                    "createdAt": d["date"],
                }
                if existing_tx:
                    existing_tx.update(tx_entry)
                else:
                    transactions_list.append(tx_entry)

            elif ds == "salaries":
                tname = d["teacher"]
                if tname not in teachers_list:
                    teachers_list.append(tname)
                    created_teachers.append(tname)
                    teacher_meta.setdefault(tname, {"defaultPct": default_pct, "pct": default_pct, "active": True})
                existing_sal = next((p for p in payouts_list if p.get("id") == d["id"]), None)
                sal_entry = {
                    "id": d["id"],
                    "teacher": tname,
                    "month": d["month"],
                    "amount": int(d["amount"]),
                    "date": d["date"],
                    "note": d.get("note") or "",
                }
                if existing_sal:
                    existing_sal.update(sal_entry)
                else:
                    payouts_list.append(sal_entry)

        state["teachers"] = teachers_list
        state["teacherMeta"] = teacher_meta
        state["subTypes"] = sub_types
        state["students"] = students_list
        state["installments"] = installments_list
        state["transactions"] = transactions_list
        state["payouts"] = payouts_list

        # Remember effective column mapping in kv when custom mapping was provided
        if column_mapping:
            col_maps = dict(state.get("columnMappings") or {})
            col_maps[ds] = preview["columnMapping"]
            state["columnMappings"] = col_maps

        commit_summary = {
            **preview["summary"],
            "committedCount": len(actionable),
            "addedCount": added_count,
            "updatedCount": updated_count,
            "skippedCount": skipped_count,
        }
        batch_id = f"imp_{int(datetime.datetime.now().timestamp())}_{secrets.token_hex(3)}"
        store.replace_all(state, actor="import")
        store.record_import_batch({
            "id": batch_id,
            "createdAt": datetime.datetime.now().isoformat(timespec="seconds"),
            "dataset": ds,
            "filename": filename,
            "mode": preview["mode"],
            "snapshotName": snap_name,
            "summary": commit_summary,
        })

        return {
            "ok": True,
            "committed": len(actionable),
            "added": added_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "duplicate": preview["summary"]["duplicateCount"],
            "invalid": preview["summary"]["invalidCount"],
            "createdTeachers": created_teachers,
            "createdPackages": created_packages,
            "batchId": batch_id,
            "snapshotName": snap_name,
            "summary": commit_summary,
            "rev": store.rev,
        }


def undo_last_import(
    store: Any,
    backup_folder: Path,
    force: bool = False,
) -> dict[str, Any]:
    """Restores the database to the pre-import safety snapshot of the last import batch."""
    with store.lock:
        last_batch = store.get_last_import_batch()
        if not last_batch or not last_batch.get("snapshotName"):
            raise ValueError("لا توجد عملية استيراد سابقة قابلة للتراجع")
        snap_file = Path(backup_folder) / last_batch["snapshotName"]
        if not snap_file.is_file():
            raise ValueError(f"ملف النسخة الاحتياطية للاستيراد غير موجود: {last_batch['snapshotName']}")

        rev_after = int(last_batch.get("revAfter") or 0)
        if not force and rev_after > 0 and store.rev > rev_after:
            return {
                "ok": False,
                "warning": True,
                "message": "تم إجراء تعديلات لاحقة بعد عملية الاستيراد؛ هل تريد التراجع واستعادة نسخة ما قبل الاستيراد على أي حال؟",
                "batch": last_batch,
            }

        students_count = store.restore_from(snap_file)
        store.log("admin", "undo_import", f"restored {last_batch['snapshotName']}")
        return {
            "ok": True,
            "restoredSnapshot": last_batch["snapshotName"],
            "studentsCount": students_count,
            "rev": store.rev,
        }
