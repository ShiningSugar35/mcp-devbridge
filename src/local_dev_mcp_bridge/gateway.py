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
from contextlib import suppress
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
from .oauth_provider import ConsentExpired, LocalOAuthProvider, _workspace_from_subject
from .platform_support import run_platform_kwargs
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


async def _read_and_close_upstream(response: httpx.Response) -> bytes:
    try:
        return await response.aread()
    finally:
        await response.aclose()


async def _stream_and_close_upstream(response: httpx.Response):
    try:
        # Iterate the leased transport stream directly instead of Response.aiter_raw().
        # The latter raises StreamConsumed when a cancellation/reconnect marks the
        # response consumed before Starlette begins forwarding it.  This stream has
        # exactly one owner and is always closed when the downstream disconnects.
        stream = cast(httpx.AsyncByteStream, response.stream)
        async for chunk in stream:
            yield chunk
    finally:
        await response.aclose()


def _sse_event_boundary_at_end(suffix: bytes) -> bool:
    return suffix.endswith(
        (b"\n\n", b"\r\r", b"\r\n\r\n", b"\r\n\n", b"\n\r\n")
    )


async def _stream_sse_with_keepalive_and_close_upstream(
    response: httpx.Response,
    *,
    keepalive_seconds: float = _SSE_KEEPALIVE_SECONDS,
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
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
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
                raise payload
            assert isinstance(payload, bytes)
            suffix = (suffix + payload)[-4:]
            at_event_boundary = _sse_event_boundary_at_end(suffix)
            yield payload
    finally:
        if not producer.done():
            producer.cancel()
        with suppress(asyncio.CancelledError):
            await producer
        await response.aclose()


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


def _analyze_tools(payload: bytes) -> tuple[int, list[str]]:
    """Return (total_tool_count, list_of_duplicate_names) from a tools/list response."""
    count = 0
    dupes: list[str] = []
    try:
        text = payload.decode("utf-8")
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            tools = data["result"].get("tools") or []
            if isinstance(tools, list):
                names = [t.get("name", "") for t in tools if isinstance(t, dict)]
                count = len(names)
                seen: set[str] = set()
                for n in names:
                    if n in seen:
                        dupes.append(n)
                    seen.add(n)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return count, dupes


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
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict) and "tools" in obj["result"]:
            return json.dumps(patch_obj(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _jsonrpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_arguments(params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        return {}
    value = params.get("arguments")
    return dict(value) if isinstance(value, dict) else {}


def _strip_route_arguments(body: bytes, rpc: dict[str, Any] | None, *, keep_workspace: bool) -> bytes:
    if rpc is None or str(rpc.get("method", "")) != "tools/call":
        return body
    params = rpc.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return body
    if _ROUTE_DEVICE_ARG not in arguments and (keep_workspace or _ROUTE_WORKSPACE_ARG not in arguments):
        return body
    copied_rpc = dict(rpc)
    copied_params = dict(params)
    copied_args = dict(arguments)
    copied_args.pop(_ROUTE_DEVICE_ARG, None)
    if not keep_workspace:
        copied_args.pop(_ROUTE_WORKSPACE_ARG, None)
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
    """Pure ASGI middleware that logs every request/response to gateway JSONL.

    Uses pure ASGI (not BaseHTTPMiddleware) to safely wrap SSE streaming responses.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_ns = time.monotonic_ns()
        method: str = scope.get("method", "")
        path: str = scope.get("path", "")
        status: list[int] = [0]
        resp_content_type: list[str] = [""]
        resp_bytes: list[int] = [0]

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status[0] = message.get("status", 0)
                for h in message.get("headers") or []:
                    if h[0].decode("latin-1").lower() == "content-type":
                        resp_content_type[0] = h[1].decode("latin-1")
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                resp_bytes[0] += len(body)
            await send(message)

        exc_type: str = ""
        exc_msg: str = ""
        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            exc_type = type(exc).__name__
            exc_msg = _diag_redact_body(str(exc)[:500])
            raise
        finally:
            duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
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
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        device_registry: DeviceRegistry | None = None,
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
        self._workspace_credential_registry = workspace_credential_registry
        self._device_registry = device_registry
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
        self.app = self._build_app()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(900.0, connect=30.0),
            transport=transport,
        )
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
        self._upstream_sessions: dict[str, dict[str, str]] = {}
        self._initialize_requests: dict[str, bytes] = {}
        self._session_lock = threading.Lock()

    # ------------------------------------------------------------ build
    @property
    def provider(self) -> LocalOAuthProvider:
        return self._provider

    def _build_app(self) -> Any:
        registration = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[constants.OAUTH_SCOPE],
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
            scopes_supported=[constants.OAUTH_SCOPE],
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
        app = Starlette(routes=routes)
        return _DiagnosticMiddleware(app)

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

        proxy_token: str | None = None
        workspace_id = ""
        authenticated_workspace = ""
        upstream_target: str | None = None
        if bearer:
            authenticated_workspace = self._workspace_for_credential(bearer)
            if _constant_time_eq(bearer, engine_credential) or authenticated_workspace:
                # A project bearer remains a backward-compatible fallback, but path/task routing
                # may override it for tools/call so the credential never becomes a routing fence.
                workspace_id = authenticated_workspace
                proxy_token = engine_credential or bearer
            else:
                record = await self._provider.load_access_token(bearer)
                if record is None:
                    return self._unauthorized()
                if record.resource and record.resource.rstrip("/") != self.resource_url:
                    return self._unauthorized()
                workspace_id = _workspace_from_subject(record.subject or "")
                proxy_token = engine_credential
                if not proxy_token and self._workspace_registry is None:
                    return self._unauthorized()
        elif self.allow_local_anonymous and _is_loopback(request):
            proxy_token = None
        else:
            return self._unauthorized()

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
                    self._session_devices[session_id] = device_id
        else:
            device_id = self._effective_device(session_id)

        remote = None
        upstream_key = ""
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
            upstream_key = f"remote:{device_id}"
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
                try:
                    inferred_workspace = self._infer_workspace_for_call(tool_name, call_arguments)
                except ValueError as exc:
                    return JSONResponse(_jsonrpc_error(rpc.get("id"), -32602, str(exc)))
                if inferred_workspace:
                    workspace_id = inferred_workspace
                elif authenticated_workspace:
                    workspace_id = authenticated_workspace

            workspace_id = self._effective_workspace(
                workspace_id,
                session_id,
                pinned=bool(route_workspace_id or inferred_workspace),
            )
            if workspace_id:
                upstream_target = self._resolve_upstream(workspace_id)
                upstream_key = f"local:{workspace_id}"
                if not upstream_target:
                    return JSONResponse(
                        _jsonrpc_error(
                            None, -32000, "目标工作区尚未运行。请先在 MCP DevBridge 桌面启动该根目录。"
                        ),
                        status_code=502,
                    )
                proxy_token = self._credential_for_workspace(
                    workspace_id, engine_credential or proxy_token
                )

        if rpc is not None and jsonrpc_method == "tools/call":
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

        body = _strip_route_arguments(body, rpc, keep_workspace=remote is not None)
        upstream_session_id = session_id
        if session_id and jsonrpc_method != "initialize" and upstream_target and upstream_key:
            ensured = await self._ensure_upstream_session(
                client_session_id=session_id,
                upstream_key=upstream_key,
                upstream_target=upstream_target,
                authorization=proxy_token,
            )
            if not ensured:
                return JSONResponse(
                    _jsonrpc_error(None, -32001, "无法为目标工作区建立 MCP 上游会话。"),
                    status_code=502,
                )
            upstream_session_id = ensured
        return await self._proxy(
            request,
            body,
            proxy_token,
            upstream_target=upstream_target,
            upstream_key=upstream_key,
            client_session_id=session_id,
            upstream_session_id=upstream_session_id,
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
        upstream_key: str = "",
        client_session_id: str = "",
        upstream_session_id: str = "",
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
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        if upstream_session_id:
            headers["mcp-session-id"] = upstream_session_id
        else:
            headers.pop("mcp-session-id", None)
        started = time.monotonic()
        try:
            upstream = await self._http.send(
                self._http.build_request(request.method, target, content=body, headers=headers),
                stream=True,
            )
        except httpx.HTTPError:
            if tool_name:
                self._audit_gateway_tool(
                    request,
                    rpc,
                    tool_name,
                    workspace_id,
                    device_id,
                    False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_type="upstream_unreachable",
                )
            return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
        if tool_name:
            self._audit_gateway_tool(
                request,
                rpc,
                tool_name,
                workspace_id,
                device_id,
                upstream.status_code < 400,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_type="" if upstream.status_code < 400 else f"http_{upstream.status_code}",
            )
        filtered = {
            key: value for key, value in upstream.headers.items() if key.lower() not in _HOP_HEADERS
        }
        if request.method == "POST":
            affinity_tool = tool_name
            if jsonrpc_method == "tools/call" and rpc is not None:
                affinity_tool, _ = self._unwrap_codexpro_call(
                    tool_name, _tool_arguments(rpc.get("params") or {})
                )
            if (
                jsonrpc_method == "tools/call"
                and affinity_tool in {"open_workspace", "open_current_workspace"}
                and workspace_id
            ):
                payload = await _read_and_close_upstream(upstream)
                handle = self._extract_structured_field(payload, "workspace_id")
                if handle:
                    with self._session_lock:
                        self._workspace_handle_roots[handle] = workspace_id
                return Response(
                    content=payload, status_code=upstream.status_code, headers=filtered
                )
            if jsonrpc_method == "tools/call" and affinity_tool == "bash" and workspace_id:
                payload = await _read_and_close_upstream(upstream)
                task_id = self._extract_task_id(payload)
                if task_id:
                    with self._session_lock:
                        self._task_workspaces[task_id] = workspace_id
                return Response(
                    content=payload, status_code=upstream.status_code, headers=filtered
                )
            if b'"tools/list"' in body:
                payload = await _read_and_close_upstream(upstream)
                rewritten = _inject_tools(payload)
                tool_count, dupes = _analyze_tools(rewritten)
                _write_diag_entry(
                    path=request.url.path,
                    method=request.method,
                    jsonrpc_method="tools/list",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                    injected_tool_count=len(_PYTHON_TOOL_DEFS),
                    total_tool_count=tool_count,
                    duplicate_tools=dupes,
                    workspace_hash=_diag_short_hash(workspace_id),
                )
                return Response(
                    content=rewritten, status_code=upstream.status_code, headers=filtered
                )
            if b"initialize" in body:
                payload = await _read_and_close_upstream(upstream)
                returned_session_id = upstream.headers.get("mcp-session-id", "").strip()
                if returned_session_id and upstream_key:
                    # The first upstream session id is also the client-facing id.
                    # Later workspace/device targets receive their own mapped ids.
                    self._remember_upstream_session(
                        returned_session_id, upstream_key, returned_session_id, body
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
        stream_body = (
            _stream_sse_with_keepalive_and_close_upstream(upstream)
            if _upstream_is_sse(upstream)
            else _stream_and_close_upstream(upstream)
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
        try:
            if name == "run_command":
                command = str(arguments.get("command", ""))
                if not command.strip():
                    raise ValueError("command 不能为空。")
                cwd_rel = str(arguments.get("cwd", "")).strip()
                cwd = self._local_tool_cwd(workspace, cwd_rel)
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
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
                cwd = self._local_tool_cwd(workspace, cwd_rel)
                timeout = max(1, min(int(arguments.get("timeout_seconds") or 10), 20))
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
                result = self._get_current_workspace(workspace_id, session_id)
                return JSONResponse(
                    _jsonrpc_result(
                        rpc_id,
                        {
                            "content": [{"type": "text", "text": result}],
                            "structuredContent": {_ROUTE_WORKSPACE_ARG: workspace_id},
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
                            "structuredContent": {_ROUTE_DEVICE_ARG: self._effective_device(session_id)},
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
                    self._session_devices[session_id] = online[0]
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
                self._session_devices[session_id] = device_id
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

    # ------------------------------------------------------ upstream sessions
    def _remember_upstream_session(
        self, client_session_id: str, upstream_key: str, upstream_session_id: str, initialize_body: bytes
    ) -> None:
        if not client_session_id or not upstream_key or not upstream_session_id:
            return
        with self._session_lock:
            self._upstream_sessions.setdefault(client_session_id, {})[upstream_key] = upstream_session_id
            if initialize_body:
                self._initialize_requests[client_session_id] = bytes(initialize_body)

    def _mapped_upstream_session(self, client_session_id: str, upstream_key: str) -> str:
        if not client_session_id or not upstream_key:
            return ""
        with self._session_lock:
            return self._upstream_sessions.get(client_session_id, {}).get(upstream_key, "")

    def _forget_upstream_session(self, client_session_id: str, upstream_key: str) -> None:
        if not client_session_id or not upstream_key:
            return
        with self._session_lock:
            mappings = self._upstream_sessions.get(client_session_id)
            if mappings is not None:
                mappings.pop(upstream_key, None)
                if not mappings:
                    self._upstream_sessions.pop(client_session_id, None)

    async def _ensure_upstream_session(
        self,
        *,
        client_session_id: str,
        upstream_key: str,
        upstream_target: str,
        authorization: str | None,
    ) -> str:
        existing = self._mapped_upstream_session(client_session_id, upstream_key)
        if existing:
            return existing
        with self._session_lock:
            initialize_body = self._initialize_requests.get(client_session_id, b"")
        if not initialize_body:
            # Compatibility fallback for clients/tests that do not expose a stable
            # initialize exchange to the Gateway. The target may still accept the
            # caller-provided session id.
            return client_session_id

        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if authorization:
            headers["authorization"] = f"Bearer {authorization}"
        try:
            response = await self._http.post(
                f"{upstream_target}/mcp", content=initialize_body, headers=headers
            )
            if response.status_code >= 400:
                return ""
            upstream_session_id = response.headers.get("mcp-session-id", "").strip()
            if not upstream_session_id:
                return ""
            initialized_headers = dict(headers)
            initialized_headers["mcp-session-id"] = upstream_session_id
            initialized = await self._http.post(
                f"{upstream_target}/mcp",
                content=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": {},
                    }
                ).encode("utf-8"),
                headers=initialized_headers,
            )
            if initialized.status_code >= 400:
                return ""
        except httpx.HTTPError:
            return ""
        self._remember_upstream_session(
            client_session_id, upstream_key, upstream_session_id, initialize_body
        )
        return upstream_session_id

    # -------------------------------------------------------- workspace helpers
    def _credential_for_workspace(self, workspace_id: str, fallback: str | None) -> str | None:
        if workspace_id and self._workspace_credential_registry is not None:
            value = self._workspace_credential_registry(workspace_id)
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
            child_text = os.path.normcase(os.path.realpath(os.path.abspath(str(child.expanduser()))))
            parent_text = os.path.normcase(os.path.realpath(os.path.abspath(str(parent.expanduser()))))
            return os.path.commonpath([child_text, parent_text]) == parent_text
        except (OSError, ValueError):
            return False

    def _local_tool_cwd(self, workspace: Path, raw_cwd: str) -> Path:
        root = workspace.expanduser().resolve()
        value = (raw_cwd or '').strip()
        candidate = Path(value).expanduser() if value else root
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        if not self._path_within(candidate, root):
            raise ValueError('cwd 超出目标工作区根目录。请使用该运行中根目录内的路径。')
        return candidate

    def _running_workspace_roots(self) -> list[tuple[str, Path]]:
        if self._workspace_registry is None:
            return []
        rows: list[tuple[str, Path]] = []
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
                    rows.append((project.id, Path(root).expanduser()))
        except Exception:
            return []
        rows.sort(
            key=lambda item: (
                -len(os.path.abspath(str(item[1]))),
                os.path.normcase(os.path.abspath(str(item[1]))),
            )
        )
        return rows

    def _workspace_for_path(self, raw_path: str, *, allow_new: bool = False) -> str:
        value = (raw_path or "").strip().strip('\"')
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

    def _infer_workspace_for_call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        effective_tool, effective_arguments = self._unwrap_codexpro_call(tool_name, arguments)
        if effective_tool != tool_name:
            return self._infer_workspace_for_call(effective_tool, effective_arguments)

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
        for value in values:
            candidate = Path(value.strip().strip('\"')).expanduser()
            if candidate.is_absolute():
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
        for value in values:
            matched = self._workspace_for_path(value, allow_new=allow_new)
            if matched:
                return matched

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
    def _extract_task_id(payload: bytes) -> str:
        return OAuthGateway._extract_structured_field(payload, "task_id")

    @staticmethod
    def _extract_session_id(request: Request) -> str:
        """Extract mcp-session-id header from request."""
        sid = request.headers.get("mcp-session-id", "")
        return sid.strip()

    def _running_workspace_ids(self) -> list[str]:
        if self._workspace_registry is None:
            return []
        try:
            from .config_store import load_projects

            return [
                project.id
                for project in load_projects()
                if project.id and self._workspace_registry(project.id) is not None
            ]
        except Exception:
            return []

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

    def _effective_workspace(self, token_workspace: str, session_id: str, *, pinned: bool = False) -> str:
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

    def _get_current_workspace(self, workspace_id: str, session_id: str) -> str:
        """Report explicit compatibility override state, not a fabricated entry root."""
        running = self._running_workspace_ids()
        with self._session_lock:
            selected = self._session_workspaces.get(session_id, "") if session_id else ""
        explicit = selected
        if explicit and explicit in running:
            root = str(self._resolve_workspace_path(explicit) or "未知")
            return (
                f"当前存在显式工作区覆盖：id={explicit}\n路径：{root}\n"
                "这是兼容覆盖；带 path/cwd/task 的调用仍按更强证据自动路由。"
            )
        if not running:
            return "当前没有运行中的工作区。请先在 MCP DevBridge 桌面启动一个或多个项目。"
        roots = [str(self._resolve_workspace_path(project_id) or project_id) for project_id in running]
        return (
            "当前会话未固定工作区；所有运行根平等参与自动路由。\n"
            + "\n".join(f"- {root}" for root in roots)
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
                self._session_workspaces[session_id] = project_id
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
        return JSONResponse({"status": "ok", "app": "oauth-gateway", "resource": self.resource_url})

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

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


__all__ = ["OAuthGateway"]
