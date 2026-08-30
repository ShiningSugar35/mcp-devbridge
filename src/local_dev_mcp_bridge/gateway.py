"""Shared OAuth/MCP Hub gateway; public modes terminate at the configured tunnel.

Listens on loopback only (``127.0.0.1:GATEWAY_PORT``). The Cloudflare route
for ``mcp.<domain>`` points at this app (instead of the engine), so the same
fixed URL ``https://<host>/mcp`` serves:

* **standard MCP OAuth** - protected-resource + authorization-server metadata,
  Dynamic Client Registration, /authorize (PKCE S256 with consent page),
  /token, /revoke - all via the mcp SDK 2.x ``create_auth_routes`` and our
  :class:`LocalOAuthProvider` (no hand-rolled protocol);
* **legacy ChatGPT bearer** - unchanged passthrough to the CodexPro engine,
  which validates the bearer itself (two-layer check);
* **OAuth access-token calls** - validated locally, then proxied to the
  engine with the engine bearer injected.

Only ``/mcp`` is exposed as the MCP resource; OAuth endpoints share the same
host. No second tunnel, no second URL, tokens never written to logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import shlex
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from email.utils import formatdate
from pathlib import Path
from typing import Any, cast

import httpx
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from . import constants
from .audit import AuditLogger
from .constants import LOG_DIR as _LOG_DIR
from .device_hub import DeviceRegistry
from .flight_recorder import FlightRecorder
from .hub_tool_contract import (
    HUB_TOOL_CONTRACT_FINGERPRINT,
    HUB_TOOL_CONTRACT_VERSION,
    HUB_TOOL_COUNT,
    load_codexpro_full_tool_contract,
)
from .oauth_provider import ConsentExpired, LocalOAuthProvider, _workspace_from_subject
from .platform_support import run_platform_kwargs
from .routing_state import load_workspace_routes, save_workspace_routes
from .secrets import SecretsStore
from .shell import detect_binaries, get_shell_info, run_command, run_program

_DEVICE_TOOL_NAMES = frozenset(
    {
        "devbridge_list_devices",
        "devbridge_get_current_device",
        "devbridge_switch_device",
    }
)
_LOCAL_TOOL_NAMES = (
    frozenset(
        {
            "run_command",
            "run_program",
            "shell_self_test",
            "devbridge_list_workspaces",
            "devbridge_get_current_workspace",
            "devbridge_switch_workspace",
        }
    )
    | _DEVICE_TOOL_NAMES
)

_WORKSPACE_CONTEXT_FREE_TOOLS = frozenset(
    {
        "shell_self_test",
        "devbridge_list_workspaces",
        "devbridge_get_current_workspace",
        "devbridge_switch_workspace",
    }
) | _DEVICE_TOOL_NAMES

_CODEXPRO_MINIMAL_TOOLS = frozenset(
    {
        "codexpro",
        "server_config",
        "codexpro_self_test",
        "open_current_workspace",
        "open_workspace",
        "read",
        "write",
        "edit",
        "apply_patch",
        "bash",
        "get_task",
        "wait_task",
        "list_tasks",
        "cancel_task",
        "show_changes",
        "long_run_start",
        "long_run_status",
        "long_run_update",
        "long_run_review",
        "long_run_complete",
        "long_run_list",
        "long_run_cancel",
    }
)
_READ_ONLY_DISABLED_TOOLS = frozenset(
    {
        "write",
        "edit",
        "apply_patch",
        "bash",
        "cancel_task",
        "long_run_start",
        "long_run_update",
        "long_run_review",
        "long_run_complete",
        "long_run_cancel",
        "run_command",
        "run_program",
        "windows_call",
    }
)
_READ_ONLY_FEATURE_UNAVAILABLE_TOOLS = frozenset(
    {
        "get_task",
        "wait_task",
        "list_tasks",
    }
)
_FULL_MODE_ONLY_TOOLS = frozenset(
    {
        "codexpro_inventory",
        "list_workspaces",
        "workspace_snapshot",
        "git_status",
        "git_diff",
        "codex_context",
        "handoff_to_codex",
    }
)
_READ_ONLY_EXTRA_SAFE_TOOLS = frozenset(
    {
        "shell_self_test",
        "devbridge_list_workspaces",
        "devbridge_get_current_workspace",
        "devbridge_switch_workspace",
        "devbridge_list_devices",
        "devbridge_get_current_device",
        "devbridge_switch_device",
        "windows_backend_status",
        "windows_list_tools",
    }
)

_ROUTE_WORKSPACE_ARG = "devbridge_workspace_id"
_ROUTE_DEVICE_ARG = "devbridge_device_id"
_ROUTE_HINT_DESCRIPTION = (
    "Compatibility override only. Normally omit this field: MCP DevBridge automatically routes "
    "each call by absolute path, cwd, existing relative path, or task affinity."
)
_ROUTE_PATH_KEYS = ("path", "root", "cwd", "target_path")
_TASK_AFFINITY_TOOLS = frozenset({"get_task", "wait_task", "cancel_task"})
_WRITE_ROUTE_TOOLS = frozenset({"write", "edit", "apply_patch"})
_CODEXPRO_ACTION_ALIASES = {
    "actions": "list_actions",
    "config": "server_config",
    "self_test": "codexpro_self_test",
    "inventory": "codexpro_inventory",
    "open": "open_current_workspace",
    "snapshot": "workspace_snapshot",
    "changes": "show_changes",
    "handoff_poll": "wait_for_handoff",
    "pro_export": "export_pro_context",
    "agent_handoff": "handoff_to_agent",
    "codex_handoff": "handoff_to_codex",
}

_PYTHON_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Run only a short PowerShell command (hard cap 20 seconds). For builds, tests, installs, crawls, or other long work, use the background bash task tool and poll with wait_task/get_task instead of holding one MCP call open.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to project root. Defaults to project root.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Short-call timeout in seconds (default 10, hard max 20). Use bash for long work.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_program",
        "description": "Run only a short program directly (hard cap 20 seconds). For long-running programs use the background bash task tool and poll wait_task/get_task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "description": "Program path or name"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Argument list",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory relative to project root. Defaults to project root.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Short-call timeout in seconds (default 10, hard max 20). Use bash for long work.",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["executable"],
        },
    },
    {
        "name": "shell_self_test",
        "description": "Check whether the default shell is executable and whether python/git/pytest/pyright are available. Returns per-line checkmarks.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "devbridge_list_workspaces",
        "description": "List all registered project workspaces with path, status, and CodexPro port.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "devbridge_get_current_workspace",
        "description": "Return the workspace project bound to the current MCP session.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "devbridge_switch_workspace",
        "description": "Switch the current MCP session to a different project workspace. Only affects the calling client, not other GPT/Gemini sessions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project ID from devbridge_list_workspaces output",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "devbridge_list_devices",
        "description": "List computers connected to this MCP DevBridge Hub and whether each one is online.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devbridge_get_current_device",
        "description": "Return the computer currently selected for this MCP session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "devbridge_switch_device",
        "description": "Switch only the current MCP session to another online computer from devbridge_list_devices.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "Device ID from devbridge_list_devices",
                },
            },
            "required": ["device_id"],
        },
    },
]

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MCP DevBridge - 访问授权</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1f2328}}
h1{{font-size:20px}}
dl{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px}}
dt{{color:#57606a;font-size:12px;text-transform:uppercase;margin-top:8px}}
dd{{margin:2px 0 8px;word-break:break-all}}
form{{margin-top:16px;display:flex;gap:12px;align-items:center}}
button{{font-size:15px;padding:8px 22px;border-radius:6px;cursor:pointer}}
.allow{{background:#1f883d;color:#fff;border:1px solid #1f883d}}
.cancel{{background:#fff;border:1px solid #d0d7de}}
.note{{color:#57606a;font-size:13px;line-height:1.55}}
</style></head>
<body><h1>MCP DevBridge - 访问授权</h1>
<p>AI 客户端正在请求访问这个 MCP DevBridge Hub。</p>
<dl>{rows}</dl>
<p class="note">授权后无需选择“入口项目”。本机所有运行中的工作区根会同时参与自动路由；有多台在线电脑时仍可按需切换目标设备。</p>
<form method="post" action="/consent">
<input type="hidden" name="id" value="{cid}">
<button type="submit" name="decision" value="allow" class="allow">允许访问</button>
<button type="submit" name="decision" value="deny" class="cancel">取消</button>
</form></body></html>"""

_HOP_HEADERS = frozenset(
    {"host", "connection", "content-length", "transfer-encoding", "authorization"}
)
_SSE_KEEPALIVE_SECONDS = 12.0
_MAX_AFFINITY_ENTRIES = 16_384
_MAX_WORKSPACE_ROUTE_ENTRIES = 512
_MAX_WORKSPACE_REHYDRATE_INFLIGHT = 64


def _remember_bounded_affinity(mapping: dict[str, str], key: str, value: str) -> None:
    if not key:
        return
    if key in mapping:
        mapping.pop(key, None)
    mapping[key] = value
    while len(mapping) > _MAX_AFFINITY_ENTRIES:
        oldest = next(iter(mapping), None)
        if oldest is None:
            break
        mapping.pop(oldest, None)


# --------------------------------------------------------- diagnostic logging
_DIAG_SENSITIVE_KEYS = frozenset(
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
    }
)
_DIAG_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
_DIAG_BODY_MAX = 2048
_DIAG_LOG_FILE: str = ""


def _diag_log_path() -> Path:
    log_dir = _LOG_DIR
    if not log_dir.exists():
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"gateway-{time.strftime('%Y-%m-%d')}.jsonl"


def _diag_short_hash(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def _diag_redact_body(body: bytes | str) -> str:
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text[:_DIAG_BODY_MAX]
    if isinstance(data, dict):
        data = dict(data)
        for key in list(data):
            if key.lower() in _DIAG_SENSITIVE_KEYS:
                data[key] = "***REDACTED***"
    return json.dumps(data, ensure_ascii=False, default=str)[:_DIAG_BODY_MAX]


def _diag_redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: "***" if k.lower() in _DIAG_SENSITIVE_HEADERS else v for k, v in headers.items()}


def _write_diag_entry(**fields: Any) -> None:
    entry = {**fields, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    try:
        p = _diag_log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


class UpstreamResponseTooLarge(RuntimeError):
    pass


async def _read_and_close_upstream(
    response: httpx.Response,
    *,
    deadline_at: float | None = None,
    max_bytes: int | None = None,
) -> bytes:
    async def consume() -> bytes:
        chunks: list[bytes] = []
        total = 0
        stream = cast(httpx.AsyncByteStream, response.stream)
        async for chunk in stream:
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise UpstreamResponseTooLarge(
                    f"upstream response exceeded {max_bytes} bytes"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        if deadline_at is None:
            return await consume()
        async with asyncio.timeout_at(deadline_at):
            return await consume()
    finally:
        await response.aclose()


async def _stream_and_close_upstream(
    response: httpx.Response,
    *,
    deadline_at: float | None = None,
    on_terminal: Callable[[str], None] | None = None,
):
    terminal = "completed"
    try:
        # Iterate the leased transport stream directly instead of Response.aiter_raw().
        # The latter raises StreamConsumed when a cancellation/reconnect marks the
        # response consumed before Starlette begins forwarding it.  This stream has
        # exactly one owner and is always closed when the downstream disconnects.
        stream = cast(httpx.AsyncByteStream, response.stream)
        if deadline_at is None:
            async for chunk in stream:
                yield chunk
        else:
            async with asyncio.timeout_at(deadline_at):
                async for chunk in stream:
                    yield chunk
    except TimeoutError:
        terminal = "deadline_exceeded"
        raise
    except (asyncio.CancelledError, GeneratorExit):
        terminal = "downstream_cancelled"
        raise
    except BaseException as exc:
        terminal = type(exc).__name__
        raise
    finally:
        try:
            await response.aclose()
        finally:
            if on_terminal is not None:
                with suppress(Exception):
                    on_terminal(terminal)


def _sse_event_boundary_at_end(suffix: bytes) -> bool:
    return suffix.endswith((b"\n\n", b"\r\r", b"\r\n\r\n", b"\r\n\n", b"\n\r\n"))


async def _stream_sse_with_keepalive_and_close_upstream(
    response: httpx.Response,
    *,
    keepalive_seconds: float = _SSE_KEEPALIVE_SECONDS,
    deadline_at: float | None = None,
    timeout_event: bytes = b"",
    on_terminal: Callable[[str], None] | None = None,
):
    """Forward one SSE stream while keeping an otherwise-idle HTTP leg alive.

    SSE comment frames are ignored by clients and never alter JSON-RPC payloads.
    Reading the upstream stays single-owner: a producer task is the only iterator
    over ``response.stream`` and this generator is the only downstream consumer.
    Cancellation closes the producer and leased response exactly once.
    """

    stream = cast(httpx.AsyncByteStream, response.stream)
    queue: asyncio.Queue[tuple[str, bytes | BaseException | None]] = asyncio.Queue(maxsize=1)

    async def _pump() -> None:
        try:
            async for chunk in stream:
                await queue.put(("data", chunk))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await queue.put(("error", exc))
        else:
            await queue.put(("done", None))

    producer = asyncio.create_task(_pump(), name="mcp-devbridge-sse-forwarder")
    interval = max(0.05, float(keepalive_seconds))
    at_event_boundary = True
    suffix = b""
    terminal = "completed"
    try:
        while True:
            remaining: float | None = None
            if deadline_at is not None:
                remaining = deadline_at - asyncio.get_running_loop().time()
                if remaining <= 0:
                    terminal = "deadline_exceeded"
                    if at_event_boundary and timeout_event:
                        yield timeout_event
                    return
            wait_seconds = interval if remaining is None else min(interval, remaining)
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
            except TimeoutError:
                if deadline_at is not None and asyncio.get_running_loop().time() >= deadline_at:
                    terminal = "deadline_exceeded"
                    if at_event_boundary and timeout_event:
                        yield timeout_event
                    return
                # A colon-prefixed SSE line is a protocol comment. It is useful
                # purely as transport liveness and is invisible to JSON-RPC. Never
                # insert it into the middle of an upstream event split across chunks.
                if at_event_boundary:
                    yield b": devbridge-keepalive\n\n"
                continue
            if kind == "done":
                return
            if kind == "error":
                assert isinstance(payload, BaseException)
                terminal = (
                    "deadline_exceeded"
                    if isinstance(payload, (TimeoutError, httpx.TimeoutException))
                    else type(payload).__name__
                )
                raise payload
            assert isinstance(payload, bytes)
            suffix = (suffix + payload)[-4:]
            at_event_boundary = _sse_event_boundary_at_end(suffix)
            yield payload
    except (asyncio.CancelledError, GeneratorExit):
        terminal = "downstream_cancelled"
        raise
    except BaseException as exc:
        if terminal == "completed":
            terminal = type(exc).__name__
        raise
    finally:
        if not producer.done():
            producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
        try:
            await response.aclose()
        finally:
            if on_terminal is not None:
                with suppress(Exception):
                    on_terminal(terminal)


def _upstream_is_sse(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.lower().split(";", 1)[0].strip() == "text/event-stream"


def _row(label: str, value: str) -> str:
    return f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"


def _constant_time_eq(left: str, right: str | None) -> bool:
    return bool(right) and hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _rewrite_server_identity(payload: bytes) -> bytes:
    """Replace the engine's serverInfo with MCP DevBridge on initialize.

    Only touches the display identity (name/title); neither the engine
    binary (a third-party upstream artifact) nor any protocol field changes.
    Handles both SSE (`data: {...}` lines) and plain JSON bodies.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    def _patch(obj: Any) -> Any:
        target = obj
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            target = obj["result"]
        if isinstance(target, dict) and isinstance(target.get("serverInfo"), dict):
            target["serverInfo"]["name"] = "mcp-devbridge"
            target["serverInfo"]["title"] = "MCP DevBridge"
            routing_note = (
                "\n\nMCP DevBridge routing: every running local workspace root is active at the "
                "same time. Do not switch/current-workspace just to reach another project. Prefer "
                "absolute path/cwd arguments when several roots are running; DevBridge chooses the "
                "longest matching active root automatically. Relative paths are auto-matched when "
                "they identify one active root. devbridge_workspace_id remains a compatibility-only "
                "explicit override. Background bash task follow-ups are routed by task_id, so "
                "wait_task/get_task/cancel_task do not need a workspace switch."
            )
            current = str(target.get("instructions") or "")
            if "MCP DevBridge routing:" not in current:
                target["instructions"] = current + routing_note
        return obj

    if text.lstrip().startswith("data:") or "\ndata:" in text:
        out_lines = []
        for line in text.splitlines(keepends=True):
            if line.startswith("data:"):
                body_line = line[5:].lstrip()
                if body_line.strip() == "[DONE]":
                    continue
                try:
                    obj = json.loads(body_line.strip())
                    out_lines.append(f"data: {json.dumps(_patch(obj), ensure_ascii=False)}\n")
                    continue
                except json.JSONDecodeError:
                    pass
            out_lines.append(line)
        return "".join(out_lines).encode("utf-8")

    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and (
            "serverInfo" in obj
            or (isinstance(obj.get("result"), dict) and "serverInfo" in obj["result"])
        ):
            return json.dumps(_patch(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _tools_payload_objects(payload: bytes) -> list[dict[str, Any]]:
    """Decode JSON or SSE-wrapped JSON-RPC responses without logging raw bodies."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    objects: list[dict[str, Any]] = []
    if text.lstrip().startswith("data:") or "\ndata:" in text:
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        return objects
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [value] if isinstance(value, dict) else []


def _tools_response_summary(payload: bytes) -> dict[str, Any]:
    """Return safe outcome/count/duplicates/schema fingerprint telemetry."""
    for data in _tools_payload_objects(payload):
        if isinstance(data.get("error"), dict):
            error = data["error"]
            return {
                "outcome": "jsonrpc_error",
                "count": 0,
                "duplicates": [],
                "schema_fingerprint": "",
                "error_code": error.get("code"),
            }
        result = data.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            continue
        tools = [tool for tool in result["tools"] if isinstance(tool, dict)]
        names = [str(tool.get("name") or "") for tool in tools]
        seen: set[str] = set()
        dupes: list[str] = []
        for name in names:
            if name in seen:
                dupes.append(name)
            seen.add(name)
        canonical = [
            {
                "name": str(tool.get("name") or ""),
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema")
                if isinstance(tool.get("inputSchema"), dict)
                else {},
            }
            for tool in tools
        ]
        canonical.sort(key=lambda item: item["name"])
        fingerprint = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "outcome": "tools_result",
            "count": len(names),
            "duplicates": dupes,
            "schema_fingerprint": fingerprint,
            "error_code": None,
        }
    return {
        "outcome": "malformed_or_empty",
        "count": 0,
        "duplicates": [],
        "schema_fingerprint": "",
        "error_code": None,
    }


def _analyze_tools(payload: bytes) -> tuple[int, list[str]]:
    """Compatibility wrapper returning count + duplicates for tools/list."""
    summary = _tools_response_summary(payload)
    return int(summary["count"]), list(summary["duplicates"])


def _inject_tools(payload: bytes) -> bytes:
    """Add Gateway tools and stateless routing hints to tools/list."""
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    def add_route_args(tool: dict[str, Any]) -> None:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            return
        props = schema.setdefault("properties", {})
        if not isinstance(props, dict):
            return
        props.setdefault(
            _ROUTE_WORKSPACE_ARG,
            {"type": "string", "description": _ROUTE_HINT_DESCRIPTION},
        )
        props.setdefault(
            _ROUTE_DEVICE_ARG,
            {"type": "string", "description": _ROUTE_HINT_DESCRIPTION},
        )
        description = str(tool.get("description") or "")
        note = " Optional MCP DevBridge routing fields may be supplied after a switch."
        if note.strip() not in description:
            tool["description"] = description + note

    def patch_obj(obj: Any) -> Any:
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools")
            if isinstance(tools, list):
                for item in tools:
                    if isinstance(item, dict):
                        add_route_args(item)
                names = {
                    str(item.get("name", ""))
                    for item in tools
                    if isinstance(item, dict) and item.get("name")
                }
                for tool in _PYTHON_TOOL_DEFS:
                    if tool["name"] in names:
                        continue
                    injected = dict(tool)
                    input_schema = dict(tool.get("inputSchema") or {})
                    input_schema["properties"] = dict(input_schema.get("properties") or {})
                    injected["inputSchema"] = input_schema
                    add_route_args(injected)
                    tools.append(injected)
                    names.add(tool["name"])
        return obj

    if decoded.lstrip().startswith("data:") or "\ndata:" in decoded:
        out_lines = []
        for line in decoded.splitlines(keepends=True):
            if line.startswith("data:"):
                body_line = line[5:].lstrip()
                if body_line.strip() == "[DONE]":
                    continue
                try:
                    obj = json.loads(body_line.strip())
                    out_lines.append(f"data: {json.dumps(patch_obj(obj), ensure_ascii=False)}\n")
                    continue
                except json.JSONDecodeError:
                    pass
            out_lines.append(line)
        return "".join(out_lines).encode("utf-8")

    try:
        obj = json.loads(decoded)
        if (
            isinstance(obj, dict)
            and isinstance(obj.get("result"), dict)
            and "tools" in obj["result"]
        ):
            return json.dumps(patch_obj(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _build_stable_hub_tools() -> tuple[dict[str, Any], ...]:
    """Build the versioned public Hub schema from embedded CodexPro + Gateway tools."""
    seed = {
        "jsonrpc": "2.0",
        "id": 0,
        "result": {"tools": load_codexpro_full_tool_contract()},
    }
    patched = _inject_tools(json.dumps(seed, ensure_ascii=False).encode("utf-8"))
    value = json.loads(patched.decode("utf-8"))
    tools = value.get("result", {}).get("tools") if isinstance(value, dict) else None
    if not isinstance(tools, list):
        raise RuntimeError("failed to construct stable Hub tool contract")
    clean = tuple(dict(tool) for tool in tools if isinstance(tool, dict))
    names = [str(tool.get("name") or "") for tool in clean]
    if len(clean) != HUB_TOOL_COUNT or len(names) != len(set(names)):
        raise RuntimeError(
            f"Hub tool contract v{HUB_TOOL_CONTRACT_VERSION} must contain "
            f"{HUB_TOOL_COUNT} unique tools; got {len(clean)}"
        )
    summary = _tools_response_summary(
        json.dumps(
            {"jsonrpc": "2.0", "id": 0, "result": {"tools": clean}},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    fingerprint = str(summary.get("schema_fingerprint") or "")
    if fingerprint != HUB_TOOL_CONTRACT_FINGERPRINT:
        raise RuntimeError(
            f"Hub tool contract v{HUB_TOOL_CONTRACT_VERSION} fingerprint mismatch: {fingerprint}"
        )
    return clean


_STABLE_HUB_TOOLS = _build_stable_hub_tools()
_STABLE_HUB_READ_ONLY_TOOLS = frozenset(
    str(tool.get("name") or "")
    for tool in _STABLE_HUB_TOOLS
    if isinstance(tool.get("annotations"), dict)
    and bool(tool["annotations"].get("readOnlyHint"))
)
_WORKSPACE_ERROR_INSPECT_MAX_BYTES = 64 * 1024
_UPSTREAM_CONTROL_DEADLINE_SECONDS = 15.0
_UPSTREAM_ORDINARY_DEADLINE_SECONDS = 45.0
_UPSTREAM_WAIT_GRACE_SECONDS = 10.0
_UPSTREAM_WAIT_TASK_MAX_SECONDS = 120
_UPSTREAM_LONG_RUN_STATUS_MAX_SECONDS = 60
_UPSTREAM_BUFFER_MAX_BYTES = 8 * 1024 * 1024


def _stable_tools_list_payload(rpc_id: Any) -> bytes:
    return json.dumps(
        {"jsonrpc": "2.0", "id": rpc_id, "result": {"tools": _STABLE_HUB_TOOLS}},
        ensure_ascii=False,
    ).encode("utf-8")


def _jsonrpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _upstream_deadline_seconds(
    request_method: str, jsonrpc_method: str, tool_name: str, arguments: dict[str, Any]
) -> float | None:
    if request_method.upper() != "POST":
        return None
    if jsonrpc_method in {"initialize", "ping", "tools/list"}:
        return float(_UPSTREAM_CONTROL_DEADLINE_SECONDS)
    if jsonrpc_method == "tools/call" and tool_name == "wait_task":
        requested = _bounded_int(
            arguments.get("wait_seconds"), 30, 1, _UPSTREAM_WAIT_TASK_MAX_SECONDS
        )
        return float(requested) + float(_UPSTREAM_WAIT_GRACE_SECONDS)
    if jsonrpc_method == "tools/call" and tool_name == "long_run_status":
        requested = _bounded_int(
            arguments.get("max_wait_seconds"), 20, 1, _UPSTREAM_LONG_RUN_STATUS_MAX_SECONDS
        )
        return float(requested) + float(_UPSTREAM_WAIT_GRACE_SECONDS)
    return float(_UPSTREAM_ORDINARY_DEADLINE_SECONDS)


def _sse_deadline_event(rpc_id: Any, deadline_seconds: float) -> bytes:
    payload = _jsonrpc_error(
        rpc_id,
        -32008,
        f"upstream call exceeded the bounded {deadline_seconds:g}s deadline; "
        "the request was cancelled and its connection released. "
        "Use bash plus wait_task/get_task for long-running work.",
    )
    return (
        "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    ).encode("utf-8")


def _tool_arguments(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    value = params.get("arguments")
    return dict(value) if isinstance(value, dict) else {}


def _strip_route_arguments(
    body: bytes,
    rpc: dict[str, Any] | None,
    *,
    keep_workspace: bool,
    drop_workspace_handle: bool = False,
    tool_name: str = "",
) -> bytes:
    if rpc is None or str(rpc.get("method", "")) != "tools/call":
        return body
    params = rpc.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return body
    nested = arguments.get("args") if tool_name == "codexpro" else None
    has_workspace_handle = (
        isinstance(nested, dict) and "workspace_id" in nested
        if tool_name == "codexpro"
        else "workspace_id" in arguments
    )
    if (
        _ROUTE_DEVICE_ARG not in arguments
        and (keep_workspace or _ROUTE_WORKSPACE_ARG not in arguments)
        and not (drop_workspace_handle and has_workspace_handle)
    ):
        return body
    copied_rpc = dict(rpc)
    copied_params = dict(params)
    copied_args = dict(arguments)
    copied_args.pop(_ROUTE_DEVICE_ARG, None)
    if not keep_workspace:
        copied_args.pop(_ROUTE_WORKSPACE_ARG, None)
    if drop_workspace_handle:
        if tool_name == "codexpro" and isinstance(nested, dict):
            copied_nested = dict(nested)
            copied_nested.pop("workspace_id", None)
            copied_args["args"] = copied_nested
        else:
            copied_args.pop("workspace_id", None)
    copied_params["arguments"] = copied_args
    copied_rpc["params"] = copied_params
    return json.dumps(copied_rpc, ensure_ascii=False).encode("utf-8")


def _is_loopback(request: Request) -> bool:
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first.startswith("127.")
    client = request.client
    return bool(
        client
        and (client.host in ("127.0.0.1", "::1", "localhost") or client.host.startswith("127."))
    )


class _DiagnosticMiddleware:
    """Pure ASGI lifecycle recorder safe for ordinary and streaming responses."""

    def __init__(self, app: Any, recorder: FlightRecorder | None = None) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_ns = time.monotonic_ns()
        method: str = scope.get("method", "")
        path: str = scope.get("path", "")
        if path == constants.DEFAULT_MCP_PATH:
            component = "mcp"
        elif path.startswith(("/authorize", "/token", "/register", "/revoke", "/consent", "/.well-known/")):
            component = "oauth"
        else:
            component = "http"
        trace_id = (
            self.recorder.start_request(method=method, path=path, component=component)
            if self.recorder is not None
            else ""
        )
        if trace_id:
            scope["devbridge_trace_id"] = trace_id
        status: list[int] = [0]
        resp_content_type: list[str] = [""]
        resp_bytes: list[int] = [0]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status[0] = message.get("status", 0)
                for h in message.get("headers") or []:
                    if h[0].decode("latin-1").lower() == "content-type":
                        resp_content_type[0] = h[1].decode("latin-1")
                if self.recorder is not None and trace_id:
                    self.recorder.stage(
                        trace_id,
                        "response_started",
                        status=status[0],
                        component=component,
                        content_type=resp_content_type[0],
                        streaming="text/event-stream" in resp_content_type[0].casefold(),
                    )
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                resp_bytes[0] += len(body)
            await send(message)

        exc_type: str = ""
        exc_msg: str = ""
        outcome = "completed"
        try:
            await self.app(scope, receive, _send)
        except asyncio.CancelledError:
            outcome = "cancelled"
            exc_type = "CancelledError"
            raise
        except Exception as exc:
            outcome = "error"
            exc_type = type(exc).__name__
            exc_msg = _diag_redact_body(str(exc)[:500])
            raise
        finally:
            duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
            if self.recorder is not None and trace_id:
                self.recorder.finish_request(
                    trace_id,
                    outcome=outcome,
                    status=status[0],
                    response_bytes=resp_bytes[0],
                    exception_type=exc_type,
                    component=component,
                    content_type=resp_content_type[0],
                    streaming="text/event-stream" in resp_content_type[0].casefold(),
                )
            _write_diag_entry(
                path=path,
                method=method,
                status=status[0],
                content_type=resp_content_type[0],
                resp_bytes=resp_bytes[0],
                duration_ms=duration_ms,
                exc_type=exc_type,
                exc_msg=exc_msg,
            )


class OAuthGateway:
    """The public Starlette app + in-process uvicorn thread (loopback only)."""

    def __init__(
        self,
        *,
        public_hostname: str,
        workspace: str = "",
        upstream_url: str = "",
        upstream_legacy_token: Callable[[], str | None] | None = None,
        allow_local_anonymous: bool = True,
        store: SecretsStore | None = None,
        provider: LocalOAuthProvider | None = None,
        transport: Any | None = None,
        workspace_registry: Callable[[str], tuple[int, str] | None] | None = None,
        workspace_project_registry: Callable[[str], str | None] | None = None,
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        device_registry: DeviceRegistry | None = None,
        flight_recorder: FlightRecorder | None = None,
        local_device_id: str = "",
    ) -> None:
        hostname = (public_hostname or "").strip().rstrip("/")
        base = (
            hostname
            if hostname.lower().startswith(("http://", "https://"))
            else f"https://{hostname}"
        )
        self.public_base = base.rstrip("/")
        self.resource_url = f"{self.public_base}{constants.DEFAULT_MCP_PATH}"
        if upstream_url:
            self.upstream_url = upstream_url
        else:
            try:
                from .engines import CODEXPRO_LOCAL_PORT
            except Exception:  # pragma: no cover - import always succeeds in-package
                CODEXPRO_LOCAL_PORT = 8787
            self.upstream_url = f"http://127.0.0.1:{CODEXPRO_LOCAL_PORT}"
        self.upstream_token_source = upstream_legacy_token or (
            lambda: SecretsStore().get(constants.ACCESS_TOKEN_CRED_NAME)
        )
        self.allow_local_anonymous = allow_local_anonymous
        self._workspace = Path(workspace) if workspace else None
        self._workspace_registry = workspace_registry
        self._workspace_project_registry = workspace_project_registry
        self._workspace_credential_registry = workspace_credential_registry
        self._device_registry = device_registry
        self._flight_recorder = flight_recorder
        self._local_device_id = local_device_id or (
            device_registry.local_device_id if device_registry else ""
        )
        self._audit = AuditLogger()
        self._provider = provider or LocalOAuthProvider(
            issuer_url=self.public_base,
            resource_url=self.resource_url,
            workspace=workspace,
            store=store or SecretsStore(),
        )
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=15.0, write=30.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=64,
                max_keepalive_connections=16,
                keepalive_expiry=30.0,
            ),
            transport=transport,
        )
        self.app = self._build_app()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        # Per-client-session routing plus per-upstream MCP transport sessions.
        # One ChatGPT-facing session can switch among several independent CodexPro
        # servers, but every CodexPro server owns a different mcp-session-id.
        self._session_workspaces: dict[str, str] = {}
        self._session_devices: dict[str, str] = {}
        self._task_workspaces: dict[str, str] = {}
        # open_workspace returns an opaque workspace_id reused by later tools that
        # may have no path/cwd argument. Keep affinity so those follow-up calls
        # stay on the root that created the handle without any DevBridge switch.
        self._workspace_handle_roots: dict[str, str] = {}
        self._workspace_handle_paths: dict[str, str] = {}
        self._workspace_route_records: dict[str, dict[str, Any]] = {}
        self._workspace_hydrated_handles: set[str] = set()
        self._workspace_rehydrate_inflight: dict[tuple[str, str], asyncio.Task[str]] = {}
        self._session_lock = threading.Lock()
        self._load_persistent_workspace_routes()
        # Hot-path routing snapshot. Project config is disk-backed; avoid re-reading
        # it for every MCP call while keeping start/stop/config changes fresh.
        self._workspace_snapshot_lock = threading.Lock()
        self._workspace_snapshot_expires = 0.0
        self._workspace_snapshot: list[tuple[str, Path, str]] = []

    # ------------------------------------------------------------ build
    @property
    def provider(self) -> LocalOAuthProvider:
        return self._provider

    def _build_app(self) -> Any:
        registration = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[constants.OAUTH_SCOPE, constants.OAUTH_OFFLINE_SCOPE],
            default_scopes=[constants.OAUTH_SCOPE],
        )
        routes: list[Route] = create_auth_routes(
            self._provider,
            issuer_url=AnyHttpUrl(self.public_base),
            client_registration_options=registration,
            revocation_options=RevocationOptions(enabled=True),
        )
        resource = create_protected_resource_routes(
            AnyHttpUrl(self.resource_url),
            [AnyHttpUrl(self.public_base)],
            scopes_supported=[constants.OAUTH_SCOPE, constants.OAUTH_OFFLINE_SCOPE],
            resource_name="MCP DevBridge",
        )
        routes.extend(resource)
        routes.append(
            Route(
                "/.well-known/oauth-protected-resource",
                endpoint=resource[0].endpoint,
                methods=["GET", "OPTIONS"],
            )
        )
        routes.append(
            Route(constants.DEFAULT_MCP_PATH, self._mcp_endpoint, methods=["GET", "POST"])
        )
        routes.append(Route("/consent", self._consent_page, methods=["GET"]))
        routes.append(Route("/consent", self._consent_submit, methods=["POST"]))
        routes.append(Route("/device/register", self._device_register, methods=["POST"]))
        routes.append(Route("/device/heartbeat", self._device_heartbeat, methods=["POST"]))
        routes.append(Route("/health", self._health))
        @asynccontextmanager
        async def lifespan(_app: Starlette):
            try:
                yield
            finally:
                if not self._http.is_closed:
                    await self._http.aclose()

        app = Starlette(routes=routes, lifespan=lifespan)
        return _DiagnosticMiddleware(app, self._flight_recorder)

    # ---------------------------------------------------------- consent
    async def _consent_page(self, request: Request) -> Response:
        consent = self._provider.get_consent(request.query_params.get("id", ""))
        if consent is None:
            return HTMLResponse(
                "<html><body><p>授权请求已过期或不存在，请重新发起连接。</p></body></html>",
                status_code=410,
            )
        rows = [
            _row("MCP Server", self.resource_url),
            _row("访问范围", "此 Hub 下的在线设备与工作区（连接后按会话切换）"),
            _row("请求的权限范围", ", ".join(consent["scopes"])),
            _row("调用方 Client ID", consent["client_id"]),
        ]
        cid = html.escape(request.query_params.get("id", ""))
        return HTMLResponse(_PAGE.format(rows="".join(rows), cid=cid))

    async def _consent_submit(self, request: Request) -> Response:
        form = await request.form()
        consent_id = str(form.get("id", ""))
        decision = str(form.get("decision", "deny"))
        _write_diag_entry(path="/consent", method="POST", decision=decision)
        try:
            if decision == "allow":
                target = self._provider.approve(consent_id)
            else:
                target = self._provider.deny(consent_id)
        except ConsentExpired:
            _write_diag_entry(
                path="/consent",
                method="POST",
                error="consent_expired",
            )
            return HTMLResponse(
                "<html><body><p>授权已过期或不存在，请重新发起连接。</p></body></html>",
                status_code=410,
            )
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------ device pairing
    async def _device_register(self, request: Request) -> Response:
        if self._device_registry is None:
            return JSONResponse(
                {"ok": False, "message": "当前 Hub 未启用多设备功能。"}, status_code=404
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确。")
            peer_value = self._device_registry.register_remote(
                pair_code=str(payload.get("pair_code", "")),
                device_id=str(payload.get("device_id", "")),
                name=str(payload.get("name", "")),
                endpoint_url=str(payload.get("mcp_url", "")),
                bearer=str(payload.get("bearer", "")),
            )
            _write_diag_entry(path="/device/register", method="POST", event="device_paired")
            return JSONResponse({"ok": True, "peer_secret": peer_value, "heartbeat_seconds": 15})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

    async def _device_heartbeat(self, request: Request) -> Response:
        if self._device_registry is None:
            return JSONResponse(
                {"ok": False, "message": "当前 Hub 未启用多设备功能。"}, status_code=404
            )
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确。")
            device = self._device_registry.heartbeat(
                device_id=str(payload.get("device_id", "")),
                peer_secret=str(payload.get("peer_secret", "")),
                endpoint_url=str(payload.get("mcp_url", "")),
                name=str(payload.get("name", "")),
                bearer=str(payload.get("bearer", "")),
            )
            return JSONResponse({"ok": True, "device_id": device.id})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=401)

    # ------------------------------------------------------------- /mcp
    async def _mcp_endpoint(self, request: Request) -> Response:
        body = await request.body()
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        engine_credential = self.upstream_token_source()
        session_id = self._extract_session_id(request)

        jsonrpc_method = ""
        tool_name = ""
        rpc: dict[str, Any] | None = None
        if request.method == "POST":
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    rpc = parsed
                    jsonrpc_method = str(parsed.get("method", ""))
                    if jsonrpc_method == "tools/call":
                        tool_name = str((parsed.get("params") or {}).get("name", ""))
            except json.JSONDecodeError:
                rpc = None

        call_arguments = _tool_arguments(rpc.get("params") if rpc is not None else {})
        route_workspace_id = str(call_arguments.get(_ROUTE_WORKSPACE_ARG) or "").strip()
        route_device_id = str(call_arguments.get(_ROUTE_DEVICE_ARG) or "").strip()
        trace_id = str(request.scope.get("devbridge_trace_id") or "")
        if self._flight_recorder is not None and trace_id:
            self._flight_recorder.enrich_request(
                trace_id,
                jsonrpc_method=jsonrpc_method,
                tool_name=tool_name,
                session_id=session_id,
                project_id=route_workspace_id,
                device_id=route_device_id,
            )

        proxy_token: str | None = None
        workspace_id = ""
        authenticated_workspace = ""
        upstream_target: str | None = None
        if bearer:
            if _constant_time_eq(bearer, engine_credential):
                proxy_token = engine_credential or bearer
            else:
                # OAuth is the normal public-connector credential. Resolve it before the
                # backward-compatible per-project bearer scan so a slow native credential
                # store cannot stall every OAuth request on the Gateway event loop.
                record = await self._provider.load_access_token(bearer)
                if record is not None:
                    if record.resource and record.resource.rstrip("/") != self.resource_url:
                        return self._unauthorized()
                    workspace_id = _workspace_from_subject(record.subject or "")
                    proxy_token = engine_credential
                    if not proxy_token and self._workspace_registry is None:
                        return self._unauthorized()
                else:
                    authenticated_workspace = await asyncio.to_thread(
                        self._workspace_for_credential, bearer
                    )
                    if not authenticated_workspace:
                        return self._unauthorized()
                    # A project bearer remains a backward-compatible fallback, but path/task
                    # routing may override it so the credential never becomes a routing fence.
                    workspace_id = authenticated_workspace
                    proxy_token = engine_credential or bearer
        elif self.allow_local_anonymous and _is_loopback(request):
            proxy_token = None
        else:
            return self._unauthorized()

        if rpc is not None and jsonrpc_method == "tools/list":
            payload = _stable_tools_list_payload(rpc.get("id"))
            tools_summary = _tools_response_summary(payload)
            _write_diag_entry(
                path=request.url.path,
                method=request.method,
                jsonrpc_method="tools/list",
                contract_version=HUB_TOOL_CONTRACT_VERSION,
                tools_outcome=tools_summary["outcome"],
                total_tool_count=tools_summary["count"],
                duplicate_tools=tools_summary["duplicates"],
                schema_fingerprint=tools_summary["schema_fingerprint"],
                source="stable_hub_contract",
            )
            return Response(
                content=payload,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )

        if route_device_id:
            views = (
                self._device_registry.views(local_online=self._local_device_online())
                if self._device_registry is not None
                else []
            )
            if self._device_registry is None:
                if route_device_id != self._local_device_id:
                    return JSONResponse(_jsonrpc_error(None, -32602, "指定电脑不存在。"))
            else:
                target_view = next((view for view in views if view.id == route_device_id), None)
                if target_view is None or not target_view.online:
                    return JSONResponse(
                        _jsonrpc_error(None, -32001, "指定电脑当前不可用。"), status_code=502
                    )
            device_id = route_device_id
            if session_id:
                with self._session_lock:
                    _remember_bounded_affinity(self._session_devices, session_id, device_id)
        else:
            device_id = self._effective_device(session_id)

        remote = None
        if self._device_registry is not None and device_id and device_id != self._local_device_id:
            remote = self._device_registry.resolve_remote(device_id)
            if remote is None:
                return JSONResponse(
                    _jsonrpc_error(
                        None, -32001, "目标电脑当前离线。请用 devbridge_list_devices 查看在线设备。"
                    ),
                    status_code=502,
                )
            upstream_target = remote.base_url
            proxy_token = remote.bearer
        else:
            inferred_workspace = ""
            if route_workspace_id:
                if self._workspace_registry and not self._workspace_registry(route_workspace_id):
                    return JSONResponse(
                        _jsonrpc_error(None, -32000, "指定工作区尚未启动或不存在。"),
                        status_code=502,
                    )
                workspace_id = route_workspace_id
            elif rpc is not None and jsonrpc_method == "tools/call":
                with self._session_lock:
                    preferred_workspace = (
                        self._session_workspaces.get(session_id, "") if session_id else ""
                    )
                try:
                    inferred_workspace = self._infer_workspace_for_call(
                        tool_name, call_arguments, preferred_workspace=preferred_workspace
                    )
                except ValueError as exc:
                    return JSONResponse(_jsonrpc_error(rpc.get("id"), -32602, str(exc)))
                if inferred_workspace:
                    workspace_id = inferred_workspace
                elif authenticated_workspace:
                    workspace_id = authenticated_workspace

            if (
                rpc is not None
                and jsonrpc_method == "tools/call"
                and tool_name not in _WORKSPACE_CONTEXT_FREE_TOOLS
                and not workspace_id
                and len(self._running_workspace_ids()) > 1
            ):
                return JSONResponse(
                    _jsonrpc_error(
                        rpc.get("id"),
                        -32006,
                        "当前有多个运行中的工作区，但本次调用缺少可验证的 "
                        "path/cwd/task/workspace 路由；已拒绝静默使用 bootstrap 根目录。"
                        "请传入绝对路径、有效 workspace_id/task_id，或 "
                        "devbridge_workspace_id。",
                    )
                )

            workspace_id = self._effective_workspace(
                workspace_id,
                session_id,
                pinned=bool(route_workspace_id or inferred_workspace),
            )
            if workspace_id:
                upstream_target = self._resolve_upstream(workspace_id)
                if not upstream_target:
                    return JSONResponse(
                        _jsonrpc_error(
                            None,
                            -32000,
                            "目标工作区尚未运行。请先在 MCP DevBridge 桌面启动该根目录。",
                        ),
                        status_code=502,
                    )
                proxy_token = await self._credential_for_workspace(
                    workspace_id, engine_credential or proxy_token
                )

        if rpc is not None and jsonrpc_method == "tools/call":
            if remote is None:
                policy_error = self._workspace_tool_policy_error(
                    tool_name, call_arguments, workspace_id
                )
                if policy_error is not None:
                    error_type, error_code, message = policy_error
                    effective_tool, _ = self._unwrap_codexpro_call(
                        tool_name, call_arguments
                    )
                    self._audit_gateway_tool(
                        request,
                        rpc,
                        tool_name,
                        workspace_id,
                        device_id,
                        False,
                        duration_ms=0,
                        error_type=error_type,
                    )
                    _write_diag_entry(
                        path=request.url.path,
                        method=request.method,
                        jsonrpc_method=jsonrpc_method,
                        event="tool_policy_blocked",
                        tool_name=effective_tool,
                        policy=error_type,
                        workspace_hash=_diag_short_hash(workspace_id),
                    )
                    return JSONResponse(
                        _jsonrpc_error(rpc.get("id"), error_code, message)
                    )
            params = rpc.get("params") or {}
            if tool_name in _DEVICE_TOOL_NAMES:
                result = await self._exec_local_tool(
                    tool_name, rpc, params, workspace_id, session_id
                )
                self._audit_gateway_tool(request, rpc, tool_name, workspace_id, device_id, True)
                return result
            if remote is None and tool_name in _LOCAL_TOOL_NAMES:
                result = await self._exec_local_tool(
                    tool_name, rpc, params, workspace_id, session_id
                )
                self._audit_gateway_tool(request, rpc, tool_name, workspace_id, device_id, True)
                return result

        if remote is None and rpc is not None and jsonrpc_method == "tools/call" and workspace_id:
            _effective_tool, effective_arguments = self._unwrap_codexpro_call(
                tool_name, call_arguments
            )
            workspace_handle = str(effective_arguments.get("workspace_id") or "").strip()
            if (
                workspace_handle.startswith("ws_")
                and not self._workspace_handle_targets_other_root(
                    tool_name, call_arguments, workspace_id
                )
            ):
                rehydrate_error = await self._ensure_workspace_handle_hydrated(
                    workspace_handle,
                    workspace_id,
                    upstream_target or self.upstream_url,
                    proxy_token,
                )
                if rehydrate_error:
                    return JSONResponse(
                        _jsonrpc_error(rpc.get("id"), -32002, rehydrate_error)
                    )

        drop_workspace_handle = bool(
            remote is None
            and rpc is not None
            and jsonrpc_method == "tools/call"
            and workspace_id
            and self._workspace_handle_targets_other_root(tool_name, call_arguments, workspace_id)
        )
        body = _strip_route_arguments(
            body,
            rpc,
            keep_workspace=remote is not None,
            drop_workspace_handle=drop_workspace_handle,
            tool_name=tool_name,
        )
        return await self._proxy(
            request,
            body,
            proxy_token,
            upstream_target=upstream_target,
            jsonrpc_method=jsonrpc_method,
            tool_name=tool_name,
            workspace_id=workspace_id,
            session_id=session_id,
            device_id=device_id,
            rpc=rpc,
        )

    async def _proxy(
        self,
        request: Request,
        body: bytes,
        authorization: str | None,
        *,
        upstream_target: str | None = None,
        jsonrpc_method: str = "",
        tool_name: str = "",
        workspace_id: str = "",
        session_id: str = "",
        device_id: str = "",
        rpc: dict[str, Any] | None = None,
    ) -> Response:
        base = upstream_target or self.upstream_url
        target = f"{base}{request.url.path}"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        headers = {
            key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS
        }
        headers["accept-encoding"] = "identity"
        affinity_parts = [
            request.headers.get("x-mcp-devbridge-client-affinity", "").strip(),
            session_id,
            request.headers.get("authorization", "").strip(),
        ]
        affinity_source = "\0".join(part for part in affinity_parts if part)
        if affinity_source:
            headers["x-mcp-devbridge-client-affinity"] = hashlib.sha256(
                affinity_source.encode("utf-8", errors="ignore")
            ).hexdigest()
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        headers.pop("mcp-session-id", None)
        started = time.monotonic()
        affinity_tool = tool_name
        affinity_arguments: dict[str, Any] = {}
        workspace_handle = ""
        if jsonrpc_method == "tools/call" and rpc is not None:
            affinity_tool, affinity_arguments = self._unwrap_codexpro_call(
                tool_name, _tool_arguments(rpc.get("params") or {})
            )
            workspace_handle = str(affinity_arguments.get("workspace_id") or "").strip()
        deadline_seconds = _upstream_deadline_seconds(
            request.method, jsonrpc_method, affinity_tool, affinity_arguments
        )
        deadline_at = (
            asyncio.get_running_loop().time() + deadline_seconds
            if deadline_seconds is not None
            else None
        )
        audited = False

        def audit_terminal(success: bool, error_type: str = "") -> None:
            nonlocal audited
            if audited or not tool_name:
                return
            audited = True
            with suppress(Exception):
                self._audit_gateway_tool(
                    request, rpc, tool_name, workspace_id, device_id, success,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type=error_type,
                )

        def deadline_response(stage: str) -> JSONResponse:
            duration_ms = int((time.monotonic() - started) * 1000)
            trace_id = str(request.scope.get("devbridge_trace_id") or "")
            if self._flight_recorder is not None and trace_id:
                self._flight_recorder.stage(
                    trace_id,
                    "upstream_deadline",
                    deadline_stage=stage,
                    deadline_seconds=deadline_seconds,
                    elapsed_ms=duration_ms,
                )
            _write_diag_entry(
                path=request.url.path,
                method=request.method,
                jsonrpc_method=jsonrpc_method,
                tool_name=affinity_tool,
                event="upstream_deadline_exceeded",
                stage=stage,
                deadline_seconds=deadline_seconds,
                duration_ms=duration_ms,
                upstream_target=target,
                workspace_hash=_diag_short_hash(workspace_id),
            )
            audit_terminal(False, "deadline_exceeded")
            return JSONResponse(
                _jsonrpc_error(
                    rpc.get("id") if rpc else None,
                    -32008,
                    f"upstream call exceeded the bounded {deadline_seconds:g}s deadline; "
                    "the request was cancelled and its connection released. "
                    "Use bash plus wait_task/get_task for long-running work.",
                ),
                status_code=504,
            )

        def too_large_response(stage: str) -> JSONResponse:
            duration_ms = int((time.monotonic() - started) * 1000)
            _write_diag_entry(
                path=request.url.path, method=request.method,
                jsonrpc_method=jsonrpc_method, tool_name=affinity_tool,
                event="upstream_response_too_large", stage=stage,
                max_bytes=_UPSTREAM_BUFFER_MAX_BYTES, duration_ms=duration_ms,
                upstream_target=target, workspace_hash=_diag_short_hash(workspace_id),
            )
            audit_terminal(False, "response_too_large")
            return JSONResponse(
                _jsonrpc_error(
                    rpc.get("id") if rpc else None, -32009,
                    f"upstream response exceeded the bounded "
                    f"{_UPSTREAM_BUFFER_MAX_BYTES} byte limit; narrow the request.",
                ), status_code=502,
            )

        async def send_once() -> httpx.Response:
            pending = self._http.send(
                self._http.build_request(request.method, target, content=body, headers=headers),
                stream=True,
            )
            if deadline_at is None:
                return await pending
            async with asyncio.timeout_at(deadline_at):
                return await pending

        trace_id = str(request.scope.get("devbridge_trace_id") or "")
        if self._flight_recorder is not None and trace_id:
            self._flight_recorder.stage(
                trace_id,
                "upstream_request",
                jsonrpc_method=jsonrpc_method,
                tool_name=affinity_tool,
                project_id=workspace_id,
                deadline_seconds=deadline_seconds,
            )
        try:
            upstream = await send_once()
            if self._flight_recorder is not None and trace_id:
                self._flight_recorder.stage(
                    trace_id,
                    "upstream_headers",
                    upstream_status=upstream.status_code,
                    content_type=upstream.headers.get("content-type", "")[:120],
                )
        except (TimeoutError, httpx.TimeoutException):
            return deadline_response("response_headers")
        except httpx.HTTPError as first_error:
            safe_retry = request.method == "POST" and jsonrpc_method in {"initialize", "ping"}
            if safe_retry:
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method=jsonrpc_method,
                    event="safe_upstream_retry",
                    error_type=type(first_error).__name__,
                    upstream_target=target,
                )
                try:
                    upstream = await send_once()
                except (TimeoutError, httpx.TimeoutException):
                    return deadline_response("safe_retry_headers")
                except httpx.HTTPError:
                    return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
            else:
                audit_terminal(False, "upstream_unreachable")
                return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
        buffered_payload: bytes | None = None
        content_type = upstream.headers.get("content-type", "").lower()
        if request.method == "POST" and "application/json" in content_type:
            try:
                buffered_payload = await _read_and_close_upstream(
                    upstream, deadline_at=deadline_at, max_bytes=_UPSTREAM_BUFFER_MAX_BYTES
                )
            except (TimeoutError, httpx.TimeoutException):
                return deadline_response("response_body")
            except UpstreamResponseTooLarge:
                return too_large_response("response_body")
        if workspace_handle.startswith("ws_") and upstream.status_code < 400:
            try:
                content_length = int(upstream.headers.get("content-length") or "-1")
            except ValueError:
                content_length = -1
            if buffered_payload is not None or (
                0 <= content_length <= _WORKSPACE_ERROR_INSPECT_MAX_BYTES
                and ("json" in content_type or "text/event-stream" in content_type)
            ):
                if buffered_payload is None:
                    try:
                        buffered_payload = await _read_and_close_upstream(
                            upstream, deadline_at=deadline_at,
                            max_bytes=_WORKSPACE_ERROR_INSPECT_MAX_BYTES,
                        )
                    except (TimeoutError, httpx.TimeoutException):
                        return deadline_response("workspace_error_inspection")
                    except UpstreamResponseTooLarge:
                        return too_large_response("workspace_error_inspection")
                workspace_error = self._extract_structured_field(buffered_payload, "error")
                unknown_marker = f"Unknown workspace_id: {workspace_handle}"
                if unknown_marker in workspace_error:
                    with self._session_lock:
                        self._workspace_hydrated_handles.discard(workspace_handle)
                    rehydrate_error = await self._ensure_workspace_handle_hydrated(
                        workspace_handle,
                        workspace_id,
                        base,
                        authorization,
                        force=True,
                    )
                    if rehydrate_error:
                        return JSONResponse(
                            _jsonrpc_error(rpc.get("id") if rpc else None, -32002, rehydrate_error)
                        )
                    if affinity_tool not in _STABLE_HUB_READ_ONLY_TOOLS:
                        return JSONResponse(
                            _jsonrpc_error(
                                rpc.get("id") if rpc else None,
                                -32003,
                                "workspace 上下文已恢复；为避免副作用重复执行，原调用未自动重放，请确认后重试一次。",
                            )
                        )
                    _write_diag_entry(
                        path=request.url.path,
                        method=request.method,
                        jsonrpc_method=jsonrpc_method,
                        event="safe_workspace_rehydrate_retry",
                        tool_name=affinity_tool,
                        workspace_hash=_diag_short_hash(workspace_id),
                        handle_hash=_diag_short_hash(workspace_handle),
                    )
                    try:
                        upstream = await send_once()
                    except (TimeoutError, httpx.TimeoutException):
                        return deadline_response("workspace_retry_headers")
                    except httpx.HTTPError:
                        return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
                    buffered_payload = None
        if buffered_payload is not None:
            audit_terminal(
                upstream.status_code < 400,
                "" if upstream.status_code < 400 else f"http_{upstream.status_code}",
            )
        filtered = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _HOP_HEADERS and key.lower() != "mcp-session-id"
        }
        if request.method == "POST":
            affinity_tool = tool_name
            if jsonrpc_method == "tools/call" and rpc is not None:
                affinity_tool, affinity_arguments = self._unwrap_codexpro_call(
                    tool_name, _tool_arguments(rpc.get("params") or {})
                )
            if (
                jsonrpc_method == "tools/call"
                and affinity_tool in {"open_workspace", "open_current_workspace"}
                and workspace_id
            ):
                if buffered_payload is not None:
                    payload = buffered_payload
                else:
                    try:
                        payload = await _read_and_close_upstream(
                            upstream,
                            deadline_at=deadline_at,
                            max_bytes=_UPSTREAM_BUFFER_MAX_BYTES,
                        )
                    except (TimeoutError, httpx.TimeoutException):
                        return deadline_response("open_workspace_body")
                    except UpstreamResponseTooLarge:
                        return too_large_response("open_workspace_body")
                    audit_terminal(
                        upstream.status_code < 400,
                        "" if upstream.status_code < 400 else f"http_{upstream.status_code}",
                    )
                handle = self._extract_structured_field(payload, "workspace_id")
                if handle:
                    root_hint = str(
                        affinity_arguments.get("root")
                        or affinity_arguments.get("path")
                        or ""
                    )
                    self._remember_persistent_workspace_handle(handle, workspace_id, root_hint)
                    with self._session_lock:
                        if session_id:
                            _remember_bounded_affinity(
                                self._session_workspaces, session_id, workspace_id
                            )
                    payload = self._inject_structured_field(
                        payload, _ROUTE_WORKSPACE_ARG, workspace_id
                    )
                return Response(content=payload, status_code=upstream.status_code, headers=filtered)
            if jsonrpc_method == "tools/call" and affinity_tool == "bash" and workspace_id:
                if buffered_payload is not None:
                    payload = buffered_payload
                else:
                    try:
                        payload = await _read_and_close_upstream(
                            upstream,
                            deadline_at=deadline_at,
                            max_bytes=_UPSTREAM_BUFFER_MAX_BYTES,
                        )
                    except (TimeoutError, httpx.TimeoutException):
                        return deadline_response("bash_body")
                    except UpstreamResponseTooLarge:
                        return too_large_response("bash_body")
                    audit_terminal(
                        upstream.status_code < 400,
                        "" if upstream.status_code < 400 else f"http_{upstream.status_code}",
                    )
                task_id = self._extract_task_id(payload)
                if task_id:
                    with self._session_lock:
                        _remember_bounded_affinity(self._task_workspaces, task_id, workspace_id)
                return Response(content=payload, status_code=upstream.status_code, headers=filtered)
            if b'"tools/list"' in body:
                payload = (
                    buffered_payload
                    if buffered_payload is not None
                    else await _read_and_close_upstream(upstream, deadline_at=deadline_at)
                )
                rewritten = _inject_tools(payload)
                tools_summary = _tools_response_summary(rewritten)
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method="tools/list",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                    injected_tool_count=len(_PYTHON_TOOL_DEFS),
                    tools_outcome=tools_summary["outcome"],
                    total_tool_count=tools_summary["count"],
                    duplicate_tools=tools_summary["duplicates"],
                    schema_fingerprint=tools_summary["schema_fingerprint"],
                    jsonrpc_error_code=tools_summary["error_code"],
                    workspace_hash=_diag_short_hash(workspace_id),
                )
                return Response(
                    content=rewritten, status_code=upstream.status_code, headers=filtered
                )
            if b"initialize" in body:
                payload = (
                    buffered_payload
                    if buffered_payload is not None
                    else await _read_and_close_upstream(upstream, deadline_at=deadline_at)
                )
                rewritten = _rewrite_server_identity(payload)
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method="initialize",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                )
                return Response(
                    content=rewritten, status_code=upstream.status_code, headers=filtered
                )
        if buffered_payload is not None:
            return Response(
                content=buffered_payload,
                status_code=upstream.status_code,
                headers=filtered,
            )
        timeout_event = (
            _sse_deadline_event(rpc.get("id") if rpc else None, deadline_seconds)
            if deadline_at is not None and deadline_seconds is not None
            else b""
        )

        def stream_terminal(reason: str) -> None:
            if self._flight_recorder is not None and trace_id:
                self._flight_recorder.stage(
                    trace_id,
                    "upstream_stream_terminal",
                    terminal_reason=reason,
                    upstream_status=upstream.status_code,
                )
            success = reason == "completed" and upstream.status_code < 400
            error_type = (
                ""
                if success
                else (f"http_{upstream.status_code}" if reason == "completed" else reason)
            )
            if not success:
                _write_diag_entry(
                    path=request.url.path, method=request.method,
                    jsonrpc_method=jsonrpc_method, tool_name=affinity_tool,
                    event="upstream_stream_terminal_failure",
                    terminal_reason=reason, upstream_status=upstream.status_code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    workspace_hash=_diag_short_hash(workspace_id),
                )
            audit_terminal(success, error_type)

        stream_body = (
            _stream_sse_with_keepalive_and_close_upstream(
                upstream, deadline_at=deadline_at, timeout_event=timeout_event,
                on_terminal=stream_terminal,
            )
            if _upstream_is_sse(upstream)
            else _stream_and_close_upstream(
                upstream, deadline_at=deadline_at, on_terminal=stream_terminal
            )
        )
        if self._flight_recorder is not None and trace_id:
            self._flight_recorder.stage(
                trace_id,
                "response_streaming",
                upstream_status=upstream.status_code,
                stream_kind="sse" if _upstream_is_sse(upstream) else "bytes",
            )
        return StreamingResponse(
            stream_body,
            status_code=upstream.status_code,
            headers=filtered,
        )

    # ---------------------------------------------------- local tools
    async def _exec_local_tool(
        self,
        name: str,
        rpc: dict[str, Any],
        params: dict[str, Any],
        workspace_id: str = "",
        session_id: str = "",
    ) -> JSONResponse:
        rpc_id = rpc.get("id")
        arguments = _tool_arguments(params)
        workspace = self._resolve_workspace_path(workspace_id) or self._workspace or Path.cwd()
        system_access = self._workspace_permission_mode(workspace_id) == "system"
        try:
            if name == "run_command":
                command = str(arguments.get("command", ""))
                if not command.strip():
                    raise ValueError("command 不能为空。")
                cwd_rel = str(arguments.get("cwd", "")).strip()
                cwd = self._local_tool_cwd(workspace, cwd_rel, system_access=system_access)
                from .execution_profile import check_execution

                allowed, reason = check_execution(
                    command, "full_system" if system_access else "safe"
                )
                if not allowed:
                    raise ValueError(reason)
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
                if system_access and os.name == "nt":
                    from .elevation import get_elevation_controller

                    elevation = get_elevation_controller()
                    if not elevation.is_registered():
                        raise ValueError(
                            "Windows full-system administrator capability is not authorized; complete the one-time UAC broker registration first."
                        )
                    res = elevation.execute_command(command, cwd, timeout)
                else:
                    res = run_command(command, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"shell: {res.shell}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]})
                )
            elif name == "run_program":
                executable = str(arguments.get("executable", ""))
                if not executable.strip():
                    raise ValueError("executable 不能为空。")
                args = [str(a) for a in (arguments.get("args") or [])]
                cwd_rel = str(arguments.get("cwd", "")).strip()
                cwd = self._local_tool_cwd(workspace, cwd_rel, system_access=system_access)
                from .execution_profile import check_execution

                command_line = " ".join([executable, *args])
                allowed, reason = check_execution(
                    command_line, "full_system" if system_access else "safe"
                )
                if not allowed:
                    raise ValueError(reason)
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
                if system_access and os.name == "nt":
                    from .elevation import get_elevation_controller

                    elevation = get_elevation_controller()
                    if not elevation.is_registered():
                        raise ValueError(
                            "Windows full-system administrator capability is not authorized; complete the one-time UAC broker registration first."
                        )
                    res = elevation.execute_program(executable, args, cwd, timeout)
                else:
                    res = run_program(executable, args, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"command: {res.command}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]})
                )
            elif name == "shell_self_test":
                lines: list[str] = []
                info = get_shell_info()
                default = info.get("default") or {}
                if isinstance(default, dict):
                    if default.get("executable"):
                        lines.append(
                            f"[✓] shell: {default.get('name', '?')} ({default.get('path', '?')})"
                        )
                    else:
                        lines.append(f"[✗] shell: 不可执行 ({default.get('path', '?')})")
                bin_versions = detect_binaries()
                for tool in ("python", "git", "node", "npm"):
                    ver = bin_versions.get(tool, "")
                    symbol = "[✓]" if ver else "[✗]"
                    lines.append(f"{symbol} {tool}: {ver or '未安装'}")
                for tool in ("pytest", "pyright"):
                    import subprocess as _sp

                    try:
                        r = _sp.run(
                            ["python", "-m", tool, "--version"],
                            capture_output=True,
                            timeout=30,
                            **run_platform_kwargs(),
                        )
                        out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
                        lines.append(f"[✓] {tool}: {out[0] if out else 'ok'} (python -m {tool})")
                    except Exception:
                        lines.append(f"[✗] {tool}: 未安装或不可调用")
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id, {"content": [{"type": "text", "text": "\n".join(lines)}]}
                    )
                )
            elif name == "devbridge_list_workspaces":
                result = self._list_workspaces()
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]})
                )
            elif name == "devbridge_get_current_workspace":
                explicit_route = str(arguments.get(_ROUTE_WORKSPACE_ARG) or "").strip()
                current_workspace_id = self._current_workspace_context(explicit_route, session_id)
                result = self._get_current_workspace(current_workspace_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_WORKSPACE_ARG: current_workspace_id},
                        },
                    )
                )
            elif name == "devbridge_switch_workspace":
                target_id = str(arguments.get("project_id", ""))
                if not target_id:
                    raise ValueError("project_id 不能为空。")
                result = self._do_switch_workspace(target_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_WORKSPACE_ARG: target_id},
                        },
                    )
                )
            elif name == "devbridge_list_devices":
                result = self._list_devices(session_id)
                return JSONResponse(
                    _jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]})
                )
            elif name == "devbridge_get_current_device":
                result = self._get_current_device(session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {
                                _ROUTE_DEVICE_ARG: self._effective_device(session_id)
                            },
                        },
                    )
                )
            elif name == "devbridge_switch_device":
                target_id = str(arguments.get("device_id", ""))
                if not target_id:
                    raise ValueError("device_id 不能为空。")
                result = self._do_switch_device(target_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_DEVICE_ARG: target_id},
                        },
                    )
                )
            else:
                raise ValueError(f"未知的本地工具: {name}")
        except ValueError as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32602, str(exc)))
        except Exception as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32603, f"工具执行失败: {exc}"))

    # ----------------------------------------------------------- device helpers
    def _local_device_online(self) -> bool:
        if self._workspace_registry is None:
            return True
        try:
            from .config_store import load_projects

            return any(p.id and self._workspace_registry(p.id) for p in load_projects())
        except Exception:
            return True

    def _effective_device(self, session_id: str) -> str:
        if self._device_registry is None:
            return self._local_device_id
        online = self._device_registry.online_ids(local_online=self._local_device_online())
        with self._session_lock:
            selected = self._session_devices.get(session_id, "") if session_id else ""
            if selected in online:
                return selected
            if len(online) == 1:
                if session_id:
                    _remember_bounded_affinity(self._session_devices, session_id, online[0])
                return online[0]
        if self._local_device_id in online:
            return self._local_device_id
        return online[0] if online else ""

    def _list_devices(self, session_id: str) -> str:
        if self._device_registry is None:
            return "当前仅启用本机设备。"
        current = self._effective_device(session_id)
        views = self._device_registry.views(local_online=self._local_device_online())
        rows = ["可用电脑："]
        for view in views:
            state = "在线" if view.online else "离线"
            suffix = "（本机）" if view.local else ""
            selected = " ← 当前" if view.id == current else ""
            rows.append(f"- {view.name}{suffix} · {state} · id={view.id}{selected}")
        online_count = sum(1 for view in views if view.online)
        if online_count == 1:
            rows.append("\n目前只有一台电脑在线，系统已自动使用它。")
        elif online_count > 1:
            rows.append("\n多台电脑在线时，可用 devbridge_switch_device 切换；只影响当前聊天会话。")
        return "\n".join(rows)

    def _get_current_device(self, session_id: str) -> str:
        current = self._effective_device(session_id)
        if self._device_registry is None:
            return "当前电脑：本机"
        view = next(
            (
                item
                for item in self._device_registry.views(local_online=self._local_device_online())
                if item.id == current
            ),
            None,
        )
        if view is None:
            return "当前没有可用电脑。"
        return f"当前电脑：{view.name}{'（本机）' if view.local else ''}\nid={view.id}"

    def _do_switch_device(self, device_id: str, session_id: str) -> str:
        if self._device_registry is None:
            raise ValueError("当前没有启用 Multi-Device Hub。")
        views = self._device_registry.views(local_online=self._local_device_online())
        target = next((view for view in views if view.id == device_id), None)
        if target is None:
            raise ValueError(f"找不到电脑：{device_id}。请先用 devbridge_list_devices 查看。")
        if not target.online:
            raise ValueError(f"电脑“{target.name}”当前离线。")
        if session_id:
            with self._session_lock:
                _remember_bounded_affinity(self._session_devices, session_id, device_id)
                self._session_workspaces.pop(session_id, None)
        return (
            f"已切换到电脑：{target.name}\n"
            f"后续工具调用请携带 {_ROUTE_DEVICE_ARG}={device_id}；这不依赖底层 MCP transport session。\n"
            "该电脑内部会按 path/cwd/task 自动路由；显式工作区切换只作为兼容覆盖。"
        )

    def _audit_gateway_tool(
        self,
        request: Request,
        rpc: dict[str, Any] | None,
        tool_name: str,
        workspace_id: str,
        device_id: str,
        success: bool,
        *,
        duration_ms: int = 0,
        error_type: str = "",
    ) -> None:
        params = (rpc or {}).get("params") or {}
        arguments = params.get("arguments") if isinstance(params, dict) else {}
        if not isinstance(arguments, dict):
            arguments = {}
        workspace = str(self._resolve_workspace_path(workspace_id) or "")
        if device_id and device_id != self._local_device_id:
            workspace = f"remote:{device_id}"
        ua = request.headers.get("user-agent", "")
        self._audit.log_tool_call(
            request_id=str((rpc or {}).get("id", "")) or None,
            client_name=ua[:120] or None,
            tool_name=tool_name,
            parameters=arguments,
            workspace=workspace,
            permission_mode="gateway",
            duration_ms=max(0, duration_ms),
            success=success,
            error_type=error_type or None,
            extra={"device_id": device_id},
        )

    # -------------------------------------------------------- workspace helpers
    async def _credential_for_workspace(self, workspace_id: str, fallback: str | None) -> str | None:
        if workspace_id and self._workspace_credential_registry is not None:
            value = await asyncio.to_thread(self._workspace_credential_registry, workspace_id)
            if value:
                return value
        return fallback

    def _workspace_for_credential(self, value: str) -> str:
        if not value or self._workspace_credential_registry is None:
            return ""
        try:
            from .config_store import load_projects

            for project in load_projects():
                if not project.id:
                    continue
                candidate = self._workspace_credential_registry(project.id)
                if candidate and _constant_time_eq(value, candidate):
                    return project.id
        except Exception:
            return ""
        return ""

    def _resolve_upstream(self, workspace_id: str) -> str | None:
        """Return the upstream target URL for a workspace, or None if not running."""
        if not workspace_id:
            return self.upstream_url
        if self._workspace_registry is None:
            return self.upstream_url
        info = self._workspace_registry(workspace_id)
        if info is None:
            return None
        port, _root = info
        return f"http://127.0.0.1:{port}"

    def _resolve_workspace_path(self, workspace_id: str) -> Path | None:
        """Return the root Path for a workspace, or None."""
        if not workspace_id or self._workspace_registry is None:
            return None
        info = self._workspace_registry(workspace_id)
        if info is None:
            return None
        _port, root = info
        return Path(root) if root else None

    @staticmethod
    def _path_within(child: Path, parent: Path) -> bool:
        try:
            # realpath resolves junctions/symlinks in existing parents while still
            # supporting a not-yet-created final path. This keeps textual children
            # from escaping a configured root through ``..`` or a link/junction.
            child_text = os.path.normcase(
                os.path.realpath(os.path.abspath(str(child.expanduser())))
            )
            parent_text = os.path.normcase(
                os.path.realpath(os.path.abspath(str(parent.expanduser())))
            )
            return os.path.commonpath([child_text, parent_text]) == parent_text
        except (OSError, ValueError):
            return False

    def _local_tool_cwd(
        self, workspace: Path, raw_cwd: str, *, system_access: bool = False
    ) -> Path:
        root = workspace.expanduser().resolve()
        value = (raw_cwd or "").strip()
        candidate = Path(value).expanduser() if value else root
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not system_access and not self._path_within(candidate, root):
            raise ValueError("cwd 超出目标工作区根目录。请使用该运行中根目录内的路径。")
        return candidate

    def _running_workspace_records(self) -> list[tuple[str, Path, str]]:
        if self._workspace_registry is None:
            return []
        now = time.monotonic()
        with self._workspace_snapshot_lock:
            if now < self._workspace_snapshot_expires:
                return list(self._workspace_snapshot)
        rows: list[tuple[str, Path, str]] = []
        try:
            from .config_store import load_projects

            for project in load_projects():
                if not project.id:
                    continue
                info = self._workspace_registry(project.id)
                if info is None:
                    continue
                _port, root = info
                if root:
                    rows.append((project.id, Path(root).expanduser(), project.permission_mode))
        except Exception:
            rows = []
        rows.sort(
            key=lambda item: (
                -len(os.path.abspath(str(item[1]))),
                os.path.normcase(os.path.abspath(str(item[1]))),
            )
        )
        with self._workspace_snapshot_lock:
            self._workspace_snapshot = list(rows)
            self._workspace_snapshot_expires = now + 0.35
        return rows

    def _running_workspace_roots(self) -> list[tuple[str, Path]]:
        return [(project_id, root) for project_id, root, _mode in self._running_workspace_records()]

    def _workspace_permission_mode(self, workspace_id: str) -> str:
        for project_id, _root, mode in self._running_workspace_records():
            if project_id == workspace_id:
                return mode
        return ""

    def _workspace_tool_policy_error(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        workspace_id: str,
    ) -> tuple[str, int, str] | None:
        """Enforce the selected local project's fixed-contract call policy.

        The public Hub schema is deliberately stable across project modes. A
        local call is therefore checked here before either a Gateway-local tool
        or CodexPro sees it. Remote devices are checked by their own Gateway and
        must not inherit the local project's permission mode.
        """

        if tool_name in _DEVICE_TOOL_NAMES:
            return None
        # Legacy/single-upstream deployments have no DevBridge project registry
        # to supply a project permission mode. Preserve their pass-through
        # behavior and rely on CodexPro's own enforcement layer.
        if self._workspace_registry is None:
            return None

        effective_tool, _effective_arguments = self._unwrap_codexpro_call(
            tool_name, arguments
        )
        mode = self._workspace_permission_mode(workspace_id)
        if not mode:
            return (
                "permission_context_unavailable",
                -32004,
                "无法确认目标工作区的权限模式，已拒绝调用；请先启动并明确选择该工作区。",
            )
        if mode == "system":
            return None
        if mode == "workspace":
            if effective_tool in _FULL_MODE_ONLY_TOOLS:
                return (
                    "feature_unavailable",
                    -32005,
                    f"工具 {effective_tool!r} 仅在完全访问模式下可用；当前项目为工作区权限。",
                )
            return None
        if mode == "read_only":
            if effective_tool in _READ_ONLY_DISABLED_TOOLS:
                return (
                    "permission_denied",
                    -32004,
                    f"permission denied：当前项目为只读模式，工具 {effective_tool!r} 会写入内容、执行命令或改变任务状态。",
                )
            if effective_tool in _READ_ONLY_FEATURE_UNAVAILABLE_TOOLS:
                return (
                    "feature_unavailable",
                    -32005,
                    f"工具 {effective_tool!r} 依赖当前只读项目未启用的后台任务能力。",
                )
            if (
                effective_tool in _CODEXPRO_MINIMAL_TOOLS
                or effective_tool in _READ_ONLY_EXTRA_SAFE_TOOLS
                or effective_tool == "list_actions"
            ):
                return None
            required_mode = (
                "完全访问" if effective_tool in _FULL_MODE_ONLY_TOOLS else "项目工作区"
            )
            return (
                "feature_unavailable",
                -32005,
                f"工具 {effective_tool!r} 未在只读项目中启用；请将项目切换到{required_mode}模式后再调用。",
            )
        return (
            "permission_context_unavailable",
            -32004,
            f"不支持的项目权限模式 {mode!r}，已拒绝调用。",
        )

    def _stable_system_workspace_id(self) -> str:
        candidates = [
            project_id
            for project_id, _root, mode in self._running_workspace_records()
            if mode == "system"
        ]
        if not candidates:
            return ""
        return self._stable_workspace_id(candidates)

    def _workspace_for_path(self, raw_path: str, *, allow_new: bool = False) -> str:
        value = (raw_path or "").strip().strip('"')
        if not value or value == ".":
            return ""
        roots = self._running_workspace_roots()
        if not roots:
            return ""
        path_value = Path(value).expanduser()
        if path_value.is_absolute():
            for project_id, root in roots:
                if self._path_within(path_value, root):
                    return project_id
            return ""

        ranked: list[tuple[int, str]] = []
        for project_id, root in roots:
            candidate = root / path_value
            if not self._path_within(candidate, root):
                continue
            if candidate.exists():
                # An existing relative path is strong evidence, but if the same
                # relative path exists below multiple active roots it is still
                # ambiguous. Root-string length is not a semantic tiebreaker.
                ranked.append((10_000, project_id))
                continue
            if not allow_new:
                continue
            parent = candidate.parent
            while self._path_within(parent, root) and parent != root and not parent.exists():
                parent = parent.parent
            if self._path_within(parent, root) and parent.exists():
                try:
                    existing_depth = len(parent.relative_to(root).parts)
                except ValueError:
                    existing_depth = 0
                ranked.append((existing_depth, project_id))
        if not ranked:
            return ""
        best_depth = max(item[0] for item in ranked)
        best = [item for item in ranked if item[0] == best_depth]
        if len({item[1] for item in best}) > 1:
            raise ValueError(
                f"相对路径 {value!r} 同时匹配多个运行中的工作区根目录；"
                "请改用绝对路径或显式 workspace_id，避免读写到错误项目。"
            )
        return best[0][1]

    @staticmethod
    def _command_candidate_paths(command: str) -> list[str]:
        """Extract explicit absolute paths from common shell argv forms.

        This is routing-only, not command validation. It intentionally ignores
        relative tokens and ambiguous shell expressions.
        """
        if not command.strip():
            return []
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = command.split()
        values: list[str] = []
        for raw in tokens:
            variants = [raw]
            if "=" in raw:
                variants.append(raw.split("=", 1)[1])
            for variant in variants:
                value = variant.strip().strip("\"'").rstrip(",;")
                if not value or "$" in value or "`" in value:
                    continue
                candidate = Path(value).expanduser()
                if candidate.is_absolute() and value not in values:
                    values.append(value)
        return values

    @staticmethod
    def _patch_candidate_paths(patch: str) -> list[str]:
        values: list[str] = []
        for line in (patch or "").splitlines():
            match = re.match(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(.+)$", line)
            if not match:
                continue
            value = match.group(1).strip()
            if value and value != "/dev/null" and value not in values:
                values.append(value)
        return values

    @staticmethod
    def _unwrap_codexpro_call(
        tool_name: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Expose a supertool's wrapped action to the DevBridge router.

        CodexPro offers a stable ``codexpro(action, args)`` wrapper for clients
        that cache one schema. Routing only on the outer tool name would lose
        path/task/workspace-handle evidence from ``args`` and can select the
        wrong active root. Keep the wrapper transparent to routing while
        preserving the original request on the wire to the chosen engine.
        """
        if tool_name != "codexpro":
            return tool_name, arguments
        raw_action = str(arguments.get("action") or "list_actions").strip().lower()
        action = re.sub(r"[\s-]+", "_", raw_action)
        action = _CODEXPRO_ACTION_ALIASES.get(action, action)
        nested = arguments.get("args")
        child_arguments = nested if isinstance(nested, dict) else {}
        if action == "codexpro":
            return tool_name, arguments
        return action, child_arguments

    def _configured_project_root(self, project_id: str) -> str:
        project_id = str(project_id or "").strip()
        if not project_id:
            return ""
        if self._workspace_project_registry is not None:
            try:
                return str(self._workspace_project_registry(project_id) or "").strip()
            except Exception:
                return ""
        try:
            from .config_store import load_projects

            for project in load_projects():
                if str(getattr(project, "id", "") or "").strip() == project_id:
                    return str(getattr(project, "root_path", "") or "").strip()
        except Exception:
            return ""
        return ""

    def _validated_route_root(self, project_id: str, root: str) -> str:
        configured_root = self._configured_project_root(project_id)
        if not configured_root:
            return ""
        try:
            base = Path(configured_root).expanduser().resolve()
            candidate = Path(root or configured_root).expanduser().resolve()
            candidate.relative_to(base)
            if not base.is_dir() or not candidate.is_dir():
                return ""
        except (OSError, ValueError):
            return ""
        return str(candidate)

    def _load_persistent_workspace_routes(self) -> None:
        invalid_handles: set[str] = set()
        for record in load_workspace_routes():
            handle = str(record.get("handle") or "").strip()
            project_id = str(record.get("project_id") or "").strip()
            root = self._validated_route_root(project_id, str(record.get("root") or ""))
            if not handle.startswith("ws_") or not root:
                if handle:
                    invalid_handles.add(handle)
                continue
            self._workspace_handle_roots[handle] = project_id
            self._workspace_handle_paths[handle] = root
            self._workspace_route_records[handle] = {
                "handle": handle,
                "project_id": project_id,
                "root": root,
                "last_used": float(record.get("last_used") or 0.0),
            }
        if invalid_handles:
            save_workspace_routes([], removed_handles=invalid_handles)

    def _forget_workspace_handle(
        self, handle: str, *, expected_project_id: str = ""
    ) -> None:
        handle = str(handle or "").strip()
        expected_project_id = str(expected_project_id or "").strip()
        if not handle:
            return
        with self._session_lock:
            current_project = self._workspace_handle_roots.get(handle, "")
            if (
                expected_project_id
                and current_project
                and current_project != expected_project_id
            ):
                return
            self._workspace_handle_roots.pop(handle, None)
            self._workspace_handle_paths.pop(handle, None)
            self._workspace_route_records.pop(handle, None)
            self._workspace_hydrated_handles.discard(handle)
        if handle.startswith("ws_"):
            save_workspace_routes([], removed_handles={handle})

    def _remember_persistent_workspace_handle(
        self, handle: str, project_id: str, root: str = ""
    ) -> None:
        handle = str(handle or "").strip()
        project_id = str(project_id or "").strip()
        persistent = handle.startswith("ws_")
        legacy = handle.startswith("ws-")
        if not project_id or not (persistent or legacy):
            return

        canonical_root = self._validated_route_root(project_id, root)
        if persistent and not canonical_root:
            # Deterministic handles are restart-safe only when their canonical
            # root remains contained by an active project and exists locally.
            return
        if legacy and (
            not self._workspace_registry or not self._workspace_registry(project_id)
        ):
            return
        now = time.time()
        routes_changed = False
        removed_handles: set[str] = set()
        persisted_records: list[dict[str, Any]] = []
        with self._session_lock:
            self._workspace_handle_roots.pop(handle, None)
            self._workspace_handle_roots[handle] = project_id
            self._workspace_handle_paths.pop(handle, None)
            if canonical_root:
                self._workspace_handle_paths[handle] = canonical_root
            # Pre-deterministic CodexPro handles (for example ``ws-...``) retain
            # bounded in-process project affinity even when their opaque child
            # root cannot be revalidated locally. They are never persisted or
            # replayed across a process restart because reopening may yield a
            # different handle. Deterministic ``ws_`` handles retain the stricter
            # canonical-root requirement above.
            if persistent:
                self._workspace_hydrated_handles.add(handle)
                self._workspace_route_records.pop(handle, None)
                self._workspace_route_records[handle] = {
                    "handle": handle,
                    "project_id": project_id,
                    "root": canonical_root,
                    "last_used": now,
                }
                routes_changed = True
            while len(self._workspace_handle_roots) > _MAX_WORKSPACE_ROUTE_ENTRIES:
                oldest = next(iter(self._workspace_handle_roots), None)
                if oldest is None:
                    break
                self._workspace_handle_roots.pop(oldest, None)
                self._workspace_handle_paths.pop(oldest, None)
                if self._workspace_route_records.pop(oldest, None) is not None:
                    removed_handles.add(oldest)
                    routes_changed = True
                self._workspace_hydrated_handles.discard(oldest)
            while len(self._workspace_route_records) > _MAX_WORKSPACE_ROUTE_ENTRIES:
                oldest = next(iter(self._workspace_route_records), None)
                if oldest is None:
                    break
                self._workspace_route_records.pop(oldest, None)
                self._workspace_handle_roots.pop(oldest, None)
                self._workspace_handle_paths.pop(oldest, None)
                self._workspace_hydrated_handles.discard(oldest)
                removed_handles.add(oldest)
                routes_changed = True
            self._workspace_hydrated_handles.intersection_update(
                self._workspace_route_records
            )
            if routes_changed:
                persisted_records = list(self._workspace_route_records.values())
        if routes_changed:
            save_workspace_routes(
                persisted_records, removed_handles=removed_handles
            )

    async def _ensure_workspace_handle_hydrated(
        self,
        handle: str,
        project_id: str,
        upstream_target: str,
        authorization: str | None,
        *,
        force: bool = False,
    ) -> str:
        """Single-flight wrapper for bounded workspace rehydration."""
        handle = str(handle or "").strip()
        project_id = str(project_id or "").strip()
        if not handle.startswith("ws_") or not project_id:
            return "workspace 上下文无效，无法恢复。"
        key = (handle, project_id)
        with self._session_lock:
            if not force and handle in self._workspace_hydrated_handles:
                return ""
            task = self._workspace_rehydrate_inflight.get(key)
            if task is None:
                if (
                    len(self._workspace_rehydrate_inflight)
                    >= _MAX_WORKSPACE_REHYDRATE_INFLIGHT
                ):
                    return "workspace 自动恢复并发已达上限，请稍后重试。"
                task = asyncio.create_task(
                    self._rehydrate_workspace_handle(
                        handle,
                        project_id,
                        upstream_target,
                        authorization,
                        force=force,
                    ),
                    name=f"workspace-rehydrate-{_diag_short_hash(handle)}",
                )
                self._workspace_rehydrate_inflight[key] = task

                def clear_inflight(
                    done: asyncio.Task[str],
                    route_key: tuple[str, str] = key,
                ) -> None:
                    with suppress(asyncio.CancelledError, Exception):
                        done.exception()
                    with self._session_lock:
                        if self._workspace_rehydrate_inflight.get(route_key) is done:
                            self._workspace_rehydrate_inflight.pop(route_key, None)

                task.add_done_callback(clear_inflight)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return f"workspace 自动恢复失败：{type(exc).__name__}"

    async def _rehydrate_workspace_handle(
        self,
        handle: str,
        project_id: str,
        upstream_target: str,
        authorization: str | None,
        *,
        force: bool = False,
    ) -> str:
        """Re-register a persisted opaque workspace before forwarding a dependent call.

        ``open_workspace`` is deliberately issued *before* the caller's operation, so a
        Gateway/CodexPro restart never requires speculative replay of a write. The
        deterministic handle returned by CodexPro must match the persisted handle;
        otherwise the route fails closed instead of silently selecting another root.
        """
        handle = str(handle or "").strip()
        project_id = str(project_id or "").strip()
        if not handle.startswith("ws_") or not project_id:
            return "workspace 上下文无效，无法恢复。"
        with self._session_lock:
            if not force and handle in self._workspace_hydrated_handles:
                return ""
            record = dict(self._workspace_route_records.get(handle) or {})
        if str(record.get("project_id") or "") != project_id:
            return "workspace 上下文与目标项目不一致，已拒绝自动恢复。"
        root = self._validated_route_root(project_id, str(record.get("root") or ""))
        if not root:
            self._forget_workspace_handle(
                handle, expected_project_id=project_id
            )
            return "workspace 根目录已失效，无法安全恢复；请重新打开该工作区。"
        target = str(upstream_target or "").rstrip("/")
        if not target:
            return "目标项目当前没有可用的 MCP 数据面。"
        headers = {
            "accept": "application/json, text/event-stream",
            "content-type": "application/json",
        }
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        request_body = {
            "jsonrpc": "2.0",
            "id": f"devbridge-rehydrate-{hashlib.sha256(handle.encode()).hexdigest()[:12]}",
            "method": "tools/call",
            "params": {
                "name": "open_workspace",
                "arguments": {
                    "root": root,
                    "include_tree": False,
                    "include_skills": False,
                },
            },
        }
        try:
            async with asyncio.timeout(8.0):
                response = await self._http.post(
                    f"{target}{constants.DEFAULT_MCP_PATH}",
                    headers=headers,
                    json=request_body,
                )
        except (TimeoutError, httpx.HTTPError) as exc:
            return f"workspace 自动恢复失败：{type(exc).__name__}"
        if response.status_code >= 400:
            return f"workspace 自动恢复失败：上游 HTTP {response.status_code}"
        recovered = self._extract_structured_field(response.content, "workspace_id")
        if recovered != handle:
            return "workspace 自动恢复返回了不匹配的上下文标识，已拒绝继续调用。"
        self._remember_persistent_workspace_handle(handle, project_id, root)
        _write_diag_entry(
            path=constants.DEFAULT_MCP_PATH,
            method="POST",
            event="workspace_rehydrated",
            workspace_hash=_diag_short_hash(project_id),
            handle_hash=_diag_short_hash(handle),
            root_hash=_diag_short_hash(root),
        )
        return ""

    def _workspace_handle_targets_other_root(
        self, tool_name: str, arguments: dict[str, Any], target_workspace: str
    ) -> bool:
        _effective_tool, effective_arguments = self._unwrap_codexpro_call(tool_name, arguments)
        workspace_handle = str(effective_arguments.get("workspace_id") or "").strip()
        if not workspace_handle:
            return False
        with self._session_lock:
            handle_workspace = self._workspace_handle_roots.get(workspace_handle, "")
        return handle_workspace != target_workspace

    def _infer_workspace_for_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        preferred_workspace: str = "",
    ) -> str:
        effective_tool, effective_arguments = self._unwrap_codexpro_call(tool_name, arguments)
        if effective_tool != tool_name:
            return self._infer_workspace_for_call(
                effective_tool, effective_arguments, preferred_workspace=preferred_workspace
            )

        # Strong operation-specific evidence wins over a stale CodexPro workspace
        # handle. A task_id identifies the engine that owns the task; path/cwd
        # identifies the active root the user is actually addressing. The opaque
        # workspace_id is only a fallback for follow-up tools with no such signal.
        task_id = str(arguments.get("task_id") or "").strip()
        if task_id and tool_name in _TASK_AFFINITY_TOOLS:
            with self._session_lock:
                task_workspace = self._task_workspaces.get(task_id, "")
            if (
                task_workspace
                and self._workspace_registry
                and self._workspace_registry(task_workspace)
            ):
                return task_workspace

        values: list[str] = []
        for key in _ROUTE_PATH_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value)
        selected_paths = arguments.get("selected_paths")
        if isinstance(selected_paths, list):
            values.extend(
                value for value in selected_paths if isinstance(value, str) and value.strip()
            )
        if tool_name == "apply_patch":
            values.extend(self._patch_candidate_paths(str(arguments.get("patch") or "")))
        if tool_name in {"bash", "run_command"}:
            values.extend(self._command_candidate_paths(str(arguments.get("command") or "")))

        allow_new = tool_name in _WRITE_ROUTE_TOOLS or tool_name in {"bash", "run_command"}
        absolute_matches: list[str] = []
        had_absolute_path = False
        for value in values:
            candidate = Path(value.strip().strip('"')).expanduser()
            if candidate.is_absolute():
                had_absolute_path = True
                matched = self._workspace_for_path(value, allow_new=allow_new)
                if matched and matched not in absolute_matches:
                    absolute_matches.append(matched)
        if len(absolute_matches) > 1:
            raise ValueError(
                "一次工具调用同时引用了多个运行中的工作区根目录；"
                "请拆成多次调用，或显式指定 workspace_id。"
            )
        if absolute_matches:
            return absolute_matches[0]

        # Explicit application workspace handles scope relative/pathless follow-ups.
        workspace_handle = str(arguments.get("workspace_id") or "").strip()
        if workspace_handle:
            with self._session_lock:
                handle_workspace = self._workspace_handle_roots.get(workspace_handle, "")
            if (
                handle_workspace
                and self._workspace_registry
                and self._workspace_registry(handle_workspace)
            ):
                return handle_workspace
            if (
                handle_workspace
                and workspace_handle.startswith("ws_")
                and self._configured_project_root(handle_workspace)
            ):
                # A configured project may be between STOPPING/STARTING and READY.
                # Preserve its durable route; the caller will receive a temporary
                # data-plane-unavailable response until the engine is ready, then
                # the deterministic handle is rehydrated before the original call.
                return handle_workspace
            if handle_workspace:
                self._forget_workspace_handle(
                    workspace_handle, expected_project_id=handle_workspace
                )
            if workspace_handle.startswith(("ws_", "ws-")):
                self._forget_workspace_handle(workspace_handle)
                raise ValueError(
                    "workspace 上下文已失效且没有可恢复的路由记录；"
                    "请重新打开该工作区后再重试，系统不会静默切换到磁盘根目录。"
                )

        # Legacy transport affinity is only a soft context. Absolute paths above
        # still override it, while relative paths stay in the selected context.
        if (
            preferred_workspace
            and self._workspace_registry
            and self._workspace_registry(preferred_workspace)
        ):
            return preferred_workspace

        if had_absolute_path:
            system_workspace = self._stable_system_workspace_id()
            if system_workspace:
                return system_workspace
            raise ValueError(
                "绝对路径位于所有运行根目录之外，且当前没有运行中的「完全访问」项目可承载该调用。"
            )

        for value in values:
            matched = self._workspace_for_path(value, allow_new=allow_new)
            if matched:
                return matched
        return ""

    @staticmethod
    def _extract_structured_field(payload: bytes, field: str) -> str:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        candidates: list[dict[str, Any]] = []
        if text.lstrip().startswith("data:") or "\ndata:" in text:
            for line in text.splitlines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if not body or body == "[DONE]":
                    continue
                try:
                    value = json.loads(body)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
        else:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict):
                candidates.append(value)
        for value in candidates:
            result = value.get("result")
            if not isinstance(result, dict):
                continue
            structured = result.get("structuredContent")
            if isinstance(structured, dict):
                field_value = str(structured.get(field) or "").strip()
                if field_value:
                    return field_value
        return ""

    @staticmethod
    def _inject_structured_field(payload: bytes, field: str, value: str) -> bytes:
        """Add one application field to a JSON-RPC tool result without replacing opaque handles."""

        def patch_obj(obj: Any) -> Any:
            if not isinstance(obj, dict):
                return obj
            result = obj.get("result")
            if not isinstance(result, dict):
                return obj
            structured = result.get("structuredContent")
            if not isinstance(structured, dict):
                structured = {}
                result["structuredContent"] = structured
            structured[field] = value
            return obj

        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload
        if decoded.lstrip().startswith("data:") or "\ndata:" in decoded:
            out: list[str] = []
            for line in decoded.splitlines(keepends=True):
                if line.startswith("data:"):
                    body = line[5:].strip()
                    if body and body != "[DONE]":
                        try:
                            obj = json.loads(body)
                        except json.JSONDecodeError:
                            pass
                        else:
                            suffix = "\r\n" if line.endswith("\r\n") else "\n"
                            out.append(
                                f"data: {json.dumps(patch_obj(obj), ensure_ascii=False)}{suffix}"
                            )
                            continue
                out.append(line)
            return "".join(out).encode("utf-8")
        try:
            obj = json.loads(decoded)
        except json.JSONDecodeError:
            return payload
        return json.dumps(patch_obj(obj), ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _extract_task_id(payload: bytes) -> str:
        return OAuthGateway._extract_structured_field(payload, "task_id")

    @staticmethod
    def _extract_session_id(request: Request) -> str:
        """Extract mcp-session-id header from request."""
        sid = request.headers.get("mcp-session-id", "")
        return sid.strip()

    def _running_workspace_ids(self) -> list[str]:
        return [project_id for project_id, _root, _mode in self._running_workspace_records()]

    def _stable_workspace_id(self, running: list[str]) -> str:
        """Choose a deterministic bootstrap root without giving it entry privileges."""
        if not running:
            return ""
        roots = {project_id: root for project_id, root in self._running_workspace_roots()}
        return min(
            running,
            key=lambda project_id: os.path.normcase(
                os.path.abspath(str(roots.get(project_id, Path(project_id))))
            ),
        )

    def _effective_workspace(
        self, token_workspace: str, session_id: str, *, pinned: bool = False
    ) -> str:
        """Resolve one upstream for this call without creating an implicit entry root.

        Only an explicit compatibility switch is persisted in ``_session_workspaces``.
        Automatic schema/bootstrap fallback is deterministic but stateless, so path,
        cwd, task and opaque workspace-handle evidence remain free to route every call.
        """
        if pinned and token_workspace:
            return token_workspace
        running = self._running_workspace_ids()
        with self._session_lock:
            selected = self._session_workspaces.get(session_id, "") if session_id else ""
            if selected and selected in running:
                return selected
            if selected and selected not in running and session_id:
                self._session_workspaces.pop(session_id, None)
        if token_workspace and (not running or token_workspace in running):
            return token_workspace
        if len(running) == 1:
            return running[0]
        return self._stable_workspace_id(running)

    def _list_workspaces(self) -> str:
        """Build a text report of all registered projects with status."""
        lines = ["已注册的工作区：", ""]
        try:
            from .config_store import load_projects

            projects = load_projects()
            for i, p in enumerate(projects, 1):
                port_info = ""
                running = "（未运行）"
                if self._workspace_registry and p.id:
                    info = self._workspace_registry(p.id)
                    if info:
                        port_info = f" : {info[0]}"
                        running = "（运行中）"
                name = p.display_name or Path(p.root_path).name
                lines.append(f"{i}. id={p.id}  {name}  {p.root_path}{port_info} {running}")
            if not projects:
                lines.append("（无已注册项目）")
        except Exception:
            lines.append("（无法加载项目列表）")
        return "\n".join(lines)

    def _current_workspace_context(self, explicit_workspace_id: str, session_id: str) -> str:
        """Return only explicit/legacy soft context, never the bootstrap fallback."""
        running = set(self._running_workspace_ids())
        if explicit_workspace_id and explicit_workspace_id in running:
            return explicit_workspace_id
        with self._session_lock:
            selected = self._session_workspaces.get(session_id, "") if session_id else ""
            if selected and selected not in running and session_id:
                self._session_workspaces.pop(session_id, None)
                selected = ""
        return selected if selected in running else ""

    def _get_current_workspace(self, workspace_id: str, session_id: str) -> str:
        """Report explicit/soft context, never a fabricated entry root."""
        running = self._running_workspace_ids()
        explicit = workspace_id or self._current_workspace_context("", session_id)
        if explicit and explicit in running:
            root = str(self._resolve_workspace_path(explicit) or "未知")
            return (
                f"当前工作区上下文：id={explicit}\n路径：{root}\n"
                "这是显式/兼容软上下文；task 与绝对 path/cwd 仍按更强证据自动路由。"
            )
        if not running:
            return "当前没有运行中的工作区。请先在 MCP DevBridge 桌面启动一个或多个项目。"
        roots = [
            str(self._resolve_workspace_path(project_id) or project_id) for project_id in running
        ]
        return "当前会话未固定工作区；所有运行根平等参与自动路由。\n" + "\n".join(
            f"- {root}" for root in roots
        )

    def _do_switch_workspace(self, project_id: str, session_id: str) -> str:
        """Switch the current session's workspace binding."""
        # Verify the project exists
        try:
            from .config_store import load_projects

            projects = load_projects()
            match = next((p for p in projects if p.id == project_id), None)
            if match is None:
                raise ValueError(f"未找到项目：{project_id}。可用 devbridge_list_workspaces 查看。")
        except Exception:
            pass
        # Verify engine is running for this workspace
        if self._workspace_registry and not self._workspace_registry(project_id):
            raise ValueError(
                f"项目 {project_id} 的 CodexPro 引擎未运行。"
                "请先在 MCP DevBridge 桌面上启动该项目的服务。"
            )
        if session_id:
            with self._session_lock:
                _remember_bounded_affinity(self._session_workspaces, session_id, project_id)
        try:
            from .config_store import load_projects

            projects = load_projects()
            match = next((p for p in projects if p.id == project_id), None)
            name = (match.display_name or Path(match.root_path).name) if match else project_id
        except Exception:
            name = project_id
        return (
            f"已切换到工作区：{name}（{project_id}）\n"
            f"后续工具调用请携带 {_ROUTE_WORKSPACE_ARG}={project_id}；这不依赖底层 MCP transport session。"
        )

    # ----------------------------------------------------------- helper
    def _unauthorized(self) -> Response:
        return JSONResponse(
            {
                "error": "unauthorized",
                "message": "需要 OAuth access token 或有效的 Bearer 访问令牌。",
            },
            status_code=401,
            headers={
                "WWW-Authenticate": (
                    f'Bearer realm="MCP DevBridge", resource_metadata="'
                    f'{self.public_base}/.well-known/oauth-protected-resource{constants.DEFAULT_MCP_PATH}"'
                ),
                "Date": formatdate(time.time(), usegmt=True),
            },
        )

    async def _health(self, request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "app": "oauth-gateway",
                "resource": self.resource_url,
                "hub_tool_contract_version": HUB_TOOL_CONTRACT_VERSION,
                "hub_tool_count": HUB_TOOL_COUNT,
                "hub_tool_schema_fingerprint": HUB_TOOL_CONTRACT_FINGERPRINT,
            }
        )

    @property
    def is_running(self) -> bool:
        return bool(
            self._thread is not None
            and self._thread.is_alive()
            and self._server is not None
            and not self._server.should_exit
        )

    # ---------------------------------------------------------- runtime
    def start(self, port: int = constants.GATEWAY_PORT) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=constants.GATEWAY_HOST,
            port=port,
            log_level="warning",
            access_log=False,
            log_config=None,  # PyInstaller 冻结环境：禁止 dictConfig 动态导入 uvicorn.logging 格式化器
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

    def _close_owned_http_client(self) -> None:
        if self._http.is_closed:
            return

        async def close_client() -> None:
            if not self._http.is_closed:
                await self._http.aclose()

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(close_client())
            return

        failures: list[BaseException] = []

        def close_on_worker_loop() -> None:
            try:
                asyncio.run(close_client())
            except BaseException as exc:  # noqa: BLE001 - bounded shutdown fallback
                failures.append(exc)

        worker = threading.Thread(target=close_on_worker_loop, name="gateway-http-close", daemon=True)
        worker.start()
        worker.join(timeout=5)
        if worker.is_alive():
            _write_diag_entry(event="gateway_http_close_timeout", timeout_seconds=5)
        elif failures:
            _write_diag_entry(event="gateway_http_close_failed", failure_type=type(failures[0]).__name__)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        thread_alive = bool(self._thread is not None and self._thread.is_alive())
        if not thread_alive:
            self._close_owned_http_client()


__all__ = ["OAuthGateway"]
