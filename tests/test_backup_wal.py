"""Backup / restore / reset safety with SQLite WAL mode (Store level, isolated temp directories only).

Every behaviour asserted here was first observed on the real code (see docs/IMPLEMENTATION_LOG.md).
DEFECT-B1 (orphan -wal/-shm files) was found by these tests in session 4 and fixed in 1.2.0.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from tests._helpers import StoreTestCase


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _count(path: Path, table: str) -> int:
    c = _ro(path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def _names(folder: Path) -> list[str]:
    return sorted(p.name for p in Path(folder).iterdir())


class WalAwareness(StoreTestCase):
    def test_precondition_live_data_is_only_in_the_wal(self):
        """If this ever fails the other tests no longer prove WAL-awareness (autocheckpoint kicked in)."""
        self.seed()
        copy = self.home / "main_file_only.sqlite3"
        shutil.copy2(self.db_path, copy)                       # what a naive file copy would capture
        with self.assertRaises(sqlite3.DatabaseError):
            _count(copy, "students")

    def test_backup_contains_data_that_is_still_in_the_wal(self):
        self.seed()
        dest = self.home / "bk" / "alahd_manual_wal.sqlite3"
        self.store.backup_to(dest)
        self.assertEqual(_count(dest, "students"), 1)
        self.assertEqual(_count(dest, "months"), 1)

    def test_backup_main_file_is_self_contained(self):
        """Copy ONLY the .sqlite3 (no -wal/-shm) somewhere else: it must still open, pass integrity_check, hold data."""
        self.seed()
        dest = self.home / "bk" / "alahd_manual_self.sqlite3"
        self.store.backup_to(dest)
        lone = self.home / "elsewhere" / "lone.sqlite3"
        lone.parent.mkdir()
        shutil.copy2(dest, lone)
        c = _ro(lone)
        try:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            c.close()
        self.assertEqual(_count(lone, "students"), 1)

    def test_close_checkpoints_and_removes_wal_and_shm(self):
        self.seed()
        wal, shm = (self.db_path.with_name(self.db_path.name + s) for s in ("-wal", "-shm"))
        self.assertTrue(wal.exists())                          # live: WAL present
        self.store.close()
        self.assertFalse(wal.exists() and wal.stat().st_size > 0, "WAL must be empty/removed after a clean close")
        self.assertFalse(shm.exists())
        lone = self.home / "lone.sqlite3"
        shutil.copy2(self.db_path, lone)                       # after a clean close the main file alone is complete
        self.assertEqual(_count(lone, "students"), 1)
        self.reopen()                                          # and the store reopens with all data
        self.assertEqual(self.store.count_students(), 1)

    def test_backup_to_is_atomic_no_part_file_left(self):
        self.seed()
        dest = self.home / "bk" / "alahd_manual_atomic.sqlite3"
        self.store.backup_to(dest)
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    # ---- DEFECT-B1 (fixed in 1.2.0): verify_sqlite_backup_file used to leave `*.part-wal`/`*.part-shm` beside every
    # backup and restore_from left `-wal`/`-shm` beside the source file. Now only the backup file itself remains.
    def test_backup_leaves_no_wal_shm_part_companions(self):
        self.seed()
        folder = self.home / "bk"
        self.store.backup_to(folder / "alahd_manual_clean.sqlite3")
        self.assertEqual(_names(folder), ["alahd_manual_clean.sqlite3"])

    def test_restore_leaves_no_companions_next_to_the_source_backup(self):
        self.seed()
        src = self.home / "bk" / "alahd_manual_src.sqlite3"
        self.store.backup_to(src)
        self.store.restore_from(src)
        self.assertEqual(_names(src.parent), ["alahd_manual_src.sqlite3"])

    def test_restore_never_deletes_companions_that_already_existed(self):
        self.seed()
        src = self.home / "bk" / "alahd_manual_pre.sqlite3"
        self.store.backup_to(src)
        wal = src.with_name(src.name + "-wal")
        wal.write_bytes(b"")                                   # pre-existing (even empty) file belongs to the user
        self.store.restore_from(src)
        self.assertTrue(wal.exists())

    def test_legacy_orphans_from_old_versions_are_swept_on_next_backup(self):
        self.seed()
        folder = self.home / "bk"
        folder.mkdir()
        (folder / "alahd_manual_old.sqlite3.part-wal").write_bytes(b"")
        (folder / "alahd_manual_old.sqlite3.part-shm").write_bytes(b"\0" * 32768)
        self.store.backup_to(folder / "alahd_manual_new.sqlite3")
        self.assertEqual(_names(folder), ["alahd_manual_new.sqlite3"])

    def test_rotate_removes_companions_of_deleted_backups(self):
        folder = self.home / "rot"
        folder.mkdir()
        for i in range(3):
            f = folder / f"alahd_auto_{i}.sqlite3"
            f.write_bytes(b"x")
            (folder / f"{f.name}-wal").write_bytes(b"")
            os.utime(f, (1000 + i, 1000 + i))
        from alahd_erp.infrastructure.database import rotate
        rotate(folder, "alahd_auto_*.sqlite3", 1)
        self.assertEqual(_names(folder), ["alahd_auto_2.sqlite3", "alahd_auto_2.sqlite3-wal"])

    def test_orphans_are_harmless_to_listing_and_restore(self):
        """Whatever the leak, the orphans must not be listed as backups nor break a later restore."""
        self.seed()
        folder = self.home / "backups"
        a = folder / "alahd_manual_a.sqlite3"
        self.store.backup_to(a)
        listed = [b["name"] for b in self.store.list_backup_files(folder)]
        self.assertEqual(listed, ["alahd_manual_a.sqlite3"])
        self.store.upsert_teacher("T2", {"pct": 50})
        self.store.restore_from(a)
        self.assertEqual(self.store.export_state()["teachers"], ["معلمة أ"])


class ResetSnapshot(StoreTestCase):
    def test_reset_takes_snapshot_first_and_it_holds_the_pre_reset_data(self):
        self.seed()
        folder = self.home / "backups"
        res = self.store.reset_application(backup_folder=folder)
        snap = folder / res["snapshot"]
        self.assertTrue(res["snapshot"].startswith("alahd_safety_before_reset_app_"))
        self.assertTrue(snap.is_file())
        self.assertEqual((_count(snap, "students"), _count(snap, "months")), (1, 1))     # data BEFORE the wipe
        self.assertEqual(self.store.count_students(), 0)                                 # live DB really wiped
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM months").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_reset_then_restore_snapshot_recovers_everything(self):
        self.seed()
        self.store.upsert_installment({"id": "i1", "studentId": "s1", "month": "2026-11", "amount": 200,
                                       "paymentDate": "2026-11-05", "receiptNo": "R1"})
        before = self.store.export_state()
        folder = self.home / "backups"
        res = self.store.reset_application(backup_folder=folder)
        self.assertEqual(self.store.export_state()["students"], [])
        self.assertEqual(self.store.restore_from(folder / res["snapshot"]), 1)
        after = self.store.export_state()
        for key in ("students", "installments", "teachers", "teacherMeta"):
            self.assertEqual(after[key], before[key], key)

    def test_reset_preserve_settings_keeps_settings_and_wipes_operational_data(self):
        self.seed()
        self.store.update_settings({"wageBasis": "gross"})
        self.store.reset_application(backup_folder=self.home / "backups", preserve_settings=True)
        self.assertEqual(self.store.get_settings()["wageBasis"], "gross")
        self.assertEqual(self.store.count_students(), 0)

    def test_reset_without_settings_preservation_restores_defaults(self):
        self.seed()
        self.store.update_settings({"wageBasis": "gross"})
        self.store.reset_application(backup_folder=self.home / "backups", preserve_settings=False)
        self.assertEqual(self.store.get_settings()["wageBasis"], "net")

    def test_reset_is_audited(self):
        self.seed()
        res = self.store.reset_application(backup_folder=self.home / "backups")
        row = self.store.db.execute("SELECT detail FROM audit WHERE action='reset_application'").fetchone()
        self.assertIn(res["snapshot"], row[0])

    def test_reset_clears_closed_months(self):
        self.seed()
        self.store.close_month("2026-11")
        self.store.reset_application(backup_folder=self.home / "backups")
        self.assertEqual(self.store.list_closed_months(), [])
        self.store.upsert_teacher("T", {"pct": 50})            # writes work again (nothing is frozen any more)


class RestoreSnapshots(StoreTestCase):
    def test_pre_restore_snapshot_holds_the_live_data_that_was_replaced(self):
        """The existing test only checks that a pre_restore file exists; this proves it holds the OLD live data."""
        self.seed()
        good = self.home / "alahd_manual_good.sqlite3"
        self.store.backup_to(good)                              # backup: 1 teacher
        self.store.upsert_teacher("T-live-only", {"pct": 55})   # live now has 2 teachers
        self.store.restore_from(good)
        self.assertEqual(self.store.export_state()["teachers"], ["معلمة أ"])
        snap = next((self.home / "backups").glob("alahd_safety_pre_restore_*.sqlite3"))
        c = _ro(snap)
        try:
            names = sorted(r[0] for r in c.execute("SELECT name FROM teachers"))
        finally:
            c.close()
        self.assertEqual(names, sorted(["معلمة أ", "T-live-only"]))

    def test_restore_ignores_garbage_wal_and_shm_next_to_the_source(self):
        self.seed()
        src = self.home / "alahd_manual_src.sqlite3"
        self.store.backup_to(src)
        for suffix, size in (("-wal", 4152), ("-shm", 32768)):
            src.with_name(src.name + suffix).write_bytes(os.urandom(size))
        self.store.upsert_teacher("T2", {"pct": 50})
        self.assertEqual(self.store.restore_from(src), 1)
        self.assertEqual(self.store.export_state()["teachers"], ["معلمة أ"])
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.store.upsert_teacher("T3", {"pct": 50})            # still writable afterwards

    def test_restore_with_empty_zero_byte_wal_next_to_source(self):
        """The state older versions (<= 1.1.0) left next to every backup."""
        self.seed()
        src = self.home / "alahd_manual_src0.sqlite3"
        self.store.backup_to(src)
        src.with_name(src.name + "-wal").write_bytes(b"")
        self.assertEqual(self.store.restore_from(src), 1)

    def test_restore_does_not_modify_the_source_backup_file(self):
        self.seed()
        src = self.home / "alahd_manual_ro.sqlite3"
        self.store.backup_to(src)
        before = src.read_bytes()
        self.store.restore_from(src)
        self.assertEqual(src.read_bytes(), before)

    def test_restored_store_survives_close_and_reopen(self):
        self.seed()
        src = self.home / "alahd_manual_re.sqlite3"
        self.store.backup_to(src)
        self.store.reset_application(backup_folder=self.home / "backups")
        self.store.restore_from(src)
        self.reopen()
        self.assertEqual(self.store.count_students(), 1)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


class SnapshotRotationAndMirror(StoreTestCase):
    def test_snapshot_rotation_keeps_newest_n(self):
        self.seed()
        folder = self.home / "backups"
        names = [self.store.snapshot(folder, f"t{i}", keep=3).name for i in range(6)]
        kept = sorted(p.name for p in folder.glob("alahd_safety_*.sqlite3"))
        self.assertEqual(len(kept), 3)
        self.assertTrue(set(kept) <= set(names))

    def test_mirror_copy_is_made_to_extra_folder(self):
        self.seed()
        extra = self.home / "usb"
        self.store.update_settings({"extraBackupFolder": str(extra)})
        dest = self.home / "bk" / "alahd_manual_m.sqlite3"
        self.store.backup_to(dest)
        self.assertTrue((extra / dest.name).is_file())
        self.assertEqual(_count(extra / dest.name, "students"), 1)

    def test_mirror_failure_is_silent_documents_F15(self):
        """F-15 (open, LOW): an unreachable extra folder makes the mirror copy fail silently - backup_to still
        returns normally and the primary backup is fine. `get_backup_health` is where the UI can learn about it."""
        self.seed()
        blocker = self.home / "not_a_dir"
        blocker.write_text("x")
        self.store.update_settings({"extraBackupFolder": str(blocker / "sub")})   # cannot be created under a file
        dest = self.home / "bk" / "alahd_manual_f.sqlite3"
        self.store.backup_to(dest)                                                # must not raise (current behaviour)
        self.assertEqual(_count(dest, "students"), 1)
        health = self.store.get_backup_health(self.home / "bk")
        self.assertFalse(health["extraFolderReachable"])
        self.assertTrue(health["extraFolderError"])


if __name__ == "__main__":
    unittest.main()
