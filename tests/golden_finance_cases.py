"""Synthetic scenarios for the finance golden tests (no real data). Shared by the generator and the test."""
from __future__ import annotations

from typing import Any

from alahd_erp.domain import finance_engine as fe

REF = "2026-11-14"
MONTHS = ["2026-10", "2026-11"]

STUDENTS: list[dict[str, Any]] = [
    {"id": "g1", "name": "طالب اصطناعي 1", "teacher": "معلمة أ", "pct": 70,
     "months": {"2026-10": {"value": 500, "discount": 0}, "2026-11": {"value": 500, "discount": 100}}},
    {"id": "g2", "name": "طالب اصطناعي 2", "teacher": "معلمة أ", "pct": 60,
     "months": {"2026-10": {"value": 333, "discount": 0}, "2026-11": {"value": 333, "discount": 33}}},
    {"id": "g3", "name": "طالب اصطناعي 3", "teacher": "معلمة ب", "pct": 50,
     "months": {"2026-11": {"value": 700, "discount": 700}}},
    {"id": "g4", "name": "طالب اصطناعي 4", "teacher": "معلمة ب", "pct": 80,
     "months": {"2026-10": {"value": 250, "discount": 0}, "2026-11": {"value": 250, "discount": 0}}},
    {"id": "g5", "name": "طالب اصطناعي 5", "teacher": "معلمة ج",
     "months": {"2026-11": {"value": 1000, "discount": 150}}},
    {"id": "g6", "name": "طالب اصطناعي 6", "teacher": "معلمة ج", "pct": 33,
     "months": {"2026-11": {"value": 0}}},
]
TEACHER_META = {"معلمة أ": {"pct": 75}, "معلمة ب": {"pct": 55}, "معلمة ج": {"pct": 65}}
INSTALLMENTS: list[dict[str, Any]] = [
    {"id": "r1", "receiptNo": "RCP-G-1", "studentId": "g1", "month": "2026-10", "amount": 500, "paymentDate": "2026-10-03"},
    {"id": "r2", "receiptNo": "RCP-G-2", "studentId": "g1", "month": "2026-11", "amount": 150, "paymentDate": "2026-11-02"},
    {"id": "r3", "receiptNo": "RCP-G-3", "studentId": "g2", "month": "2026-11", "amount": 600, "paymentDate": "2026-11-05"},
    {"id": "r4", "receiptNo": "RCP-G-4", "studentId": "g4", "month": "2026-11", "amount": 250, "paymentDate": "2026-12-20"},
    {"id": "r5", "receiptNo": "RCP-G-5", "studentId": "g5", "month": "2026-11", "amount": 400, "paymentDate": "2026-11-10"},
]
PAYOUTS: list[dict[str, Any]] = []
TRANSACTIONS: list[dict[str, Any]] = []
POLICIES = [("net", "accrued"), ("gross", "accrued"), ("net", "collected")]


def compute_all() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for basis, mode in POLICIES:
        for pct_source in ("student", "teacher"):
            for mk in MONTHS:
                key = f"stats|{basis}|{mode}|{pct_source}|{mk}"
                out[key] = fe.compute_month_stats(STUDENTS, mk, INSTALLMENTS, TEACHER_META, pct_source=pct_source,
                                                  wage_basis=basis, wage_accrual_mode=mode, reference_date=REF)
                key = f"summary|{basis}|{mode}|{pct_source}|{mk}"
                out[key] = fe.compute_accounting_summary(STUDENTS, PAYOUTS, TRANSACTIONS, INSTALLMENTS, mk, TEACHER_META,
                                                         pct_source=pct_source, wage_basis=basis, wage_accrual_mode=mode,
                                                         reference_date=REF)
        for mk in MONTHS:
            out[f"accrual|{basis}|{mode}|{mk}"] = fe.compute_accrual_month_report(
                STUDENTS, mk, INSTALLMENTS, TEACHER_META, wage_basis=basis, wage_accrual_mode=mode, reference_date=REF)
    for mk in MONTHS:
        out[f"collection|{mk}"] = fe.compute_collection_report(STUDENTS, mk, INSTALLMENTS, reference_date=REF)
    return out
