"""Frontend guards: escaping / XSS / CSV injection, offline-only assets, and F-03 (window secret stripped from the URL).

The real `dom.js`, `csv.js`, `api.js` modules are executed with Node (no browser needed; those classes are skipped if
`node` is missing). Static checks scan the UI sources for dangerous sinks and remote references.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from tests._helpers import ROOT

UI = ROOT / "alahd_erp" / "ui"
JS = UI / "js"
MODS = JS / "modules"
HARNESS = ROOT / "tests" / "js" / "f03_harness.mjs"
NODE = shutil.which("node")

PAYLOADS = [
    "<script>alert(1)</script>",
    '"><img src=x onerror=alert(1)>',
    "' onmouseover='alert(1)",
    '" autofocus onfocus="alert(1)',
    "<svg/onload=alert(1)>",
    "javascript:alert(1)",
    "&lt;already&gt; & <b>bold</b>",
    "<<script>script>",
    "<div style=\"x\" onclick='y'>",
    "طالبة <b>عربي</b> \" ' & ٣٤٥",
    "名前\u202e<i>unicode</i>",
    "`${alert(1)}`",
]


def run_node(code: str):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = pathlib.Path(tmp)
        for js_file in MODS.glob("*.js"):
            shutil.copy2(js_file, tmp_path / js_file.name)
        (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
        rewritten = code.replace(MODS.as_uri(), ".")
        out = subprocess.run([NODE, "--input-type=module", "-e", rewritten], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not available")
class DomEscaping(unittest.TestCase):
    def _html(self, tpl_js: str):
        code = f"""
import {{ html, esc }} from '{(JS / 'modules' / 'dom.js').as_uri()}';
const P = {json.dumps(PAYLOADS)};
const res = P.map(p => {tpl_js});
console.log(JSON.stringify(res));
"""
        return run_node(code)

    def test_esc_neutralises_all_html_metacharacters(self):
        for out in self._html("esc(p)"):
            for ch in '<>"\'':
                self.assertNotIn(ch, out)

    def test_text_context(self):
        for p, out in zip(PAYLOADS, self._html("html`<td>${p}</td>`.value")):
            self.assertTrue(out.startswith("<td>") and out.endswith("</td>"))
            self.assertNotRegex(out[4:-5], r"[<>]")

    def test_double_quoted_attribute_context(self):
        # The pattern used by app.js: data-teacher="${t}" / value="${s.id}"
        for out in self._html('html`<option value="${p}" data-teacher="${p}">${p}</option>`.value'):
            self.assertEqual(len(re.findall(r'"', out)), 4, out)   # only the template's own quotes remain
            self.assertNotRegex(out, r"<(script|img|svg|b|i)\b")

    def test_single_quoted_attribute_context(self):
        for out in self._html("html`<a title='${p}'>x</a>`.value"):
            self.assertEqual(out.count("'"), 2, out)

    def test_arrays_and_nested_templates_escape_inner_values(self):
        for out in self._html('html`<ul>${[p, p].map(v => html`<li>${v}</li>`)}</ul>`.value'):
            self.assertEqual(out.count("<li>"), 2)
            self.assertNotRegex(out, r"<(script|img|svg|b|i)\b")

    def test_null_undefined_and_numbers(self):
        code = f"""
import {{ html }} from '{(JS / 'modules' / 'dom.js').as_uri()}';
console.log(JSON.stringify([html`${{null}}|${{undefined}}|${{0}}|${{12.5}}`.value]));
"""
        self.assertEqual(run_node(code), ["||0|12.5"])

    def test_plain_string_is_never_treated_as_markup_by_html_tag(self):
        code = f"""
import {{ html }} from '{(JS / 'modules' / 'dom.js').as_uri()}';
console.log(JSON.stringify([html`${{'<b>x</b>'}}`.value]));
"""
        self.assertEqual(run_node(code), ["&lt;b&gt;x&lt;/b&gt;"])


@unittest.skipUnless(NODE, "node not available")
class CsvInjection(unittest.TestCase):
    def _cells(self, values):
        code = f"""
import {{ csvCell }} from '{(JS / 'modules' / 'csv.js').as_uri()}';
console.log(JSON.stringify({json.dumps(values)}.map(csvCell)));
"""
        return run_node(code)

    def test_formula_prefixes_are_neutralised(self):
        out = self._cells(["=HYPERLINK(\"http://x\",\"y\")", "@SUM(A1)", "+cmd|' /C calc'!A0", "-2+3+cmd", "\t=1", "\r=1"])
        for cell in out:
            self.assertRegex(cell, r"^\"'")

    def test_normal_values_and_phones_are_unchanged(self):
        out = self._cells(["أمل محمد", "+201001234567", "-", "010 1234 567", "(02) 123-4567", "", None, 5])
        self.assertEqual(out[0], '"أمل محمد"')
        self.assertEqual(out[1], '"+201001234567"')
        self.assertEqual(out[2], '"-"')
        self.assertEqual(out[3], '"010 1234 567"')
        self.assertEqual(out[4], '"(02) 123-4567"')
        self.assertEqual(out[5:7], ['""', '""'])
        self.assertEqual(out[7], '"5"')

    def test_quotes_are_doubled_everywhere(self):
        self.assertEqual(self._cells(['a"b']), ['"a""b"'])


class StaticSinkChecks(unittest.TestCase):
    def _sources(self):
        return [p for p in JS.rglob("*.js")]

    def test_raw_helper_has_no_call_sites_outside_its_definition(self):
        for p in self._sources():
            if p.name == "dom.js":
                continue
            text = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?<![\w.])raw\(", text), f"raw() call found in {p.name}")

    def test_innerhtml_is_assigned_only_in_dom_js_or_cleared(self):
        for p in self._sources():
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\.(innerHTML|outerHTML)\s*=", line) and p.name != "dom.js":
                    self.assertRegex(line, r"=\s*''\s*;", f"{p.name}:{n}: {line.strip()}")
                self.assertNotIn("insertAdjacentHTML", line, f"{p.name}:{n}")
                self.assertNotIn("document.write", line, f"{p.name}:{n}")
                self.assertIsNone(re.search(r"\beval\(|new Function\(", line), f"{p.name}:{n}")

    def test_no_inline_event_handler_attributes_with_interpolation(self):
        for p in self._sources():
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                self.assertIsNone(re.search(r"\bon[a-z]+\s*=\s*[\"'][^\"']*\$\{", line), f"{p.name}:{n}: {line.strip()}")

    def test_no_external_urls_loaded_by_scripts(self):
        for p in self._sources():
            text = p.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"""(?:src|href|import\s+.*from|fetch\()\s*[=(]?\s*['"`]https?://""", text), p.name)


# --------------------------------------------------------------------------- offline-only UI (no CDN)
REMOTE = re.compile(r"""(?:src|href)\s*=\s*["']https?://|url\(\s*["']?https?://|@import\s+(?:url\()?["']?https?://|fetch\(\s*["']https?://""", re.I)


class OfflineAssets(unittest.TestCase):
    def test_ui_has_no_remote_resource_references(self):
        offenders = []
        for p in list(UI.glob("*.html")) + list(UI.glob("css/**/*.css")) + list(UI.glob("js/**/*.js")) + [UI / "vendor" / "cairo.css"]:
            if p.is_file() and REMOTE.search(p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(p.relative_to(UI)))
        self.assertEqual(offenders, [])

    def test_cairo_font_files_are_vendored(self):
        css = (UI / "vendor" / "cairo.css").read_text(encoding="utf-8")
        for name in re.findall(r"url\(([^)]+\.woff2)\)", css):
            self.assertTrue((UI / "vendor" / name).is_file(), name)


# --------------------------------------------------------------------------- F-03: window secret leaves the URL
@unittest.skipUnless(NODE, "node not available")
class WsecIsStrippedFromUrl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp())
        for name in ("api.js", "dom.js", "store.js"):
            shutil.copy2(MODS / name, cls.tmp / name)
        shutil.copy2(HARNESS, cls.tmp / "harness.mjs")
        (cls.tmp / "package.json").write_text('{"type":"module"}')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_case(self, href: str, win_secret: str | None = None) -> dict:
        out = subprocess.run([NODE, "harness.mjs", json.dumps({"href": href, "winSecret": win_secret})], cwd=self.tmp,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_secret_captured_and_removed_keeping_other_params_and_hash(self):
        r = self.run_case("http://127.0.0.1:8890/?wsec=SECRET123&x=1#/students")
        self.assertEqual(r["secret"], "SECRET123")
        self.assertEqual(r["replaceCalls"], ["/?x=1#/students"])

    def test_secret_only_query_becomes_clean_path(self):
        r = self.run_case("http://127.0.0.1:8890/?wsec=ABC")
        self.assertEqual((r["secret"], r["replaceCalls"]), ("ABC", ["/"]))

    def test_no_secret_in_url_means_no_history_rewrite(self):
        r = self.run_case("http://127.0.0.1:8890/#/dashboard")
        self.assertEqual((r["secret"], r["replaceCalls"]), ("", []))

    def test_injected_window_variable_still_works_as_fallback(self):
        r = self.run_case("http://127.0.0.1:8890/", win_secret="FROMWIN")
        self.assertEqual((r["secret"], r["replaceCalls"]), ("FROMWIN", []))

    def test_no_other_module_reads_the_secret_from_location(self):
        offenders = [p.name for p in JS.rglob("*.js")
                     if "vendor" not in p.parts and p.name != "api.js" and "wsec" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
