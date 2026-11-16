"""Phase 1 — pure unit tests for alahd_erp/api/security_utils.py (no FastAPI needed)."""
from __future__ import annotations

import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from alahd_erp.api.security_utils import (
    UploadParseError,
    host_allowed,
    host_only,
    origin_allowed,
    parse_multipart_upload,
    sanitize_upload_filename,
)

CT = "multipart/form-data; boundary=B"


def _mp(parts: list[tuple[str, bytes]]) -> bytes:
    out = b""
    for header, body in parts:
        out += b"--B\r\n" + header.encode("utf-8") + b"\r\n\r\n" + body + b"\r\n"
    return out + b"--B--\r\n"


class HostAndOrigin(unittest.TestCase):
    def test_host_parsing(self):
        self.assertEqual(host_only("127.0.0.1:8890"), "127.0.0.1")
        self.assertEqual(host_only("[::1]:8890"), "::1")
        self.assertEqual(host_only("[::1]"), "::1")   # old rsplit logic produced '[:' and rejected this
        self.assertEqual(host_only("LOCALHOST"), "localhost")

    def test_allowed_and_rejected_hosts(self):
        for h in ("127.0.0.1:8890", "localhost:8890", "[::1]:8890", "[::1]", ""):
            self.assertTrue(host_allowed(h), h)
        for h in ("evil.com", "evil.com:8890", "localhost.evil.com", "127.0.0.1.evil.com:80", "10.0.0.5:8890"):
            self.assertFalse(host_allowed(h), h)

    def test_origin(self):
        self.assertTrue(origin_allowed(None, 8890))          # no Origin: secret still required elsewhere
        self.assertTrue(origin_allowed("http://127.0.0.1:8890", 8890))
        self.assertTrue(origin_allowed("HTTP://LOCALHOST:8890", 8890))
        for o in ("http://evil.com", "null", "https://127.0.0.1:8890", "http://127.0.0.1:9999",
                  "http://127.0.0.1:8890.evil.com", "file://"):
            self.assertFalse(origin_allowed(o, 8890), o)


class FilenameSanitizing(unittest.TestCase):
    def test_traversal_and_separators(self):
        self.assertEqual(sanitize_upload_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_upload_filename("..\\..\\win\\evil.xlsx"), "evil.xlsx")
        self.assertEqual(sanitize_upload_filename("C:\\x\\y.csv"), "y.csv")

    def test_control_chars_and_length(self):
        self.assertEqual(sanitize_upload_filename("a\x00b\r\n.xlsx"), "ab.xlsx")
        self.assertLessEqual(len(sanitize_upload_filename("x" * 1000 + ".xlsx")), 255)

    def test_empty_and_dots_fall_back(self):
        self.assertEqual(sanitize_upload_filename("", default="d.xlsx"), "d.xlsx")
        self.assertEqual(sanitize_upload_filename("..", default="d.xlsx"), "d.xlsx")
        self.assertEqual(sanitize_upload_filename(None), "upload.xlsx")

    def test_unicode_and_arabic_preserved(self):
        self.assertEqual(sanitize_upload_filename("طلاب العهد ٢٠٢٦.xlsx"), "طلاب العهد ٢٠٢٦.xlsx")


class MultipartParsing(unittest.TestCase):
    def test_basic_file_and_options(self):
        body = _mp([
            ('Content-Disposition: form-data; name="file"; filename="s.xlsx"\r\nContent-Type: application/octet-stream', b"PK\x03\x04abc"),
            ('Content-Disposition: form-data; name="dataset"', b"students"),
            ('Content-Disposition: form-data; name="autoCreateRefs"', b"false"),
            ('Content-Disposition: form-data; name="columnMapping"', b'{"a": "b"}'),
        ])
        data, fname, opts = parse_multipart_upload(body, CT)
        self.assertEqual(data, b"PK\x03\x04abc")
        self.assertEqual(fname, "s.xlsx")
        self.assertEqual(opts["dataset"], "students")
        self.assertIs(opts["autoCreateRefs"], False)
        self.assertEqual(opts["columnMapping"], {"a": "b"})

    def test_param_order_does_not_confuse_name_and_filename(self):
        # The old regex `name="..."` matched inside `filename="..."` when filename came first.
        body = _mp([('Content-Disposition: form-data; filename="x.csv"; name="file"', b"a,b\n1,2")])
        data, fname, _ = parse_multipart_upload(body, CT)
        self.assertEqual((data, fname), (b"a,b\n1,2", "x.csv"))

    def test_traversal_filename_is_reduced(self):
        body = _mp([('Content-Disposition: form-data; name="file"; filename="../../x/evil.xlsx"', b"zz")])
        self.assertEqual(parse_multipart_upload(body, CT)[1], "evil.xlsx")

    def test_arabic_filename(self):
        body = _mp([('Content-Disposition: form-data; name="file"; filename="طلاب.xlsx"', b"zz")])
        self.assertEqual(parse_multipart_upload(body, CT)[1], "طلاب.xlsx")

    def test_bad_column_mapping_json_becomes_none(self):
        body = _mp([('Content-Disposition: form-data; name="columnMapping"', b"{not json")])
        self.assertIsNone(parse_multipart_upload(body, CT)[2]["columnMapping"])

    def test_empty_and_garbage_bodies_never_crash_unexpectedly(self):
        # Either a clean UploadParseError (-> HTTP 400 in server.py) or an empty result; never another exception.
        for raw in (b"", b"garbage", b"--B--\r\n", b"\x00\xff" * 50):
            try:
                data, fname, _ = parse_multipart_upload(raw, CT)
            except UploadParseError:
                continue
            self.assertEqual((data, fname), (b"", "upload.xlsx"))

    def test_not_multipart_raises(self):
        with self.assertRaises(UploadParseError):
            parse_multipart_upload(b"x", "application/json")


if __name__ == "__main__":
    unittest.main()
