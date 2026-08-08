"""Build the MCP backend: MCPServer + Starlette app with auth/health/control."""

from __future__ import annotations

import collections
import hmac
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, constants
from .audit import AuditLogger
from .models import RuntimeConfig
from .secrets import SecretsStore
from .tools import LocalDevTools

_REQUEST_ID_KEY = "request_id"

# DNS rebinding protection stays ON; only the published tunnel domain is
# added to the Host allow-list (方案 B). Loopback hosts are always allowed.
TRANSPORT_LOOPBACK_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def build_transport_security(public_hostname: str = "") -> TransportSecuritySettings:
    """Settings for ``streamable_http_app``: blocking rebinding attacks while
    accepting the tunneled public Host header."""
    allowed_hosts = list(TRANSPORT_LOOPBACK_HOSTS)
    hostname = (public_hostname or "").strip()
    for scheme in ("https://", "http://"):
        if hostname.startswith(scheme):
            hostname = hostname[len(scheme):]
            break
    hostname = hostname.split("/", 1)[0].rstrip("/").strip()
    if hostname:
        allowed_hosts.append(hostname)
        allowed_hosts.append(f"{hostname}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
    )


# ---------------------------------------------------------------------------
# Rate limiter for auth failures
# ---------------------------------------------------------------------------


class AuthRateLimiter:
    def __init__(
        self,
        limit: int = constants.AUTH_FAIL_LIMIT,
        window_seconds: int = constants.AUTH_FAIL_WINDOW_SECONDS,
        lockout_seconds: int = constants.AUTH_LOCKOUT_SECONDS,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self.lockout = lockout_seconds
        self._failures: dict[str, collections.deque[float]] = {}
        self._lockouts: dict[str, float] = {}
        self._lock = threading.Lock()

    def record_failure(self, key: str, now: float | None = None) -> bool:
        """Return True when this failure pushes the client into lockout."""
        now = now or time.monotonic()
        with self._lock:
            q = self._failures.setdefault(key, collections.deque())
            cutoff = now - self.window
            while q and q[0] < cutoff:
                q.popleft()
            q.append(now)
            if len(q) >= self.limit:
                self._lockouts[key] = now + self.lockout
                q.clear()
                return True
        return False

    def locked_until(self, key: str, now: float | None = None) -> float:
        now = now or time.monotonic()
        with self._lock:
            until = self._lockouts.get(key, 0.0)
            if until <= now:
                self._lockouts.pop(key, None)
                return 0.0
            return until


# ---------------------------------------------------------------------------
# MCP server middleware: per-request audit logging
# ---------------------------------------------------------------------------


class AuditMiddleware:
    """ServerMiddleware recording every request to the JSONL audit log."""

    def __init__(self, logger: AuditLogger | None = None, workspace: str = "", permission_mode: str = "") -> None:
        self.logger = logger or AuditLogger()
        self.workspace = workspace
        self.permission_mode = permission_mode

    async def __call__(self, ctx: Any, call_next: Callable) -> Any:
        start = time.monotonic()
        method = getattr(ctx, "method", "")
        request_id = getattr(ctx, "request_id", None)
        params = getattr(ctx, "params", None) or {}
        client_name = self._client_name(ctx)
        success = True
        error_type: str | None = None
        tool_name: str | None = None
        parameter_summary: dict[str, Any] | None = None
        try:
            result = await call_next(ctx)
            if method == "tools/call" and isinstance(params, dict):
                tool_name = params.get("name")
                parameter_summary = params.get("arguments") or {}
                is_error = bool(getattr(result, "is_error", False))
                if is_error:
                    success = False
                    error_type = "tool_error"
            return result
        except Exception as exc:
            success = False
            error_type = type(exc).__name__
            raise
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            if method == "tools/call":
                self.logger.log_tool_call(
                    request_id=str(request_id) if request_id is not None else None,
                    client_name=client_name,
                    tool_name=tool_name or "",
                    parameters=parameter_summary,
                    workspace=self.workspace,
                    permission_mode=self.permission_mode,
                    duration_ms=duration_ms,
                    success=success,
                    error_type=error_type,
                )

    @staticmethod
    def _client_name(ctx: Any) -> str | None:
        request = getattr(ctx, "request", None)
        if request is None:
            return None
        try:
            ua = request.headers.get("user-agent")
            return ua[:120] if ua else None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# HTTP auth middleware
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str:
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first
    client = request.client
    return client.host if client else "unknown"


def _is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")


def _token_equals(given: str, expected: str | None) -> bool:
    if not expected:
        return False
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class AuthMiddleware:
    """Pure-ASGI auth gate (must not buffer streaming/SSE responses)."""

    def __init__(
        self,
        app: Any,
        *,
        allow_local_anonymous: bool,
        require_public_bearer: bool,
        secrets: SecretsStore,
        limiter: AuthRateLimiter,
    ) -> None:
        self.app = app
        self.allow_local_anonymous = allow_local_anonymous
        self.require_public_bearer = require_public_bearer
        self.secrets = secrets
        self.limiter = limiter

    def _token(self) -> str | None:
        try:
            return self.secrets.get(constants.ACCESS_TOKEN_CRED_NAME)
        except Exception:
            return None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if path == "/health":
            await self.app(scope, receive, send)
            return

        client_key = _client_ip(request)
        locked = self.limiter.locked_until(client_key)
        if locked > 0:
            response = JSONResponse(
                {"error": "rate_limited", "retry_after": int(locked - time.monotonic())},
                status_code=429,
                headers={"Retry-After": str(int(locked - time.monotonic()))},
            )
            await response(scope, receive, send)
            return

        local = _is_loopback(client_key)
        is_control = path.startswith("/control/")

        if is_control:
            if not self._check_bearer(request):
                self._record_failure(client_key)
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return

        if local:
            if self.allow_local_anonymous:
                await self.app(scope, receive, send)
                return
        elif not self.require_public_bearer:
            await self.app(scope, receive, send)
            return

        if self._check_bearer(request):
            await self.app(scope, receive, send)
            return
        self._record_failure(client_key)
        response = JSONResponse(
            {
                "error": "unauthorized",
                "message": "需要有效的 Bearer 访问令牌。请在客户端配置 Authorization: Bearer <token>。",
            },
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)

    def _check_bearer(self, request: Request) -> bool:
        expected = self._token()
        if expected is None:
            return False
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            given = auth_header[7:].strip()
            if _token_equals(given, expected):
                return True
        given = request.headers.get("x-api-key", "")
        return bool(given) and _token_equals(given, expected)

    def _record_failure(self, key: str) -> None:
        self.limiter.record_failure(key)


# ---------------------------------------------------------------------------
# Application assembly
# ---------------------------------------------------------------------------


def build_backend(rc: RuntimeConfig) -> tuple[MCPServer, Starlette, LocalDevTools]:
    """Build the MCP server, tools instance and the HTTP app for a runtime config."""
    workspace = Path(rc.workspace)
    tools = LocalDevTools(
        workspace,
        rc.permission_mode,
        test_command=rc.test_command,
        lint_command=rc.lint_command,
        typecheck_command=rc.typecheck_command,
        build_command=rc.build_command,
        shell=rc.shell,
        ignore_patterns=rc.ignore_patterns,
        max_file_bytes=rc.max_file_bytes,
    )

    logger = AuditLogger(directory=Path(rc.log_dir) if rc.log_dir else None)
    mcp = MCPServer(
        "mcp-devbridge",
        title="MCP DevBridge",
        version=__version__,
        instructions=(
            "你连接的是用户本机的开发环境。工具操作默认限于当前项目目录。"
            "写文件、改代码、执行命令前先读取相关文件；不要修改 .git、.env、凭据与虚拟环境文件；"
            "不要在没有明确授权的情况下向远程仓库推送。修改后用配置的测试/检查命令验证。"
            "工具返回中文说明。"
        ),
        middleware=[AuditMiddleware(logger=logger, workspace=str(workspace), permission_mode=rc.permission_mode)],
    )

    for name, title, description, fn, tool_annotations in _register_tools(tools):
        mcp.add_tool(
            fn,
            name=name,
            title=title,
            description=description,
            annotations=tool_annotations,
        )

    mcp_app = mcp.streamable_http_app(
        streamable_http_path=constants.DEFAULT_MCP_PATH,
        transport_security=build_transport_security(rc.public_hostname),
    )

    def _health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "app": "mcp-devbridge",
                "version": __version__,
                "workspace": str(workspace),
                "permission_mode": rc.permission_mode,
                "uptime_seconds": round(time.monotonic() - _START_TIME, 1),
                "managed_processes": len(tools.registry.list()),
                "pid": os.getpid(),
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    def _control_status(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "workspace": str(workspace),
                "permission_mode": rc.permission_mode,
                "uptime_seconds": round(time.monotonic() - _START_TIME, 1),
                "managed_processes": tools.registry.list(),
                "client_sessions": _session_count(mcp),
                "pid": os.getpid(),
            }
        )

    async def _control_stop(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        force = bool(payload.get("force"))
        results = tools.registry.stop_all(force=force)
        logger.raw("control_stop_all", count=len(results), force=force)
        return JSONResponse({"stopped": results})

    # The MCP app runs its session-manager through its own lifespan; keep it as
    # the root application and only add extra routes/middleware around it.
    mcp_app.add_route("/health", _health)
    mcp_app.add_route("/control/status", _control_status)
    mcp_app.add_route("/control/stop-procs", _control_stop, methods=["POST"])

    wrapped = AuthMiddleware(
        mcp_app,
        allow_local_anonymous=rc.allow_local_anonymous,
        require_public_bearer=rc.require_public_bearer,
        secrets=SecretsStore(),
        limiter=AuthRateLimiter(),
    )
    return mcp, wrapped, tools


_START_TIME = time.monotonic()


def _session_count(mcp: MCPServer) -> int:
    try:
        manager = mcp.session_manager
        instances = getattr(manager, "_server_instances", None)
        return len(instances) if instances is not None else 0
    except Exception:
        return 0


def _register_tools(tools: LocalDevTools) -> list[tuple[str, str, str, Callable, Any]]:
    """(name, title, description, callable, annotations) for each tool."""
    from .tools import destructive_tool, read_tool, write_tool

    items: list[tuple[str, str, str, Callable, Any]] = []
    read = read_tool
    write = write_tool
    destructive = destructive_tool

    items.extend(
        [
            ("get_workspace_info", "获取项目信息", "返回项目根目录、权限模式、Git 分支、工具版本与默认测试命令。", tools.get_workspace_info, read()),
            ("get_capabilities", "获取能力清单", "返回当前允许的工具类型、写入/命令/项目外访问开关与输出限制。", tools.get_capabilities, read()),
            ("get_system_info", "获取系统信息", "返回 Windows 版本、CPU 架构、磁盘空间与常用开发工具版本。", tools.get_system_info, read()),
            ("get_environment_variable", "读取环境变量", "读取单个环境变量（名称含 KEY/TOKEN/SECRET/PASSWORD/COOKIE/AUTH 时值被遮罩）。", tools.get_environment_variable, read()),
            ("list_directory", "列出目录", "列出目录内容，可选递归；返回项目相对路径。", tools.list_directory, read()),
            ("stat_path", "路径状态", "返回文件或目录的类型、大小、修改时间、只读与符号链接状态。", tools.stat_path, read()),
            ("read_file", "读取文件", "读取文本文件（自动尝试 UTF-8/UTF-8 BOM/GB18030），带行号，默认最多 400 行。", tools.read_file, read()),
            ("read_files", "批量读取文件", "一次读取多个小文件（最多 20 个，单文件 ≤64KB，总输出 ≤60000 字符）。", tools.read_files, read()),
            ("find_files", "查找文件", "按 glob、文件名关键词、扩展名查找文件；默认排除 .git/.venv/node_modules 等。", tools.find_files, read()),
            ("search_text", "搜索文本", "在文本文件中搜索字符串或正则，返回 文件:行号: 内容。", tools.search_text, read()),
            ("write_file", "写入文件", "写入文本文件（原子写入）；overwrite=true 才覆盖；可用 expected_sha256 校验现状。", tools.write_file, write()),
            ("replace_text", "替换文本", "精确文本替换；匹配数必须等于 expected_count 才执行。", tools.replace_text, write()),
            ("apply_patch", "应用补丁", "应用 unified diff（校验上下文；任一 hunk 不匹配则全部不修改）。", tools.apply_patch, write()),
            ("make_directory", "创建目录", "创建目录（自动创建父目录）。", tools.make_directory, write()),
            ("copy_path", "复制", "复制文件或目录。", tools.copy_path, write()),
            ("move_path", "移动", "移动文件或目录。", tools.move_path, write()),
            ("delete_path", "删除", "删除文件或目录（目录需 recursive=true）。", tools.delete_path, destructive()),
            ("git_status", "Git 状态", "显示 Git 工作区状态（只读）。", tools.git_status, read()),
            ("git_diff", "Git 差异", "显示 Git 差异，可选 staged 或指定文件。", tools.git_diff, read()),
            ("git_log", "Git 历史", "显示 Git 提交历史（只读）。", tools.git_log, read()),
            ("git_branch", "Git 分支", "查看(list)或创建(create) Git 分支。", tools.git_branch, write()),
            ("git_add", "Git 暂存", "将文件加入 Git 暂存区。", tools.git_add, write()),
            ("git_commit", "Git 提交", "创建 Git 提交。", tools.git_commit, write()),
            ("git_checkout", "Git 切换", "切换 Git 分支或提交（会改变工作区状态）。", tools.git_checkout, write()),
            ("git_restore", "Git 恢复", "恢复文件，会丢弃未提交更改（staged 恢复从暂存区）。", tools.git_restore, destructive()),
            ("git_push", "Git 推送", "推送提交到远程仓库（高风险写操作）。", tools.git_push, destructive()),
            ("run_command", "执行命令", "执行 PowerShell 命令；返回 shell、退出码与 stdout/stderr；超时默认 10 分钟。", tools.run_command, write()),
            ("run_program", "运行程序", "直接运行程序（参数数组，不经 shell 解析）。", tools.run_program, write()),
            ("start_process", "启动进程", "启动长期运行进程（dev server 等），返回 process_id 并跟踪日志。", tools.start_process, write()),
            ("poll_process", "查询进程", "返回管理进程的增量输出与状态。", tools.poll_process, read()),
            ("stop_process", "停止进程", "停止管理进程及其子进程树。", tools.stop_process, write()),
            ("list_managed_processes", "列出进程", "列出所有受管进程。", tools.list_managed_processes, read()),
            ("stop_all_managed_processes", "停止全部进程", "停止全部受管进程（含子进程树）。", tools.stop_all_managed_processes, destructive()),
        ]
    )
    return items