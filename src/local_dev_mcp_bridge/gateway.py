"""OAuth gateway: public entry terminated by the Cloudflare Named Tunnel.

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

import hashlib
import hmac
import html
import json
import threading
import time
from collections.abc import Callable
from email.utils import formatdate
from pathlib import Path
from typing import Any

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
from .constants import LOG_DIR as _LOG_DIR
from .oauth_provider import ConsentExpired, LocalOAuthProvider, _workspace_from_subject
from .secrets import SecretsStore
from .shell import detect_binaries, get_shell_info, run_command, run_program

_LOCAL_TOOL_NAMES = frozenset({
    "run_command", "run_program", "shell_self_test",
    "devbridge_list_workspaces", "devbridge_get_current_workspace", "devbridge_switch_workspace",
})

_PYTHON_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "Execute a command in the project's PowerShell shell. Returns shell type, exit code, stdout, and stderr. WSL is never auto-selected.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
                "cwd": {"type": "string", "description": "Working directory relative to project root. Defaults to project root."},
                "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 600, max 1800)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_program",
        "description": "Run a program directly with an argument array, bypassing shell parsing (prevents injection).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "description": "Program path or name"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Argument list"},
                "cwd": {"type": "string", "description": "Working directory relative to project root. Defaults to project root."},
                "timeout_seconds": {"type": "integer", "description": "Timeout in seconds (default 600, max 1800)"},
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
                "project_id": {"type": "string", "description": "Project ID from devbridge_list_workspaces output"},
            },
            "required": ["project_id"],
        },
    },
]

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MCP DevBridge - 工作区授权</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px;color:#1f2328}}
h1{{font-size:20px}}
dl{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px}}
dt{{color:#57606a;font-size:12px;text-transform:uppercase;margin-top:8px}}
dd{{margin:2px 0 8px;word-break:break-all}}
select{{font-size:14px;padding:6px 10px;border:1px solid #d0d7de;border-radius:6px;width:100%;margin:8px 0}}
form{{margin-top:16px;display:flex;gap:12px}}
button{{font-size:15px;padding:8px 22px;border-radius:6px;cursor:pointer}}
.allow{{background:#1f883d;color:#fff;border:1px solid #1f883d}}
.cancel{{background:#fff;border:1px solid #d0d7de}}
.workspace-label{{font-weight:600;margin-top:12px}}
</style></head>
<body><h1>MCP DevBridge - 工作区授权</h1>
<p>AI 客户端正在请求访问你的本地开发环境。请选择要绑定的项目工作区。</p>
<dl>{rows}</dl>
<form method="post" action="/consent">
<input type="hidden" name="id" value="{cid}">
<div class="workspace-label">选择工作区：</div>
<select name="workspace_id">{workspace_options}</select>
<p style="color:#57606a;font-size:13px">所选工作区将绑定到此客户端的 OAuth 授权中。不同客户端可以绑定不同工作区。</p>
<button type="submit" name="decision" value="allow" class="allow">允许访问</button>
<button type="submit" name="decision" value="deny" class="cancel">取消</button>
</form></body></html>"""

_HOP_HEADERS = frozenset({"host", "connection", "content-length", "transfer-encoding", "authorization"})

# --------------------------------------------------------- diagnostic logging
_DIAG_SENSITIVE_KEYS = frozenset({
    "command", "code", "access_token", "refresh_token", "client_secret",
    "token", "authorization", "bearer", "code_verifier",
})
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
    return {
        k: "***" if k.lower() in _DIAG_SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }


def _write_diag_entry(**fields: Any) -> None:
    entry = {**fields, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    try:
        p = _diag_log_path()
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


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
            "serverInfo" in obj or (isinstance(obj.get("result"), dict) and "serverInfo" in obj["result"])
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
    """Add Python tool definitions to the tools/list response.
    
    Handles both SSE (``data: {...}`` lines) and plain JSON bodies.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload

    def _patch(obj: Any) -> Any:
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools")
            if isinstance(tools, list):
                tools.extend(_PYTHON_TOOL_DEFS)
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
        if isinstance(obj, dict) and isinstance(obj.get("result"), dict) and "tools" in obj["result"]:
            return json.dumps(_patch(obj), ensure_ascii=False).encode("utf-8")
    except json.JSONDecodeError:
        pass
    return payload


def _jsonrpc_result(rpc_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _jsonrpc_error(rpc_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _is_loopback(request: Request) -> bool:
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first.startswith("127.")
    client = request.client
    return bool(client and (client.host in ("127.0.0.1", "::1", "localhost") or client.host.startswith("127.")))


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
    ) -> None:
        hostname = (public_hostname or "").strip().rstrip("/")
        base = hostname if hostname.lower().startswith(("http://", "https://")) else f"https://{hostname}"
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
        # Session → workspace_id mapping for switch_workspace tool
        self._session_workspaces: dict[str, str] = {}
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
        routes.append(Route(constants.DEFAULT_MCP_PATH, self._mcp_endpoint, methods=["GET", "POST"]))
        routes.append(Route("/consent", self._consent_page, methods=["GET"]))
        routes.append(Route("/consent", self._consent_submit, methods=["POST"]))
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
            _row("项目目录", self._provider.workspace or "（当前项目）"),
            _row("请求的权限范围", ", ".join(consent["scopes"])),
            _row("调用方 Client ID", consent["client_id"]),
        ]
        workspace_options = self._build_workspace_options()
        cid = html.escape(request.query_params.get("id", ""))
        return HTMLResponse(
            _PAGE.format(rows="".join(rows), cid=cid, workspace_options=workspace_options)
        )

    def _build_workspace_options(self) -> str:
        """Build <option> tags for available projects (for consent page dropdown)."""
        if self._workspace_registry is None:
            options = ['<option value="">（当前激活项目）</option>']
        else:
            options = ['<option value="" selected disabled>（请选择一个运行中的项目）</option>']
        if self._workspace_registry is not None:
            try:
                from .config_store import load_projects
                projects = load_projects()
                for p in projects:
                    if p.id:
                        port = ""
                        info = self._workspace_registry(p.id)
                        if info:
                            port = f"（运行中 :{info[0]}）"
                        selected = ""
                        name = p.display_name or Path(p.root_path).name
                        options.append(
                            f'<option value="{html.escape(p.id)}" {selected}>'
                            f"{html.escape(name)} - {html.escape(p.root_path)}{html.escape(port)}"
                            f"</option>"
                        )
            except Exception:
                pass
        return "\n".join(options)

    async def _consent_submit(self, request: Request) -> Response:
        form = await request.form()
        consent_id = str(form.get("id", ""))
        decision = str(form.get("decision", "deny"))
        workspace_id = str(form.get("workspace_id", ""))
        _write_diag_entry(
            path="/consent", method="POST",
            decision=decision,
            workspace_hash=_diag_short_hash(workspace_id),
        )
        try:
            if decision == "allow":
                if self._workspace_registry is not None:
                    if not workspace_id:
                        _write_diag_entry(
                            path="/consent", method="POST",
                            event="consent_missing_workspace",
                            error="workspace_id is empty - no workspace selected",
                        )
                        return HTMLResponse(
                            "<html><body><p>请选择一个正在运行的项目后再授权。</p>"
                            "<p>请返回 MCP DevBridge 启动目标项目，然后重新发起连接。</p></body></html>",
                            status_code=400,
                        )
                    if not self._workspace_registry(workspace_id):
                        _write_diag_entry(
                            path="/consent", method="POST",
                            event="consent_workspace_not_ready",
                            workspace_hash=_diag_short_hash(workspace_id),
                            error="Selected workspace CodexPro is not running",
                        )
                        return HTMLResponse(
                            "<html><body><p>所选项目尚未运行，授权已阻止。</p>"
                            "<p>请先在 MCP DevBridge 中启动该项目服务，再重新发起连接。</p></body></html>",
                            status_code=409,
                        )
                if workspace_id:
                    self._provider.bind_workspace(consent_id, workspace_id)
                target = self._provider.approve(consent_id)
            else:
                target = self._provider.deny(consent_id)
        except ConsentExpired:
            _write_diag_entry(
                path="/consent", method="POST",
                error="consent_expired",
            )
            return HTMLResponse(
                "<html><body><p>授权已过期或不存在，请重新发起连接。</p></body></html>",
                status_code=410,
            )
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------- /mcp
    async def _mcp_endpoint(self, request: Request) -> Response:
        body = await request.body()
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        engine_credential = self.upstream_token_source()

        # --- resolve proxy token and workspace ---
        proxy_token: str | None = None
        workspace_id: str = ""
        upstream_target: str | None = None
        session_id = self._extract_session_id(request)

        if bearer:
            direct_workspace = self._workspace_for_credential(bearer)
            if _constant_time_eq(bearer, engine_credential) or direct_workspace:
                workspace_id = direct_workspace
                with self._session_lock:
                    if session_id and session_id in self._session_workspaces:
                        workspace_id = self._session_workspaces[session_id]
                if workspace_id:
                    upstream_target = self._resolve_upstream(workspace_id)
                    if not upstream_target:
                        return JSONResponse(
                            _jsonrpc_error(
                                None,
                                -32000,
                                "工作区未就绪。请先在桌面启动对应项目的 CodexPro 引擎。",
                            ),
                            status_code=502,
                        )
                proxy_token = self._credential_for_workspace(
                    workspace_id, engine_credential or bearer
                )
            else:
                record = await self._provider.load_access_token(bearer)
                if record is not None:
                    if record.resource and record.resource.rstrip("/") != self.resource_url:
                        return self._unauthorized()
                    workspace_id = _workspace_from_subject(record.subject or "")
                    with self._session_lock:
                        if session_id and session_id in self._session_workspaces:
                            workspace_id = self._session_workspaces[session_id]
                    upstream_target = self._resolve_upstream(workspace_id)
                    if not upstream_target:
                        return JSONResponse(
                            _jsonrpc_error(
                                None,
                                -32000,
                                "工作区未就绪。请先在桌面启动对应项目的 CodexPro 引擎。",
                            ),
                            status_code=502,
                        )
                    proxy_token = self._credential_for_workspace(workspace_id, engine_credential)
                    if not proxy_token:
                        return self._unauthorized()
                else:
                    return self._unauthorized()
        elif self.allow_local_anonymous and _is_loopback(request):
            proxy_token = None
        else:
            return self._unauthorized()

        # --- intercept local Python tool calls ---
        jsonrpc_method: str = ""
        tool_name: str = ""
        rpc = None  # type: ignore[assignment]
        if request.method == "POST":
            try:
                rpc = json.loads(body)
                if isinstance(rpc, dict):
                    jsonrpc_method = str(rpc.get("method", ""))
            except json.JSONDecodeError:
                rpc = None
            if isinstance(rpc, dict) and rpc.get("method") == "tools/call":
                params = rpc.get("params") or {}
                name = params.get("name", "")
                tool_name = name
                if name in _LOCAL_TOOL_NAMES:
                    result = await self._exec_local_tool(name, rpc, params, workspace_id, session_id)
                    _write_diag_entry(
                        path=request.url.path,
                        method=request.method,
                        local_tool=name,
                        workspace_hash=_diag_short_hash(workspace_id),
                        session_id=session_id[:16],
                    )
                    return result

        return await self._proxy(request, body, proxy_token, upstream_target=upstream_target,
                                   jsonrpc_method=jsonrpc_method, tool_name=tool_name,
                                   workspace_id=workspace_id, session_id=session_id)

    async def _proxy(
        self, request: Request, body: bytes, authorization: str | None, *,
        upstream_target: str | None = None,
        jsonrpc_method: str = "", tool_name: str = "",
        workspace_id: str = "", session_id: str = "",
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
        try:
            upstream = await self._http.send(
                self._http.build_request(request.method, target, content=body, headers=headers),
                stream=True,
            )
        except httpx.HTTPError:
            return JSONResponse({"error": "upstream_unreachable"}, status_code=502)
        filtered = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in _HOP_HEADERS
        }
        if request.method == "POST":
            if b'"tools/list"' in body:
                await upstream.aread()
                rewritten = _inject_tools(upstream.content)
                tool_count, dupes = _analyze_tools(rewritten)
                _write_diag_entry(
                    path=request.url.path, method=request.method,
                    jsonrpc_method="tools/list",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                    injected_tool_count=len(_PYTHON_TOOL_DEFS),
                    total_tool_count=tool_count,
                    duplicate_tools=dupes,
                    workspace_hash=_diag_short_hash(workspace_id),
                )
                return Response(content=rewritten, status_code=upstream.status_code, headers=filtered)
            if b"initialize" in body:
                await upstream.aread()
                rewritten = _rewrite_server_identity(upstream.content)
                _write_diag_entry(
                    path=request.url.path, method=request.method,
                    jsonrpc_method="initialize",
                    upstream_status=upstream.status_code,
                    upstream_target=target,
                )
                return Response(content=rewritten, status_code=upstream.status_code, headers=filtered)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=filtered,
        )

    # ---------------------------------------------------- local tools
    async def _exec_local_tool(
        self, name: str, rpc: dict[str, Any], params: dict[str, Any],
        workspace_id: str = "", session_id: str = "",
    ) -> JSONResponse:
        rpc_id = rpc.get("id")
        workspace = self._resolve_workspace_path(workspace_id) or self._workspace or Path.cwd()
        try:
            if name == "run_command":
                command = str(params.get("command", ""))
                if not command.strip():
                    raise ValueError("command 不能为空。")
                cwd_rel = str(params.get("cwd", "")).strip()
                cwd = (workspace / cwd_rel).resolve() if cwd_rel else workspace
                timeout = max(1, min(int(params.get("timeout_seconds") or 600), 1800))
                res = run_command(command, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"shell: {res.shell}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]}))
            elif name == "run_program":
                executable = str(params.get("executable", ""))
                if not executable.strip():
                    raise ValueError("executable 不能为空。")
                args = [str(a) for a in (params.get("args") or [])]
                cwd_rel = str(params.get("cwd", "")).strip()
                cwd = (workspace / cwd_rel).resolve() if cwd_rel else workspace
                timeout = max(1, min(int(params.get("timeout_seconds") or 600), 1800))
                res = run_program(executable, args, cwd=cwd, timeout_seconds=timeout)
                text = (
                    f"command: {res.command}\n"
                    f"exit_code: {res.exit_code}\n"
                    f"duration: {res.duration_seconds:.2f}s\n"
                    f"{'*** TIMED OUT ***' if res.timed_out else ''}\n"
                    f"--- stdout ---\n{res.stdout}\n--- stderr ---\n{res.stderr}"
                )
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": text}]}))
            elif name == "shell_self_test":
                lines: list[str] = []
                info = get_shell_info()
                default = info.get("default") or {}
                if isinstance(default, dict):
                    if default.get("executable"):
                        lines.append(f"[✓] shell: {default.get('name', '?')} ({default.get('path', '?')})")
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
                            creationflags=0x08000000 if hasattr(_sp, "CREATE_NO_WINDOW") else 0,
                        )
                        out = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
                        lines.append(f"[✓] {tool}: {out[0] if out else 'ok'} (python -m {tool})")
                    except Exception:
                        lines.append(f"[✗] {tool}: 未安装或不可调用")
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": "\n".join(lines)}]}))
            elif name == "devbridge_list_workspaces":
                result = self._list_workspaces()
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]}))
            elif name == "devbridge_get_current_workspace":
                result = self._get_current_workspace(workspace_id, session_id)
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]}))
            elif name == "devbridge_switch_workspace":
                args = params.get("arguments", {}) if isinstance(params, dict) else {}
                target_id = str(args.get("project_id", ""))
                if not target_id:
                    raise ValueError("project_id 不能为空。")
                result = self._do_switch_workspace(target_id, session_id)
                return JSONResponse(_jsonrpc_result(rpc_id, {"content": [{"type": "text", "text": result}]}))
            else:
                raise ValueError(f"未知的本地工具: {name}")
        except ValueError as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32602, str(exc)))
        except Exception as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32603, f"工具执行失败: {exc}"))

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
    def _extract_session_id(request: Request) -> str:
        """Extract mcp-session-id header from request."""
        sid = request.headers.get("mcp-session-id", "")
        return sid.strip()

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
        """Return info about the current session's workspace."""
        with self._session_lock:
            effective = self._session_workspaces.get(session_id, workspace_id) if session_id else workspace_id
        if not effective:
            default_root = str(self._workspace) if self._workspace else "（未设置）"
            return f"当前工作区：默认（{default_root}）"
        root = str(self._resolve_workspace_path(effective) or "未知")
        return f"当前工作区：id={effective}\n路径：{root}"

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
        if not session_id:
            raise ValueError("无法识别当前会话（缺少 mcp-session-id 请求头），请使用支持会话的 MCP 客户端。")
        # Verify engine is running for this workspace
        if self._workspace_registry and not self._workspace_registry(project_id):
            raise ValueError(
                f"项目 {project_id} 的 CodexPro 引擎未运行。"
                "请先在 MCP DevBridge 桌面上启动该项目的服务。"
            )
        with self._session_lock:
            self._session_workspaces[session_id] = project_id
        try:
            from .config_store import load_projects
            projects = load_projects()
            match = next((p for p in projects if p.id == project_id), None)
            name = (match.display_name or Path(match.root_path).name) if match else project_id
        except Exception:
            name = project_id
        return f"已切换到工作区：{name}（{project_id}）\n仅影响当前 MCP 会话。"

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
