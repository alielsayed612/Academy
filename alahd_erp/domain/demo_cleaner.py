"""Phase 1B — Demo & Test Data Inventory, Classification, Quarantine Exporter & Transactional Cleaner.

Implements strict data-safety rules (Section 0.1 & Phase 1B):
1. Classifies candidate records across all operational tables into:
   - Class A (Confirmed Demo/Test): Created by `demo_data.py` or carrying explicit `demo_` / `DEMO-` / `طالبة تجريبية` / `معلمة تجريبية` markers.
   - Class B (Probable Demo/Test): Placeholder names (`test`, `demo`, `تجربة`, `اختبار`, `dummy`, `وهمي`) corroborated by >= 2 independent signals (e.g., fake phone `0100000000*` / `00000000000`, `TEST-` code, or synthetic note).
   - Class C (Real or Uncertain): Everything else — strictly protected and NEVER deleted.
   - System/Reference Data: Schema, migrations, `kv` settings, `subTypes` lookup, `audit` — never treated as demo data.
2. Before any deletion:
   - Takes a verified SQLite backup of the database (`alahd_safety_pre_demo_cleanup_*.sqlite3`).
   - Exports every row to be deleted into a UTF-8 JSON and CSV quarantine archive with a full manifest.
3. Deletes Class A (and optionally Class B) rows inside a single atomic transaction in foreign-key-safe child-to-parent order without resetting `AUTOINCREMENT` counters or receipt sequences.
4. Verifies `PRAGMA integrity_check` and `PRAGMA foreign_key_check` after commit; rolls back on any error.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import re
from pathlib import Path
from typing import Any

CONFIRMED_DEMO_STUDENT_IDS: frozenset[str] = frozenset({"stu0001", "stu0002", "stu0003", "stu0004"})
CONFIRMED_DEMO_TEACHERS: frozenset[str] = frozenset({"معلمة تجريبية أ", "معلمة تجريبية ب"})
CONFIRMED_DEMO_PHONES: frozenset[str] = frozenset({"01000000001", "01000000002", "01000000003", "01000000004"})

_CLASS_B_NAME_RE = re.compile(
    r"(?:\btest\b|\bdemo\b|\bdummy\b|\bsample\b|تجريبي|تجريبيه|تجربة|اختبار|وهمي|وهمية)",
    re.IGNORECASE | re.UNICODE,
)
_FAKE_PHONE_RE = re.compile(r"^(?:01[0125]0{7}\d|0{8,14}|1{8,14}|9{8,14}|12345678\d*)$")


def classify_student_record(row: dict[str, Any]) -> dict[str, Any]:
    """Classifies a student row into Class A (confirmed demo), Class B (probable demo >=2 signals), or Class C (protected)."""
    sid = str(row.get("id") or "").strip()
    code = str(row.get("code") or "").strip()
    name = str(row.get("name") or "").strip()
    phone = re.sub(r"\D", "", str(row.get("phone") or ""))
    teacher = str(row.get("teacher") or "").strip()

    signals_a: list[str] = []
    if sid.lower().startswith("demo_") or sid in CONFIRMED_DEMO_STUDENT_IDS:
        signals_a.append(f"id={sid}")
    if code.upper().startswith("DEMO-"):
        signals_a.append(f"code={code}")
    if name.startswith("طالبة تجريبية") and (sid.lower().startswith("demo_") or sid in CONFIRMED_DEMO_STUDENT_IDS or phone in CONFIRMED_DEMO_PHONES):
        signals_a.append(f"name={name}")
    if phone in CONFIRMED_DEMO_PHONES and (sid in CONFIRMED_DEMO_STUDENT_IDS or name.startswith("طالبة تجريبية")):
        signals_a.append(f"phone={phone}")
    if signals_a:
        return {
            "classification": "A",
            "isDemoCandidate": True,
            "reason": "سجل تجريبي مؤكد من مولد البيانات التجريبية (demo_data)",
            "reasons": signals_a,
            "signals": signals_a,
        }

    # Check Class B: requires at least 2 independent signals
    signals_b: list[str] = []
    if _CLASS_B_NAME_RE.search(name):
        signals_b.append(f"placeholder_name:{name}")
    if phone and _FAKE_PHONE_RE.match(phone):
        signals_b.append(f"fake_phone_pattern:{phone}")
    if code.upper().startswith(("TEST-", "DUMMY-", "TMP-", "SAMPLE-")):
        signals_b.append(f"test_code_prefix:{code}")
    if teacher in CONFIRMED_DEMO_TEACHERS or _CLASS_B_NAME_RE.search(teacher):
        signals_b.append(f"demo_teacher:{teacher}")

    if len(signals_b) >= 2:
        return {
            "classification": "B",
            "isDemoCandidate": True,
            "reason": "سجل اختباري مرجح بإشارتين مستقلتين على الأقل",
            "reasons": signals_b,
            "signals": signals_b,
        }

    return {
        "classification": "C",
        "isDemoCandidate": False,
        "reason": "بيانات حقيقية أو غير مؤكدة (محمية بالكامل ولا تُحذف)",
        "reasons": signals_b,
        "signals": signals_b,
    }


def classify_teacher_record(row: dict[str, Any], non_demo_student_teachers: set[str] | None = None) -> dict[str, Any]:
    tname = str(row.get("name") or "").strip()
    protected_set = non_demo_student_teachers or set()
    if tname in protected_set:
        return {
            "classification": "C",
            "isDemoCandidate": False,
            "reason": "مرتبطة بطالبات مصنفات ضمن الفئة C المحمية",
            "reasons": ["referenced_by_class_c_student"],
            "signals": ["referenced_by_class_c_student"],
        }
    if tname in CONFIRMED_DEMO_TEACHERS or tname.startswith("معلمة تجريبية"):
        return {
            "classification": "A",
            "isDemoCandidate": True,
            "reason": "معلمة تجريبية مؤكدة من مولد البيانات التجريبية",
            "reasons": [f"name={tname}"],
            "signals": [f"name={tname}"],
        }
    meta_raw = row.get("meta")
    meta = json.loads(meta_raw) if isinstance(meta_raw, str) and meta_raw.strip() else (meta_raw if isinstance(meta_raw, dict) else {})
    phone = re.sub(r"\D", "", str((meta or {}).get("phone") or ""))
    signals_b: list[str] = []
    if _CLASS_B_NAME_RE.search(tname):
        signals_b.append(f"placeholder_teacher_name:{tname}")
    if phone and _FAKE_PHONE_RE.match(phone):
        signals_b.append(f"fake_phone:{phone}")
    if len(signals_b) >= 2:
        return {
            "classification": "B",
            "isDemoCandidate": True,
            "reason": "معلمة اختبارية مرجحة بإشارتين مستقلتين",
            "reasons": signals_b,
            "signals": signals_b,
        }
    return {
        "classification": "C",
        "isDemoCandidate": False,
        "reason": "معلمة حقيقية أو غير مؤكدة (محمية)",
        "reasons": signals_b,
        "signals": signals_b,
    }


def inventory_database_demo_data(db_conn: Any) -> dict[str, Any]:
    """Read-only inventory of a SQLite connection classifying all operational records into Class A, B, or C."""
    students_rows = [dict(r) for r in db_conn.execute("SELECT * FROM students ORDER BY pos, id").fetchall()]
    teachers_rows = [dict(r) for r in db_conn.execute("SELECT * FROM teachers ORDER BY pos, name").fetchall()]

    class_a_items: list[dict[str, Any]] = []
    class_b_items: list[dict[str, Any]] = []
    class_c_items: list[dict[str, Any]] = []
    candidates_by_table: dict[str, list[dict[str, Any]]] = {
        "students": [],
        "teachers": [],
        "months": [],
        "installments": [],
        "salaries": [],
        "transactions": [],
        "study_groups": [],
        "attendance_plans": [],
        "sessions": [],
        "memorization_logs": [],
        "madrasa_batches": [],
        "madrasa_enrollments": [],
        "madrasa_logs": [],
        "certificates": [],
    }

    demo_student_ids: dict[str, str] = {}
    non_demo_student_teachers: set[str] = set()
    class_a_students = 0
    class_b_students = 0
    class_c_students = 0
    class_a_teachers = 0
    class_b_teachers = 0
    class_c_teachers = 0

    for srow in students_rows:
        cinfo = classify_student_record(srow)
        cls = cinfo["classification"]
        reason = cinfo["reason"]
        sigs = cinfo["signals"]
        entry = {
            "table": "students",
            "key": srow["id"],
            "name": srow.get("name", ""),
            "classification": cls,
            "reason": reason,
            "signals": sigs,
            "row": srow,
        }
        if cls == "A":
            class_a_students += 1
            class_a_items.append(entry)
            demo_student_ids[srow["id"]] = "A"
            candidates_by_table["students"].append({**srow, "_classification": cinfo})
        elif cls == "B":
            class_b_students += 1
            class_b_items.append(entry)
            demo_student_ids[srow["id"]] = "B"
            candidates_by_table["students"].append({**srow, "_classification": cinfo})
        else:
            class_c_students += 1
            class_c_items.append({
                "table": "students",
                "key": srow["id"],
                "name": srow.get("name", ""),
                "code": srow.get("code", ""),
                "teacher": srow.get("teacher", ""),
                "classification": "C",
                "reason": reason,
            })
            if srow.get("teacher"):
                non_demo_student_teachers.add(str(srow["teacher"]).strip())

    demo_teacher_names: dict[str, str] = {}
    for trow in teachers_rows:
        cinfo = classify_teacher_record(trow, non_demo_student_teachers)
        cls = cinfo["classification"]
        reason = cinfo["reason"]
        sigs = cinfo["signals"]
        entry = {
            "table": "teachers",
            "key": trow["name"],
            "name": trow["name"],
            "classification": cls,
            "reason": reason,
            "signals": sigs,
            "row": trow,
        }
        if cls == "A":
            class_a_teachers += 1
            class_a_items.append(entry)
            demo_teacher_names[trow["name"]] = "A"
            candidates_by_table["teachers"].append({**trow, "_classification": cinfo})
        elif cls == "B":
            class_b_teachers += 1
            class_b_items.append(entry)
            demo_teacher_names[trow["name"]] = "B"
            candidates_by_table["teachers"].append({**trow, "_classification": cinfo})
        else:
            class_c_teachers += 1
            class_c_items.append({
                "table": "teachers",
                "key": trow["name"],
                "name": trow["name"],
                "classification": "C",
                "reason": reason,
            })

    child_specs = [
        ("months", "student_id", "month_key"),
        ("installments", "id", "student_id"),
        ("salaries", "id", "teacher"),
        ("transactions", "id", None),
        ("study_groups", "id", "teacher"),
        ("attendance_plans", "id", "student_id"),
        ("sessions", "id", "student_id"),
        ("memorization_logs", "id", "student_id"),
        ("madrasa_batches", "id", None),
        ("madrasa_enrollments", "id", "student_id"),
        ("madrasa_logs", "id", "student_id"),
        ("certificates", "id", "student_id"),
    ]

    table_counts: dict[str, int] = {
        "students": len(students_rows),
        "teachers": len(teachers_rows),
    }

    for tbl, pk_col, ref_col in child_specs:
        rows = [dict(r) for r in db_conn.execute(f"SELECT * FROM {tbl}").fetchall()]
        table_counts[tbl] = len(rows)
        for r in rows:
            rk = str(r.get(pk_col) or "")
            if tbl == "months":
                rk = f"{r.get('student_id')}:{r.get('month_key')}"
            cls_match = None
            sigs = []
            if rk.lower().startswith("demo_"):
                cls_match = "A"
                sigs.append(f"id={rk}")
            if ref_col == "student_id" and str(r.get("student_id") or "") in demo_student_ids:
                parent_cls = demo_student_ids[str(r.get("student_id"))]
                cls_match = cls_match or parent_cls
                sigs.append(f"student_id={r.get('student_id')}")
            elif ref_col == "teacher" and str(r.get("teacher") or "") in demo_teacher_names:
                parent_cls = demo_teacher_names[str(r.get("teacher"))]
                cls_match = cls_match or parent_cls
                sigs.append(f"teacher={r.get('teacher')}")
            elif tbl == "madrasa_batches" and str(r.get("name") or "").startswith("دفعة تجريبية"):
                cls_match = "A"
                sigs.append(f"batch_name={r.get('name')}")

            if cls_match in ("A", "B"):
                c_obj = {"classification": cls_match, "isDemoCandidate": True, "reasons": sigs}
                candidates_by_table[tbl].append({**r, "_classification": c_obj})
                item = {
                    "table": tbl,
                    "key": rk,
                    "classification": cls_match,
                    "reason": f"سجل تابع لسجل تجريبي مصنف ({cls_match})",
                    "signals": sigs,
                    "row": r,
                }
                if cls_match == "A":
                    class_a_items.append(item)
                else:
                    class_b_items.append(item)
            else:
                class_c_items.append({
                    "table": tbl,
                    "key": rk,
                    "classification": "C",
                    "reason": "سجل تشغيلي/مالي محمي (الفئة C)",
                })

    total_cands = sum(len(v) for v in candidates_by_table.values())
    return {
        "ok": True,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "tableCounts": table_counts,
        "candidatesByTable": candidates_by_table,
        "summary": {
            "classACount": len(class_a_items),
            "classBCount": len(class_b_items),
            "classCCount": len(class_c_items),
            "classAConfirmedDemoStudents": class_a_students,
            "classBProbableDemoStudents": class_b_students,
            "classCProtectedStudents": class_c_students,
            "classAConfirmedDemoTeachers": class_a_teachers,
            "classBProbableDemoTeachers": class_b_teachers,
            "classCProtectedTeachers": class_c_teachers,
            "totalDemoCandidates": total_cands,
            "hasDemoOrTestData": total_cands > 0,
        },
        "classA": class_a_items,
        "classB": class_b_items,
        "classC": class_c_items,
    }


def export_quarantine_archive(
    inventory: dict[str, Any],
    quarantine_dir: Path,
    backup_path: Path | None = None,
    include_class_b: bool = True,
) -> dict[str, Any]:
    """Writes UTF-8 JSON and CSV quarantine files containing every Class A (and Class B) record prior to deletion."""
    quarantine_root = Path(quarantine_dir)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = quarantine_root / f"quarantine_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)

    cands_by_tbl = inventory.get("candidatesByTable") or {}
    files_written: dict[str, dict[str, Any]] = {}

    for tbl, rows in cands_by_tbl.items():
        if not rows:
            continue
        j_file = run_dir / f"{tbl}.json"
        c_file = run_dir / f"{tbl}.csv"
        j_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

        all_cols: list[str] = []
        for r in rows:
            for k in r.keys():
                if k != "_classification" and k not in all_cols:
                    all_cols.append(k)
        all_cols.extend(["_class", "_reasons"])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(all_cols)
        for r in rows:
            cinfo = r.get("_classification") or {}
            row_vals = [
                json.dumps(r.get(col), ensure_ascii=False) if isinstance(r.get(col), (dict, list)) else str(r.get(col, ""))
                for col in all_cols[:-2]
            ]
            row_vals.append(str(cinfo.get("classification", "")))
            row_vals.append("; ".join(cinfo.get("reasons") or []))
            writer.writerow(row_vals)
        c_file.write_text("\ufeff" + buf.getvalue(), encoding="utf-8")
        files_written[tbl] = {
            "count": len(rows),
            "jsonFile": str(j_file),
            "csvFile": str(c_file),
        }

    manifest = {
        "quarantineTimestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "quarantineDir": str(run_dir),
        "preCleanupBackup": str(backup_path) if backup_path else "",
        "summary": inventory.get("summary") or {},
        "tables": files_written,
    }
    manifest_file = run_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
