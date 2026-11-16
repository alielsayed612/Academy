"""F7 - a foreign service on the port (even answering 401) must not be mistaken for a running Academy instance."""
from __future__ import annotations

import http.server
import json
import threading
import unittest

from tests import _helpers  # noqa: F401  (sets sys.path)
from alahd_erp import APP_ID
from alahd_erp.infrastructure.instance_probe import probe_instance


def _serve(status: int, body: bytes):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class ProbeTests(unittest.TestCase):
    def _probe(self, status, body, secret="s3cret"):
        srv = _serve(status, body)
        try:
            return probe_instance(srv.server_address[1], secret, timeout=2.0)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_foreign_401_is_not_ours(self):
        self.assertIsNone(self._probe(401, b'{"error":"unauthorized"}'))
        self.assertIsNone(self._probe(401, b"Unauthorized"))

    def test_foreign_200_is_not_ours(self):
        self.assertIsNone(self._probe(200, b"<html>ok alahd</html>"))
        self.assertIsNone(self._probe(200, json.dumps({"ok": True, "appId": "other"}).encode()))

    def test_our_healthy_instance(self):
        self.assertEqual(self._probe(200, json.dumps({"ok": True, "appId": APP_ID}).encode()), "s3cret")

    def test_our_instance_with_stale_secret(self):
        self.assertEqual(self._probe(401, b'{"detail":"missing or invalid window secret"}'), "s3cret")

    def test_nothing_listening(self):
        self.assertIsNone(probe_instance(1, "x", timeout=0.5))


if __name__ == "__main__":
    unittest.main()
