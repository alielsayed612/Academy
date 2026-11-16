"""F6 - Excel import must stay bounded in time/memory no matter what a (tiny) .xlsx declares.

Each hostile file below is a few KB; before the fix the stray-cell file made `_extract_tabular_rows_from_file`
iterate ~17 billion cells (MemoryError at 3 GB).
"""
from __future__ import annotations

import io
import time
import tracemalloc
import unittest
import zipfile

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from tests import _helpers  # noqa: F401  (sets sys.path)
from alahd_erp.services import import_service as svc

HEADERS = ["اسم الطالب", "المعلمة", "قيمة الاشتراك"]


def _book(extra=None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    ws.append(["طالب اصطناعي ١", "معلمة أ", 100])
    ws.append(["طالب اصطناعي ٢", "معلمة ب", 200])
    if extra:
        extra(ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _run(data: bytes):
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        return svc._extract_tabular_rows_from_file(data, "x.xlsx", "students")
    finally:
        _run.elapsed = time.perf_counter() - t0
        _run.peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()


class ExcelLimitsTests(unittest.TestCase):
    def test_normal_file_unchanged(self):
        _sheet, _all, headers, rows, errs = _run(_book())
        self.assertEqual(headers[:3], HEADERS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(errs, [])

    def test_far_styled_empty_cell_is_ignored_quickly(self):
        def extra(ws):
            ws["XFD1048576"].fill = PatternFill("solid", fgColor="FFFF00")
        _sheet, _all, headers, rows, _errs = _run(_book(extra))
        self.assertEqual(len(rows), 2)
        self.assertLess(_run.elapsed, 5.0)
        self.assertLess(_run.peak, 150 * 1024 * 1024)

    def test_far_value_cell_is_rejected_quickly(self):
        def extra(ws):
            ws["XFD1048576"].value = "stray"
        with self.assertRaises(ValueError) as cm:
            _run(_book(extra))
        self.assertIn("خارج الحدود", str(cm.exception))
        self.assertLess(_run.elapsed, 5.0)
        self.assertLess(_run.peak, 150 * 1024 * 1024)

    def test_zip_bomb_is_rejected_before_parsing(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("xl/worksheets/sheet1.xml", b"0" * (svc.MAX_XLSX_UNCOMPRESSED_BYTES + 1024))
        self.assertLess(len(buf.getvalue()), svc.MAX_IMPORT_BYTES)
        with self.assertRaises(ValueError) as cm:
            _run(buf.getvalue())
        self.assertIn("فك الضغط", str(cm.exception))
        self.assertLess(_run.elapsed, 5.0)

    def test_giant_merged_range_is_rejected_before_parsing(self):
        src = _book()
        out = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "xl/worksheets/sheet1.xml":
                    data = data.replace(b"</sheetData>", b'</sheetData><mergeCells count="1"><mergeCell ref="A1:XFD1048576"/></mergeCells>')
                    self.assertIn(b"mergeCell ", data)
                zout.writestr(item, data)
        with self.assertRaises(ValueError) as cm:
            _run(out.getvalue())
        self.assertIn("دمج", str(cm.exception))
        self.assertLess(_run.elapsed, 5.0)

    def test_regular_merged_header_still_works(self):
        def extra(ws):
            ws.insert_rows(1)
            ws["A1"] = "بيانات الطلاب"
            ws.merge_cells("A1:C1")
        # Output was verified byte-identical to the pre-fix parser for this file (merged banner is propagated).
        _sheet, _all, headers, rows, _errs = _run(_book(extra))
        self.assertEqual(headers[:2], HEADERS[:2])
        self.assertEqual([d["اسم الطالب"] for _n, d in rows], ["طالب اصطناعي ١", "طالب اصطناعي ٢"])


if __name__ == "__main__":
    unittest.main()
