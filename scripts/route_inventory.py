"""Generates the API route inventory table from alahd_erp/api/server.py (AST only - no FastAPI needed).

Usage: python scripts/route_inventory.py > routes.md
"""
from __future__ import annotations

import ast
import pathlib
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "alahd_erp" / "api" / "server.py"
# Paths exercised by tests/test_http_api.py (written in Phase 1; executed only where fastapi+httpx exist).
HTTP_TESTED = {
    "/api/health", "/api/sync", "/api/teachers", "/api/students", "/api/installments", "/api/settings",
    "/api/pin/verify", "/api/pin/set", "/api/pin/clear", "/api/pin/status", "/api/system/reset", "/api/system/reset-app",
    "/api/demo/load", "/api/export/xlsx", "/api/export/html", "/api/import/template", "/api/import/preview",
    "/api/import/error-report", "/api/backup", "/api/backups", "/api/restore", "/api/accounting", "/api/stats",
    "/api/{rest:path}", "/api/students/{student_id}",
}


def main() -> None:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        routes = []
        for d in node.decorator_list:
            if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
                    and d.func.attr in {"get", "post", "put", "patch", "delete"} and d.args
                    and isinstance(d.args[0], ast.Constant)):
                routes.append((d.func.attr.upper(), d.args[0].value))
        if not routes:
            continue
        src = ast.get_source_segment(SRC.read_text(encoding="utf-8"), node) or ""
        val = []
        for a in node.args.args:
            ann = ast.unparse(a.annotation) if a.annotation else ""
            if ann.endswith("Req") or ann.endswith("Req | None"):
                val.append(f"pydantic `{ann}`")
        if "request.json()" in src:
            val.append("manual JSON-object check")
        if "request.body()" in src:
            val.append("size cap + JSON/multipart parse")
        if "MONTH_RE" in src or "allowed_scopes" in src or "SUPPORTED" in src or "BACKUP_NAME" in src:
            val.append("allow-list / regex")
        if "_extract_expected_rev" in src:
            val.append("rev check (409)")
        if not val:
            val.append("path/query params only (typed by FastAPI)" if node.args.args else "none needed")
        err = "domain mapper (400/404/409)" if "_handle_domain_or_store_error" in src else (
            "HTTPException" if "HTTPException" in src else "default (500 generic)")
        for method, path in routes:
            rows.append((path, method, "X-Window-Secret (middleware)", "; ".join(dict.fromkeys(val)), err,
                         "HTTP tests written (not executed here)" if path in HTTP_TESTED else "NOT COVERED by HTTP tests"))
    rows.sort(key=lambda r: (r[0], r[1]))
    print("| Route | Method | Authentication | Validation | Error Handling | Test Status |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| `%s` | %s | %s | %s | %s | %s |" % r)
    print(f"\nTotal API route registrations: {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
