"""Synthetic Demo Dataset Generator (Strictly Fake Names & Phones Only).

Used ONLY when the user explicitly clicks "تحميل بيانات تجريبية" (Load Demo Data)
in the System screen after confirming. Never loaded automatically on startup.
"""
from __future__ import annotations

import datetime
from typing import Any

from .attendance_engine import parse_iso_date


def build_demo_dataset(reference_date: str | None = None) -> dict[str, Any]:
    """Generates a self-contained synthetic demo dataset with clearly fake names and phones."""
    ref_dt = parse_iso_date(str(reference_date or "")) or datetime.date.today()
    cur_mk = f"{ref_dt.year:04d}-{ref_dt.month:02d}"
    prev_total = (ref_dt.year * 12 + (ref_dt.month - 1)) - 1
    prev_mk = f"{prev_total // 12:04d}-{(prev_total % 12) + 1:02d}"

    teachers = ["معلمة تجريبية أ", "معلمة تجريبية ب"]
    teacher_meta = {
        "معلمة تجريبية أ": {"pct": 70},
        "معلمة تجريبية ب": {"pct": 70},
    }
    sub_types = [
        "8 حلقات-40 دقيقة",
        "8 حلقات-ساعة",
        "4 حلقات - نصف ساعة",
        "مجموعة",
    ]
    pricing_config = [
        {"name": "8 حلقات-40 دقيقة", "price": 300, "sessions": 8, "durationDays": 30},
        {"name": "8 حلقات-ساعة", "price": 400, "sessions": 8, "durationDays": 30},
        {"name": "4 حلقات - نصف ساعة", "price": 200, "sessions": 4, "durationDays": 30},
        {"name": "مجموعة", "price": 150, "sessions": 8, "durationDays": 30},
    ]

    students = [
        {
            "id": "stu0001",
            "code": "DEMO-0001",
            "name": "طالبة تجريبية ١",
            "phone": "01000000001",
            "teacher": "معلمة تجريبية أ",
            "type": "8 حلقات-40 دقيقة",
            "pct": 70,
            "paymentDate": f"{cur_mk}-02",
            "actualStartDate": f"{cur_mk}-05",
            "months": {
                prev_mk: {
                    "value": 300,
                    "discount": 0,
                    "paymentDate": f"{prev_mk}-03",
                    "actualStartDate": f"{prev_mk}-05",
                    "endDate": f"{cur_mk}-05",
                    "paidStatus": "paid",
                },
                cur_mk: {
                    "value": 300,
                    "discount": 0,
                    "paymentDate": f"{cur_mk}-02",
                    "actualStartDate": f"{cur_mk}-05",
                    "endDate": f"{cur_mk}-28",
                    "paidStatus": "paid",
                },
            },
        },
        {
            "id": "stu0002",
            "code": "DEMO-0002",
            "name": "طالبة تجريبية ٢",
            "phone": "01000000002",
            "teacher": "معلمة تجريبية أ",
            "type": "8 حلقات-ساعة",
            "pct": 70,
            "paymentDate": f"{cur_mk}-08",
            "actualStartDate": f"{cur_mk}-03",
            "months": {
                cur_mk: {
                    "value": 400,
                    "discount": 50,
                    "paymentDate": f"{cur_mk}-08",
                    "actualStartDate": f"{cur_mk}-03",
                    "endDate": f"{cur_mk}-28",
                    "paidStatus": "partial",
                },
            },
        },
        {
            "id": "stu0003",
            "code": "DEMO-0003",
            "name": "طالبة تجريبية ٣",
            "phone": "01000000003",
            "teacher": "معلمة تجريبية ب",
            "type": "4 حلقات - نصف ساعة",
            "pct": 70,
            "paymentDate": "",
            "actualStartDate": f"{cur_mk}-04",
            "months": {
                cur_mk: {
                    "value": 200,
                    "discount": 0,
                    "paymentDate": "",
                    "actualStartDate": f"{cur_mk}-04",
                    "endDate": f"{cur_mk}-28",
                    "paidStatus": "unpaid",
                },
            },
        },
        {
            "id": "stu0004",
            "code": "DEMO-0004",
            "name": "طالبة تجريبية ٤",
            "phone": "01000000004",
            "teacher": "معلمة تجريبية ب",
            "type": "مجموعة",
            "pct": 70,
            "paymentDate": f"{cur_mk}-01",
            "actualStartDate": f"{cur_mk}-10",
            "months": {
                cur_mk: {
                    "value": 150,
                    "discount": 0,
                    "paymentDate": f"{cur_mk}-01",
                    "actualStartDate": f"{cur_mk}-10",
                    "endDate": f"{cur_mk}-28",
                    "paidStatus": "paid",
                },
            },
        },
    ]

    installments = [
        {
            "id": "demo_rcp_1",
            "receiptNo": f"RCP-{prev_mk.replace('-', '')}-0001",
            "studentId": "stu0001",
            "month": prev_mk,
            "amount": 300,
            "date": f"{prev_mk}-03",
            "paymentDate": f"{prev_mk}-03",
            "method": "فودافون كاش",
            "note": "دفعة تجريبية سابقة",
        },
        {
            "id": "demo_rcp_2",
            "receiptNo": f"RCP-{cur_mk.replace('-', '')}-0001",
            "studentId": "stu0001",
            "month": cur_mk,
            "amount": 300,
            "date": f"{cur_mk}-02",
            "paymentDate": f"{cur_mk}-02",
            "method": "فودافون كاش",
            "note": "دفع مسبق قبل تاريخ البداية الفعلي",
        },
        {
            "id": "demo_rcp_3",
            "receiptNo": f"RCP-{cur_mk.replace('-', '')}-0002",
            "studentId": "stu0002",
            "month": cur_mk,
            "amount": 200,
            "date": f"{cur_mk}-08",
            "paymentDate": f"{cur_mk}-08",
            "method": "إنستاباي",
            "note": "دفعة جزئية تجريبية",
        },
        {
            "id": "demo_rcp_4",
            "receiptNo": f"RCP-{cur_mk.replace('-', '')}-0003",
            "studentId": "stu0004",
            "month": cur_mk,
            "amount": 150,
            "date": f"{cur_mk}-01",
            "paymentDate": f"{cur_mk}-01",
            "method": "نقدي",
            "note": "دفعة تجريبية",
        },
    ]

    payouts = [
        {
            "id": "demo_sal_1",
            "teacher": "معلمة تجريبية أ",
            "month": prev_mk,
            "amount": 210,
            "date": f"{prev_mk}-28",
            "note": "راتب تجريبي",
        }
    ]

    transactions = [
        {
            "id": "demo_tx_1",
            "type": "expense",
            "category": "مصروفات تشغيلية تجريبية",
            "amount": 50,
            "date": f"{cur_mk}-05",
            "month": cur_mk,
            "method": "نقدي",
            "note": "قيد تجريبي",
        }
    ]

    plans = [
        {
            "id": "demo_pln_1",
            "studentId": "stu0001",
            "studentName": "طالبة تجريبية ١",
            "teacher": "معلمة تجريبية أ",
            "count": 4,
            "days": [0, 3],
            "time": "16:00",
            "dur": 40,
            "startDate": f"{cur_mk}-05",
            "actualStartDate": f"{cur_mk}-05",
        }
    ]

    sessions = [
        {
            "id": "demo_sess_1",
            "planId": "demo_pln_1",
            "studentId": "stu0001",
            "teacher": "معلمة تجريبية أ",
            "date": f"{cur_mk}-05",
            "time": "16:00",
            "dur": 40,
            "status": "done",
            "notes": "حصة تجريبية مكتملة",
        },
        {
            "id": "demo_sess_2",
            "planId": "demo_pln_1",
            "studentId": "stu0001",
            "teacher": "معلمة تجريبية أ",
            "date": f"{cur_mk}-08",
            "time": "16:00",
            "dur": 40,
            "status": "cancel_teacher",
            "notes": "إلغاء معلمة تجريبي غير معوض بعد",
        },
    ]

    memorization = [
        {
            "id": "demo_mem_1",
            "studentId": "stu0001",
            "teacher": "معلمة تجريبية أ",
            "date": f"{cur_mk}-05",
            "track": "حفظ جديد",
            "fromSurah": "الملك",
            "fromAyah": 1,
            "toSurah": "الملك",
            "toAyah": 15,
            "juz": 29,
            "pages": 1.5,
            "grade": "ممتاز متقن",
            "score": 96,
            "notes": "تسميع تجريبي",
        }
    ]

    madrasa_batches = [
        {
            "id": "demo_batch_1",
            "name": "دفعة تجريبية - الأولى",
            "slogan": "يوم لا يخلو من القرآن",
            "fee": 60,
            "startDate": f"{cur_mk}-01",
            "supervisor": "معلمة تجريبية أ",
            "active": True,
        }
    ]

    madrasa_enrollments = [
        {
            "id": "demo_enr_1",
            "batchId": "demo_batch_1",
            "studentId": "stu0001",
            "enrolledAt": f"{cur_mk}-01",
            "feePaid": 60,
            "streak": 5,
            "bestStreak": 5,
            "badges": ["🌟 نجمة المواظبة"],
            "tracksProgress": {"qurani": 100, "taabudi": 90, "ilmi": 85, "tarbawi": 95},
        }
    ]

    return {
        "teachers": teachers,
        "teacherMeta": teacher_meta,
        "subTypes": sub_types,
        "pricingConfig": pricing_config,
        "students": students,
        "installments": installments,
        "payouts": payouts,
        "transactions": transactions,
        "plans": plans,
        "sessions": sessions,
        "memorization": memorization,
        "madrasaBatches": madrasa_batches,
        "madrasaEnrollments": madrasa_enrollments,
        "madrasaLogs": [],
        "certificates": [],
    }
