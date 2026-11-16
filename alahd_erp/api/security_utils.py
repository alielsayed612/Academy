"""Pure-Python security helpers for the API layer (no FastAPI import -> unit-testable anywhere).

Phase 1 additions. Behaviour of the existing routes is preserved; these helpers only make the checks that
`server.py` already performed more robust and testable.
"""
from __future__ import annotations

import json
import re
from email.parser import BytesParser
from email.policy import default as email_default_policy
from typing import Any

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
MAX_FILENAME_LEN = 255
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_SEP = re.compile(r"[\\/]")


class UploadParseError(ValueError):
    """Raised for a malformed upload body. `str(exc)` is a short, safe English message."""


def host_only(host_header: str) -> str:
    """Returns the lower-cased host of a `Host` header without port or IPv6 brackets.

    Handles `127.0.0.1:8890`, `localhost`, `[::1]:8890` and bare `[::1]` (the old `rsplit(':', 1)` logic turned
    the bare IPv6 form into `[:` and rejected it).
    """
    h = (host_header or "").strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h[1:]
    if h.count(":") == 1:
        return h.rsplit(":", 1)[0]
    return h


def host_allowed(host_header: str) -> bool:
    h = host_only(host_header)
    return (not h) or h in ALLOWED_HOSTS


def allowed_origins(port: int) -> frozenset[str]:
    return frozenset({
        f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}",
        "http://127.0.0.1", "http://localhost", "http://[::1]", "http://testserver",
    })


def origin_allowed(origin: str | None, port: int) -> bool:
    """Missing Origin is accepted (the per-launch window secret is still required); anything else must match."""
    return (not origin) or origin.lower() in allowed_origins(port)


def sanitize_upload_filename(name: str | None, default: str = "upload.xlsx") -> str:
    """Keeps only the final path component, strips control characters and bounds the length.

    The importer only uses the extension and stores the name as metadata, but a client-supplied name must never
    carry directory components (Path Traversal) or control characters into logs, audit rows or reports.
    Arabic / Unicode names are preserved.
    """
    base = _SEP.split(str(name or ""))[-1]
    base = _CTRL.sub("", base).strip().strip(".") if base.strip(".") else ""
    base = base[:MAX_FILENAME_LEN]
    return base or default


def parse_multipart_upload(raw_body: bytes, content_type: str) -> tuple[bytes, str, dict[str, Any]]:
    """Parses a `multipart/form-data` body into `(file_bytes, filename, options)`.

    Same contract as the previous inline parser, but reads the Content-Disposition parameters with the email
    library's own header parser instead of a regex that could mistake `filename="x"` for `name="x"`.
    """
    if "multipart/form-data" not in (content_type or "").lower():
        raise UploadParseError("not multipart")
    msg = BytesParser(policy=email_default_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw_body
    )
    if not msg.is_multipart():
        raise UploadParseError("malformed multipart body")
    file_bytes = b""
    filename = "upload.xlsx"
    opts: dict[str, Any] = {}
    for part in msg.iter_parts():
        _fn = part.get_param("name", header="content-disposition")
        field_name = _fn if isinstance(_fn, str) else ""
        fname = part.get_filename()
        _raw = part.get_payload(decode=True)
        payload = _raw if isinstance(_raw, bytes) else b""
        if fname or field_name == "file":
            file_bytes = payload
            if fname:
                filename = fname
        elif field_name:
            val_str = payload.decode("utf-8", errors="replace").strip()
            if field_name == "columnMapping":
                try:
                    opts[field_name] = json.loads(val_str) if val_str else None
                except Exception:
                    opts[field_name] = None
            elif field_name == "autoCreateRefs":
                opts[field_name] = val_str.lower() in ("1", "true", "yes", "on")
            else:
                opts[field_name] = val_str
    return file_bytes, sanitize_upload_filename(filename), opts
