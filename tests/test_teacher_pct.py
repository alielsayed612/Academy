import json
import unittest

from tests._helpers import StoreTestCase


class TeacherPctAliases(StoreTestCase):
    """`pct` (wage calc / UI) and `defaultPct` (importer / wizard) used to drift apart."""

    def meta(self, name):
        return self.store.export_state()["teacherMeta"][name]

    def test_upsert_with_pct_fills_default_pct(self):
        self.store.upsert_teacher("أ", {"pct": 60})
        self.assertEqual(self.meta("أ")["pct"], 60)
        self.assertEqual(self.meta("أ")["defaultPct"], 60)

    def test_upsert_with_default_pct_fills_pct(self):
        self.store.upsert_teacher("أ", {"defaultPct": 55})
        self.assertEqual(self.meta("أ")["pct"], 55)

    def test_editing_pct_updates_both_keys(self):
        self.store.upsert_teacher("أ", {"defaultPct": 70})
        self.store.upsert_teacher("أ", {"pct": 50})
        self.assertEqual(self.meta("أ")["pct"], 50)
        self.assertEqual(self.meta("أ")["defaultPct"], 50)

    def test_invalid_pct_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.upsert_teacher("أ", {"pct": 150})

    def test_legacy_rows_with_only_default_pct_are_migrated_on_open(self):
        self.store.upsert_teacher("قديمة", {"phone": "1"})
        with self.store.lock:
            self.store.db.execute(
                "UPDATE teachers SET meta=? WHERE name=?",
                (json.dumps({"defaultPct": 65, "active": True}), "قديمة"),
            )
            self.store.db.commit()
        self.assertEqual(self.reopen().export_state()["teacherMeta"]["قديمة"]["pct"], 65)

    def test_backfill_failure_is_logged_not_fatal(self):
        class Boom:
            def execute(self, *a, **k):
                raise RuntimeError("boom")
        real = self.store.db
        self.store.db = Boom()
        try:
            with self.assertLogs("alahd_erp.infrastructure.database", level="ERROR"):
                self.store._backfill_teacher_pct()  # must not raise
        finally:
            self.store.db = real

    def test_imported_percentage_drives_teacher_wage(self):
        """A 65% teacher imported from a file must give a 65% wage, not the 70% fallback."""
        from decimal import Decimal
        from alahd_erp.domain.finance_engine import resolve_wage_pct
        from alahd_erp.services.import_service import commit_import

        bk = self.home / "backups"
        bk.mkdir()
        csv = "الاسم,النسبة\nمعلمة ب,65\n".encode("utf-8-sig")
        commit_import(self.store, csv, "t.csv", "teachers", backup_folder=bk)
        meta = self.store.export_state()["teacherMeta"]
        pct = resolve_wage_pct({"teacher": "معلمة ب"}, meta, pct_source="teacher")
        self.assertEqual(pct, Decimal("0.65"))


if __name__ == "__main__":
    unittest.main()
