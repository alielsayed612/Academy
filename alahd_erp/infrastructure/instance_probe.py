"""Stdlib-only probe that decides whether a local port is held by *our* running Academy Suite instance.

Kept separate from `main.py` (which imports FastAPI/uvicorn at import time) so it can be unit-tested with a fake HTTP
server. A foreign service on the port - even one answering 401 - must NOT be treated as an Academy instance (F7).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from alahd_erp import APP_ID

# Exact body our auth middleware returns for a missing/wrong window secret (`api/server.py`).
_OUR_401_DETAIL = "missing or invalid window secret"


def _our_json(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def probe_instance(port: int, candidate_secret: str = "", timeout: float = 1.5) -> str | None:
    """Returns the window secret to use for the instance on `port`, or None if it is not (provably) ours."""
    headers = {"X-Window-Secret": candidate_secret} if candidate_secret else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health", headers=headers)
    # Bypass any system/corporate proxy (Windows "Internet Options"): a loopback probe must never leave this machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = _our_json(r.read(65536))
            if r.status == 200 and body and body.get("ok") is True and body.get("appId") == APP_ID:
                return candidate_secret
    except urllib.error.HTTPError as he:
        if he.code == 401 and candidate_secret:
            body = _our_json(he.read(65536))
            if body and body.get("detail") == _OUR_401_DETAIL:
                return candidate_secret
    except Exception:
        pass
    return None
