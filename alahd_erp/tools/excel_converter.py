"""Academy_Management_Dashboard → import-ready workbooks (design notes: docs/ARCHITECTURE.md).

The dashboard is a *wide* sheet: one row per student, then a block of 4 columns per month
(قيمة الاشتراك / تاريخ الدفع / تاريخ النهاية / أجر المعلمة). The app's importer wants a *long* sheet: one
row per (student, month), and it rejects every formula cell. This tool:

* reads the source with ``data_only=True`` (cached values) — it never modifies the original;
* detects the base columns (via the importer's own header synonyms) and the month blocks from the sheet itself;
* writes NEW plain-value ``.xlsx`` files (students / teachers) and a separate issues report;
* reports, instead of guessing: uncomputed formulas, error values, missing fields, odd dates, probable duplicates.

Rules that are deliberately NOT decided here:

* ``أجر المعلمة`` is a formula in the dashboard and is **ignored** — the app recomputes wages from the percentage.
* The dashboard has no "amount paid" column. IMPORTANT (verified on the real store): the app itself converts any
  month that has a fee AND a payment date into a fully-paid receipt when the data is saved (legacy migration in
  ``Store``, receipts ``RCP-MIG-…``), whatever this tool does. So ``paid_mode`` does not change the money:
  ``"none"`` (default) leaves that to the app's migration; ``"full"`` writes an explicit paid amount (= fee) so the
  receipts are ``RCP-IMP-…`` with the note «مستورد من Excel». Totals are identical either way (tested).
* Two rows with the same phone are kept as two students (flagged); merging them is the user's call.

No API route / UI / schema is touched.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..domain.models import normalize_phone_digits
from ..services import import_service as imp

PAID_MODES = ("none", "full")
_TOTAL_WORDS = ("الإجمالي", "الاجمالي", "المجموع")
_STU_HEADERS = [
    "كود الطالبة", "اسم الطالبة", "رقم التواصل", "اسم المعلمة", "نوع الاشتراك", "نسبة المعلمة", "الشهر",
    "قيمة الاشتراك", "الخصم", "المبلغ المسدد", "تاريخ الدفع", "تاريخ البداية الفعلي", "تاريخ النهاية", "ملاحظات",
]
_TEACHER_HEADERS = ["اسم المعلمة", "رقم الهاتف", "التخصص", "نسبة المعلمة", "ملاحظات"]
_MONTH_FIELDS = {  # sub-header label → internal field
    "قيمة الاشتراك": "value",
    "تاريخ الدفع": "paymentDate",
    "تاريخ النهاية": "endDate",
    "أجر المعلمة": "wage",
}


@dataclass
class Issue:
    severity: str  # "error" | "warning" | "info"
    code: str
    message: str
    row: int | None = None
    student: str = ""
    month: str = ""
    column: str = ""


@dataclass
class ConversionResult:
    students_xlsx: bytes = b""
    teachers_xlsx: bytes = b""
    issues_xlsx: bytes = b""
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def count(self, severity: str) -> int:
        return sum(1 for i in self.issues if i.severity == severity)


class ConversionError(ValueError):
    """The source file cannot be converted at all (wrong layout / unreadable)."""


# ----------------------------------------------------------------------------------------------- helpers
def _txt(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _is_error_value(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("#") and v.strip().endswith(("!", "?", "A", "0!"))


def _safe_text(s: Any) -> Any:
    """A value written into an output cell must never become a live formula."""
    if isinstance(s, str) and s[:1] in ("=", "+", "-", "@") and not re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return "'" + s
    return s


def _pct_to_percent(v: Any) -> tuple[int | None, bool]:
    """0.7 → 70; 70 → 70. Returns (value, ok)."""
    if v is None or v == "":
        return None, True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None, False
    if 0 <= f <= 1:
        f *= 100
    elif not (f == int(f) and 10 <= f <= 100):  # e.g. 1.5 / 5 / 150 are ambiguous or out of range → report
        return None, False
    if not 0 <= f <= 100:
        return None, False
    return int(round(f)), True


def _money(v: Any) -> int | None:
    """None for empty; raises ValueError for junk/negative."""
    if v is None or v == "":
        return None
    r = imp.parse_flexible_money(v, allow_empty=False)
    if r is None:
        raise ValueError(v)
    return r


def _style_header(ws, ncols: int) -> None:
    fill = PatternFill("solid", fgColor="134E4A")
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = fill
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(c)].width = 18
    ws.freeze_panes = "A2"
    ws.sheet_view.rightToLeft = True


def _to_bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------------------- layout detection
@dataclass
class _Layout:
    header_row: int          # row holding the base headers + month titles (1-based)
    sub_row: int             # row holding قيمة/تاريخ... labels
    base: dict[str, int]     # field → column index (1-based): name, phone, teacher, type, pct
    months: list[tuple[str, dict[str, int]]]  # [(YYYY-MM, {field: col})]
    first_data_row: int


def _detect_layout(ws) -> _Layout:
    max_scan = min(ws.max_row, 15)
    for hr in range(1, max_scan + 1):
        cells = {c.column: _txt(c.value) for c in ws[hr] if _txt(c.value)}
        if not cells:
            continue
        mapped = imp.auto_map_headers("students", list(cells.values()))
        if "name" not in mapped:
            continue
        col_of = {h: c for c, h in cells.items()}
        base = {f: col_of[h] for f, h in mapped.items() if f in ("name", "phone", "teacher", "type", "pct")}
        # month titles in the same row: anything that parses as a month AND carries a year
        month_cols: list[tuple[int, str]] = []
        for col, text in cells.items():
            if col in base.values():
                continue
            if not re.search(r"\d{4}", imp.normalize_digits_to_ascii(text)):
                continue
            mk = imp.parse_flexible_month(text, default_month="")
            if mk and re.fullmatch(r"\d{4}-\d{2}", mk) and any(n in text for n in imp.ARABIC_MONTH_NAMES):
                month_cols.append((col, mk))
        if not month_cols:
            continue
        month_cols.sort()
        sub_row = hr + 1
        months: list[tuple[str, dict[str, int]]] = []
        for i, (col, mk) in enumerate(month_cols):
            end = month_cols[i + 1][0] if i + 1 < len(month_cols) else ws.max_column + 1
            fields: dict[str, int] = {}
            for c in range(col, end):
                label = _txt(ws.cell(row=sub_row, column=c).value)
                if label in _MONTH_FIELDS and _MONTH_FIELDS[label] not in fields:
                    fields[_MONTH_FIELDS[label]] = c
            months.append((mk, fields))
        return _Layout(hr, sub_row, base, months, sub_row + 1)
    raise ConversionError("مش لاقي صف العناوين (اسم الطالب + أعمدة الأشهر) في الشيت — الملف مش بنفس تصميم الداشبورد")


# ------------------------------------------------------------------------------------------- main entry
def convert_dashboard(
    source: bytes | str | Path,
    *,
    student_sheet: str = "الطلاب",
    teachers_sheet: str = "المعلمات",
    summary_sheet: str = "ملخص",
    paid_mode: str = "none",
) -> ConversionResult:
    """Converts the dashboard workbook (path or bytes) to import-ready files. Never writes the source."""
    if paid_mode not in PAID_MODES:
        raise ValueError(f"paid_mode لازم يكون واحد من {PAID_MODES}")
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else bytes(source)
    if not raw:
        raise ConversionError("الملف فاضي")
    if len(raw) > imp.MAX_IMPORT_BYTES * 3:
        raise ConversionError("حجم الملف كبير بشكل غير طبيعي")
    try:
        wb_v = load_workbook(io.BytesIO(raw), data_only=True)   # cached values
        wb_f = load_workbook(io.BytesIO(raw), data_only=False)  # to know which cells are formulas
    except Exception as exc:  # corrupt / not xlsx
        raise ConversionError(f"ملف Excel غير صالح أو تالف: {exc}") from exc
    if student_sheet not in wb_v.sheetnames:
        raise ConversionError(f"مفيش شيت اسمه «{student_sheet}» — الشيتات: {wb_v.sheetnames}")

    ws, wsf = wb_v[student_sheet], wb_f[student_sheet]
    lay = _detect_layout(ws)
    res = ConversionResult()
    issues = res.issues

    # ---- formulas: any in a *used* column is an error (value would be lost); wage/other = info only
    used_cols = set(lay.base.values())
    for _, fields in lay.months:
        used_cols |= {c for f, c in fields.items() if f != "wage"}
    formula_total = formula_uncomputed = 0
    total_rows = {
        r for r in range(lay.first_data_row, ws.max_row + 1)
        if any(any(w in _txt(ws.cell(row=r, column=c).value) for w in _TOTAL_WORDS) for c in range(1, 4))
    }
    for row in wsf.iter_rows(min_row=lay.first_data_row):
        for c in row:
            if c.row in total_rows:  # the totals row is never imported; its formulas are not data
                continue
            if c.data_type == "f" or (isinstance(c.value, str) and c.value.startswith("=")):
                formula_total += 1
                cached = ws[c.coordinate].value
                if c.column in used_cols and (cached is None or _is_error_value(cached)):
                    issues.append(Issue("error", "FORMULA_NO_VALUE",
                                        "خلية صيغة قيمتها المخزّنة فاضية/خطأ (الملف ما اتحسبش في Excel) — افتحه واحفظه الأول",
                                        c.row, column=c.coordinate))
                if cached is None:
                    formula_uncomputed += 1
    wage_cols = {f["wage"] for _, f in lay.months if "wage" in f}
    if formula_total:
        issues.append(Issue("info", "FORMULAS_IGNORED",
                            f"{formula_total} خلية صيغة في الشيت ({formula_uncomputed} منها بدون قيمة مخزّنة)؛ "
                            f"أعمدة «أجر المعلمة» ({len(wage_cols)} عمود) اتتجاهلت لأن التطبيق بيحسب الأجر من النسبة"))

    # ---- student rows
    last = ws.max_row
    src_rows: list[dict[str, Any]] = []
    for r in range(lay.first_data_row, last + 1):
        name = _txt(ws.cell(row=r, column=lay.base["name"]).value)
        first_cells = [_txt(ws.cell(row=r, column=c).value) for c in range(1, 4)]
        if any(any(w in s for w in _TOTAL_WORDS) for s in first_cells):
            continue
        has_any = any(ws.cell(row=r, column=c).value not in (None, "") for c in range(1, ws.max_column + 1))
        if not has_any:
            continue
        if not name:
            issues.append(Issue("error", "NO_NAME", "صف بيانات بدون اسم طالب — اتخطّى", r))
            continue
        src_rows.append({"row": r, "name": name})

    out_rows: list[list[Any]] = []
    teachers_seen: dict[str, int | None] = {}
    phones: dict[str, list[tuple[int, str]]] = {}
    pay_date_counter: dict[str, int] = {}
    month_value_sum: dict[str, int] = {}
    month_students: dict[str, int] = {}
    n_receipts = 0

    for i, sr in enumerate(src_rows, start=1):
        r, name = sr["row"], sr["name"]

        def cell(f: str, _r: int = r) -> Any:
            col = lay.base.get(f)
            return ws.cell(row=_r, column=col).value if col else None

        phone = _txt(cell("phone"))
        teacher = _txt(cell("teacher"))
        ptype = _txt(cell("type"))
        pct, pct_ok = _pct_to_percent(cell("pct"))
        code = f"STU-{i:03d}"

        if not pct_ok:
            issues.append(Issue("error", "BAD_PCT", f"نسبة المعلمة غير صالحة: {cell('pct')!r}", r, name, column="نسبة المعلمة"))
        if not teacher:
            issues.append(Issue("warning", "NO_TEACHER", "من غير معلمة", r, name))
        else:
            teachers_seen.setdefault(teacher, pct)
        if not ptype:
            issues.append(Issue("warning", "NO_TYPE", "من غير نوع اشتراك", r, name))
        if not phone:
            issues.append(Issue("warning", "NO_PHONE", "من غير رقم تواصل", r, name))
        else:
            digits = normalize_phone_digits(phone)
            phones.setdefault(digits, []).append((r, name))
            if not re.fullmatch(r"(0?1[0125]\d{8})", digits):
                issues.append(Issue("warning", "NON_EGYPT_PHONE", f"رقم مش بصيغة موبايل مصري: {phone}", r, name))
        if pct == 0:
            issues.append(Issue("info", "PCT_ZERO", "نسبة المعلمة = 0", r, name))

        emitted = 0
        for mk, fields in lay.months:
            def mval(f: str, _r: int = r, _fields: dict[str, int] = fields) -> Any:
                col = _fields.get(f)
                return ws.cell(row=_r, column=col).value if col else None

            rv, rp, re_ = mval("value"), mval("paymentDate"), mval("endDate")
            if rv in (None, "") and rp in (None, "") and re_ in (None, ""):
                continue
            ok = True
            try:
                value = _money(rv)
            except ValueError:
                issues.append(Issue("error", "BAD_VALUE", f"قيمة اشتراك غير صالحة: {rv!r}", r, name, mk, "قيمة الاشتراك"))
                value, ok = None, False
            pay = imp.parse_flexible_date(rp)
            end = imp.parse_flexible_date(re_)
            if pay is None:
                issues.append(Issue("error", "BAD_DATE", f"تاريخ دفع غير صالح: {rp!r}", r, name, mk, "تاريخ الدفع"))
                pay, ok = "", False
            if end is None:
                issues.append(Issue("error", "BAD_DATE", f"تاريخ نهاية غير صالح: {re_!r}", r, name, mk, "تاريخ النهاية"))
                end, ok = "", False
            if not ok:
                continue
            if value is None:
                # The importer fills an empty fee from the package price whenever the month has a date, which would
                # invent a subscription the dashboard does not have (it counts a month only when fee > 0).
                issues.append(Issue("warning", "DATE_WITHOUT_FEE",
                                    f"تواريخ من غير قيمة اشتراك (دفع={pay or '—'}، نهاية={end or '—'}) — الشهر ده اتتجاهل "
                                    "عشان المستورد كان هيحط له قيمة افتراضية من الباقة", r, name, mk))
                continue
            if value == 0:
                issues.append(Issue("info", "ZERO_FEE", "قيمة الاشتراك = 0", r, name, mk))
            if pay and end and end < pay:
                issues.append(Issue("warning", "END_BEFORE_PAYMENT", f"تاريخ النهاية ({end}) قبل تاريخ الدفع ({pay})", r, name, mk))
            if pay and not pay.startswith(mk) and pay[:7] > mk:
                issues.append(Issue("info", "PAID_AFTER_MONTH", f"الدفع ({pay}) بعد الشهر", r, name, mk))
            paid = None
            if paid_mode == "full" and value and pay:
                paid = value  # discount is always 0 here, so net = value
                n_receipts += 1
            if pay:
                pay_date_counter[pay] = pay_date_counter.get(pay, 0) + 1
            if value:
                month_value_sum[mk] = month_value_sum.get(mk, 0) + value
                month_students[mk] = month_students.get(mk, 0) + 1
            out_rows.append([code, name, phone, teacher, ptype, pct, mk, value, 0 if value is not None else None,
                             paid, pay or None, None, end or None, None])
            emitted += 1
        if emitted == 0:
            # keep the student itself; the importer creates it and skips the empty month
            out_rows.append([code, name, phone, teacher, ptype, pct, lay.months[0][0], None, None, None, None, None, None, None])
            issues.append(Issue("info", "NO_MONTH_DATA", "طالب بدون أي بيانات شهرية — هيتضاف كطالب فقط", r, name))

    # ---- what the app will do with dated months (see module docstring)
    dated = [r for r in out_rows if r[7] and r[10]]
    if dated:
        issues.append(Issue("warning", "AUTO_RECEIPTS",
                            f"{len(dated)} شهر فيه قيمة + تاريخ دفع (إجمالي {sum(r[7] for r in dated)} ج) — التطبيق هيعتبرها "
                            "مسدّدة بالكامل وهيعمل لها إيصالات تلقائياً؛ لو في تواريخ مش دفع فعلي امسحها من الملف قبل الاستيراد"))
    # ---- cross-row checks
    for digits, who in phones.items():
        if len(who) > 1:
            names = " / ".join(f"{n} (صف {rr})" for rr, n in who)
            issues.append(Issue("warning", "DUP_PHONE", f"نفس الرقم لأكتر من صف (ممكن نفس الطالبة): {names}", who[0][0], who[0][1]))
    if pay_date_counter:
        top_date, top_n = max(pay_date_counter.items(), key=lambda kv: kv[1])
        total_dates = sum(pay_date_counter.values())
        if top_n >= 5 and top_n / total_dates >= 0.15:
            issues.append(Issue("warning", "SUSPECT_DEFAULT_DATE",
                                f"{top_n} من {total_dates} تاريخ دفع = {top_date} (غالباً تاريخ تحديث الملف مش دفع فعلي) — راجعهم"))

    # ---- reconcile with «ملخص» (stored numbers)
    res.stats["month_value_sum"] = dict(sorted(month_value_sum.items()))
    res.stats["month_paying_students"] = dict(sorted(month_students.items()))
    res.stats["reconcile"] = _reconcile(wb_v, summary_sheet, month_value_sum, issues)

    # ---- teachers
    t_rows: list[list[Any]] = []
    listed: set[str] = set()
    if teachers_sheet in wb_v.sheetnames:
        tws = wb_v[teachers_sheet]
        hdr = next((r for r in range(1, min(tws.max_row, 8) + 1)
                    if "اسم المعلمة" in [_txt(c.value) for c in tws[r]]), None)
        if hdr:
            cols = {_txt(c.value): c.column for c in tws[hdr] if _txt(c.value)}
            for r in range(hdr + 1, tws.max_row + 1):
                tn = _txt(tws.cell(row=r, column=cols["اسم المعلمة"]).value)
                if not tn or any(w in tn for w in _TOTAL_WORDS):
                    continue
                tp, tp_ok = _pct_to_percent(tws.cell(row=r, column=cols["نسبة المعلمة"]).value) if "نسبة المعلمة" in cols else (None, True)
                if not tp_ok:
                    issues.append(Issue("error", "BAD_PCT", "نسبة معلمة غير صالحة", r, tn, column=teachers_sheet))
                note = _txt(tws.cell(row=r, column=cols["ملاحظات"]).value) if "ملاحظات" in cols else ""
                listed.add(tn)
                t_rows.append([tn, None, None, tp if tp is not None else 70, note or None])
    for tn, pct in teachers_seen.items():
        if tn not in listed:
            issues.append(Issue("warning", "TEACHER_NOT_LISTED", f"المعلمة «{tn}» موجودة في الطلاب ومش في شيت «{teachers_sheet}»", column=tn))
            t_rows.append([tn, None, None, pct if pct is not None else 70, None])

    # ---- write outputs
    wbs = Workbook()
    wss = wbs.active
    wss.title = "الطالبات"
    wss.append(_STU_HEADERS)
    for row in out_rows:
        wss.append([_safe_text(v) for v in row])
    for row in wss.iter_rows(min_row=2):
        for c in row:
            if c.data_type == "f":
                c.data_type = "s"
        row[2].number_format = "@"  # phone stays text (leading 0)
    _style_header(wss, len(_STU_HEADERS))
    res.students_xlsx = _to_bytes(wbs)

    wbt = Workbook()
    wst = wbt.active
    wst.title = "المعلمات"
    wst.append(_TEACHER_HEADERS)
    for row in t_rows:
        wst.append([_safe_text(v) for v in row])
    _style_header(wst, len(_TEACHER_HEADERS))
    res.teachers_xlsx = _to_bytes(wbt)

    res.stats.update({
        "students": len(src_rows),
        "month_rows": sum(1 for r in out_rows if r[7] is not None or r[10] or r[12]),
        "output_rows": len(out_rows),
        "teachers": len(t_rows),
        "formula_cells": formula_total,
        "paid_mode": paid_mode,
        "receipts_to_create": n_receipts,
        "months_detected": [m for m, _ in lay.months],
        "layout": {"header_row": lay.header_row, "sub_row": lay.sub_row, "base_columns": lay.base},
    })
    res.issues_xlsx = _issues_xlsx(issues)
    return res


def _reconcile(wb, summary_sheet: str, computed: dict[str, int], issues: list[Issue]) -> dict[str, Any]:
    """Compares our per-month fee totals to the numbers stored in «ملخص» (if the sheet follows that layout)."""
    out: dict[str, Any] = {"checked": False, "months": {}}
    if summary_sheet not in wb.sheetnames:
        return out
    ws = wb[summary_sheet]
    hdr = next((r for r in range(1, min(ws.max_row, 6) + 1) if "المؤشر" in [_txt(c.value) for c in ws[r]]), None)
    if not hdr:
        return out
    row_idx = next((r for r in range(hdr + 1, ws.max_row + 1) if "إجمالي الاشتراكات" in _txt(ws.cell(row=r, column=1).value)), None)
    if not row_idx:
        return out
    out["checked"] = True
    for c in ws[hdr]:
        label = _txt(c.value)
        if not label.startswith("تفصيل"):
            continue
        mk = imp.parse_flexible_month(label.replace("تفصيل", "").strip(), default_month="")
        stored = ws.cell(row=row_idx, column=c.column).value
        if not mk or not isinstance(stored, (int, float)):
            continue
        mine = computed.get(mk, 0)
        out["months"][mk] = {"summary_sheet": stored, "converted": mine, "match": int(stored) == mine}
        if int(stored) != mine:
            issues.append(Issue("warning", "TOTAL_MISMATCH",
                                f"إجمالي اشتراكات {mk}: «ملخص» = {stored} والمحوّل = {mine}", month=mk))
    return out


def _issues_xlsx(issues: list[Issue]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "مشاكل التحويل"
    heads = ["الخطورة", "الكود", "صف المصدر", "الطالب/ة", "الشهر", "العمود", "التفاصيل"]
    ws.append(heads)
    order = {"error": 0, "warning": 1, "info": 2}
    ar = {"error": "خطأ", "warning": "تنبيه", "info": "معلومة"}
    for i in sorted(issues, key=lambda x: (order.get(x.severity, 9), x.row or 0)):
        ws.append([ar.get(i.severity, i.severity), i.code, i.row, _safe_text(i.student), i.month, _safe_text(i.column), _safe_text(i.message)])
    for row in ws.iter_rows(min_row=2):
        for c in row:
            if c.data_type == "f":
                c.data_type = "s"
    _style_header(ws, len(heads))
    ws.column_dimensions["G"].width = 80
    return _to_bytes(wb)


# ------------------------------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="تحويل Academy_Management_Dashboard إلى ملفات جاهزة للاستيراد (مش بيعدّل الأصل)")
    p.add_argument("source", help="مسار ملف الداشبورد .xlsx")
    p.add_argument("-o", "--out-dir", default=".", help="مجلد الإخراج (الافتراضي: الحالي)")
    p.add_argument("--paid", choices=PAID_MODES, default="none",
                   help="none (افتراضي) = المسدد فاضي والتطبيق نفسه بيرحّل أي شهر فيه قيمة+تاريخ دفع لإيصال | full = المسدد = القيمة صراحةً (نفس المبالغ)")
    p.add_argument("--student-sheet", default="الطلاب")
    args = p.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        res = convert_dashboard(args.source, student_sheet=args.student_sheet, paid_mode=args.paid)
    except ConversionError as exc:
        print(f"فشل التحويل: {exc}")
        return 2
    (out / "import_students.xlsx").write_bytes(res.students_xlsx)
    (out / "import_teachers.xlsx").write_bytes(res.teachers_xlsx)
    (out / "conversion_issues.xlsx").write_bytes(res.issues_xlsx)
    s = res.stats
    print(f"طلاب: {s['students']} | صفوف (طالب×شهر): {s['output_rows']} | معلمات: {s['teachers']} | شهور: {len(s['months_detected'])}")
    print(f"إيصالات هتتولّد: {s['receipts_to_create']} (paid={s['paid_mode']})")
    print(f"مشاكل: {res.count('error')} خطأ، {res.count('warning')} تنبيه، {res.count('info')} معلومة → {out / 'conversion_issues.xlsx'}")
    return 1 if res.count("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
