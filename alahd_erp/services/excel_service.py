"""Multi-Scope Excel Workbook Export Service — Yawm Jadid Academy Suite.

Supports 9 export scopes (`full`, `teacher`, `teachers_stats`, `students`, `payroll`,
`attendance`, `memorization`, `madrasa`, `accounting`) and explicitly includes both:
- `تاريخ الدفع` (Payment Date — `paymentDate`)
- `تاريخ البداية الفعلي` (Actual Start Date — `actualStartDate`)
in student sheets and financial/study-period reports.
"""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ..domain.attendance_engine import evaluate_student_month_dates
from ..domain.finance_engine import (
    effective_month_collected,
    effective_month_net_subscription,
    effective_month_wage,
    index_installments_by_student_month,
    parse_pct_int,
    resolve_actual_start_date,
    resolve_end_date,
    resolve_payment_date,
    round_half_up,
)

TEAL_DARK = "44563F"      # brand sage (dark)  - name kept for minimal diff
TEAL_MID = "6C7C64"       # brand sage (mid)
GOLD = "B45309"
GOLD_L = "F6ECD0"
SURFACE = "F8FAFC"
C_VALUE = "FEF9C3"
C_PAY = "DBEAFE"
C_START = "E0E7FF"
C_END = "DCFCE7"
C_WAGE = "D1FAE5"
C_UNPAID = "FEE2E2"
ZEBRA = "F1F5F9"
LINE = "CBD5E1"

FONT = "Calibri"
F_TITLE = Font(name=FONT, size=15, bold=True, color="FFFFFF")
F_HEAD = Font(name=FONT, size=9.5, bold=True, color="FFFFFF")
F_MONTH = Font(name=FONT, size=10.5, bold=True, color="44563F")
F_CELL = Font(name=FONT, size=10)
F_CELL_B = Font(name=FONT, size=10, bold=True)
F_TOTAL = Font(name=FONT, size=10.5, bold=True, color="44563F")

FILL_TITLE = PatternFill("solid", fgColor=TEAL_DARK)
FILL_MONTH = PatternFill("solid", fgColor="C9A54A")
FILL_HEAD = PatternFill("solid", fgColor=TEAL_DARK)
FILL_INFO = PatternFill("solid", fgColor=TEAL_MID)
FILL_TOTAL = PatternFill("solid", fgColor=GOLD_L)
FILL_VALUE = PatternFill("solid", fgColor=C_VALUE)
FILL_PAY = PatternFill("solid", fgColor=C_PAY)
FILL_START = PatternFill("solid", fgColor=C_START)
FILL_END = PatternFill("solid", fgColor=C_END)
FILL_WAGE = PatternFill("solid", fgColor=C_WAGE)
FILL_UNPAID = PatternFill("solid", fgColor=C_UNPAID)
FILL_ZEBRA = PatternFill("solid", fgColor=ZEBRA)
FILL_SURFACE = PatternFill("solid", fgColor=SURFACE)

THIN = Side(style="thin", color=LINE)
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
RIGHT = Alignment(horizontal="right", vertical="center", wrap_text=True)

AR_MONTHS = {
    1: "يناير", 2: "فبراير", 3: "مارس", 4: "أبريل", 5: "مايو", 6: "يونيو",
    7: "يوليو", 8: "أغسطس", 9: "سبتمبر", 10: "أكتوبر", 11: "نوفمبر", 12: "ديسمبر",
}
AR_DAYS = {
    0: "الأحد", 1: "الإثنين", 2: "الثلاثاء", 3: "الأربعاء",
    4: "الخميس", 5: "الجمعة", 6: "السبت",
}
SESS_STATUS_AR = {
    "scheduled": "مجدولة", "done": "انعقدت", "absent": "غياب بلا عذر", "excused": "اعتذار مسبق",
    "late": "حضرت متأخرة", "cancel_teacher": "ألغيت من المعلمة", "cancel_admin": "ألغيت إداريًا",
    "postponed": "مؤجلة", "makeup": "تعويضية",
}
SESS_FILL = {
    "done": FILL_END, "makeup": FILL_END, "late": FILL_PAY,
    "absent": FILL_UNPAID, "cancel_teacher": FILL_UNPAID, "cancel_admin": FILL_UNPAID,
    "excused": FILL_VALUE, "postponed": FILL_VALUE, "scheduled": FILL_ZEBRA,
}


def month_label(key: str) -> str:
    try:
        y, m = key.split("-")
        return f"{AR_MONTHS[int(m)]} {y}"
    except Exception:
        return key


def _num(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _wage(stu, m, teacher_meta=None, wage_basis="net", wage_accrual_mode="accrued", collected_amount=None):
    return effective_month_wage(
        stu,
        m,
        teacher_meta=teacher_meta,
        wage_basis=wage_basis,
        wage_accrual_mode=wage_accrual_mode,
        collected_amount=collected_amount,
    )


def _collected(m, student_month_installments=None):
    # Same rule as the dashboard/reports: receipts dated in the future are not collected yet.
    return effective_month_collected(
        m,
        student_month_installments=student_month_installments,
        reference_date=date.today().isoformat(),
    )


def _school_year_label(month_keys) -> str:
    """Derives the academic-year caption (Oct -> Sep) from the data instead of a hard-coded year."""
    keys = sorted(k for k in (month_keys or []) if isinstance(k, str) and len(k) == 7 and k[4] == "-")
    try:
        y, m = (int(keys[0][:4]), int(keys[0][5:7])) if keys else (date.today().year, date.today().month)
    except ValueError:
        y, m = date.today().year, date.today().month
    start = y if m >= 10 else y - 1
    return f"السنة الدراسية: أكتوبر {start} – سبتمبر {start + 1}"


_SHEET_TITLE_FORBIDDEN = re.compile(r"[\\/?*\[\]:]")


def _safe_sheet_title(name, fallback: str = "المعلمة") -> str:
    """Turns free text (a teacher name) into a valid Excel sheet title.

    Excel forbids `\\ / ? * [ ] :` in sheet titles, forbids a leading/trailing apostrophe, and limits titles to
    31 characters. openpyxl raises ValueError for the forbidden characters, which used to abort the whole export
    for a teacher named e.g. «أ/ فاطمة». Titles that were already valid are returned unchanged.
    """
    title = _SHEET_TITLE_FORBIDDEN.sub("-", str(name or ""))[:31].strip("'")
    return title if title.strip() else fallback


def _neutralize_formulas(wb) -> None:
    """Forces every cell that openpyxl would store as a formula (text starting with '=') to plain text.

    Student/teacher names, notes and categories are user input; without this a name such as
    `=HYPERLINK(...)` becomes a live formula in the exported workbook (spreadsheet injection).
    The exporter itself never writes formulas.
    """
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.data_type == "f":
                    c.data_type = "s"


def _net_val(m):
    if not isinstance(m, dict) or m.get("value") is None or m.get("value") == "":
        return None
    return effective_month_net_subscription(m)


def _style_cell(c, fill=None, font=F_CELL, align=CENTER, border=True, fmt=None):
    if fill is not None:
        c.fill = fill
    c.font = font
    c.alignment = align
    if border:
        c.border = BORDER
    if fmt:
        c.number_format = fmt


def _sheet_rtl(ws):
    ws.sheet_view.rightToLeft = True
    ws.sheet_view.showGridLines = False


def _add_logo(ws, logo_path, last_col=None):
    try:
        if not logo_path:
            return
        p = Path(logo_path)
        if not p.exists():
            return
        img = XLImage(str(p))
        img.height = 30
        img.width = 30
        ws.add_image(img, f"{get_column_letter(1)}1")
    except Exception:
        pass


def _students_sheet(
    wb,
    state,
    students,
    sheet_name,
    header_title,
    subtitle,
    tab_color=TEAL_DARK,
    first=False,
    filter_month=None,
):
    """Generates the monthly student subscription grid with 5 columns per month:
    [قيمة الاشتراك, تاريخ الدفع (Payment Date), تاريخ البداية الفعلي (Actual Start Date), تاريخ النهاية, أجر المعلمة]
    """
    ws = wb.active if first else wb.create_sheet()
    ws.title = _safe_sheet_title(sheet_name)
    ws.sheet_properties.tabColor = tab_color
    _sheet_rtl(ws)

    info_heads = ["م", "اسم الطالب", "رقم التواصل", "اسم المعلمة", "نوع الاشتراك", "نسبة المعلمة"]
    sub_heads = ["قيمة الاشتراك", "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية", "أجر المعلمة"]
    n_sub = len(sub_heads)
    month_keys = []
    if filter_month:
        month_keys = [filter_month]
    else:
        for s in students:
            for k in (s.get("months") or {}):
                if k not in month_keys:
                    month_keys.append(k)
        month_keys = sorted(month_keys) or [date.today().strftime("%Y-%m")]

    n_info = len(info_heads)
    n_cols = n_info + len(month_keys) * n_sub
    last_col = get_column_letter(n_cols)

    settings = state.get("settings") or {}
    acad_name = settings.get("academyName") or "أكاديمية يوم جديد"
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")

    ws.merge_cells(f"A1:{last_col}1")
    t = ws["A1"]
    t.value = acad_name + " — " + header_title
    _style_cell(t, FILL_TITLE, F_TITLE, Alignment(horizontal="center", vertical="center"), border=False)
    ws.row_dimensions[1].height = 36
    for c in range(2, n_cols + 1):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    ws.merge_cells("A2:F2")
    y = ws["A2"]
    y.value = subtitle + "   |   تاريخ الدفع (مالي) مستقل تمامًا عن تاريخ البداية الفعلي (دراسي):"
    _style_cell(y, FILL_SURFACE, Font(name=FONT, size=10, bold=True, color=TEAL_DARK), RIGHT, border=False)
    ws.row_dimensions[2].height = 20

    for i, h in enumerate(info_heads, start=1):
        ws.merge_cells(start_row=3, start_column=i, end_row=4, end_column=i)
        cell = ws.cell(row=3, column=i, value=h)
        _style_cell(cell, FILL_INFO if i > 1 else FILL_HEAD, F_HEAD, CENTER)
        ws.cell(row=4, column=i).fill = FILL_INFO if i > 1 else FILL_HEAD

    for mi, mk in enumerate(month_keys):
        c1 = n_info + 1 + mi * n_sub
        ws.merge_cells(start_row=3, start_column=c1, end_row=3, end_column=c1 + n_sub - 1)
        cell = ws.cell(row=3, column=c1, value=month_label(mk))
        _style_cell(cell, FILL_MONTH, F_MONTH, CENTER)
        for cc in range(c1 + 1, c1 + n_sub):
            ws.cell(row=3, column=cc).fill = FILL_MONTH
        for si, sh in enumerate(sub_heads):
            cell = ws.cell(row=4, column=c1 + si, value=sh)
            _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)

    ws.row_dimensions[3].height = 22
    ws.row_dimensions[4].height = 30

    teacher_meta = state.get("teacherMeta") or {}
    inst_map = index_installments_by_student_month(state.get("installments") or [])

    r = 5
    for idx, s in enumerate(students):
        zebra = FILL_ZEBRA if idx % 2 else None
        row_vals = [
            idx + 1,
            s.get("name", ""),
            s.get("phone", "") or "",
            s.get("teacher", "") or "بدون معلمة",
            s.get("type", "") or "",
            parse_pct_int(s.get("pct")) / 100.0,
        ]
        for i, v in enumerate(row_vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            if i == 1:
                _style_cell(cell, zebra, F_CELL_B)
            elif i == 2:
                _style_cell(cell, zebra, F_CELL_B, RIGHT)
            elif i == 3:
                _style_cell(cell, zebra, F_CELL, CENTER)
                cell.number_format = "@"
            elif i == 6:
                _style_cell(cell, zebra, F_CELL, CENTER, fmt="0%")
            else:
                _style_cell(cell, zebra, F_CELL, CENTER if i != 5 else RIGHT)

        for mi, mk in enumerate(month_keys):
            m = (s.get("months") or {}).get(mk) or {}
            c1 = n_info + 1 + mi * n_sub
            v = _net_val(m)
            pay_dt = resolve_payment_date(m)
            start_dt = resolve_actual_start_date(m)
            end_dt = resolve_end_date(m)
            coll_m = _collected(m, inst_map.get((s.get("id"), mk)))
            w = _wage(
                s,
                m,
                teacher_meta=teacher_meta,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                collected_amount=coll_m,
            )

            cell_v = ws.cell(row=r, column=c1, value=v if v is not None else None)
            _style_cell(cell_v, FILL_VALUE, F_CELL, CENTER, fmt="#,##0")

            cell_p = ws.cell(row=r, column=c1 + 1, value=pay_dt or None)
            unpaid = (v or 0) > 0 and coll_m <= 0
            _style_cell(
                cell_p,
                FILL_UNPAID if unpaid else FILL_PAY,
                Font(name=FONT, size=9, color="B91C1C" if unpaid else "1D4ED8", bold=False),
                CENTER,
            )

            cell_s = ws.cell(row=r, column=c1 + 2, value=start_dt or None)
            _style_cell(cell_s, FILL_START, Font(name=FONT, size=9, color="3730A3"), CENTER)

            cell_e = ws.cell(row=r, column=c1 + 3, value=end_dt or None)
            _style_cell(cell_e, FILL_END, Font(name=FONT, size=9, color="166534"), CENTER)

            has_wage = ((v or 0) > 0) or (w > 0)
            cell_w = ws.cell(row=r, column=c1 + 4, value=round_half_up(w) if has_wage else None)
            _style_cell(cell_w, FILL_WAGE, Font(name=FONT, size=9.5, bold=True, color="065F46"), CENTER, fmt="#,##0")
        r += 1

    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=n_info)
    tot = ws.cell(row=r, column=1, value="الإجمالي")
    _style_cell(tot, FILL_TOTAL, F_TOTAL, CENTER)
    for c in range(2, n_info + 1):
        ws.cell(row=r, column=c).fill = FILL_TOTAL
    for mi, mk in enumerate(month_keys):
        c1 = n_info + 1 + mi * n_sub
        sv = sum((_net_val((s.get("months") or {}).get(mk, {})) or 0) for s in students)
        sw = sum(
            _wage(
                s,
                (s.get("months") or {}).get(mk) or {},
                teacher_meta=teacher_meta,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                collected_amount=_collected((s.get("months") or {}).get(mk) or {}, inst_map.get((s.get("id"), mk))),
            )
            for s in students
            if (
                (_net_val((s.get("months") or {}).get(mk, {})) or 0) > 0
                or _wage(
                    s,
                    (s.get("months") or {}).get(mk) or {},
                    teacher_meta=teacher_meta,
                    wage_basis=wage_basis,
                    wage_accrual_mode=wage_accrual_mode,
                    collected_amount=_collected((s.get("months") or {}).get(mk) or {}, inst_map.get((s.get("id"), mk))),
                ) > 0
            )
        )
        cv = ws.cell(row=r, column=c1, value=round_half_up(sv))
        _style_cell(cv, FILL_TOTAL, F_TOTAL, CENTER, fmt="#,##0")
        for cc in (c1 + 1, c1 + 2, c1 + 3):
            cell = ws.cell(row=r, column=cc)
            _style_cell(cell, FILL_TOTAL, F_TOTAL)
        cw = ws.cell(row=r, column=c1 + 4, value=round_half_up(sw))
        _style_cell(cw, FILL_TOTAL, F_TOTAL, CENTER, fmt="#,##0")
    ws.row_dimensions[r].height = 24

    widths = [5, 22, 14, 15, 18, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for mi in range(len(month_keys)):
        for si, w in enumerate([11, 12, 13, 12, 10.5]):
            ws.column_dimensions[get_column_letter(n_info + 1 + mi * n_sub + si)].width = w
    ws.freeze_panes = "G5"
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:4"
    return ws, month_keys


def _dates_separation_sheet(wb, state, students, month_keys, first=False):
    """Dedicated sheet auditing Payment Date (`paymentDate`) vs. Actual Start Date (`actualStartDate`)."""
    ws = wb.active if first else wb.create_sheet("تواريخ الدفع والبداية")
    if first:
        ws.title = "تواريخ الدفع والبداية"
    ws.sheet_properties.tabColor = "4F46E5"
    _sheet_rtl(ws)

    widths = [6, 22, 16, 18, 13, 13, 15, 15, 15, 14, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.merge_cells("A1:K1")
    t = ws["A1"]
    t.value = "سجل تتبع تواريخ الدفع (المالية) وتواريخ البداية الفعلية (الدراسية)"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 12):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    heads = [
        "م", "اسم الطالبة", "المعلمة", "نوع الاشتراك", "الشهر", "قيمة الاشتراك",
        "تاريخ الدفع (Payment Date)", "تاريخ البداية الفعلي (Actual Start)",
        "تاريخ نهاية الدورة", "مدة الدراسة (يوم)", "تصنيف السداد والبدء",
    ]
    for i, htxt in enumerate(heads, start=1):
        c = ws.cell(row=3, column=i, value=htxt)
        _style_cell(c, FILL_HEAD, F_HEAD, CENTER)
    ws.row_dimensions[3].height = 26

    r = 4
    seq = 1
    for s in students:
        for mk in month_keys:
            m = (s.get("months") or {}).get(mk)
            if not isinstance(m, dict):
                continue
            v = _num(m.get("value")) or 0
            pay_dt = resolve_payment_date(m)
            start_dt = resolve_actual_start_date(m)
            end_dt = resolve_end_date(m)
            if v <= 0 and not pay_dt and not start_dt and not end_dt:
                continue
            eval_info = evaluate_student_month_dates(m)
            timing = "غير مسدد"
            if pay_dt:
                if start_dt and pay_dt < start_dt:
                    timing = "دفع مسبق قبل بدء الدراسة"
                elif start_dt and pay_dt > start_dt:
                    timing = "دفع لاحق بعد بدء الدراسة"
                elif start_dt and pay_dt == start_dt:
                    timing = "دفع في يوم بدء الدراسة"
                else:
                    timing = "مسدد"
            vals = [
                seq,
                s.get("name", ""),
                s.get("teacher", "") or "بدون معلمة",
                s.get("type", "") or "",
                month_label(mk),
                round_half_up(v),
                pay_dt or "—",
                start_dt or "—",
                eval_info["endDate"] or "—",
                eval_info["studyPeriod"]["durationDays"] or 30,
                timing,
            ]
            for ci, val in enumerate(vals, start=1):
                c = ws.cell(row=r, column=ci, value=val)
                fill = FILL_PAY if ci == 7 else (FILL_START if ci == 8 else (FILL_END if ci == 9 else (FILL_ZEBRA if seq % 2 == 0 else None)))
                _style_cell(c, fill, F_CELL_B if ci in (2, 7, 8) else F_CELL, RIGHT if ci in (2, 3, 4, 11) else CENTER, fmt="#,##0" if ci == 6 else None)
            r += 1
            seq += 1

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return ws


def _monthly_teachers_comparative_sheet(wb, state, students, teachers, first=False):
    """Per-teacher comparative payroll across *every* month that has subscription data.

    (Previously hard-coded to 2026-10 and 2026-11, so from December onwards the totals silently
    omitted all later months.)  Uses NET subscriptions and the wage policy from settings.
    """
    ws = wb.active if first else wb.create_sheet("رواتب المعلمات الشهرية")
    if first:
        ws.title = "رواتب المعلمات الشهرية"
    ws.sheet_properties.tabColor = TEAL_MID
    _sheet_rtl(ws)

    settings = state.get("settings") or {}
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")
    meta = state.get("teacherMeta") or {}
    inst_map = index_installments_by_student_month(state.get("installments") or [])

    month_keys = sorted({
        mk
        for st_ in students
        for mk, m in (st_.get("months") or {}).items()
        if isinstance(m, dict) and (_num(m.get("value")) or 0) > 0
    })[-12:] or [date.today().strftime("%Y-%m")]

    n_cols = 3 + 3 * len(month_keys) + 2
    widths = [5, 18, 11] + [11, 14, 13] * len(month_keys) + [16, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    last_col = get_column_letter(n_cols)

    ws.merge_cells(f"A1:{last_col}1")
    t = ws["A1"]
    t.value = "كشف رواتب المعلمات الشهرية المقارن — أكاديمية يوم جديد"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 36
    for c in range(2, n_cols + 1):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    ws.merge_cells(f"A2:{last_col}2")
    sub = ws["A2"]
    sub.value = "تُحسب الرواتب تلقائيًا من سجلات الطالبات لكل شهر على حدة بقاعدة Round Half Up (الاشتراكات بعد الخصم)"
    _style_cell(sub, FILL_SURFACE, Font(name=FONT, size=10, bold=True, color=TEAL_DARK), CENTER, border=False)
    ws.row_dimensions[2].height = 22
    for c in range(2, n_cols + 1):
        ws.cell(row=2, column=c).fill = FILL_SURFACE

    for col_idx, htxt in ((1, "م"), (2, "اسم المعلمة"), (3, "النسبة الافتراضية")):
        ws.merge_cells(start_row=3, start_column=col_idx, end_row=4, end_column=col_idx)
        c = ws.cell(row=3, column=col_idx, value=htxt)
        _style_cell(c, FILL_HEAD, F_HEAD, CENTER)
        ws.cell(row=4, column=col_idx).fill = FILL_HEAD

    for mi, mk in enumerate(month_keys):
        c0 = 4 + mi * 3
        ws.merge_cells(start_row=3, start_column=c0, end_row=3, end_column=c0 + 2)
        c = ws.cell(row=3, column=c0, value=month_label(mk))
        _style_cell(c, FILL_MONTH, F_MONTH, CENTER)
        for cc in (c0 + 1, c0 + 2):
            ws.cell(row=3, column=cc).fill = FILL_MONTH
        for off, htxt in enumerate(("عدد الطلاب", "صافي الاشتراكات", f"راتب {AR_MONTHS.get(int(mk[5:7]), mk)}")):
            hc = ws.cell(row=4, column=c0 + off, value=htxt)
            _style_cell(hc, FILL_INFO, F_HEAD, CENTER)

    for col_idx, htxt in ((n_cols - 1, "إجمالي رواتب المعلمة"), (n_cols, "صافي الأكاديمية من المعلمة")):
        ws.merge_cells(start_row=3, start_column=col_idx, end_row=4, end_column=col_idx)
        c = ws.cell(row=3, column=col_idx, value=htxt)
        _style_cell(c, FILL_HEAD, F_HEAD, CENTER)
        ws.cell(row=4, column=col_idx).fill = FILL_HEAD
    ws.row_dimensions[3].height = 22
    ws.row_dimensions[4].height = 28

    teacher_list = list(teachers)
    if any(not (s_.get("teacher") or "").strip() for s_ in students):
        teacher_list.append("بدون معلمة")

    r = 5
    col_tot_n = [0] * len(month_keys)
    col_tot_s = [0] * len(month_keys)
    col_tot_w = [0] * len(month_keys)
    for idx, tname in enumerate(teacher_list, start=1):
        is_unassigned = tname == "بدون معلمة"
        sub_studs = [
            s_ for s_ in students
            if ((not (s_.get("teacher") or "").strip()) if is_unassigned else (s_.get("teacher") == tname))
        ]
        per_month_vals = []
        for mi, mk in enumerate(month_keys):
            n = subs = wages = 0
            for s_ in sub_studs:
                m = (s_.get("months") or {}).get(mk) or {}
                net_v = _net_val(m) or 0
                coll = _collected(m, inst_map.get((s_.get("id"), mk)))
                w = _wage(
                    s_, m, teacher_meta=meta, wage_basis=wage_basis,
                    wage_accrual_mode=wage_accrual_mode, collected_amount=coll,
                )
                if net_v <= 0 and w <= 0:
                    continue
                if (_num(m.get("value")) or 0) > 0:
                    n += 1
                subs += net_v
                wages += w
            per_month_vals.append((n, subs, wages))
            col_tot_n[mi] += n
            col_tot_s[mi] += subs
            col_tot_w[mi] += wages

        tot_w = sum(v[2] for v in per_month_vals)
        net_acad = sum(v[1] for v in per_month_vals) - tot_w
        t_pct = None
        if not is_unassigned:
            raw_pct = (meta.get(tname) or {}).get("pct")
            t_pct = parse_pct_int(raw_pct if raw_pct not in (None, "") else 70, default=70) / 100.0

        vals = [idx if not is_unassigned else "—", tname if not is_unassigned else "بدون معلمة (مجموعات)", t_pct]
        for n, subs, wages in per_month_vals:
            vals.extend([n, subs, wages])
        vals.extend([tot_w, net_acad])

        zebra = FILL_ZEBRA if (idx % 2 == 0) else None
        for ci, val in enumerate(vals, start=1):
            c = ws.cell(row=r, column=ci, value=val)
            in_months = 4 <= ci <= 3 + 3 * len(month_keys)
            kind = (ci - 4) % 3 if in_months else -1  # 0 count, 1 subs, 2 wage
            fmt = "0%" if ci == 3 else ("#,##0" if (kind in (1, 2) or ci >= n_cols - 1) else None)
            if kind == 2 or ci == n_cols - 1:
                fill = FILL_WAGE
            elif kind == 1:
                fill = FILL_VALUE
            elif ci == n_cols:
                fill = FILL_END
            else:
                fill = zebra
            bold = ci == 2 or kind == 2 or ci >= n_cols - 1
            _style_cell(c, fill, F_CELL_B if bold else F_CELL, RIGHT if ci == 2 else CENTER, fmt=fmt)
        ws.row_dimensions[r].height = 22
        r += 1

    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    c_tot = ws.cell(row=r, column=1, value="الإجمالي العام")
    _style_cell(c_tot, FILL_TOTAL, F_TOTAL, CENTER)
    for cc in (2, 3):
        ws.cell(row=r, column=cc).fill = FILL_TOTAL
    for mi in range(len(month_keys)):
        c0 = 4 + mi * 3
        for off, val in enumerate((col_tot_n[mi], col_tot_s[mi], col_tot_w[mi])):
            c = ws.cell(row=r, column=c0 + off, value=val)
            _style_cell(c, FILL_TOTAL, F_TOTAL, CENTER, fmt="#,##0")
    grand_w = sum(col_tot_w)
    for ci, val in ((n_cols - 1, grand_w), (n_cols, sum(col_tot_s) - grand_w)):
        c = ws.cell(row=r, column=ci, value=val)
        _style_cell(c, FILL_TOTAL, F_TOTAL, CENTER, fmt="#,##0")
    ws.row_dimensions[r].height = 25

    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    return ws


def _summary_sheet(wb, state, students, per_month, per_teacher, payroll, first=False):
    ws = wb.active if first else wb.create_sheet("ملخص")
    if first:
        ws.title = "ملخص"
    ws.sheet_properties.tabColor = GOLD
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [24, 15, 15, 15, 15, 15, 15, 15]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "الملخص المالي — أكاديمية يوم جديد"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    named = [s for s in students if (s.get("name") or "").strip()]
    total_subs = sum(m["value"] for m in per_month)
    total_coll = sum(m["collected"] for m in per_month)
    total_wages = sum(m["wages"] for m in per_month)
    paid_out = sum(p.get("amount", 0) or 0 for p in payroll)

    kpis = [
        ("عدد سجلات الطالبات", len(named), FILL_WAGE),
        ("إجمالي الاشتراكات المسجلة", round_half_up(total_subs), FILL_VALUE),
        ("المحصّل (حسب تاريخ الدفع)", round_half_up(total_coll), FILL_PAY),
        ("غير المحصّل", round_half_up(total_subs - total_coll), FILL_UNPAID),
        ("إجمالي أجور المعلمات", round_half_up(total_wages), FILL_WAGE),
        ("صافي الأكاديمية", round_half_up(total_subs - total_wages), FILL_END),
        ("مصروف مرتبات المعلمات", round_half_up(paid_out), FILL_PAY),
        ("متبقي مرتبات المعلمات", round_half_up(total_wages - paid_out), FILL_UNPAID),
    ]
    r = 3
    for i in range(0, len(kpis), 2):
        for j, (label, value, fill) in enumerate(kpis[i:i + 2]):
            c1 = 1 + j * 4
            ws.merge_cells(start_row=r, start_column=c1, end_row=r, end_column=c1 + 1)
            lc = ws.cell(row=r, column=c1, value=label)
            _style_cell(lc, FILL_SURFACE, F_CELL_B, RIGHT)
            ws.cell(row=r, column=c1 + 1).fill = FILL_SURFACE
            ws.merge_cells(start_row=r, start_column=c1 + 2, end_row=r, end_column=c1 + 3)
            vc = ws.cell(row=r, column=c1 + 2, value=value)
            _style_cell(vc, fill, Font(name=FONT, size=13, bold=True, color=TEAL_DARK), CENTER, fmt="#,##0")
            ws.cell(row=r, column=c1 + 3).fill = fill
        ws.row_dimensions[r].height = 26
        r += 1

    r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
    h = ws.cell(row=r, column=1, value="الحركة الشهرية على مدار السنة الدراسية")
    _style_cell(h, FILL_HEAD, F_HEAD, CENTER)
    for c in range(2, 9):
        ws.cell(row=r, column=c).fill = FILL_HEAD
    r += 1
    heads = ["الشهر", "قيمة الاشتراكات", "المحصّل", "غير المحصّل", "أجور المعلمات", "صافي الأكاديمية", "عدد المشتركات", ""]
    for i, htxt in enumerate(heads[:-1], start=1):
        cell = ws.cell(row=r, column=i, value=htxt)
        _style_cell(cell, FILL_INFO, F_HEAD, CENTER)
    ws.cell(row=r, column=8).fill = FILL_INFO
    r += 1
    for m in per_month:
        vals = [
            m["label"], round_half_up(m["value"]), round_half_up(m["collected"]),
            round_half_up(m["value"] - m["collected"]), round_half_up(m["wages"]),
            round_half_up(m["value"] - m["wages"]), m["count"],
        ]
        for i, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=i, value=v)
            _style_cell(
                cell, FILL_ZEBRA if (r % 2) else None, F_CELL_B if i == 1 else F_CELL,
                RIGHT if i == 1 else CENTER, fmt="#,##0" if isinstance(v, (int, float)) else None,
            )
        r += 1
    vals = [
        "الإجمالي", round_half_up(total_subs), round_half_up(total_coll),
        round_half_up(total_subs - total_coll), round_half_up(total_wages),
        round_half_up(total_subs - total_wages), sum(m["count"] for m in per_month),
    ]
    for i, v in enumerate(vals, start=1):
        cell = ws.cell(row=r, column=i, value=v)
        _style_cell(cell, FILL_TOTAL, F_TOTAL, CENTER, fmt="#,##0" if i > 1 else None)
    return ws


def _payroll_sheet(wb, state, per_month, payroll, first=False):
    ws = wb.active if first else wb.create_sheet("مرتبات المعلمات")
    if first:
        ws.title = "مرتبات المعلمات"
    ws.sheet_properties.tabColor = "0D7A56"
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [15, 17, 11, 15, 15, 15, 15, 22]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "كشف مرتبات المعلمات الشهري وسجل الصرف"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    r = 3
    heads = ["الشهر", "المعلمة", "الطالبات", "الاشتراكات", "المحصّل", "المستحق", "المصروف", "المتبقي"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=r, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r += 1
    for m in per_month:
        for t_row in m["teachers"]:
            vals = [
                m["label"], t_row["name"], t_row["n"], round_half_up(t_row["subs"]),
                round_half_up(t_row["collected"]), round_half_up(t_row["wages"]),
                round_half_up(t_row.get("paid", 0)),
                round_half_up(t_row["wages"] - t_row.get("paid", 0)),
            ]
            for i, v in enumerate(vals, start=1):
                cell = ws.cell(row=r, column=i, value=v)
                remain_neg = i == 8 and isinstance(v, (int, float)) and v > 0
                _style_cell(
                    cell, FILL_UNPAID if remain_neg else (FILL_ZEBRA if (r % 2) else None),
                    F_CELL_B if i in (6, 8) else F_CELL, RIGHT if i <= 2 else CENTER,
                    fmt="#,##0" if i >= 4 else None,
                )
            r += 1
    return ws


def _teachers_sheet(wb, state, per_teacher, payroll, first=False):
    ws = wb.active if first else wb.create_sheet("المعلمات")
    if first:
        ws.title = "المعلمات"
    ws.sheet_properties.tabColor = TEAL_MID
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [6, 18, 13, 12, 15, 15, 13, 26]):
        ws.column_dimensions[col].width = w
    meta = state.get("teacherMeta") or {}

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "بيانات المعلمات وأجورها"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    heads = ["م", "اسم المعلمة", "النسبة الافتراضية", "عدد السجلات", "إجمالي الأجور", "المصروف لها", "المتبقي", "ملاحظات"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    ws.row_dimensions[3].height = 24
    r = 4
    for i, t_row in enumerate(per_teacher, start=1):
        m = meta.get(t_row["name"]) or {}
        paid = sum((_num(p.get("amount")) or 0) for p in payroll if p.get("teacher") == t_row["name"])
        default_pct = (m.get("pct", 70) or 70) / 100.0 if isinstance(m.get("pct", 70), (int, float)) else 0.7
        if t_row["name"] == "بدون معلمة":
            default_pct = 0.0
        vals = [
            i, t_row["name"], default_pct, t_row["n"], round_half_up(t_row["wages"]),
            round_half_up(paid), round_half_up(t_row["wages"] - paid), m.get("notes", "") or "",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            fmt = "0%" if ci == 3 else ("#,##0" if ci in (5, 6, 7) else None)
            _style_cell(
                cell, FILL_ZEBRA if (r % 2) else None, F_CELL_B if ci == 2 else F_CELL,
                RIGHT if ci in (2, 8) else CENTER, fmt=fmt,
            )
        r += 1
    return ws


def _sessions_sheet(wb, state, students, sessions, groups, first=False):
    ws = wb.active if first else wb.create_sheet("الحلقات والمواعيد")
    if first:
        ws.title = "الحلقات والمواعيد"
    ws.sheet_properties.tabColor = TEAL_MID
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [12, 11, 9, 20, 15, 18, 16, 24]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "جدول الحلقات والمواعيد وتسجيل الحضور"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    name_by_id = {s.get("id"): s.get("name", "") for s in students}
    group_name = {g.get("id"): g.get("name", "") for g in groups}
    heads = ["التاريخ", "اليوم", "الوقت", "الطالبة", "المعلمة", "الحلقة/المجموعة", "الحالة", "ملاحظة"]
    for i, h in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=h)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r = 4
    for s in sorted(sessions, key=lambda x: (str(x.get("date")), str(x.get("time")))):
        wd = ""
        try:
            y, mo, d = str(s.get("date")).split("-")
            wd = AR_DAYS.get((date(int(y), int(mo), int(d)).weekday() + 1) % 7, "")
        except Exception:
            wd = ""
        status = s.get("status") if s.get("status") in SESS_STATUS_AR else "scheduled"
        vals = [
            s.get("date") or "", wd, s.get("time") or "",
            name_by_id.get(s.get("studentId"), "؟"), s.get("teacher") or "",
            group_name.get(s.get("groupId"), "") or "فردي",
            SESS_STATUS_AR[status], s.get("notes") or "",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            fill = SESS_FILL.get(status) if ci == 7 else (FILL_ZEBRA if (r % 2) else None)
            _style_cell(cell, fill, F_CELL_B if ci in (4, 7) else F_CELL, RIGHT if ci in (4, 5, 8) else CENTER)
        r += 1
    return ws


def _accounting_sheet(wb, state, per_month, payroll, transactions, first=False):
    ws = wb.active if first else wb.create_sheet("المحاسبة والمصروفات")
    if first:
        ws.title = "المحاسبة والمصروفات"
    ws.sheet_properties.tabColor = "2A5CA8"
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [14, 14, 18, 16, 15, 16, 26, 14]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "المحاسبة العامة للمصروفات والإيرادات وصافي الخزينة"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    heads = ["التاريخ", "الشهر", "النوع", "التصنيف / البند", "المبلغ", "وسيلة الدفع", "البيان / الملاحظة", "المعرّف"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r = 4
    for tx in sorted(transactions, key=lambda x: str(x.get("date") or ""), reverse=True):
        is_rev = tx.get("type") == "revenue"
        mk = tx.get("month") or str(tx.get("date") or "")[:7]
        vals = [
            tx.get("date") or "", month_label(mk),
            "إيراد إضافي (+)" if is_rev else "مصروف تشغيلي (-)",
            tx.get("category") or "", round_half_up(_num(tx.get("amount")) or 0),
            tx.get("method") or "نقدي", tx.get("note") or "", tx.get("id") or "",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            fill = (FILL_END if is_rev else FILL_UNPAID) if ci in (3, 5) else (FILL_ZEBRA if (r % 2) else None)
            _style_cell(cell, fill, F_CELL_B if ci in (3, 4, 5) else F_CELL, RIGHT if ci in (4, 7) else CENTER, fmt="#,##0" if ci == 5 else None)
        r += 1
    return ws


def _installments_sheet(wb, state, students, installments, first=False):
    ws = wb.active if first else wb.create_sheet("إيصالات الدفعات الجزئية")
    if first:
        ws.title = "إيصالات الدفعات الجزئية"
    ws.sheet_properties.tabColor = GOLD
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGH", [18, 13, 20, 16, 15, 14, 15, 26]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "سجل الدفعات الجزئية المتعددة وإيصالات السداد المستقلة"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 9):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    stu_by_id = {s.get("id"): s for s in students}
    heads = ["رقم الإيصال", "تاريخ الدفع", "الطالبة", "المعلمة", "شهر الاشتراك", "المبلغ المسدد", "طريقة الدفع", "ملاحظة"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r = 4
    for inst in sorted(installments, key=lambda x: (str(x.get("date") or ""), str(x.get("receiptNo") or ""))):
        stu = stu_by_id.get(inst.get("studentId")) or {}
        amt = _num(inst.get("amount")) or 0.0
        vals = [
            inst.get("receiptNo") or "", inst.get("paymentDate") or inst.get("date") or "",
            stu.get("name") or inst.get("studentName") or "؟", stu.get("teacher") or "",
            month_label(inst.get("month") or ""), round_half_up(amt),
            inst.get("method") or "فودافون كاش", inst.get("note") or "",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            _style_cell(cell, FILL_PAY if ci == 6 else (FILL_ZEBRA if (r % 2) else None), F_CELL_B if ci in (1, 3, 6) else F_CELL, RIGHT if ci in (3, 4, 8) else CENTER, fmt="#,##0" if ci == 6 else None)
        r += 1
    return ws


def _memorization_sheet(wb, state, students, memorization, first=False):
    ws = wb.active if first else wb.create_sheet("الحفظ والمراجعة")
    if first:
        ws.title = "الحفظ والمراجعة"
    ws.sheet_properties.tabColor = TEAL_DARK
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGHIJ", [13, 20, 16, 15, 18, 18, 9, 11, 15, 24]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:J1")
    t = ws["A1"]
    t.value = "سجل متابعة الحفظ والمراجعة والتقييم القرآني للطالبات"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 11):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    stu_by_id = {s.get("id"): s for s in students}
    heads = ["التاريخ", "الطالبة", "المعلمة", "المسار", "من (سورة / آية)", "إلى (سورة / آية)", "الجزء", "الأوجه", "التقدير والدرجة", "ملاحظات التجويد والتقييم"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r = 4
    for m in sorted(memorization, key=lambda x: str(x.get("date") or ""), reverse=True):
        stu = stu_by_id.get(m.get("studentId")) or {}
        from_txt = f"{m.get('fromSurah') or ''} ({m.get('fromAyah') or 1})" if m.get("fromSurah") else ""
        to_txt = f"{m.get('toSurah') or m.get('fromSurah') or ''} ({m.get('toAyah') or ''})" if (m.get("toSurah") or m.get("toAyah")) else ""
        grade = m.get("grade") or "ممتاز متقن"
        score = m.get("score")
        score_num = _num(score) if (score is not None and score != "") else None
        score_disp = (int(score_num) if score_num == int(score_num) else round(score_num, 1)) if score_num is not None else score
        score_denom = 100 if (score_num is not None and score_num > 10) else 10
        grade_txt = f"{grade} ({score_disp}/{score_denom})" if score_num is not None else grade
        vals = [
            m.get("date") or "", stu.get("name") or "؟", m.get("teacher") or stu.get("teacher") or "",
            m.get("track") or "حفظ جديد", from_txt, to_txt, m.get("juz") or "",
            _num(m.get("pages")) or 0, grade_txt, m.get("notes") or "",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            _style_cell(cell, FILL_END if (ci == 9 and "ممتاز" in grade) else (FILL_ZEBRA if (r % 2) else None), F_CELL_B if ci in (2, 4, 9) else F_CELL, RIGHT if ci in (2, 3, 5, 6, 10) else CENTER)
        r += 1
    return ws


def _madrasa_sheet(wb, state, students, batches, enrollments, logs, first=False):
    ws = wb.active if first else wb.create_sheet("المدرسة الإيمانية")
    if first:
        ws.title = "المدرسة الإيمانية"
    ws.sheet_properties.tabColor = GOLD
    _sheet_rtl(ws)
    for col, w in zip("ABCDEFGHIJ", [18, 20, 16, 13, 14, 15, 15, 15, 22, 24]):
        ws.column_dimensions[col].width = w

    ws.merge_cells("A1:J1")
    t = ws["A1"]
    t.value = "المدرسة الإيمانية — «يوم لا يخلو من القرآن»"
    _style_cell(t, FILL_TITLE, F_TITLE, CENTER, border=False)
    ws.row_dimensions[1].height = 34
    for c in range(2, 11):
        ws.cell(row=1, column=c).fill = FILL_TITLE

    stu_by_id = {s.get("id"): s for s in students}
    batch_by_id = {b.get("id"): b for b in batches}
    heads = ["الدفعة", "الطالبة", "المشرفة التربوية", "رسوم الدفعة", "أيام الحماسة", "المسار القرآني", "المسار التعبدي", "المسار العلمي", "المسار التربوي", "الأوسمة"]
    for i, htxt in enumerate(heads, start=1):
        cell = ws.cell(row=3, column=i, value=htxt)
        _style_cell(cell, FILL_HEAD, F_HEAD, CENTER)
    r = 4
    for enr in enrollments:
        stu = stu_by_id.get(enr.get("studentId")) or {}
        b = batch_by_id.get(enr.get("batchId")) or {}
        tp = enr.get("tracksProgress") or {}
        badges = " ، ".join(enr.get("badges") or [])
        vals = [
            b.get("name") or "دفعة الفجر - الأولى", stu.get("name") or "؟",
            b.get("supervisor") or "مشرفة الدفعة", round_half_up(_num(enr.get("feePaid")) or _num(b.get("fee")) or 60),
            int(enr.get("streak") or 0), f"{tp.get('qurani', 0)}%", f"{tp.get('taabudi', 0)}%",
            f"{tp.get('ilmi', 0)}%", f"{tp.get('tarbawi', 0)}%", badges or "ملتزمة",
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=ci, value=v)
            _style_cell(cell, FILL_ZEBRA if (r % 2) else None, F_CELL_B if ci in (1, 2, 5) else F_CELL, RIGHT if ci in (1, 2, 3, 10) else CENTER)
        r += 1
    return ws


def build_workbook(
    state: dict,
    logo_path=None,
    scope: str = "full",
    teacher: str | None = None,
    month: str | None = None,
) -> Workbook:
    students = [s for s in (state.get("students") or []) if (s.get("name") or "").strip()]
    if teacher:
        if teacher == "بدون معلمة":
            students = [s for s in students if not (s.get("teacher") or "").strip()]
        else:
            students = [s for s in students if s.get("teacher") == teacher]

    teachers = state.get("teachers") or []
    if teacher and teacher in teachers:
        teachers = [teacher]
    payroll = state.get("payouts") or []
    if teacher:
        payroll = [p for p in payroll if p.get("teacher") == teacher]

    month_keys = []
    if month:
        month_keys = [month]
    else:
        for s in students:
            for k in (s.get("months") or {}):
                if k not in month_keys:
                    month_keys.append(k)
        month_keys = sorted(month_keys) or [date.today().strftime("%Y-%m")]

    year_label = _school_year_label(month_keys)
    teacher_meta = state.get("teacherMeta") or {}
    inst_map = index_installments_by_student_month(state.get("installments") or [])
    settings = state.get("settings") or {}
    wage_basis = str(settings.get("wageBasis") or "net")
    wage_accrual_mode = str(settings.get("wageAccrualMode") or "accrued")

    per_month = []
    for mk in month_keys:
        teachers_map: dict[str, Any] = {}
        value = collected = wages = 0.0
        count = 0
        for s in students:
            m = (s.get("months") or {}).get(mk) or {}
            v = float(_net_val(m) or 0)
            coll_m = _collected(m, inst_map.get((s.get("id"), mk)))
            w = _wage(
                s,
                m,
                teacher_meta=teacher_meta,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                collected_amount=coll_m,
            )
            if v <= 0 and w <= 0:
                continue
            tname = s.get("teacher") or "بدون معلمة"
            o = teachers_map.setdefault(tname, {"name": tname, "n": 0, "subs": 0.0, "collected": 0.0, "wages": 0.0})
            if v > 0:
                o["n"] += 1
                count += 1
            o["subs"] += v
            o["collected"] += coll_m
            o["wages"] += w
            value += v
            collected += coll_m
            wages += w
        for t_entry in teachers_map.values():
            t_entry["paid"] = sum(
                (_num(p.get("amount")) or 0) for p in payroll
                if p.get("teacher") == t_entry["name"] and p.get("month") == mk
            )
        per_month.append({
            "key": mk, "label": month_label(mk), "value": value, "collected": collected,
            "wages": wages, "count": count,
            "teachers": sorted(teachers_map.values(), key=lambda x: -x["wages"]),
        })

    per_teacher = []
    for t in teachers:
        sub = [s for s in students if s.get("teacher") == t]
        subs = collected = wages = 0.0
        for s in sub:
            for mk in month_keys:
                m = (s.get("months") or {}).get(mk) or {}
                v = float(_net_val(m) or 0)
                coll_m = _collected(m, inst_map.get((s.get("id"), mk)))
                w = _wage(
                    s,
                    m,
                    teacher_meta=teacher_meta,
                    wage_basis=wage_basis,
                    wage_accrual_mode=wage_accrual_mode,
                    collected_amount=coll_m,
                )
                if v > 0 or w > 0:
                    subs += v
                    collected += coll_m
                    wages += w
        per_teacher.append({"name": t, "n": len(sub), "subs": subs, "collected": collected, "wages": wages})
    no_teach = [s for s in students if not (s.get("teacher") or "").strip()]
    if no_teach:
        subs = sum(float(_net_val((s.get("months") or {}).get(mk, {})) or 0) for s in no_teach for mk in month_keys)
        coll = sum(_collected((s.get("months") or {}).get(mk) or {}, inst_map.get((s.get("id"), mk))) for s in no_teach for mk in month_keys)
        wgs = sum(
            _wage(
                s,
                (s.get("months") or {}).get(mk) or {},
                teacher_meta=teacher_meta,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                collected_amount=_collected((s.get("months") or {}).get(mk) or {}, inst_map.get((s.get("id"), mk))),
            )
            for s in no_teach for mk in month_keys
        )
        per_teacher.append({"name": "بدون معلمة", "n": len(no_teach), "subs": subs, "collected": coll, "wages": wgs})

    wb = Workbook()
    scope_norm = (scope or "full").strip().lower()

    if scope_norm == "teacher":
        first_used = False
        for t in teachers:
            sub = [s for s in students if s.get("teacher") == t]
            if not sub and len(teachers) > 1:
                continue
            ws_t, _ = _students_sheet(
                wb, state, sub, t[:31], "قسم المعلمة: " + t,
                year_label,
                tab_color=GOLD, first=(not first_used), filter_month=month,
            )
            if not first_used:
                _add_logo(ws_t, logo_path)
                first_used = True
        if not first_used:
            ws_t, _ = _students_sheet(
                wb, state, students, (teacher or "المعلمة")[:31], "قسم المعلمة: " + (teacher or "الكل"),
                year_label,
                tab_color=GOLD, first=True, filter_month=month,
            )
            _add_logo(ws_t, logo_path)
        _monthly_teachers_comparative_sheet(wb, state, students, teachers, first=False)
        _payroll_sheet(wb, state, per_month, payroll, first=False)
        return wb

    if scope_norm in ("teachers_stats", "payroll"):
        ws1 = _monthly_teachers_comparative_sheet(wb, state, students, teachers, first=True)
        _add_logo(ws1, logo_path)
        _payroll_sheet(wb, state, per_month, payroll, first=False)
        _teachers_sheet(wb, state, per_teacher, payroll, first=False)
        _summary_sheet(wb, state, students, per_month, per_teacher, payroll, first=False)
        return wb

    if scope_norm == "students":
        ws_students, mks = _students_sheet(
            wb, state, students,
            "الطلاب", "سجل الطالبات الشهري" + (f" — {teacher}" if teacher else ""),
            year_label,
            tab_color=TEAL_DARK, first=True, filter_month=month,
        )
        _add_logo(ws_students, logo_path)
        _dates_separation_sheet(wb, state, students, mks, first=False)
        return wb

    if scope_norm == "attendance":
        sessions = state.get("sessions") or []
        if teacher:
            sessions = [x for x in sessions if x.get("teacher") == teacher]
        ws_att = _sessions_sheet(wb, state, students, sessions, state.get("groups") or [], first=True)
        _add_logo(ws_att, logo_path)
        return wb

    if scope_norm == "memorization":
        memorization = state.get("memorization") or []
        if teacher:
            memorization = [x for x in memorization if x.get("teacher") == teacher]
        ws_mem = _memorization_sheet(wb, state, students, memorization, first=True)
        _add_logo(ws_mem, logo_path)
        return wb

    if scope_norm == "madrasa":
        ws_mad = _madrasa_sheet(
            wb, state, students,
            state.get("madrasaBatches") or [],
            state.get("madrasaEnrollments") or [],
            state.get("madrasaLogs") or [],
            first=True,
        )
        _add_logo(ws_mad, logo_path)
        return wb

    if scope_norm == "accounting":
        ws_acc = _accounting_sheet(wb, state, per_month, payroll, state.get("transactions") or [], first=True)
        _add_logo(ws_acc, logo_path)
        _installments_sheet(wb, state, students, state.get("installments") or [], first=False)
        _dates_separation_sheet(wb, state, students, month_keys, first=False)
        _summary_sheet(wb, state, students, per_month, per_teacher, payroll, first=False)
        return wb

    # Default scope: full
    ws_students, mks = _students_sheet(
        wb, state, students,
        "الطلاب", "سجل الطالبات الشهري",
        year_label,
        tab_color=TEAL_DARK, first=True, filter_month=month,
    )
    _add_logo(ws_students, logo_path)
    _dates_separation_sheet(wb, state, students, mks, first=False)
    _monthly_teachers_comparative_sheet(wb, state, students, teachers, first=False)
    _summary_sheet(wb, state, students, per_month, per_teacher, payroll, first=False)
    _payroll_sheet(wb, state, per_month, payroll, first=False)
    _teachers_sheet(wb, state, per_teacher, payroll, first=False)
    sessions = state.get("sessions") or []
    if sessions:
        _sessions_sheet(wb, state, students, sessions, state.get("groups") or [], first=False)
    transactions = state.get("transactions") or []
    if transactions:
        _accounting_sheet(wb, state, per_month, payroll, transactions, first=False)
    installments = state.get("installments") or []
    if installments:
        _installments_sheet(wb, state, students, installments, first=False)
    memorization = state.get("memorization") or []
    if memorization:
        _memorization_sheet(wb, state, students, memorization, first=False)
    madrasa_enr = state.get("madrasaEnrollments") or []
    madrasa_logs = state.get("madrasaLogs") or []
    if madrasa_enr or madrasa_logs:
        _madrasa_sheet(wb, state, students, state.get("madrasaBatches") or [], madrasa_enr, madrasa_logs, first=False)
    for t in teachers:
        sub = [s for s in students if s.get("teacher") == t]
        if not sub:
            continue
        _students_sheet(
            wb, state, sub, t[:31], "قسم المعلمة: " + t,
            year_label,
            tab_color=GOLD, filter_month=month,
        )
    return wb


def export_bytes(
    state: dict,
    logo_path=None,
    scope: str = "full",
    teacher: str | None = None,
    month: str | None = None,
) -> bytes:
    wb = build_workbook(state, logo_path=logo_path, scope=scope, teacher=teacher, month=month)
    _neutralize_formulas(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
