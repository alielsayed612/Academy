"""Regression guard for source<->EXE content consistency (scripts/verify_build_artifact.py).

No real PyInstaller build exists in the sandbox, so these tests synthesise a PyInstaller-format PE (onefile cookie,
TOC, PYZ) from the REAL source tree and prove the verifier catches each mismatch class seen on the v1.0.0 EXE that was
supplied next to the v1.2.0 source: stale modules, old version, changed/missing/extra UI files, embedded seed data,
wrong Python runtime.  They prove the *verifier*; they do not prove a Windows build or its runtime behaviour.
"""
from __future__ import annotations

import contextlib
import io
import marshal
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from tests._helpers import ROOT

sys.path.insert(0, str(ROOT / "scripts"))
import verify_build_artifact as vba  # noqa: E402

from alahd_erp import __version__  # noqa: E402


def _entry(name: str, typ: str, data: bytes, pos: int) -> tuple[bytes, bytes]:
    comp = zlib.compress(data)
    nm = name.encode() + b"\0"
    el = (18 + len(nm) + 15) // 16 * 16
    head = struct.pack("!IIIIB", el, pos, len(comp), len(data), 1) + typ.encode() + nm
    return head.ljust(el, b"\0"), comp


def build_exe(*, version: str = __version__, drop_modules=(), extra_modules=(), ui_files=None, pyver: int = 312,
              machine: int = 0x8664, include_ui_in_toc: bool = True, extra_files=()) -> bytes:
    stub = bytearray(0x200)
    stub[0:2] = b"MZ"
    struct.pack_into("<I", stub, 0x3C, 0x80)
    stub[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", stub, 0x84, machine)

    # PYZ with the app modules (code bytes are opaque to the verifier; only constants are inspected)
    names = set(vba.source_modules(ROOT)) - set(drop_modules) | set(extra_modules)
    body, toc_items = bytearray(), []
    for n in sorted(names):
        code = zlib.compress(b"\xe3\x00__version__\x00" + version.encode() if n == "alahd_erp" else b"\xe3\x00x")
        toc_items.append((n, (0, 12 + len(body), len(code))))
        body += code
    pyz = bytearray(b"PYZ\0" + b"\0\0\0\0" + struct.pack("!i", 12 + len(body))) + body + marshal.dumps(toc_items)

    files = [("main", "s", b"print(1)"), ("PYZ.pyz", "z", bytes(pyz))]
    if include_ui_in_toc:
        ui = ui_files if ui_files is not None else {
            f.relative_to(ROOT / "alahd_erp" / "ui").as_posix(): f.read_bytes()
            for f in (ROOT / "alahd_erp" / "ui").rglob("*") if f.is_file()}
        files += [("alahd_erp\\ui\\" + k.replace("/", "\\"), "b", v) for k, v in ui.items()]
    files += [(n, "b", d) for n, d in extra_files]

    blobs, toc, pos = bytearray(), bytearray(), 0
    for name, typ, data in files:
        head, comp = _entry(name, typ, data, pos)
        toc += head
        blobs += comp
        pos += len(comp)
    pkg_len = len(blobs) + len(toc) + vba.COOKIE_LEN
    cookie = vba.PYI_MAGIC + struct.pack("!IIII64s", pkg_len, len(blobs), len(toc), pyver, b"python312.dll")
    return bytes(stub) + bytes(blobs) + bytes(toc) + cookie


def status(results, prefix):
    return next(s for cid, s, _ in results if cid.startswith(prefix))


def message(results, prefix):
    return next(m for cid, _, m in results if cid.startswith(prefix))


class VerifierBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def run_verify(self, exe_bytes: bytes, **kw):
        exe = self.dir / "App.exe"
        exe.write_bytes(exe_bytes)
        return vba.verify(exe, ROOT, **kw)[0]


class GoodBuild(VerifierBase):
    def test_matching_build_has_no_failures(self):
        res = self.run_verify(build_exe())
        self.assertEqual([r for r in res if r[1] in ("FAIL", "BLOCKED")], [], res)
        for prefix in ("A1", "A2", "B1", "B2", "B3", "C1", "D1"):
            self.assertEqual(status(res, prefix), "PASS", prefix)

    def test_source_module_set_is_the_real_package(self):
        mods = vba.source_modules(ROOT)
        self.assertIn("alahd_erp.domain.finance_engine", mods)
        self.assertIn("alahd_erp.infrastructure.database", mods)
        self.assertNotIn("alahd_erp.main", mods)       # frozen as the entry script, not a PYZ module


class MismatchesAreCaught(VerifierBase):
    def test_missing_module_fails(self):
        res = self.run_verify(build_exe(drop_modules=("alahd_erp.services.import_service",)))
        self.assertEqual(status(res, "B1"), "FAIL")
        self.assertIn("alahd_erp.services.import_service", message(res, "B1"))

    def test_stale_module_not_in_source_fails(self):
        res = self.run_verify(build_exe(extra_modules=("alahd_erp.legacy_thing",)))
        self.assertEqual(status(res, "B1"), "FAIL")
        self.assertIn("legacy_thing", message(res, "B1"))

    def test_old_version_fails(self):
        res = self.run_verify(build_exe(version="1.0.0"))
        self.assertEqual(status(res, "B2"), "FAIL")

    def test_changed_ui_file_fails(self):
        ui = {f.relative_to(ROOT / "alahd_erp" / "ui").as_posix(): f.read_bytes()
              for f in (ROOT / "alahd_erp" / "ui").rglob("*") if f.is_file()}
        ui["js/app.js"] += b"\n// stale"
        res = self.run_verify(build_exe(ui_files=ui))
        self.assertEqual(status(res, "C1"), "FAIL")
        self.assertIn("js/app.js", message(res, "C1"))

    def test_missing_ui_module_fails(self):
        ui = {f.relative_to(ROOT / "alahd_erp" / "ui").as_posix(): f.read_bytes()
              for f in (ROOT / "alahd_erp" / "ui").rglob("*") if f.is_file()}
        ui.pop("js/modules/store.js")
        res = self.run_verify(build_exe(ui_files=ui))
        self.assertEqual(status(res, "C1"), "FAIL")
        self.assertIn("MISSING", message(res, "C1"))

    def test_extra_ui_file_fails(self):
        ui = {f.relative_to(ROOT / "alahd_erp" / "ui").as_posix(): f.read_bytes()
              for f in (ROOT / "alahd_erp" / "ui").rglob("*") if f.is_file()}
        ui["js/seed-data.js"] = b"x"
        res = self.run_verify(build_exe(ui_files=ui))
        self.assertEqual(status(res, "C1"), "FAIL")
        self.assertEqual(status(res, "D1"), "FAIL")     # seed-data is also a forbidden payload

    def test_embedded_database_or_seed_is_forbidden(self):
        for bad in ("alahd_erp\\ui\\assets\\seed_state.json", "data\\alahd_erp_v1.sqlite3", "backups\\a.sqlite3", ".env"):
            res = self.run_verify(build_exe(extra_files=[(bad, b"x")]))
            self.assertEqual(status(res, "D1"), "FAIL", bad)

    def test_wrong_python_runtime_fails(self):
        res = self.run_verify(build_exe(pyver=314))
        self.assertEqual(status(res, "A2"), "FAIL")
        self.assertIn("3.14", message(res, "A2"))

    def test_x86_build_fails(self):
        self.assertEqual(status(self.run_verify(build_exe(machine=0x14C)), "A1"), "FAIL")

    def test_not_a_pe_or_not_pyinstaller(self):
        self.assertEqual(status(self.run_verify(b"not an exe"), "A0"), "FAIL")
        self.assertEqual(status(self.run_verify(build_exe()[:0x200] + b"\0" * 64), "A0"), "FAIL")


class OnedirLayout(VerifierBase):
    """PyInstaller one-folder builds keep data files on disk (``_internal/alahd_erp/ui``), not in the archive TOC."""

    def test_ui_on_disk_is_compared_byte_for_byte(self):
        exe = self.dir / "App.exe"
        exe.write_bytes(build_exe(include_ui_in_toc=False))
        disk = self.dir / "_internal" / "alahd_erp" / "ui"
        for f in (ROOT / "alahd_erp" / "ui").rglob("*"):
            if f.is_file():
                t = disk / f.relative_to(ROOT / "alahd_erp" / "ui")
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_bytes(f.read_bytes())
        res = vba.verify(exe, ROOT)[0]
        self.assertEqual(status(res, "C1"), "PASS", res)
        (disk / "js" / "app.js").write_bytes(b"tampered")
        self.assertEqual(status(vba.verify(exe, ROOT)[0], "C1"), "FAIL")

    def test_no_ui_anywhere_is_blocked_not_pass(self):
        res = self.run_verify(build_exe(include_ui_in_toc=False))
        self.assertEqual(status(res, "C1"), "BLOCKED")


class CommandLine(VerifierBase):
    def _run(self, exe_bytes: bytes, *extra: str) -> tuple[int, str]:
        exe = self.dir / "App.exe"
        exe.write_bytes(exe_bytes)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = vba.main([str(exe), "--src", str(ROOT), *extra])
        return code, buf.getvalue()

    def test_exit_codes(self):
        code, out = self._run(build_exe())
        self.assertEqual(code, 0, out)
        self.assertIn("sha256=", out)
        code, out = self._run(build_exe(version="1.0.0"))
        self.assertEqual(code, 1)
        self.assertIn("[FAIL", out)

    def test_strict_turns_blocked_into_failure(self):
        self.assertEqual(self._run(build_exe(include_ui_in_toc=False))[0], 0)
        self.assertEqual(self._run(build_exe(include_ui_in_toc=False), "--strict")[0], 1)


if __name__ == "__main__":
    unittest.main()
