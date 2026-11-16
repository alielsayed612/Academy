"""Shared helpers for the Al-Ahd ERP test-suite (stdlib `unittest` only)."""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

import os

# ALAHD_SRC_ROOT lets the same tests run against another checkout (e.g. the original code, to prove a bug).
ROOT = pathlib.Path(os.environ.get("ALAHD_SRC_ROOT") or pathlib.Path(__file__).resolve().parents[1])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alahd_erp.infrastructure.database import Store  # noqa: E402


class StoreTestCase(unittest.TestCase):
    """Creates an isolated SQLite store per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = pathlib.Path(self._tmp.name)
        self.db_path = self.home / "t.sqlite3"
        self.store = Store(self.db_path)
        self.addCleanup(self._safe_close)

    def _safe_close(self) -> None:
        try:
            self.store.close()
        except Exception:
            pass

    def reopen(self) -> Store:
        self.store.close()
        self.store = Store(self.db_path)
        return self.store

    # -- tiny builders -------------------------------------------------
    def seed(self, month: str = "2026-11", value: int = 500, discount: int = 0) -> None:
        """One teacher, one student with one subscribed month."""
        self.store.upsert_teacher("معلمة أ", {"pct": 70})
        self.store.upsert_student({
            "id": "s1", "name": "طالبة ١", "phone": "01000000001", "teacher": "معلمة أ",
            "type": "8 حلق", "pct": 70,
            "months": {month: {"value": value, "discount": discount,
                               "actualStartDate": f"{month}-02", "endDate": f"{month}-28"}},
        })
