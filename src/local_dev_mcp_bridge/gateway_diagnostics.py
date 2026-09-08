"""Fail-open, bounded diagnostic JSONL; never a store for request bodies or credentials."""

from __future__ import annotations

import json
import re
import threading
import time
from itertools import islice
from pathlib import Path
from typing import Any

from .engines import redact_line

_QUERY = re.compile(r"(?i)(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*")
_SENSITIVE = frozenset(
    {
        "command",
        "code",
        "access_token",
        "refresh_token",
        "client_secret",
        "token",
        "authorization",
        "bearer",
        "code_verifier",
        "cookie",
        "set_cookie",
        "key",
        "x_api_key",
    }
)
_LOCK = threading.Lock()
_LAST_PRUNE: tuple[str, str] | None = None


def scrub_text(value: str, limit: int = 2048) -> str:
    # Strip the whole query, including percent-encoded key names and non-token
    # parameters. Diagnostic URLs need origin/path, not bearer capabilities.
    return _QUERY.sub(r"\1?***", redact_line(value)).replace("\r", " ").replace("\n", " ")[:limit]


def safe_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE or any(
        part in normalized for part in ("password", "secret", "credential")
    ):
        return "***REDACTED***"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 5:
        return "[nested diagnostic omitted]"
    if isinstance(value, dict):
        return {
            str(k)[:80]: safe_value(v, key=str(k), depth=depth + 1)
            for k, v in islice(value.items(), 48)
        }
    if isinstance(value, (list, tuple)):
        return [safe_value(item, depth=depth + 1) for item in islice(value, 20)]
    return scrub_text(str(value))


def scrub_body(body: bytes | str) -> str:
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return scrub_text(text)
    return json.dumps(safe_value(value), ensure_ascii=False, default=str)[:2048]


def write_entry(
    path: Path, fields: dict[str, Any], *, max_bytes: int, retention_days: int = 7
) -> None:
    """One current file + one backup per day; no writer thread or unbounded cache."""
    global _LAST_PRUNE
    try:
        entry = safe_value(fields)
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        encoded = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        budget = max(256, max_bytes)
        if len(encoded) > min(16 * 1024, budget):
            entry = {
                "ts": entry["ts"],
                "event": "oversize_diagnostic_omitted",
                "bytes": len(encoded),
            }
            encoded = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            prune_key = (str(path.parent), time.strftime("%Y-%m-%d", time.gmtime()))
            if prune_key != _LAST_PRUNE:
                cutoff = time.time() - max(1, retention_days) * 86_400
                for old in path.parent.glob("gateway-*.jsonl*"):
                    try:
                        if old.is_file() and old.stat().st_mtime < cutoff:
                            old.unlink()
                    except OSError:
                        continue
                _LAST_PRUNE = prune_key
            size = path.stat().st_size if path.exists() else 0
            if size and size + len(encoded) > budget:
                path.replace(path.with_suffix(path.suffix + ".1"))
            with path.open("ab") as handle:
                handle.write(encoded)
    except Exception:
        # Diagnostics must not turn a healthy MCP request into an application error.
        return
