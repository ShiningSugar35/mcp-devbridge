"""Inspect the public tool contract without executing tools or exporting credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
ALLOWED_ENDPOINTS = ("http://127.0.0.1:8786/mcp", "https://mcp.shiningsugar.shop/mcp")
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


def decode_response(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
        for line in raw.splitlines():
            if line.startswith(b"data:"):
                candidate = json.loads(line[5:].strip())
                if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                    value = candidate
                    break
    if not isinstance(value, dict):
        raise ValueError("No JSON-RPC object in response")
    if "error" in value:
        raise ValueError("Endpoint returned a JSON-RPC error")
    return value


def fetch_catalog(endpoint: str) -> tuple[dict, list]:
    if endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError("This diagnostic accepts only the owner's existing endpoints")
    from local_dev_mcp_bridge.constants import ACCESS_TOKEN_CRED_NAME
    from local_dev_mcp_bridge.secrets import SecretsStore

    credential = SecretsStore().get(ACCESS_TOKEN_CRED_NAME)
    if not credential:
        raise RuntimeError("Existing Hub credential unavailable; nothing generated or changed")
    headers = {"Authorization": "Bearer " + credential, "Accept": "application/json, text/event-stream"}
    with httpx.Client(timeout=15, follow_redirects=False, headers=headers) as client:
        def send(body: dict) -> dict:
            with client.stream("POST", endpoint, json=body) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"Endpoint returned HTTP {response.status_code}")
                raw = bytearray()
                for chunk in response.iter_bytes(8192):
                    raw.extend(chunk)
                    if len(raw) > 4 * 1024 * 1024:
                        raise RuntimeError("Contract response exceeds diagnostic budget")
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    client.headers["Mcp-Session-Id"] = session
                return decode_response(bytes(raw))
        init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2026-07-28", "capabilities": {},
            "clientInfo": {"name": "DevBridge-annotation-audit", "version": "1"},
        }})
        initialization = init.get("result", {})
        version = initialization.get("protocolVersion")
        if isinstance(version, str):
            client.headers["MCP-Protocol-Version"] = version
        result = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return initialization.get("serverInfo", {}), result["result"]["tools"]


def summarize(tools: list) -> dict:
    rows = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise ValueError("Malformed catalog entry")
        hints = tool.get("annotations")
        row = {"name": tool["name"], "annotations_present": isinstance(hints, dict)}
        if isinstance(hints, dict):
            row.update({key: hints[key] for key in HINTS if type(hints.get(key)) is bool})
        rows.append(row)
    rows.sort(key=lambda row: row["name"])
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {
        "count": len(rows), "unique_count": len({row["name"] for row in rows}),
        "annotation_fingerprint": hashlib.sha256(canonical).hexdigest(),
        "missing_annotations": [row["name"] for row in rows if not row["annotations_present"]],
        "tools": rows,
    }


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", choices=ALLOWED_ENDPOINTS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(ROOT) or output.exists():
        parser.error("Choose a new output file inside the project")
    if args.endpoint:
        identity, tools = fetch_catalog(args.endpoint)
    else:
        from local_dev_mcp_bridge.gateway import _STABLE_HUB_TOOLS
        identity, tools = {}, list(_STABLE_HUB_TOOLS)
    result = {"observed_at": datetime.now(UTC).isoformat(), "source": args.endpoint or "source-candidate",
              "server_version": identity.get("version"), **summarize(tools)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "tools"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
