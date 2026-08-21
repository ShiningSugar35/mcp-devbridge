"""Shared Hub transport coordinator.

Project engines are owned by :mod:`project_manager`. This coordinator owns
only the shared Gateway/Tunnel transport so no project can become a privileged
entry or bootstrap root.

States (EngineState):
    IDLE -> STARTING -> READY -> STOPPING -> IDLE
                      \\-> ERROR

Rules enforced here:
    * Hub startup is independent from every project root and engine port.
    * Local mode also uses the shared loopback Gateway so one local MCP URL can
      auto-route across every running project root.
    * Public modes point the tunnel at that same shared Gateway.
    * Stopping the Hub never stops project engines; ProjectManager owns them.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .device_hub import DeviceRegistry
from .engines import EngineState, SpawnError, port_listening
from .gateway import OAuthGateway
from .tunnel_manager import ConnectionMethod, TunnelManager

StateCallback = Callable[[EngineState, str | None], None]


@dataclass
class StartOptions:
    """Shared Hub transport settings for one start() call."""

    connection: ConnectionMethod = ConnectionMethod.LOCAL
    public_hostname: str = ""
    cloudflare_config: Path | None = None
    tunnel_token: str | None = None
    gateway_port: int = 0

    def __post_init__(self) -> None:
        from . import constants as _c

        if not self.gateway_port:
            self.gateway_port = _c.DEFAULT_GATEWAY_PORT

    @property
    def url_mutable(self) -> bool:
        return self.connection == ConnectionMethod.QUICK


class ServiceCoordinator:
    """State machine owning only the shared Gateway/Tunnel Hub."""

    def __init__(
        self,
        *,
        tunnel: TunnelManager | None = None,
        workspace_registry: Callable[[str], tuple[int, str] | None] | None = None,
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        device_registry: DeviceRegistry | None = None,
        local_device_id: str = "",
    ) -> None:
        self.tunnel = tunnel or TunnelManager()
        self.gateway: OAuthGateway | None = None
        self._workspace_registry = workspace_registry
        self._workspace_credential_registry = workspace_credential_registry
        self._device_registry = device_registry
        self._local_device_id = local_device_id
        self._lock = threading.Lock()
        self._state = EngineState.IDLE
        self._message: str | None = None
        self._public_url: str = ""
        self._url_mutable = False
        self._listeners: list[StateCallback] = []

    def listen(self, callback: StateCallback) -> None:
        self._listeners.append(callback)

    def _emit(self, state: EngineState, message: str | None) -> None:
        for callback in list(self._listeners):
            with contextlib.suppress(Exception):
                callback(state, message)

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
        states: dict[str, EngineState] = {"tunnel": self.tunnel.state}
        if self.gateway is not None:
            states["gateway"] = EngineState.READY if self.gateway.is_running else EngineState.ERROR
        return states

    def _start_gateway(self, options: StartOptions) -> None:
        from .constants import ACCESS_TOKEN_CRED_NAME
        from .secrets import SecretsStore

        if options.connection == ConnectionMethod.LOCAL:
            hostname = f"http://127.0.0.1:{options.gateway_port}"
        else:
            public = (self.tunnel.public_url or "").strip()
            if public:
                from urllib.parse import urlsplit

                parsed = urlsplit(public if "://" in public else f"https://{public}")
                hostname = parsed.netloc
            else:
                hostname = options.public_hostname.strip()
            if not hostname:
                raise SpawnError("缺少公网域名。")

        self.gateway = OAuthGateway(
            public_hostname=hostname,
            workspace="",
            upstream_url="",
            upstream_legacy_token=lambda: SecretsStore().get(ACCESS_TOKEN_CRED_NAME),
            workspace_registry=self._workspace_registry,
            workspace_credential_registry=self._workspace_credential_registry,
            device_registry=self._device_registry,
            local_device_id=self._local_device_id,
        )
        self.gateway.start(port=options.gateway_port)
        if not self._wait_gateway_ready(options.gateway_port):
            self.gateway.stop()
            self.gateway = None
            raise SpawnError("共享 Gateway 启动失败（端口被占用？）。")

    @staticmethod
    def _wait_gateway_ready(port: int, timeout_seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if port_listening(port):
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _check_gateway_port(port: int) -> None:
        if port_listening(port):
            raise SpawnError(f"共享 Gateway 端口 {port} 已被占用，无法启动。请先停止占用该端口的程序。")

    def start(self, options: StartOptions) -> None:
        if self.running:
            raise SpawnError("Hub 已在启动或运行中。")
        self._check_gateway_port(options.gateway_port)
        self._set_state(EngineState.STARTING, "正在启动共享 Hub…")
        try:
            self.tunnel.port = options.gateway_port
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
                from . import constants as _c

                self._set_url(
                    f"http://127.0.0.1:{options.gateway_port}{_c.DEFAULT_MCP_PATH}",
                    False,
                )

            self._start_gateway(options)
            self._set_state(EngineState.READY, "共享 Hub 已启动。")
        except Exception as exc:
            message = str(exc)
            self._cleanup_after_failure()
            self._set_state(EngineState.ERROR, message)

    def stop(self, message: str = "正在停止共享 Hub…") -> None:
        if self.state == EngineState.IDLE:
            return
        self._set_state(EngineState.STOPPING, message)
        self.stop_callable()

    def stop_callable(self) -> None:
        """Stop shared transport only; project engines are owned elsewhere."""
        self._set_state(EngineState.STOPPING, "正在停止共享 Hub…")
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        self._set_url("", False)
        self._set_state(EngineState.IDLE, None)

    def restart(self, options: StartOptions) -> None:
        self.stop()
        self.start(options)

    def _cleanup_after_failure(self) -> None:
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        self._set_url("", False)

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