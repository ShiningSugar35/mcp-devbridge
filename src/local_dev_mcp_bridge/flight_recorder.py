"""Bounded, fail-open flight recorder for transport and MCP lifecycle diagnostics.

The recorder intentionally stores only controlled metadata. It never records request
bodies, headers, environment variables, OAuth material, or tunnel credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .engines import redact_line

_DEFAULT_MAX_BYTES = 8_000_000
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_MAX_ACTIVE_REQUESTS = 512
_DEFAULT_MAX_TERMINALS = 2_048
_MAX_STRING_CHARS = 500
_MAX_FIELDS = 48
_SENSITIVE_KEYS = {
    "authorization",
    "proxy_authorization",
    "access_token",
    "refresh_token",
    "oauth_code",
    "code_verifier",
    "code_challenge",
    "client_secret",
    "tunnel_token",
    "cookie",
    "set_cookie",
    "password",
    "bearer",
    "credential",
}
_HASH_KEYS = {
    "workspace_id",
    "project_id",
    "session_id",
    "device_id",
    "task_id",
    "request_id",
    "handle",
    "root",
    "path_value",
}


def _utc_stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _short_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _redact_free_text(value: str) -> str:
    """Apply the shared scrubber plus strict token-shaped free-text redaction."""

    safe = redact_line(value)
    safe = re.sub(r"(?i)(\bbearer\s+)\S+", r"\1***", safe)
    safe = re.sub(
        r"(?i)\b(?:key|token|secret|password|auth)(?:[_\-][a-z0-9_]+)?\s*=\s*\S+",
        lambda match: match.group(0).rsplit("=", 1)[0] + "=***",
        safe,
    )
    return safe.replace("\r", " ").replace("\n", " ")


def _key_parts(key: str) -> set[str]:
    return {part for part in re.split(r"[^a-z0-9]+", key.casefold()) if part}


def _is_sensitive_key(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    parts = _key_parts(lowered)
    if lowered in _SENSITIVE_KEYS:
        return True
    if parts.intersection(
        {
            "password",
            "passwd",
            "secret",
            "credential",
            "credentials",
            "bearer",
            "cookie",
        }
    ):
        return True
    if "token" in parts or lowered.endswith("_key") or lowered == "key":
        return True
    return bool("oauth" in parts and parts.intersection({"code", "verifier", "challenge"}))


def _should_hash_key(key: str) -> bool:
    lowered = key.casefold().replace("-", "_")
    return lowered in _HASH_KEYS or lowered.endswith(
        (
            "_workspace_id",
            "_session_id",
            "_device_id",
            "_project_id",
            "_task_id",
            "_request_id",
            "_handle",
            "_root",
            "_path",
        )
    )


class FlightRecorder:
    """Append-only diagnostics with bounded memory, disk, and retention.

    All public methods are fail-open: diagnostics must never break the MCP hot path.
    """

    def __init__(
        self,
        log_dir: Path,
        *,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        max_active_requests: int = _DEFAULT_MAX_ACTIVE_REQUESTS,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.max_bytes = max(1_024, int(max_bytes))
        self.retention_days = max(1, int(retention_days))
        self.max_active_requests = max(1, int(max_active_requests))
        self._lock = threading.RLock()
        self._active: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._terminal: OrderedDict[str, None] = OrderedDict()
        self._last_prune_day = ""

    @staticmethod
    def _safe_key(key: object) -> str:
        return str(key or "field")[:80]

    @classmethod
    def _safe_value(cls, key: str, value: Any) -> Any:
        if _is_sensitive_key(key):
            return "[REDACTED]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            safe = _redact_free_text(value)[:_MAX_STRING_CHARS]
            if _should_hash_key(key):
                return _short_hash(safe)
            return safe
        if isinstance(value, dict):
            return {
                cls._safe_key(child_key): cls._safe_value(cls._safe_key(child_key), child_value)
                for child_key, child_value in list(value.items())[:20]
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._safe_value(key, item) for item in list(value)[:20]]
        return _redact_free_text(str(value))[:_MAX_STRING_CHARS]

    @classmethod
    def _safe_fields(cls, fields: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, value in list(fields.items())[:_MAX_FIELDS]:
            key = cls._safe_key(raw_key)
            result[key] = cls._safe_value(key, value)
        return result

    def _paths_for_today(self) -> tuple[Path, Path]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        base = self.log_dir / f"flight-recorder-{day}.jsonl"
        return base, base.with_suffix(base.suffix + ".1")

    def _prune_locked(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day == self._last_prune_day:
            return
        self._last_prune_day = day
        cutoff = time.time() - self.retention_days * 86_400
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            for path in self.log_dir.glob("flight-recorder-*.jsonl*"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
        except OSError:
            return

    def _write_locked(self, entry: dict[str, Any]) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self._prune_locked()
            payload = json.dumps(entry, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            encoded = payload.encode("utf-8", errors="replace")
            if len(encoded) > self.max_bytes:
                fallback = {
                    "ts": entry.get("ts", _utc_stamp()),
                    "mono_ns": entry.get("mono_ns", time.monotonic_ns()),
                    "event": "oversize_diagnostic_dropped",
                    "source_event": str(entry.get("event", ""))[:80],
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                }
                payload = json.dumps(fallback, ensure_ascii=False, separators=(",", ":")) + "\n"
                encoded = payload.encode("utf-8")
            base, rotated = self._paths_for_today()
            current_size = base.stat().st_size if base.exists() else 0
            if current_size and current_size + len(encoded) > self.max_bytes:
                try:
                    if rotated.exists():
                        rotated.unlink()
                    base.replace(rotated)
                except OSError:
                    return
            with base.open("ab") as handle:
                handle.write(encoded)
        except OSError:
            return

    def record(self, event: str, **fields: Any) -> None:
        try:
            entry = {
                "ts": _utc_stamp(),
                "mono_ns": time.monotonic_ns(),
                "event": str(event or "diagnostic")[:100],
                **self._safe_fields(fields),
            }
            with self._lock:
                self._write_locked(entry)
        except Exception:
            return

    def _remember_terminal_locked(self, trace_id: str) -> None:
        self._terminal[trace_id] = None
        self._terminal.move_to_end(trace_id)
        while len(self._terminal) > _DEFAULT_MAX_TERMINALS:
            self._terminal.popitem(last=False)

    def start_request(self, *, method: str, path: str, **fields: Any) -> str:
        trace_id = uuid.uuid4().hex[:16]
        now_ns = time.monotonic_ns()
        try:
            with self._lock:
                while len(self._active) >= self.max_active_requests:
                    evicted_id, evicted = self._active.popitem(last=False)
                    self._remember_terminal_locked(evicted_id)
                    self._write_locked(
                        {
                            "ts": _utc_stamp(),
                            "mono_ns": now_ns,
                            "event": "request_terminal",
                            "trace_id": evicted_id,
                            "outcome": "tracking_evicted",
                            "duration_ms": max(0, int((now_ns - int(evicted["start_ns"])) / 1_000_000)),
                        }
                    )
                safe = self._safe_fields(fields)
                self._active[trace_id] = {
                    "start_ns": now_ns,
                    "method": str(method)[:16],
                    "path": str(path)[:240],
                    **safe,
                }
                self._write_locked(
                    {
                        "ts": _utc_stamp(),
                        "mono_ns": now_ns,
                        "event": "request_started",
                        "trace_id": trace_id,
                        "method": str(method)[:16],
                        "path": str(path)[:240],
                        **safe,
                    }
                )
        except Exception:
            pass
        return trace_id

    def enrich_request(self, trace_id: str, **fields: Any) -> None:
        try:
            with self._lock:
                current = self._active.get(trace_id)
                if current is None:
                    return
                safe = self._safe_fields(fields)
                current.update(safe)
                self._active.move_to_end(trace_id)
                self._write_locked(
                    {
                        "ts": _utc_stamp(),
                        "mono_ns": time.monotonic_ns(),
                        "event": "request_context",
                        "trace_id": trace_id,
                        **safe,
                    }
                )
        except Exception:
            return

    def stage(self, trace_id: str, stage: str, **fields: Any) -> None:
        try:
            with self._lock:
                current = self._active.get(trace_id)
                if current is None:
                    return
                now_ns = time.monotonic_ns()
                current["stage"] = str(stage)[:100]
                current["stage_ns"] = now_ns
                self._active.move_to_end(trace_id)
                self._write_locked(
                    {
                        "ts": _utc_stamp(),
                        "mono_ns": now_ns,
                        "event": "request_stage",
                        "trace_id": trace_id,
                        "stage": str(stage)[:100],
                        "elapsed_ms": max(0, int((now_ns - int(current["start_ns"])) / 1_000_000)),
                        **self._safe_fields(fields),
                    }
                )
        except Exception:
            return

    def finish_request(self, trace_id: str, *, outcome: str, **fields: Any) -> bool:
        try:
            with self._lock:
                if trace_id in self._terminal:
                    return False
                current = self._active.pop(trace_id, None)
                if current is None:
                    return False
                now_ns = time.monotonic_ns()
                self._remember_terminal_locked(trace_id)
                self._write_locked(
                    {
                        "ts": _utc_stamp(),
                        "mono_ns": now_ns,
                        "event": "request_terminal",
                        "trace_id": trace_id,
                        "outcome": str(outcome or "unknown")[:80],
                        "duration_ms": max(0, int((now_ns - int(current["start_ns"])) / 1_000_000)),
                        "last_stage": str(current.get("stage") or "")[:100],
                        **self._safe_fields(fields),
                    }
                )
                return True
        except Exception:
            return False

    def snapshot(self) -> dict[str, int]:
        now_ns = time.monotonic_ns()
        try:
            with self._lock:
                oldest_ns = min((int(item["start_ns"]) for item in self._active.values()), default=now_ns)
                return {
                    "active_requests": len(self._active),
                    "oldest_request_age_ms": max(0, int((now_ns - oldest_ns) / 1_000_000)),
                    "tracked_terminals": len(self._terminal),
                }
        except Exception:
            return {"active_requests": 0, "oldest_request_age_ms": 0, "tracked_terminals": 0}


__all__ = ["FlightRecorder"]
