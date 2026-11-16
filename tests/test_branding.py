"""Identity + packaging consistency for the 1.2.0 "Yawm Jadid" release. Pure file/constant checks (no network, no GUI)."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

try:
    from PIL import Image
except ImportError:          # Pillow is a dev-only dependency: asset-pixel tests are skipped without it
    Image = None

from tests._helpers import ROOT
from alahd_erp import (APP_NAME, APP_TAGLINE, DATA_DIR_NAME, DEFAULT_ACADEMY_NAME, EXECUTABLE_NAME, __version__)

PKG = ROOT / "alahd_erp"
UI = PKG / "ui"
LEGACY = ("منظومة العهد الجديد", "Al-Ahd Academy ERP", "أكاديمية العهد الجديد")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class Identity(unittest.TestCase):
    def test_names(self):
        self.assertEqual(APP_NAME, "أكاديمية يوم جديد")
        self.assertEqual(APP_TAGLINE, "يوم جديد هو عهد جديد")
        self.assertEqual(DEFAULT_ACADEMY_NAME, APP_NAME)
        self.assertEqual(EXECUTABLE_NAME, "YawmJadidAcademy")

    def test_default_settings_and_js_defaults_use_new_name(self):
        from alahd_erp.domain.models import DEFAULT_SETTINGS   # noqa: PLC0415
        self.assertEqual(DEFAULT_SETTINGS["academyName"], DEFAULT_ACADEMY_NAME)
        self.assertIn(f"academyName: '{DEFAULT_ACADEMY_NAME}'", read(UI / "js" / "modules" / "store.js"))
        self.assertIn(f'id="wizAcademyName" value="{DEFAULT_ACADEMY_NAME}"', read(UI / "index.html"))

    def test_no_legacy_brand_strings_in_shipped_code(self):
        offenders = []
        for p in list(PKG.rglob("*")):
            if p.suffix in (".py", ".js", ".html", ".css") and "vendor" not in p.parts:
                t = read(p)
                offenders += [f"{p.relative_to(ROOT)}: {s}" for s in LEGACY if s in t]
        self.assertEqual(offenders, [])


class DataSafety(unittest.TestCase):
    """The data folder MUST NOT follow the rebrand: existing installs keep their database there."""

    def test_data_dir_name_is_frozen(self):
        self.assertEqual(DATA_DIR_NAME, "AlAhdAcademySuite")
        self.assertNotEqual(DATA_DIR_NAME, EXECUTABLE_NAME)

    def test_resolve_app_home_uses_frozen_folder_under_localappdata(self):
        from alahd_erp import config   # noqa: PLC0415
        with tempfile.TemporaryDirectory() as d:
            saved = {k: os.environ.pop(k, None) for k in ("ALAHD_ERP_HOME", "ACADEMY_HOME")}
            old = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = d
            try:
                self.assertEqual(config.resolve_app_home(), (Path(d) / "AlAhdAcademySuite").resolve())
            finally:
                if old is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v

    def test_database_filename_unchanged(self):
        from alahd_erp import DB_FILENAME   # noqa: PLC0415
        self.assertEqual(DB_FILENAME, "alahd_erp_v1.sqlite3")


@unittest.skipIf(Image is None, "Pillow not installed (dev-only dependency)")
class Assets(unittest.TestCase):
    def test_logo_is_circular_with_transparent_corners(self):
        with Image.open(UI / "assets" / "logo.png") as im:
            self.assertEqual((im.size, im.mode), ((512, 512), "RGBA"))
            self.assertEqual(im.getpixel((2, 2))[3], 0)
            self.assertEqual(im.getpixel((510, 510))[3], 0)
            self.assertEqual(im.getpixel((256, 256))[3], 255)

    def test_favicon_banner_and_multisize_icon(self):
        for name, size in (("favicon.png", (64, 64)), ("banner.jpg", (1254, 203))):
            with Image.open(UI / "assets" / name) as im:
                self.assertEqual(im.size, size, name)
        with Image.open(PKG / "icon.ico") as ico:
            sizes = ico.info["sizes"]
        for s in (16, 32, 48, 64, 128, 256):
            self.assertIn((s, s), sizes)

    def test_html_references_existing_assets(self):
        html = read(UI / "index.html")
        for name in set(re.findall(r'assets/([\w.-]+\.(?:png|jpg|ico))', html)):
            self.assertTrue((UI / "assets" / name).is_file(), name)
        for name in ("logo.png", "banner.jpg", "favicon.png"):
            self.assertIn(f"assets/{name}", html)

    def test_brand_palette_in_css_variables(self):
        css = read(UI / "css" / "design-system.css")
        for token in ("--brand-primary: #556a4f;", "--brand-primary: #8fa886;", "--bg-sidebar: #2b3628;", "--brand-accent: #b08d3c;"):
            self.assertIn(token, css)
        for old in ("#0f766e", "#0d9488", "#14b8a6", "#134e4a"):
            self.assertNotIn(old, css.lower())


class PackagingConsistency(unittest.TestCase):
    def test_versions_agree_everywhere(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertIn(f'version = "{__version__}"', read(ROOT / "pyproject.toml"))
        html = read(UI / "index.html")
        self.assertEqual(set(re.findall(r"v(\d+\.\d+\.\d+)", html)), {__version__})
        vi = read(ROOT / "installer" / "version_info.txt")
        four = f"{__version__}.0"
        self.assertIn(f"StringStruct('FileVersion', '{four}')", vi)
        self.assertIn(f"StringStruct('ProductVersion', '{four}')", vi)
        self.assertIn("filevers=(" + ", ".join(four.split(".")) + ")", vi)
        self.assertIn(f'#define MyAppVersion "{__version__}"', read(ROOT / "installer" / "YawmJadidAcademy.iss"))
        self.assertIn(f"## {__version__} ", read(ROOT / "CHANGELOG.md"))

    def test_spec_and_installer_use_the_executable_name_and_ship_the_ui(self):
        spec = read(ROOT / f"{EXECUTABLE_NAME}.spec")
        self.assertEqual(len(re.findall(rf'name="{EXECUTABLE_NAME}"', spec)), 2)       # EXE + COLLECT
        self.assertIn('icon="alahd_erp/icon.ico"', spec)
        self.assertIn('("alahd_erp/ui", "alahd_erp/ui")', spec)
        self.assertTrue((ROOT / "installer" / "version_info.txt").is_file())
        iss = read(ROOT / "installer" / "YawmJadidAcademy.iss")
        self.assertIn(f'#define MyAppExe "{EXECUTABLE_NAME}.exe"', iss)
        self.assertIn(f"dist\\{EXECUTABLE_NAME}", iss)
        code = "\n".join(ln for ln in iss.splitlines() if not ln.lstrip().startswith(";"))
        self.assertNotIn("[UninstallDelete]", code)          # the data folder must survive uninstall
        self.assertIn(f"pyinstaller {EXECUTABLE_NAME}.spec", read(ROOT / "build_windows.bat"))
        self.assertFalse((ROOT / "AlAhdAcademySuite.spec").exists())

    def test_requirements_cover_every_runtime_import(self):
        req = read(ROOT / "requirements.txt").lower()
        for dep in ("fastapi", "uvicorn", "pydantic", "pywebview", "openpyxl"):
            self.assertIn(dep, req)

    def test_spec_is_valid_python(self):
        compile(read(ROOT / f"{EXECUTABLE_NAME}.spec"), "spec", "exec")


if __name__ == "__main__":
    unittest.main()
