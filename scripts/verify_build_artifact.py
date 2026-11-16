#!/usr/bin/env python3
"""Verify that a PyInstaller build of the Windows app really contains THIS source tree.

Why: a hash or a successful build proves nothing about *which* source went in.  In session 10 the supplied
``AlAhdAcademySuite.exe`` turned out to be a v1.0.0 build (older UI, 7 modules missing, a 63-student seed file
embedded) sitting next to v1.2.0 source.  This script is the regression guard for that class of mismatch.

It is **static**: it parses the PyInstaller archive (no execution, no Windows needed, standard library only).
It does NOT prove runtime behaviour - that stays the job of ``scripts/smoke_exe.ps1`` and the release checklist.

Usage:  python scripts/verify_build_artifact.py EXE [--src ROOT] [--ui-dir DIR] [--expected-python 3.12]
                                                    [--strict] [--json]
Exit code: 1 if any check FAILs (or is BLOCKED with --strict), else 0.
Statuses: PASS / FAIL / BLOCKED (could not be evaluated, with the reason) / INFO.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import marshal
import re
import struct
import sys
import zlib
from pathlib import Path, PurePosixPath

PYI_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
COOKIE_LEN = 88  # magic(8) + pkg_len + toc_off + toc_len + pyver (4 x 4) + pylib name (64)
# Payloads that must never ship inside a distributable (customer data / local state).
FORBIDDEN = re.compile(r"(seed_state|seed-data|\.sqlite3?(-wal|-shm)?$|\.db$|(^|/)\.env$|(^|/)backups?/)", re.I)


class ArtifactError(Exception):
    pass


def _norm(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def read_pyinstaller(data: bytes) -> dict:
    """Return {'pyver': 'X.Y', 'toc': [(name, type, flag, bytes_loader)], 'machine': int}."""
    if data[:2] != b"MZ":
        raise ArtifactError("not a PE file (missing MZ header)")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ArtifactError("not a PE file (missing PE signature)")
    machine = struct.unpack_from("<H", data, pe + 4)[0]
    cookie = data.rfind(PYI_MAGIC)
    if cookie < 0:
        raise ArtifactError("no PyInstaller archive cookie found")
    _, pkg_len, toc_off, toc_len, pyver = struct.unpack_from("!8sIIII", data, cookie)
    start = cookie + COOKIE_LEN - pkg_len
    if start < 0 or toc_off + toc_len > pkg_len:
        raise ArtifactError("corrupt PyInstaller archive header")
    toc, p, end = [], start + toc_off, start + toc_off + toc_len
    while p < end:
        entry_len, pos, comp_len, _ulen, flag = struct.unpack_from("!IIIIB", data, p)
        if entry_len < 19:
            raise ArtifactError("corrupt PyInstaller TOC")
        typ = chr(data[p + 17])
        name = data[p + 18:p + entry_len].rstrip(b"\0").decode("utf-8", "replace")

        def loader(pos=pos, comp_len=comp_len, flag=flag):
            raw = data[start + pos:start + pos + comp_len]
            return zlib.decompress(raw) if flag else raw

        toc.append((name, typ, flag, loader))
        p += entry_len
    return {"pyver": f"{pyver // 100}.{pyver % 100}", "toc": toc, "machine": machine}


def read_pyz_modules(pyz: bytes) -> dict[str, bytes]:
    """PYZ -> {dotted module name: decompressed code bytes}. (Code objects are NOT unmarshalled: they are
    version specific; only their raw constants are inspected.)"""
    if pyz[:4] != b"PYZ\0":
        raise ArtifactError("bad PYZ header")
    toc_pos = struct.unpack("!i", pyz[8:12])[0]
    out = {}
    for name, (_typ, pos, ln) in marshal.loads(pyz[toc_pos:]):
        name = name.decode() if isinstance(name, bytes) else name
        try:
            out[name] = zlib.decompress(pyz[pos:pos + ln]) if ln else b""
        except zlib.error:  # unreadable entry: keep the name (module presence is what matters), no constants
            out[name] = b""
    return out


def source_modules(src: Path) -> set[str]:
    """Dotted names PyInstaller stores in the PYZ for the app package (``main`` is stored as the entry script)."""
    mods = set()
    for f in (src / "alahd_erp").rglob("*.py"):
        rel = f.relative_to(src).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts.pop()
        mods.add(".".join(parts))
    mods.discard("alahd_erp.main")
    return mods


def source_version(src: Path) -> str:
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (src / "alahd_erp" / "__init__.py").read_text("utf-8"), re.M)
    if not m:
        raise ArtifactError("cannot read __version__ from source")
    return m.group(1)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def verify(exe: Path, src: Path, ui_dir: Path | None = None, expected_python: str = "3.12") -> tuple[list[tuple], dict]:
    results: list[tuple[str, str, str]] = []

    def add(cid, status, msg):
        results.append((cid, status, msg))

    data = exe.read_bytes()
    meta = {"exe": exe.name, "sha256": sha(data), "bytes": len(data)}
    try:
        arc = read_pyinstaller(data)
    except ArtifactError as exc:
        add("A0 archive", "FAIL", str(exc))
        return results, meta
    toc = arc["toc"]
    add("A1 architecture", "PASS" if arc["machine"] == 0x8664 else "FAIL", f"PE machine=0x{arc['machine']:x} (expect 0x8664 x64)")
    add("A2 python runtime", "PASS" if arc["pyver"] == expected_python else "FAIL",
        f"bundled Python {arc['pyver']} (expected {expected_python})")

    # --- app modules + version -------------------------------------------------------------------------
    pyz_entry = next((e for e in toc if e[1] == "z"), None)
    if not pyz_entry:
        add("B1 app modules", "BLOCKED", "no PYZ in the EXE archive (layout not understood)")
        add("B2 version", "BLOCKED", "no PYZ in the EXE archive")
    else:
        mods = read_pyz_modules(pyz_entry[3]())
        have = {m for m in mods if m == "alahd_erp" or m.startswith("alahd_erp.")}
        want = source_modules(src)
        missing, stale = sorted(want - have), sorted(have - want)
        add("B1 app modules", "PASS" if not missing and not stale else "FAIL",
            f"{len(have)} in EXE / {len(want)} in source" + (f"; MISSING from EXE: {missing}" if missing else "")
            + (f"; NOT in source (stale): {stale}" if stale else ""))
        ver = source_version(src)
        raw = mods.get("alahd_erp", b"")
        found = sorted(set(re.findall(rb"\d+\.\d+\.\d+", raw)))
        add("B2 version", "PASS" if ver.encode() in found else "FAIL",
            f"source={ver}; EXE alahd_erp constants={[v.decode() for v in found]}")
    add("B3 entry script", "PASS" if any(e[1] == "s" and e[0] == "main" for e in toc) else "FAIL",
        "script 'main' present" if any(e[1] == "s" and e[0] == "main" for e in toc) else "entry script 'main' missing")

    # --- UI payload: byte-for-byte against source -------------------------------------------------------
    src_ui = src / "alahd_erp" / "ui"
    want_ui = {f.relative_to(src_ui).as_posix(): sha(f.read_bytes()) for f in src_ui.rglob("*") if f.is_file()}
    got_ui: dict[str, str] = {}
    for name, typ, _fl, loader in toc:
        n = _norm(name)
        for prefix in ("alahd_erp/ui/", "ui/"):
            if typ not in "szmMdoO" and n.startswith(prefix):
                got_ui[n[len(prefix):]] = sha(loader())
                break
    disk_dir = ui_dir or exe.parent / "_internal" / "alahd_erp" / "ui"
    where = "archive TOC"
    if not got_ui and disk_dir.is_dir():
        got_ui = {f.relative_to(disk_dir).as_posix(): sha(f.read_bytes()) for f in disk_dir.rglob("*") if f.is_file()}
        where = str(disk_dir)
    if not got_ui:
        add("C1 UI payload", "BLOCKED", "no UI files in the archive and no UI directory on disk (use --ui-dir)")
    else:
        diff = sorted(k for k in want_ui if k in got_ui and want_ui[k] != got_ui[k])
        miss = sorted(k for k in want_ui if k not in got_ui)
        extra = sorted(k for k in got_ui if k not in want_ui)
        ok = not (diff or miss or extra)
        add("C1 UI payload", "PASS" if ok else "FAIL",
            f"{len(want_ui)} source files vs {len(got_ui)} packaged ({where})"
            + (f"; DIFFERENT: {diff}" if diff else "") + (f"; MISSING: {miss}" if miss else "")
            + (f"; EXTRA: {extra}" if extra else ""))

    # --- forbidden payload (customer data / local state) -------------------------------------------------
    names = [_norm(e[0]) for e in toc]
    if disk_dir.is_dir():
        names += [f.relative_to(disk_dir.parent.parent).as_posix() for f in disk_dir.parent.parent.rglob("*") if f.is_file()]
    bad = sorted({n for n in names if FORBIDDEN.search(n)})
    add("D1 forbidden payload", "PASS" if not bad else "FAIL",
        "none (no seed/db/env/backup files)" if not bad else f"must not ship: {bad}")

    # --- informational: bundled distributions vs declared runtime deps ------------------------------------
    dists = sorted({PurePosixPath(n).parts[0].rsplit(".dist-info", 1)[0] for n in names
                    if PurePosixPath(n).parts and PurePosixPath(n).parts[0].endswith(".dist-info")})
    add("E1 bundled distributions", "INFO", ", ".join(dists) if dists else "none listed in archive TOC")
    return results, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("exe", type=Path)
    ap.add_argument("--src", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--ui-dir", type=Path)
    ap.add_argument("--expected-python", default="3.12")
    ap.add_argument("--strict", action="store_true", help="treat BLOCKED as failure")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    results, meta = verify(a.exe, a.src, a.ui_dir, a.expected_python)
    if a.json:
        print(json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=1))
    else:
        print(f"{meta['exe']}  sha256={meta['sha256']}  {meta['bytes']} bytes")
        for cid, st, msg in results:
            print(f"[{st:7}] {cid}: {msg}")
    bad = {"FAIL"} | ({"BLOCKED"} if a.strict else set())
    return 1 if any(r[1] in bad for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
