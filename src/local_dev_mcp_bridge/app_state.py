"""Service coordinator: orchestrates CodexPro + Windows-MCP + tunnel.

Pure-Python state machine (no Qt) so transitions, ordering, URL immutability
and failure cleanup are unit-testable. The Qt window consumes this module.

States (EngineState):
    IDLE -> STARTING -> READY -> STOPPING -> IDLE
                      \\-> ERROR (cleanup restores IDLE-ready managers)

Rules enforced here:
    * Start requires a project + a valid codexpro token.
    * ``public_url`` is computed ONCE per session from the fixed hostname and
      never changes on restart (named / ngrok / local). Quick tunnel is the
      single exception and is flagged ``url_mutable=True``.
    * `windows` bridge starts only when enabled; if it fails, the whole
      service goes to ERROR (never a half-started service).
    * Stop order: Codex first, then bridge, then tunnel. Secret tokens are
      never copied to any state/log.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .engines import (
    CodexProManager,
    EngineState,
    SpawnError,
    WindowsBridgeManager,
    port_listening,
)
from .gateway import OAuthGateway
from .tunnel_manager import ConnectionMethod, TunnelManager

StateCallback = Callable[[EngineState, str | None], None]  # state, message


@dataclass
class StartOptions:
    """Everything needed for one start() call (created by the UI)."""

    project_root: str
    permission_mode: str = "workspace"
    codex_token: str = ""
    windows_enabled: bool = False
    windows_token: str = ""
    connection: ConnectionMethod = ConnectionMethod.LOCAL
    public_hostname: str = ""
    cloudflare_config: Path | None = None
    tunnel_token: str | None = None
    # 端口（默认值集中在 constants.DEFAULT_*_PORT）
    gateway_port: int = 0
    codexpro_port: int = 0
    windows_mcp_port: int = 0

    def __post_init__(self) -> None:
        from . import constants as _c

        if not self.gateway_port:
            self.gateway_port = _c.DEFAULT_GATEWAY_PORT
        if not self.codexpro_port:
            self.codexpro_port = _c.DEFAULT_CODEXPRO_PORT
        if not self.windows_mcp_port:
            self.windows_mcp_port = _c.DEFAULT_WINDOWS_MCP_PORT

    @property
    def url_mutable(self) -> bool:
        return self.connection == ConnectionMethod.QUICK


class ServiceCoordinator:
    """State machine owning the three subprocess managers."""

    def __init__(
        self,
        *,
        codex: CodexProManager | None = None,
        windows: WindowsBridgeManager | None = None,
        tunnel: TunnelManager | None = None,
    ) -> None:
        self.codex = codex or CodexProManager()
        self.windows = windows or WindowsBridgeManager()
        self.tunnel = tunnel or TunnelManager()
        self.gateway: OAuthGateway | None = None
        self._lock = threading.Lock()
        self._state = EngineState.IDLE
        self._message: str | None = None
        self._public_url: str = ""
        self._url_mutable = False
        self._listeners: list[StateCallback] = []

    # --------------------------------------------------------- listeners
    def listen(self, callback: StateCallback) -> None:
        self._listeners.append(callback)

    def _emit(self, state: EngineState, message: str | None) -> None:
        for callback in list(self._listeners):
            with contextlib.suppress(Exception):
                callback(state, message)

    # ---------------------------------------------------------- state
    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._state

    @property
    def message(self) -> str | None:
        with self._lock:
            return self._message

    @property
    def public_url(self) -> str:
        with self._lock:
            return self._public_url

    @property
    def url_mutable(self) -> bool:
        with self._lock:
            return self._url_mutable

    @property
    def running(self) -> bool:
        return self.state in (EngineState.STARTING, EngineState.READY)

    def component_states(self) -> dict[str, EngineState]:
        states: dict[str, EngineState] = {
            "tunnel": self.tunnel.state,
            "codex": self.codex.state,
            "windows": self.windows.state,
        }
        if self.gateway is not None:
            states["gateway"] = EngineState.READY if self.gateway is not None else EngineState.ERROR
        return states

    def _start_gateway(self, options: StartOptions) -> None:
        """Public modes get the OAuth gateway on the configured loopback port
        before the tunnel is considered ready; the Cloudflare route targets it."""
        from .constants import ACCESS_TOKEN_CRED_NAME
        from .secrets import SecretsStore

        public = (self.tunnel.public_url or "").split("/")[2] if self.tunnel.public_url else ""
        hostname = public or options.public_hostname
        if not hostname:
            raise SpawnError("缺少公网域名。")
        self.gateway = OAuthGateway(
            public_hostname=hostname,
            workspace=options.project_root,
            upstream_url=f"http://127.0.0.1:{options.codexpro_port}",
            upstream_legacy_token=lambda: SecretsStore().get(ACCESS_TOKEN_CRED_NAME),
        )
        self.gateway.start(port=options.gateway_port)
        if not self._wait_gateway_ready(options.gateway_port):
            self.gateway.stop()
            self.gateway = None
            raise SpawnError("OAuth 网关启动失败（端口被占用？）。")

    @staticmethod
    def _wait_gateway_ready(port: int, timeout_seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if port_listening(port):
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _check_engine_port(name: str, port: int) -> None:
        """Refuse to start on an occupied internal port (no silent retry)."""
        if port_listening(port):
            raise SpawnError(f"{name} 端口 {port} 已被占用，无法启动。请先停止占用该端口的程序。")

    # -------------------------------------------------------- lifecycle
    def start(self, options: StartOptions) -> None:
        if self.running:
            raise SpawnError("服务已在启动或运行中。")
        if not options.project_root:
            raise SpawnError("请先选择项目目录。")
        if not Path(options.project_root).is_dir():
            raise SpawnError(f"项目目录不存在：{options.project_root}")
        if len(options.codex_token) < 24:
            raise SpawnError("访问令牌未生成或长度不足（至少 24 字节）。")
        if options.windows_enabled and len(options.windows_token) < 24:
            raise SpawnError("Windows 控制令牌未生成。")

        # 端口占用预检（引擎子进程绑定失败即崩溃；占用即报错，绝不偷偷换端口）。
        self._check_engine_port("CodexPro", options.codexpro_port)
        if options.windows_enabled:
            self._check_engine_port("Windows 控制桥", options.windows_mcp_port)

        self._set_state(EngineState.STARTING, "正在启动服务…")
        try:
            self.codex.port = options.codexpro_port
            self.windows.port = options.windows_mcp_port
            self.tunnel.port = options.codexpro_port
            if options.connection != ConnectionMethod.LOCAL:
                self.tunnel.start(
                    kind=options.connection,
                    hostname=options.public_hostname,
                    cloudflare_config=options.cloudflare_config,
                    tunnel_token=options.tunnel_token,
                )
                if not self.tunnel.wait_ready():
                    raise SpawnError("隧道建立失败。")
                self._set_url(self.tunnel.public_url, options.url_mutable)
            else:
                self._set_url("", False)

            self.codex.start(
                options.project_root,
                options.codex_token,
                permission_mode=options.permission_mode,
                windows_token=options.windows_token if options.windows_enabled else None,
                extra_env=(
                    {"CODEXPRO_WINDOWS_BRIDGE_URL": f"http://127.0.0.1:{options.windows_mcp_port}/mcp"}
                    if options.windows_enabled
                    else None
                ),
            )
            if not self.codex.wait_ready():
                raise SpawnError(self.codex.error or "Codex 引擎启动失败。")

            if options.connection != ConnectionMethod.LOCAL:
                self._start_gateway(options)

            if options.windows_enabled:
                self.windows.start(options.windows_token)
                if not self.windows.wait_ready():
                    raise SpawnError(self.windows.error or "Windows 桥接启动失败。")

            self._set_state(EngineState.READY, "服务已启动。")
        except Exception as exc:
            message = str(exc)
            self._cleanup_after_failure()
            self._set_state(EngineState.ERROR, message)

    def stop(self, message: str = "正在停止服务…") -> None:
        if self.state == EngineState.IDLE:
            return
        self._set_state(EngineState.STOPPING, message)
        self.stop_callable()

    def stop_callable(self) -> None:
        """Stop order: Codex -> Windows -> gateway -> tunnel (best-effort)."""
        self._set_state(EngineState.STOPPING, "正在停止服务…")
        if self.codex.is_running:
            self.codex.stop()
        if self.windows.is_running:
            self.windows.stop()
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        self._set_state(EngineState.IDLE, None)

    def restart(self, options: StartOptions) -> None:
        """Stop then start, keeping the fixed public URL from the session."""
        self.stop()
        self.start(options)

    def _cleanup_after_failure(self) -> None:
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        if self.windows.is_running:
            self.windows.stop()
        if self.codex.is_running:
            self.codex.stop()

    def _set_state(self, state: EngineState, message: str | None = None) -> None:
        with self._lock:
            self._set_state_locked(state, message)
        self._emit(state, message)

    def _set_state_locked(self, state: EngineState, message: str | None = None) -> None:
        self._state = state
        self._message = message

    def _set_url(self, url: str, mutable: bool) -> None:
        with self._lock:
            self._public_url = url
            self._url_mutable = mutable


class TunnelError(RuntimeError):
    """Raised when a tunnel fails to establish."""


__all__ = ["ServiceCoordinator", "StartOptions", "TunnelError"]