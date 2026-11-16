"""Phase 1 — backup/restore safety (Store level, isolated temp databases only)."""
from __future__ import annotations

import sqlite3

from tests._helpers import StoreTestCase


class RestoreSafety(StoreTestCase):
    def _seed(self):
        self.store.upsert_teacher("T1", {"pct": 60}, "t", None)

    def _fake_backup(self, name: str, ddl: str):
        f = self.home / name
        c = sqlite3.connect(f)
        c.executescript(ddl)
        c.commit()
        c.close()
        return f

    def test_incompatible_backup_rolls_back_and_keeps_data(self):
        """Finding F-01: an integrity-valid but schema-incompatible backup used to leave the live DB broken."""
        self._seed()
        bad = self._fake_backup(
            "alahd_manual_bad.sqlite3",
            "CREATE TABLE students(x); CREATE TABLE months(x); CREATE TABLE teachers(x); PRAGMA user_version=0;",
        )
        with self.assertLogs("alahd_erp.infrastructure.database", level="ERROR"):
            with self.assertRaises(ValueError) as cm:
                self.store.restore_from(bad)
        self.assertNotIn("no such column", str(cm.exception))      # no internal SQL detail in the message
        self.assertEqual(self.store.export_state().get("teachers"), ["T1"])
        self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_store_still_usable_after_failed_restore(self):
        self._seed()
        bad = self._fake_backup(
            "alahd_manual_bad2.sqlite3",
            "CREATE TABLE students(x); CREATE TABLE months(x); CREATE TABLE teachers(x);",
        )
        with self.assertLogs("alahd_erp.infrastructure.database", level="ERROR"):
            with self.assertRaises(ValueError):
                self.store.restore_from(bad)
        self.store.upsert_teacher("T2", {"pct": 55}, "t", None)
        self.assertEqual(sorted(self.store.export_state()["teachers"]), ["T1", "T2"])

    def test_rev_is_not_lower_after_failed_restore(self):
        self._seed()
        before = self.store.rev
        bad = self._fake_backup("alahd_manual_bad3.sqlite3", "CREATE TABLE students(x); CREATE TABLE months(x); CREATE TABLE teachers(x);")
        with self.assertLogs("alahd_erp.infrastructure.database", level="ERROR"):
            with self.assertRaises(ValueError):
                self.store.restore_from(bad)
        self.assertGreaterEqual(self.store.rev, before)

    def test_non_sqlite_and_empty_files_are_rejected_without_touching_data(self):
        self._seed()
        junk = self.home / "alahd_manual_junk.sqlite3"
        junk.write_bytes(b"this is not a database" * 100)
        empty = self.home / "alahd_manual_empty.sqlite3"
        empty.write_bytes(b"")
        for f in (junk, empty):
            with self.assertRaises(ValueError):
                self.store.restore_from(f)
        self.assertEqual(self.store.export_state().get("teachers"), ["T1"])

    def test_sqlite_without_academy_tables_is_rejected(self):
        self._seed()
        other = self._fake_backup("alahd_manual_other.sqlite3", "CREATE TABLE foo(a);")
        with self.assertRaises(ValueError):
            self.store.restore_from(other)
        self.assertEqual(self.store.export_state().get("teachers"), ["T1"])

    def test_valid_restore_roundtrip_and_pre_restore_snapshot(self):
        self._seed()
        good = self.home / "alahd_manual_good.sqlite3"
        self.store.backup_to(good)
        self.store.upsert_teacher("T2", {"pct": 50}, "t", None)
        self.store.restore_from(good)
        self.assertEqual(self.store.export_state()["teachers"], ["T1"])
        snaps = list((self.home / "backups").glob("alahd_safety_pre_restore_*.sqlite3"))
        self.assertTrue(snaps, "a pre_restore safety snapshot must exist")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_backup_to_never_leaves_partial_file_on_success(self):
        self._seed()
        dest = self.home / "alahd_manual_x.sqlite3"
        self.store.backup_to(dest)
        self.assertTrue(dest.is_file())
        self.assertFalse((self.home / "alahd_manual_x.sqlite3.part").exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
