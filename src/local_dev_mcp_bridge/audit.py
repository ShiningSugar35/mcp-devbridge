"""Structured JSONL audit logging with secret redaction."""

from __future__ import annotations

import contextlib
import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .constants import LOG_DIR, MAX_JSONL_BYTES, RETENTION_DAYS, SENSITIVE_NAME_MARKERS, ensure_dirs

_PARAM_REDACT_KEYS = {"content", "old_text", "new_text", "patch", "environment", "command"}
_MAX_PARAM_SUMMARY_CHARS = 400


class AuditLogger:
    """Thread-safe JSONL logger. Never logs full secret values."""

    def __init__(self, directory: Path | None = None, retention_days: int = RETENTION_DAYS) -> None:
        self.directory = Path(directory or LOG_DIR)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._handle: Any = None
        self._current_day = ""
        self.directory.mkdir(parents=True, exist_ok=True)
        ensure_dirs()

    def _ensure_file(self) -> Any:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._handle is None or self._current_day != today:
            if self._handle is not None:
                with contextlib.suppress(Exception):
                    self._handle.close()
            path = self.directory / f"mcp-{today}.jsonl"
            self._handle = path.open("a", encoding="utf-8")
            self._current_day = today
            self._rotate_if_needed()
        return self._handle

    def _rotate_if_needed(self) -> None:
        try:
            total = sum(f.stat().st_size for f in self.directory.glob("*.jsonl") if f.is_file())
            if total > MAX_JSONL_BYTES:
                old = sorted(self.directory.glob("*.jsonl"), key=lambda f: f.name)
                for f in old[:-2]:
                    with contextlib.suppress(OSError):
                        f.unlink()
            cutoff = datetime.now() - timedelta(days=self.retention_days)
            for f in self.directory.glob("*.jsonl"):
                with contextlib.suppress(OSError):
                    if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                        f.unlink()
        except OSError:
            pass

    @staticmethod
    def redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
        if not params:
            return {}
        summary: dict[str, Any] = {}
        for key, value in params.items():
            lowered = key.lower()
            if lowered in _PARAM_REDACT_KEYS:
                summary[key] = "[redacted]"
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            upper = key.upper()
            if any(marker in upper for marker in SENSITIVE_NAME_MARKERS):
                summary[key] = "[redacted]"
                continue
            summary[key] = text[:_MAX_PARAM_SUMMARY_CHARS]
        return summary

    def log_tool_call(
        self,
        *,
        request_id: str | None,
        client_name: str | None,
        tool_name: str,
        parameters: dict[str, Any] | None,
        workspace: str,
        permission_mode: str,
        duration_ms: int,
        success: bool,
        exit_code: int | None = None,
        error_type: str | None = None,
        output_truncated: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "request_id": request_id,
            "client_name": client_name,
            "tool_name": tool_name,
            "parameter_summary": self.redact_params(parameters),
            "workspace": workspace,
            "permission_mode": permission_mode,
            "duration_ms": duration_ms,
            "success": bool(success),
            "exit_code": exit_code,
            "error_type": error_type,
            "output_truncated": output_truncated,
        }
        if extra:
            record.update(extra)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            handle = self._ensure_file()
            try:
                handle.write(line + "\n")
                handle.flush()
            except OSError:
                pass

    def raw(self, event: str, **fields: Any) -> None:
        record = {"timestamp": datetime.now().isoformat(timespec="milliseconds"), "event": event, **fields}
        with self._lock:
            handle = self._ensure_file()
            try:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
            except OSError:
                pass


class AuditQuery:
    """Filters for :func:`query_logs`."""

    def __init__(
        self,
        *,
        day: str = "",
        tool_name: str = "",
        success: bool | None = None,
        limit: int = 500,
    ) -> None:
        self.day = day  # "YYYY-MM-DD"；空 = 全部日期
        self.tool_name = tool_name.strip()  # 空 = 全部工具
        self.success = success  # None = 全部；True/False 过滤
        self.limit = max(1, limit)


def query_logs(filters: AuditQuery | None = None, directory: Path | None = None) -> list[dict[str, Any]]:
    """Read mcp-*.jsonl under ``directory`` and return records (newest first)
    matching the filters. Records are already redacted at write time."""
    query = filters or AuditQuery()
    directory = Path(directory or LOG_DIR)
    if not directory.is_dir():
        return []
    patterns = []
    if query.day:
        patterns.append(f"mcp-{query.day}.jsonl")
    else:
        patterns.append("mcp-*.jsonl")
    records: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "timestamp" not in record:
                            continue
                        if query.tool_name and record.get("tool_name") != query.tool_name:
                            continue
                        if query.success is not None and record.get("success") is not query.success:
                            continue
                        records.append(record)
            except OSError:
                continue
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records[: query.limit]


def available_tool_names(directory: Path | None = None) -> list[str]:
    """Sorted unique tool names seen in recent audit logs (for the UI combo)."""
    directory = Path(directory or LOG_DIR)
    names: set[str] = set()
    if not directory.is_dir():
        return []
    for path in sorted(directory.glob("mcp-*.jsonl"))[-7:]:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    name = record.get("tool_name")
                    if name:
                        names.add(name)
        except OSError:
            continue
    return sorted(names)


def summarize_value(value: Any, max_chars: int = 120) -> str:
    """Summarize a parameter value, truncating in the middle when long."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "…[截断]" + text[-half:]


def looks_sensitive(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in SENSITIVE_NAME_MARKERS)


def sanitize_path_display(path: str) -> str:
    return re.sub(r"(?i)(token|key|secret|password)=([^&\s]+)", r"\1=[隐藏]", path)


__all__ = [
    "AuditLogger",
    "AuditQuery",
    "query_logs",
    "available_tool_names",
    "summarize_value",
    "looks_sensitive",
    "sanitize_path_display",
]