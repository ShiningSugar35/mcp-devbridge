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
from .oauth_provider import ConsentExpired, LocalOAuthProvider
from .secrets import SecretsStore
from .shell import detect_binaries, get_shell_info, run_command, run_program

_LOCAL_TOOL_NAMES = frozenset({"run_command", "run_program", "shell_self_test"})

_PYTHON_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "run_command",
        "description": "在项目的 PowerShell (Windows) 中执行命令。返回 shell、退出码与 stdout/stderr。WSL 不会被默认调用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "cwd": {"type": "string", "description": "工作目录（相对项目根目录的相对路径，默认使用项目根目录）"},
                "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 600（10 分钟）"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_program",
        "description": "直接运行程序（参数数组，不经 shell 解析，避免注入风险）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "executable": {"type": "string", "description": "程序路径或名称"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "参数列表"},
                "cwd": {"type": "string", "description": "工作目录（相对项目根目录的相对路径，默认使用项目根目录）"},
                "timeout_seconds": {"type": "integer", "description": "超时秒数，默认 600（10 分钟）"},
            },
            "required": ["executable"],
        },
    },
    {
        "name": "shell_self_test",
        "description": "检测默认 Shell 是否可执行以及 python/git/pytest/pyright 是否可调用，返回逐项 ✓/✗ 结果。",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>MCP DevBridge</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:540px;margin:48px auto;padding:0 16px;color:#1f2328}}
h1{{font-size:20px}}
dl{{background:#f6f8fa;border:1px solid #d0d7de;border-radius:8px;padding:12px 16px}}
dt{{color:#57606a;font-size:12px;text-transform:uppercase;margin-top:8px}}
dd{{margin:2px 0 8px;word-break:break-all}}
form{{margin-top:16px;display:flex;gap:12px}}
button{{font-size:15px;padding:8px 22px;border-radius:6px;cursor:pointer}}
.allow{{background:#1f883d;color:#fff;border:1px solid #1f883d}}
.cancel{{background:#fff;border:1px solid #d0d7de}}
</style></head>
<body><h1>MCP DevBridge</h1>
<p>Gemini 正在请求访问你的本地开发环境。</p>
<dl>{rows}</dl>
<form method="post" action="/consent">
<input type="hidden" name="id" value="{cid}">
<button type="submit" name="decision" value="allow" class="allow">允许访问</button>
<button type="submit" name="decision" value="deny" class="cancel">取消</button>
</form></body></html>"""

_HOP_HEADERS = frozenset({"host", "connection", "content-length", "transfer-encoding", "authorization"})


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

    # ------------------------------------------------------------ build
    @property
    def provider(self) -> LocalOAuthProvider:
        return self._provider

    def _build_app(self) -> Starlette:
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
        return Starlette(routes=routes)

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
        cid = html.escape(request.query_params.get("id", ""))
        return HTMLResponse(_PAGE.format(rows="".join(rows), cid=cid))

    async def _consent_submit(self, request: Request) -> Response:
        form = await request.form()
        consent_id = str(form.get("id", ""))
        decision = str(form.get("decision", "deny"))
        try:
            if decision == "allow":
                target = self._provider.approve(consent_id)
            else:
                target = self._provider.deny(consent_id)
        except ConsentExpired:
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
        engine_token = self.upstream_token_source()

        # --- resolve proxy token (same as before) ---
        proxy_token: str | None = None
        if bearer:
            if _constant_time_eq(bearer, engine_token):
                proxy_token = bearer
            else:
                record = await self._provider.load_access_token(bearer)
                if record is not None:
                    if record.resource and record.resource.rstrip("/") != self.resource_url:
                        return self._unauthorized()
                    if engine_token:
                        proxy_token = engine_token
                    else:
                        return self._unauthorized()
                else:
                    return self._unauthorized()
        elif self.allow_local_anonymous and _is_loopback(request):
            proxy_token = None
        else:
            return self._unauthorized()

        # --- intercept local Python tool calls ---
        if request.method == "POST":
            try:
                rpc = json.loads(body)
            except json.JSONDecodeError:
                rpc = None
            if isinstance(rpc, dict) and rpc.get("method") == "tools/call":
                params = rpc.get("params") or {}
                name = params.get("name", "")
                if name in _LOCAL_TOOL_NAMES:
                    return await self._exec_local_tool(name, rpc, params)

        return await self._proxy(request, body, proxy_token)

    async def _proxy(self, request: Request, body: bytes, authorization: str | None) -> Response:
        target = f"{self.upstream_url}{request.url.path}"
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
                return Response(content=rewritten, status_code=upstream.status_code, headers=filtered)
            if b"initialize" in body:
                await upstream.aread()
                rewritten = _rewrite_server_identity(upstream.content)
                return Response(content=rewritten, status_code=upstream.status_code, headers=filtered)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=filtered,
        )

    # ---------------------------------------------------- local tools
    async def _exec_local_tool(self, name: str, rpc: dict[str, Any], params: dict[str, Any]) -> JSONResponse:
        rpc_id = rpc.get("id")
        workspace = self._workspace or Path.cwd()
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
            else:
                raise ValueError(f"未知的本地工具: {name}")
        except ValueError as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32602, str(exc)))
        except Exception as exc:
            return JSONResponse(_jsonrpc_error(rpc_id, -32603, f"工具执行失败: {exc}"))

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