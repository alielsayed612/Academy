"""Centralized Financial Calculation Engine (Pure Decimal Arithmetic).

Strict Engineering & Business Rules:
1. Zero floating-point representation errors: all monetary and percentage math uses Decimal,
   snapped to 6 decimal places using ROUND_HALF_UP before integer rounding.
2. Unified rounding rule across the entire system: Round Half Up to nearest integer EGP.
3. Discount consistency: NET subscription (value - discount) is used across month stats,
   accounting summary, collection reports, and uncollected/arrears. A discounted amount
   never appears as arrears.
4. Teacher wage basis is settings-driven:
   - `wage_basis`: "net" (default, value - discount) or "gross" (value)
   - `wage_accrual_mode`: "accrued" (default, based on subscription) or "collected" (based on collected amount)
5. Single source of truth for payment status:
   - Payments are recorded as receipts (`installments`, each with its own date, amount, method, receiptNo).
   - Derived status (`unpaid` / `partial` / `paid` / `over_discounted`) is computed strictly from receipts.
   - A non-empty `paymentDate` alone on a month entry does NOT mark a month paid.
   - Future-dated receipts (`paymentDate > reference_date`) do NOT count as collected until their date arrives.
6. Percentages are stored and validated as integer percent (0..100), where 1 means 1% and 100 means 100%.
7. Strict separation of Payment Date (`paymentDate`) and Actual Start Date (`actualStartDate`).
"""
from __future__ import annotations

import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

_SCALE_6 = Decimal("0.000001")


def to_decimal(val: Any, default: Decimal = Decimal("0")) -> Decimal:
    """Converts any numeric input to Decimal without IEEE-754 float drift."""
    if val is None or val == "" or isinstance(val, bool):
        return default
    if isinstance(val, Decimal):
        if not val.is_finite():
            raise ValueError("non-finite decimal")
        return val
    if isinstance(val, int):
        return Decimal(val)
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            raise ValueError("non-finite float")
        return Decimal(str(val)).quantize(_SCALE_6, rounding=ROUND_HALF_UP)
    try:
        s = str(val).strip()
        if not s:
            return default
        d = Decimal(s)
        if not d.is_finite():
            raise ValueError("non-finite decimal")
        return d.quantize(_SCALE_6, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"invalid numeric value: {val!r}") from exc


def round_half_up(val: Any) -> int:
    """Rounds any number to the nearest integer EGP using standard ROUND_HALF_UP."""
    d = to_decimal(val, Decimal("0"))
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def parse_pct_int(pct_val: Any, default: int = 0) -> int:
    """Parses and validates a percentage as an integer in 0..100.
    Removes the ambiguous '1 means 100%' behavior: 1 is 1%, 70 is 70%, 100 is 100%.
    For backward compatibility with legacy float fractions (e.g. 0.7 or 0.5):
    if 0 < val < 1, val is treated as a fractional percentage (val * 100).
    """
    if pct_val is None or pct_val == "" or isinstance(pct_val, bool):
        if isinstance(pct_val, bool):
            raise ValueError("percentage cannot be boolean")
        return default
    d = to_decimal(pct_val)
    if Decimal("0") < d < Decimal("1"):
        d = d * Decimal("100")
    if d < Decimal("0") or d > Decimal("100"):
        raise ValueError(f"percentage must be between 0 and 100, got {pct_val!r}")
    return round_half_up(d)


def normalize_pct_decimal(pct_val: Any) -> Decimal:
    """Converts an integer percentage (0..100) into a Decimal fraction (e.g. 70 -> Decimal('0.700000')).
    Note: 1 means 1% (0.01), 70 means 70% (0.70), 100 means 100% (1.00).
    """
    pct_int = parse_pct_int(pct_val, default=0)
    return (Decimal(pct_int) / Decimal("100")).quantize(_SCALE_6, rounding=ROUND_HALF_UP)


def resolve_payment_date(
    month_entry: dict[str, Any] | None,
    student_month_installments: list[dict[str, Any]] | None = None,
    reference_date: str | None = None,
) -> str:
    """Returns the latest valid non-future receipt payment date if receipts are present,
    else the explicit `paymentDate` / `pay` field on the month record.
    NEVER falls back to `actualStartDate` or `startDate`.
    """
    if student_month_installments:
        valid_dates = []
        for inst in student_month_installments:
            if not isinstance(inst, dict):
                continue
            dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
            if dt and (not reference_date or dt <= reference_date):
                valid_dates.append(dt)
        if valid_dates:
            return max(valid_dates)
    if not isinstance(month_entry, dict):
        return ""
    if "paymentDate" in month_entry and month_entry["paymentDate"] is not None:
        return str(month_entry.get("paymentDate") or "").strip()
    return str(month_entry.get("pay") or "").strip()


def resolve_actual_start_date(month_entry: dict[str, Any] | None) -> str:
    """Returns the explicit Actual Start Date for a monthly record.
    Checks `actualStartDate` first, then `startDate`.
    NEVER falls back to `paymentDate` or `pay`.
    """
    if not isinstance(month_entry, dict):
        return ""
    if "actualStartDate" in month_entry and month_entry["actualStartDate"] is not None:
        return str(month_entry.get("actualStartDate") or "").strip()
    return str(month_entry.get("startDate") or "").strip()


def resolve_end_date(month_entry: dict[str, Any] | None) -> str:
    """Returns the subscription end date for a monthly record."""
    if not isinstance(month_entry, dict):
        return ""
    if "endDate" in month_entry and month_entry["endDate"] is not None and str(month_entry["endDate"]).strip():
        return str(month_entry.get("endDate") or "").strip()
    return str(month_entry.get("end") or "").strip()


def resolve_wage_pct(
    student: dict[str, Any],
    teacher_meta: dict[str, Any] | None = None,
    pct_source: str = "student",
    default_pct: int = 70,
) -> Decimal:
    """Resolves the teacher wage percentage fraction (0..1) according to policy."""
    tname = str((student or {}).get("teacher") or "").strip()
    t_pct_raw = None
    if teacher_meta and tname and isinstance(teacher_meta.get(tname), dict):
        t_pct_raw = teacher_meta[tname].get("pct")

    s_pct_raw = (student or {}).get("pct")

    if pct_source == "teacher" and t_pct_raw is not None and t_pct_raw != "":
        return normalize_pct_decimal(t_pct_raw)

    if s_pct_raw is not None and s_pct_raw != "":
        return normalize_pct_decimal(s_pct_raw)

    if t_pct_raw is not None and t_pct_raw != "":
        return normalize_pct_decimal(t_pct_raw)

    return normalize_pct_decimal(default_pct)


def effective_month_subscription(month_entry: dict[str, Any] | None) -> int:
    """Returns the gross monthly subscription value as a non-negative integer."""
    if not isinstance(month_entry, dict):
        return 0
    v = month_entry.get("value")
    if v is None or v == "":
        return 0
    return max(0, round_half_up(v))


def raw_month_discount(month_entry: dict[str, Any] | None) -> int:
    """Returns the raw non-negative integer discount before capping to subscription value."""
    if not isinstance(month_entry, dict):
        return 0
    disc_raw = month_entry.get("discount")
    if disc_raw is None or disc_raw == "":
        return 0
    return max(0, round_half_up(disc_raw))


def effective_month_discount(month_entry: dict[str, Any] | None) -> int:
    """Returns the monthly discount bounded between 0 and the subscription value."""
    sub = effective_month_subscription(month_entry)
    disc = raw_month_discount(month_entry)
    return min(sub, disc)


def effective_month_net_subscription(month_entry: dict[str, Any] | None) -> int:
    """Returns the NET subscription due (value - discount), never negative."""
    sub = effective_month_subscription(month_entry)
    if sub <= 0:
        return 0
    disc = effective_month_discount(month_entry)
    return max(0, sub - disc)


def effective_month_collected(
    month_entry: dict[str, Any] | None,
    student_month_installments: list[dict[str, Any]] | None = None,
    reference_date: str | None = None,
) -> int:
    """Calculates the actual collected fee for a month strictly from valid non-future receipts (`installments`).
    - If receipts/installments are present, they are the single source of truth.
    - If no installments list is present at all, falls back to explicit non-future `paymentDate` on `month_entry`.
    - A future-dated receipt or paymentDate (`> reference_date`) does NOT count as collected.
    - `actualStartDate` NEVER marks a subscription as paid or collected.
    - Collected amount is capped at the net subscription (`value - discount`).
    """
    if not isinstance(month_entry, dict):
        return 0
    net_sub = effective_month_net_subscription(month_entry)
    if net_sub <= 0:
        return 0

    ref_dt = str(reference_date or "").strip() or None

    # 1. Check explicit installments list passed or stored in month_entry
    insts = student_month_installments
    if insts is None and isinstance(month_entry.get("installments"), list):
        insts = month_entry.get("installments")

    if insts is not None:
        inst_total = 0
        for inst in insts:
            if not isinstance(inst, dict):
                continue
            pay_dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
            if pay_dt and ref_dt and pay_dt > ref_dt:
                continue
            amt = max(0, round_half_up(inst.get("amount") or 0))
            inst_total += amt
        return min(net_sub, inst_total)

    # 2. Legacy fallback when no installments list is present at all, but paymentDate is explicitly set
    pay_dt = resolve_payment_date(month_entry, reference_date=ref_dt)
    if pay_dt and (not ref_dt or pay_dt <= ref_dt):
        return net_sub

    return 0


def derive_month_payment_status(
    month_entry: dict[str, Any] | None,
    student_month_installments: list[dict[str, Any]] | None = None,
    reference_date: str | None = None,
) -> str:
    """Derives the canonical payment status strictly from net subscription and receipts:
    - 'unsubscribed': gross value <= 0
    - 'over_discounted': raw discount > gross value or (gross > 0 and net == 0)
    - 'paid': collected >= net_sub (and net_sub > 0)
    - 'partial': 0 < collected < net_sub
    - 'unpaid': collected == 0 and net_sub > 0
    """
    if not isinstance(month_entry, dict):
        return "unsubscribed"
    sub = effective_month_subscription(month_entry)
    if sub <= 0:
        return "unsubscribed"
    raw_disc = raw_month_discount(month_entry)
    if raw_disc >= sub:
        return "over_discounted"
    net_sub = max(0, sub - raw_disc)
    coll = effective_month_collected(month_entry, student_month_installments, reference_date=reference_date)
    if coll >= net_sub:
        return "paid"
    if coll > 0:
        return "partial"
    return "unpaid"


def effective_month_wage(
    student: dict[str, Any] | None,
    month_entry: dict[str, Any] | None,
    teacher_meta: dict[str, Any] | None = None,
    pct_source: str = "student",
    wage_basis: str = "net",
    wage_accrual_mode: str = "accrued",
    student_month_installments: list[dict[str, Any]] | None = None,
    reference_date: str | None = None,
    default_pct: int = 70,
    collected_amount: int | None = None,
    **kwargs: Any,
) -> int:
    """Calculates the teacher's monthly wage rounded via Round Half Up.
    Supports:
      - `wage_basis`: 'net' (default: value - discount) or 'gross' (value)
      - `wage_accrual_mode`: 'accrued' (default: based on subscription) or 'collected' (only from collected receipts)
    """
    if not isinstance(month_entry, dict):
        return 0
    cw = month_entry.get("customWage")
    if cw is not None and cw != "":
        cw_int = max(0, round_half_up(cw))
        if wage_accrual_mode == "collected":
            coll = (
                collected_amount
                if collected_amount is not None
                else effective_month_collected(month_entry, student_month_installments, reference_date=reference_date)
            )
            return cw_int if coll > 0 else 0
        return cw_int

    gross_sub = effective_month_subscription(month_entry)
    if gross_sub <= 0:
        return 0
    net_sub = effective_month_net_subscription(month_entry)

    if wage_accrual_mode == "collected":
        base_amount = (
            collected_amount
            if collected_amount is not None
            else effective_month_collected(month_entry, student_month_installments, reference_date=reference_date)
        )
    else:
        base_amount = gross_sub if wage_basis == "gross" else net_sub

    if base_amount <= 0:
        return 0
    pct = resolve_wage_pct(student or {}, teacher_meta=teacher_meta, pct_source=pct_source, default_pct=default_pct)
    return max(0, round_half_up(Decimal(base_amount) * pct))


def index_installments_by_student_month(
    installments: list[dict[str, Any]] | None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Indexes installments by (studentId, monthKey)."""
    idx: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for inst in installments or []:
        if not isinstance(inst, dict):
            continue
        sid = str(inst.get("studentId") or "").strip()
        mk = str(inst.get("month") or "").strip()
        if sid and mk:
            idx.setdefault((sid, mk), []).append(inst)
    return idx


def migrate_legacy_payments_to_receipts(
    state_or_students: dict[str, Any] | list[dict[str, Any]],
    existing_installments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Converts any legacy `pay` / `paymentDate` / `partialPaid` / `paidStatus` on student month entries
    that do not yet have corresponding receipts in `installments` into canonical receipt records,
    preserving exact totals before and after migration.
    Accepts either `(state_dict)` or `(students_list, existing_installments)`.
    """
    if isinstance(state_or_students, dict):
        students = state_or_students.get("students") or []
        raw_insts = state_or_students.get("installments") or []
    else:
        students = state_or_students or []
        raw_insts = existing_installments or []

    existing_insts = [dict(x) for x in raw_insts if isinstance(x, dict)]
    inst_map = index_installments_by_student_month(existing_insts)
    existing_ids = {str(x.get("id") or "") for x in existing_insts}

    for stu in students:
        if not isinstance(stu, dict):
            continue
        sid = str(stu.get("id") or "").strip()
        if not sid:
            continue
        months = stu.get("months") or {}
        if not isinstance(months, dict):
            continue
        for mk, m in sorted(months.items()):
            if not isinstance(m, dict):
                continue
            if inst_map.get((sid, mk)):
                continue
            net_sub = effective_month_net_subscription(m)
            if net_sub <= 0:
                continue

            legacy_amt = 0
            pp = m.get("partialPaid")
            pay_dt = str(m.get("paymentDate") or m.get("pay") or "").strip()
            paid_st = str(m.get("paidStatus") or "").strip()

            if pp is not None and pp != "":
                pp_int = max(0, round_half_up(pp))
                legacy_amt = min(net_sub, pp_int)
            elif pay_dt or paid_st == "paid":
                legacy_amt = net_sub

            if legacy_amt > 0:
                rcp_date = pay_dt if len(pay_dt) == 10 else f"{mk}-01"
                inst_id = f"inst_mig_{sid}_{mk}"
                suffix = 1
                while inst_id in existing_ids:
                    suffix += 1
                    inst_id = f"inst_mig_{sid}_{mk}_{suffix}"
                existing_ids.add(inst_id)
                new_inst = {
                    "id": inst_id,
                    "receiptNo": f"RCP-MIG-{sid}-{mk.replace('-', '')}",
                    "studentId": sid,
                    "month": mk,
                    "amount": legacy_amt,
                    "date": rcp_date,
                    "paymentDate": rcp_date,
                    "method": "نقدي / محوّل",
                    "note": "إيصال مرحّل تلقائيًا من السجل السابق",
                    "createdAt": rcp_date,
                }
                existing_insts.append(new_inst)
                inst_map.setdefault((sid, mk), []).append(new_inst)

    if isinstance(state_or_students, dict):
        state_or_students["installments"] = existing_insts
    return existing_insts


def compute_month_stats(
    students: list[dict[str, Any]],
    month_key: str,
    installments: list[dict[str, Any]] | None = None,
    teacher_meta: dict[str, Any] | None = None,
    pct_source: str = "student",
    wage_basis: str = "net",
    wage_accrual_mode: str = "accrued",
    reference_date: str | None = None,
    default_pct: int = 70,
) -> dict[str, Any]:
    """Aggregates monthly statistics per teacher and academy-wide using NET subscriptions (value - discount)."""
    inst_map = index_installments_by_student_month(installments)
    by_teacher: dict[str, dict[str, Any]] = {}

    for stu in students or []:
        if not isinstance(stu, dict):
            continue
        months = stu.get("months") or {}
        m = months.get(month_key)
        if not isinstance(m, dict):
            continue
        gross_sub = effective_month_subscription(m)
        disc = effective_month_discount(m)
        net_sub = effective_month_net_subscription(m)
        sid = str(stu.get("id") or "")
        stu_insts = inst_map.get((sid, month_key))
        wage = effective_month_wage(
            stu,
            m,
            teacher_meta=teacher_meta,
            pct_source=pct_source,
            wage_basis=wage_basis,
            wage_accrual_mode=wage_accrual_mode,
            student_month_installments=stu_insts,
            reference_date=reference_date,
            default_pct=default_pct,
        )
        if gross_sub <= 0 and wage <= 0:
            continue
        coll = effective_month_collected(m, stu_insts, reference_date=reference_date)
        tname = str(stu.get("teacher") or "")
        entry = by_teacher.setdefault(
            tname,
            {
                "teacher": tname,
                "n": 0,
                "grossSubs": 0,
                "discounts": 0,
                "subs": 0,
                "collected": 0,
                "uncollected": 0,
                "wages": 0,
            },
        )
        if gross_sub > 0:
            entry["n"] += 1
        entry["grossSubs"] += gross_sub
        entry["discounts"] += disc
        entry["subs"] += net_sub
        entry["collected"] += coll
        entry["uncollected"] += max(0, net_sub - coll)
        entry["wages"] += wage

    teachers_list = sorted(by_teacher.values(), key=lambda r: (r["wages"], r["subs"]), reverse=True)
    tot_subs = sum(t["subs"] for t in teachers_list)
    tot_coll = sum(t["collected"] for t in teachers_list)
    tot = {
        "n": sum(t["n"] for t in teachers_list),
        "grossSubs": sum(t["grossSubs"] for t in teachers_list),
        "discounts": sum(t["discounts"] for t in teachers_list),
        "subs": tot_subs,
        "collected": tot_coll,
        "uncollected": max(0, tot_subs - tot_coll),
        "wages": sum(t["wages"] for t in teachers_list),
    }
    return {"month": month_key, "teachers": teachers_list, "total": tot}


def compute_collection_report(
    students: list[dict[str, Any]],
    month_key: str | None = "",
    installments: list[dict[str, Any]] | None = None,
    start_payment_date: str | None = None,
    end_payment_date: str | None = None,
    basis: str = "accrual",
    reference_date: str | None = None,
) -> dict[str, Any]:
    """Generates both:
    1) Accrual report by subscription `month_key` (`basis='accrual'`), using NET subscription (value - discount)
       and receipt-based collection.
    2) Cash-basis report strictly driven by receipt payment dates (`basis='cash'`), listing actual cash receipts
       received within the date window or month.
    """
    ref_dt = str(reference_date or "").strip() or None
    stu_by_id = {str(s.get("id") or ""): s for s in (students or []) if isinstance(s, dict)}
    inst_map = index_installments_by_student_month(installments)

    # Cash-basis receipts list
    cash_receipts: list[dict[str, Any]] = []
    cash_total = 0
    for inst in installments or []:
        if not isinstance(inst, dict):
            continue
        pay_dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
        if not pay_dt or (ref_dt and pay_dt > ref_dt):
            continue
        if start_payment_date and pay_dt < start_payment_date:
            continue
        if end_payment_date and pay_dt > end_payment_date:
            continue
        if not start_payment_date and not end_payment_date and month_key:
            # In cash mode with only month_key given, match receipts paid in that calendar month OR for that month
            if basis == "cash" and not pay_dt.startswith(month_key):
                continue
        amt = max(0, round_half_up(inst.get("amount") or 0))
        sid = str(inst.get("studentId") or "")
        stu = stu_by_id.get(sid) or {}
        sub_mk = str(inst.get("month") or "")
        m_obj = (stu.get("months") or {}).get(sub_mk) if isinstance(stu.get("months"), dict) else {}
        actual_start = resolve_actual_start_date(m_obj)
        cash_total += amt
        cash_receipts.append({
            "receiptId": str(inst.get("id") or ""),
            "receiptNo": str(inst.get("receiptNo") or ""),
            "studentId": sid,
            "studentName": str(stu.get("name") or ""),
            "teacher": str(stu.get("teacher") or ""),
            "subscriptionMonth": sub_mk,
            "paymentDate": pay_dt,
            "actualStartDate": actual_start,
            "amount": amt,
            "method": str(inst.get("method") or "نقدي"),
            "note": str(inst.get("note") or ""),
        })

    # Accrual-basis subscription records for month_key
    records: list[dict[str, Any]] = []
    total_gross = 0
    total_discount = 0
    total_due = 0
    total_collected = 0
    advance_payments_count = 0
    deferred_payments_count = 0

    for stu in students or []:
        if not isinstance(stu, dict):
            continue
        sid = str(stu.get("id") or "")
        months_dict = stu.get("months") or {}
        target_mks = [month_key] if month_key else sorted(months_dict.keys())
        for mk in target_mks:
            m = months_dict.get(mk)
            if not isinstance(m, dict):
                continue
            sub = effective_month_subscription(m)
            disc = effective_month_discount(m)
            net_sub = effective_month_net_subscription(m)
            if sub <= 0 and net_sub <= 0:
                continue

            stu_insts = inst_map.get((sid, mk)) if installments is not None else None
            pay_date = resolve_payment_date(m, stu_insts, reference_date=ref_dt)
            actual_start = resolve_actual_start_date(m)
            end_date = resolve_end_date(m)
            coll = effective_month_collected(m, stu_insts, reference_date=ref_dt)
            remaining = max(0, net_sub - coll)
            derived_status = derive_month_payment_status(m, stu_insts, reference_date=ref_dt)

            if start_payment_date and (not pay_date or pay_date < start_payment_date):
                continue
            if end_payment_date and (not pay_date or pay_date > end_payment_date):
                continue

            payment_timing = "unpaid"
            if coll > 0:
                if pay_date and actual_start:
                    if pay_date < actual_start:
                        payment_timing = "paid_in_advance"
                        advance_payments_count += 1
                    elif pay_date > actual_start:
                        payment_timing = "paid_after_start"
                        deferred_payments_count += 1
                    else:
                        payment_timing = "paid_on_start"
                else:
                    payment_timing = "paid"

            total_gross += sub
            total_discount += disc
            total_due += net_sub
            total_collected += coll
            records.append({
                "studentId": sid,
                "studentName": str(stu.get("name") or ""),
                "teacher": str(stu.get("teacher") or ""),
                "month": mk,
                "subscription": sub,
                "discount": disc,
                "netSubscription": net_sub,
                "collected": coll,
                "remaining": remaining,
                "status": derived_status,
                "paymentDate": pay_date,
                "actualStartDate": actual_start,
                "endDate": end_date,
                "paymentTiming": payment_timing,
            })

    return {
        "month": month_key,
        "basis": basis,
        "totalGrossSubscriptions": total_gross,
        "totalDiscounts": total_discount,
        "totalNetSubscriptions": total_due,
        "totalCollected": total_collected,
        "totalRemaining": max(0, total_due - total_collected),
        "cashCollectedInPeriod": cash_total,
        "cashReceipts": cash_receipts,
        "advancePaymentsCount": advance_payments_count,
        "deferredPaymentsCount": deferred_payments_count,
        "records": records,
    }


def compute_accrual_month_report(
    students: list[dict[str, Any]],
    month_key: str,
    installments: list[dict[str, Any]] | None = None,
    teacher_meta: dict[str, Any] | None = None,
    wage_basis: str = "net",
    wage_accrual_mode: str = "accrued",
    reference_date: str | None = None,
    default_pct: int = 70,
) -> dict[str, Any]:
    """Generates an accrual-basis report for a specific subscription month including teacher wages and student records."""
    stats = compute_month_stats(
        students,
        month_key,
        installments=installments,
        teacher_meta=teacher_meta,
        wage_basis=wage_basis,
        wage_accrual_mode=wage_accrual_mode,
        reference_date=reference_date,
        default_pct=default_pct,
    )
    col_rep = compute_collection_report(
        students,
        month_key=month_key,
        installments=installments,
        basis="accrual",
        reference_date=reference_date,
    )
    return {
        "month": month_key,
        "summary": stats["total"],
        "teachers": stats["teachers"],
        "records": col_rep["records"],
        "totalGrossSubscriptions": col_rep["totalGrossSubscriptions"],
        "totalDiscounts": col_rep["totalDiscounts"],
        "totalNetSubscriptions": col_rep["totalNetSubscriptions"],
        "totalCollected": col_rep["totalCollected"],
        "totalRemaining": col_rep["totalRemaining"],
    }


def compute_accounting_summary(
    students: list[dict[str, Any]],
    payouts: list[dict[str, Any]] | None = None,
    transactions: list[dict[str, Any]] | None = None,
    installments: list[dict[str, Any]] | None = None,
    month_key: str | None = None,
    teacher_meta: dict[str, Any] | None = None,
    pct_source: str = "student",
    wage_basis: str = "net",
    wage_accrual_mode: str = "accrued",
    reference_date: str | None = None,
    default_pct: int = 70,
) -> dict[str, Any]:
    """Calculates accounting and treasury summary clearly separating:
    - Accrual Basis (أساس الاستحقاق): net subscriptions for the subscription month, accrued teacher wages.
    - Cash Basis (الأساس النقدي): receipts actually paid during the period (by receipt paymentDate),
      salaries actually paid out, and extra revenues/expenses.
    """
    ref_dt = str(reference_date or "").strip() or datetime.date.today().isoformat()
    inst_map = index_installments_by_student_month(installments)
    gross_subs = 0
    discounts_total = 0
    net_subs = 0
    accrual_collected = 0
    wages = 0

    for stu in students or []:
        if not isinstance(stu, dict):
            continue
        sid = str(stu.get("id") or "")
        months = stu.get("months") or {}
        target_keys = [month_key] if month_key else list(months.keys())
        for mk in target_keys:
            m = months.get(mk)
            if not isinstance(m, dict):
                continue
            gross_v = effective_month_subscription(m)
            disc_v = effective_month_discount(m)
            net_v = effective_month_net_subscription(m)
            stu_insts = inst_map.get((sid, mk))
            w = effective_month_wage(
                stu,
                m,
                teacher_meta=teacher_meta,
                pct_source=pct_source,
                wage_basis=wage_basis,
                wage_accrual_mode=wage_accrual_mode,
                student_month_installments=stu_insts,
                reference_date=ref_dt,
                default_pct=default_pct,
            )
            if gross_v <= 0 and w <= 0:
                continue
            c = effective_month_collected(m, stu_insts, reference_date=ref_dt)
            gross_subs += gross_v
            discounts_total += disc_v
            net_subs += net_v
            accrual_collected += c
            wages += w

    # Cash-basis receipts collected by receipt paymentDate
    cash_receipts_in = 0
    for inst in installments or []:
        if not isinstance(inst, dict):
            continue
        pay_dt = str(inst.get("paymentDate") or inst.get("date") or "").strip()
        if not pay_dt or pay_dt > ref_dt:
            continue
        if month_key and not pay_dt.startswith(month_key):
            continue
        cash_receipts_in += max(0, round_half_up(inst.get("amount") or 0))

    filtered_payouts = payouts or []
    filtered_txs = transactions or []
    if month_key:
        filtered_payouts = [p for p in filtered_payouts if isinstance(p, dict) and p.get("month") == month_key]
        filtered_txs = [
            t
            for t in filtered_txs
            if isinstance(t, dict)
            and (t.get("month") or str(t.get("date") or "")[:7]) == month_key
        ]

    salary_paid = sum(max(0, round_half_up(p.get("amount") or 0)) for p in filtered_payouts if isinstance(p, dict))
    extra_rev = sum(
        max(0, round_half_up(t.get("amount") or 0))
        for t in filtered_txs
        if isinstance(t, dict) and t.get("type") == "revenue"
    )
    op_exp = sum(
        max(0, round_half_up(t.get("amount") or 0))
        for t in filtered_txs
        if isinstance(t, dict) and t.get("type") == "expense"
    )
    total_cash_in = cash_receipts_in + extra_rev
    total_cash_out = salary_paid + op_exp

    return {
        "scope": month_key or "all",
        "grossSubscriptions": gross_subs,
        "discounts": discounts_total,
        "subscriptions": net_subs,
        "collected": accrual_collected,
        "uncollected": max(0, net_subs - accrual_collected),
        "cashReceiptsInPeriod": cash_receipts_in,
        "wages": wages,
        "salaryPaid": salary_paid,
        "salaryRemaining": max(0, wages - salary_paid),
        "extraRevenue": extra_rev,
        "operatingExpense": op_exp,
        "totalCashIn": total_cash_in,
        "totalCashOut": total_cash_out,
        "netCashBalance": total_cash_in - total_cash_out,
        "netOperatingProfit": (net_subs - wages) + extra_rev - op_exp,
        "accrualBasis": {
            "label": "أساس الاستحقاق (حسب شهر الاشتراك)",
            "grossSubscriptions": gross_subs,
            "discounts": discounts_total,
            "netSubscriptions": net_subs,
            "collectedForMonth": accrual_collected,
            "uncollectedArrears": max(0, net_subs - accrual_collected),
            "accruedWages": wages,
            "netOperatingProfit": (net_subs - wages) + extra_rev - op_exp,
        },
        "cashBasis": {
            "label": "الأساس النقدي (حركة الخزينة الفعلية بالتاريخ)",
            "cashReceiptsIn": cash_receipts_in,
            "extraRevenueIn": extra_rev,
            "totalCashIn": total_cash_in,
            "salariesPaidOut": salary_paid,
            "operatingExpenseOut": op_exp,
            "totalCashOut": total_cash_out,
            "netCashFlow": total_cash_in - total_cash_out,
        },
    }
