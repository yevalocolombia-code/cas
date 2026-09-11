from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

_ENV_TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def import_mcp_orders(database_path: Path, orders: list[dict[str, Any]], *, source_ref: str) -> dict[str, int]:
    """Persist a read-only MCP order snapshot in this installation's database."""
    rows = [dict(item) for item in orders if str(item.get("id") or "").strip()]
    with sqlite3.connect(database_path) as connection:
        run_id = connection.execute(
            "INSERT INTO import_runs(source_path, rows_count) VALUES(?, ?)", (source_ref, len(rows))
        ).lastrowid
        created = 0
        updated = 0
        for item in rows:
            order_id = str(item["id"])
            guide = str(item.get("shippingGuide") or "").strip() or None
            status = str(item.get("status") or "").strip() or None
            carrier = str(item.get("carrier") or "").strip() or None
            movement = str(item.get("lastMovementAt") or "").strip() or None
            raw = json.dumps(item, ensure_ascii=False, sort_keys=True)
            exists = connection.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone() is not None
            connection.execute(
                """INSERT INTO orders(order_id, guide, status, carrier, last_movement_at, raw_json)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(order_id) DO UPDATE SET
                     guide=COALESCE(excluded.guide, orders.guide), status=excluded.status,
                     carrier=COALESCE(excluded.carrier, orders.carrier),
                     last_movement_at=excluded.last_movement_at, raw_json=excluded.raw_json,
                     updated_at=CURRENT_TIMESTAMP""",
                (order_id, guide, status, carrier, movement, raw),
            )
            connection.execute(
                """INSERT INTO order_snapshots(import_run_id, order_id, status, guide, carrier, last_movement_at, raw_json)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (run_id, order_id, status, guide, carrier, movement, raw),
            )
            created += 0 if exists else 1
            updated += 1 if exists else 0
    return {"rows": len(rows), "created": created, "updated": updated}


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value.strip().strip("'\"")
    return values


def _resolve_header_templates(value: str, *, environ: Mapping[str, str], dotenv: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in environ and environ[name] != "":
            return str(environ[name])
        if name in dotenv and dotenv[name] != "":
            return str(dotenv[name])
        raise ValueError(f"MCP header template references unset environment variable: {name}")

    return _ENV_TEMPLATE.sub(replace, value)


def _parse_connection(text: str, *, config_path: Path | None = None) -> tuple[str, dict[str, str]]:
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "ecommerce360:"), None)
    if start is None:
        raise ValueError("MCP configuration has no ecommerce360 server.")
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    url = ""
    headers: dict[str, str] = {}
    headers_indent: int | None = None
    for line in lines[start + 1:]:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped and indent <= base_indent:
            break
        if stripped.startswith("url:"):
            url = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped == "headers:":
            headers_indent = indent
        elif headers_indent is not None and indent > headers_indent and ":" in stripped:
            key, value = stripped.split(":", 1)
            headers[key.strip()] = value.strip().strip("'\"")
    if not url or not headers:
        raise ValueError("MCP ecommerce360 requires URL and authenticated headers in its private config.")
    dotenv: dict[str, str] = {}
    if config_path is not None:
        dotenv = _load_dotenv(config_path.expanduser().resolve().parent / ".env")
    resolved = {
        key: _resolve_header_templates(value, environ=os.environ, dotenv=dotenv)
        for key, value in headers.items()
    }
    return url, resolved


def _call(url: str, headers: dict[str, str], method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(str(payload["error"].get("message") or "MCP request failed"))
    return payload["result"]


def fetch_mcp_orders(config_path: Path, *, window_days: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    config_path = config_path.expanduser()
    url, headers = _parse_connection(config_path.read_text(encoding="utf-8"), config_path=config_path)
    _call(url, headers, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dropi-cas-automation", "version": "0.1.0"}}, 1)
    until = date.today()
    from_date = until - timedelta(days=window_days)
    result = _call(
        url,
        headers,
        "tools/call",
        {"name": "dropi_list_orders", "arguments": {"from": from_date.isoformat(), "until": until.isoformat(), "pageSize": 100, "maxPages": 20}},
        2,
    )
    content = result.get("structuredContent")
    if not isinstance(content, dict):
        raise RuntimeError("MCP did not return structured orders.")
    meta = dict(content.get("meta") or {})
    if meta.get("truncated"):
        raise RuntimeError("MCP range was truncated; refusing a partial order import.")
    return list(content.get("orders") or []), meta
