"""Bounded durable routing hints for deterministic CodexPro workspace handles."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from . import constants

_MAX_ROUTES = 512
_MAX_TOMBSTONES = 512
_VERSION = 2
_SUPPORTED_VERSIONS = frozenset({1, _VERSION})
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_RETRY_SECONDS = 0.02
_ROUTE_STATE_LOCK = threading.RLock()


def _path() -> Path:
    constants.ensure_dirs()
    return constants.config_dir() / "workspace-routes.json"


def _clean_route(item: dict[str, Any]) -> dict[str, Any] | None:
    handle = str(item.get("handle") or "").strip()
    project_id = str(item.get("project_id") or "").strip()
    root = str(item.get("root") or "").strip()
    if not handle or not project_id or not root:
        return None
    try:
        last_used = float(item.get("last_used") or 0.0)
    except (TypeError, ValueError):
        last_used = 0.0
    return {
        "handle": handle,
        "project_id": project_id,
        "root": root,
        "last_used": last_used,
    }


def _read_state_unlocked(path: Path) -> tuple[list[dict[str, Any]], dict[str, float]]:
    if not path.is_file():
        return [], {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    if not isinstance(payload, dict) or payload.get("version") not in _SUPPORTED_VERSIONS:
        return [], {}

    tombstones: dict[str, float] = {}
    raw_tombstones = payload.get("tombstones")
    if isinstance(raw_tombstones, list):
        for item in raw_tombstones[-_MAX_TOMBSTONES:]:
            if not isinstance(item, dict):
                continue
            handle = str(item.get("handle") or "").strip()
            if not handle:
                continue
            try:
                removed_at = float(item.get("removed_at") or 0.0)
            except (TypeError, ValueError):
                removed_at = 0.0
            if removed_at > tombstones.get(handle, 0.0):
                tombstones[handle] = removed_at

    routes = payload.get("routes")
    if not isinstance(routes, list):
        return [], tombstones
    result: list[dict[str, Any]] = []
    for item in routes[-_MAX_ROUTES:]:
        if not isinstance(item, dict):
            continue
        clean = _clean_route(item)
        if clean is None:
            continue
        if clean["last_used"] <= tombstones.get(clean["handle"], -1.0):
            continue
        result.append(clean)
    return result, tombstones


def load_workspace_routes() -> list[dict[str, Any]]:
    # Writers replace a complete file atomically, so readers never observe a
    # partially-written JSON document. The in-process lock also makes tests and
    # same-process Gateway handoffs deterministic.
    with _ROUTE_STATE_LOCK:
        routes, _tombstones = _read_state_unlocked(_path())
        return routes


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize route updates across Gateway threads and overlapping processes."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    acquired = False
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("workspace route state lock timed out") from None
                    time.sleep(_LOCK_RETRY_SECONDS)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("workspace route state lock timed out") from None
                    time.sleep(_LOCK_RETRY_SECONDS)
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                with suppress(OSError):
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with suppress(OSError):
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def save_workspace_routes(
    routes: list[dict[str, Any]],
    *,
    removed_handles: set[str] | frozenset[str] | None = None,
) -> None:
    """Atomically merge route upserts and explicit removals.

    Each Gateway holds an in-memory snapshot. During a restart or short
    double-instance window, a stale writer must not erase a route written by
    another instance. Incoming records therefore upsert by handle while
    ``removed_handles`` carries intentional invalidation separately. Bounded
    tombstones prevent an older snapshot from resurrecting a deleted route.
    """

    incoming: dict[str, dict[str, Any]] = {}
    for item in routes:
        if not isinstance(item, dict):
            continue
        clean = _clean_route(item)
        if clean is None:
            continue
        current = incoming.get(clean["handle"])
        if current is None or clean["last_used"] >= current["last_used"]:
            incoming[clean["handle"]] = clean
    removed = {
        str(handle or "").strip()
        for handle in (removed_handles or set())
        if str(handle or "").strip()
    }

    path = _path()
    with _ROUTE_STATE_LOCK, _exclusive_file_lock(path):
        existing_routes, tombstones = _read_state_unlocked(path)
        merged = {record["handle"]: record for record in existing_routes}
        removed_at = time.time()
        for handle in removed:
            merged.pop(handle, None)
            tombstones[handle] = max(tombstones.get(handle, 0.0), removed_at)
        for handle, record in incoming.items():
            removed_before = tombstones.get(handle, -1.0)
            if record["last_used"] <= removed_before:
                continue
            existing = merged.get(handle)
            if existing is None or record["last_used"] >= existing["last_used"]:
                merged[handle] = record
                tombstones.pop(handle, None)

        clean_routes = sorted(
            merged.values(), key=lambda item: item["last_used"]
        )[-_MAX_ROUTES:]
        clean_tombstones = sorted(
            (
                {"handle": handle, "removed_at": removed_at_value}
                for handle, removed_at_value in tombstones.items()
                if handle and removed_at_value > 0.0
            ),
            key=lambda item: item["removed_at"],
        )[-_MAX_TOMBSTONES:]

        tmp = path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with tmp.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    {
                        "version": _VERSION,
                        "routes": clean_routes,
                        "tombstones": clean_tombstones,
                    },
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            with suppress(OSError):
                tmp.unlink()


__all__ = ["load_workspace_routes", "save_workspace_routes"]
