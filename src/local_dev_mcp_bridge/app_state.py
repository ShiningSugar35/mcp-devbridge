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
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from .device_hub import DeviceRegistry
from .engines import EngineState, SpawnError, port_listening
from .gateway import OAuthGateway
from .power_guard import SystemAwakeGuard
from .tunnel_manager import ConnectionMethod, TunnelManager

StateCallback = Callable[[EngineState, str | None], None]

TRANSPORT_HEALTH_INITIAL_DELAY_SECONDS = 5.0
TRANSPORT_HEALTH_INTERVAL_SECONDS = 15.0
TRANSPORT_HEALTH_TIMEOUT_SECONDS = 5.0
TRANSPORT_HEALTH_TTFB_LIMIT_SECONDS = 5.0
TRANSPORT_HEALTH_FAILURE_THRESHOLD = 3
TRANSPORT_RESTART_COOLDOWN_SECONDS = 60.0
GATEWAY_HEALTH_FAILURE_THRESHOLD = 2
GATEWAY_RESTART_COOLDOWN_SECONDS = 30.0


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
        awake_guard: SystemAwakeGuard | None = None,
        workspace_registry: Callable[[str], tuple[int, str] | None] | None = None,
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        device_registry: DeviceRegistry | None = None,
        local_device_id: str = "",
    ) -> None:
        self.tunnel = tunnel or TunnelManager()
        self.awake_guard = awake_guard or SystemAwakeGuard()
        self.gateway: OAuthGateway | None = None
        self._workspace_registry = workspace_registry
        self._workspace_credential_registry = workspace_credential_registry
        self._device_registry = device_registry
        self._local_device_id = local_device_id
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._state = EngineState.IDLE
        self._message: str | None = None
        self._public_url: str = ""
        self._url_mutable = False
        self._listeners: list[StateCallback] = []
        self._active_options: StartOptions | None = None
        self._transport_stop = threading.Event()
        self._transport_thread: threading.Thread | None = None
        self._transport_failures = 0
        self._last_transport_restart = 0.0
        self._gateway_failures = 0
        self._last_gateway_restart = 0.0
        self._transport_probe = self._probe_health_url

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
        with self._lifecycle_lock:
            if self.running:
                if self._same_transport_options(self._active_options, options):
                    return
                raise SpawnError("Hub 已使用不同连接配置启动；请先停止后再切换连接配置。")
            self._active_options = options
            self._start_once(options)
            if self.state == EngineState.READY:
                self._start_transport_monitor(options)
            else:
                self._active_options = None

    def _start_once(self, options: StartOptions) -> None:
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
            if options.connection != ConnectionMethod.LOCAL:
                awake_ok = self.awake_guard.start()
                self._write_transport_health(
                    event="awake_guard_started" if awake_ok else "awake_guard_failed",
                    error=self.awake_guard.last_error if not awake_ok else "",
                )
            self._set_state(EngineState.READY, "共享 Hub 已启动。")
        except Exception as exc:
            message = str(exc)
            self._cleanup_after_failure()
            self._set_state(EngineState.ERROR, message)

    def stop(self, message: str = "正在停止共享 Hub…") -> None:
        with self._lifecycle_lock:
            if self.state == EngineState.IDLE:
                return
            self._set_state(EngineState.STOPPING, message)
            self.stop_callable()

    def stop_callable(self) -> None:
        """Stop shared transport only; project engines are owned elsewhere."""
        self._set_state(EngineState.STOPPING, "正在停止共享 Hub…")
        self._stop_transport_monitor()
        self.awake_guard.stop()
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        self._active_options = None
        self._transport_failures = 0
        self._gateway_failures = 0
        self._set_url("", False)
        self._set_state(EngineState.IDLE, None)

    def restart(self, options: StartOptions) -> None:
        with self._lifecycle_lock:
            self.stop()
            self.start(options)

    def _cleanup_after_failure(self) -> None:
        self._stop_transport_monitor()
        self.awake_guard.stop()
        if self.gateway is not None:
            self.gateway.stop()
            self.gateway = None
        if self.tunnel.is_running:
            self.tunnel.stop()
        self._active_options = None
        self._set_url("", False)

    @staticmethod
    def _same_transport_options(left: StartOptions | None, right: StartOptions) -> bool:
        if left is None:
            return False
        return (
            left.connection == right.connection
            and left.public_hostname.strip().rstrip("/") == right.public_hostname.strip().rstrip("/")
            and left.gateway_port == right.gateway_port
            and left.cloudflare_config == right.cloudflare_config
        )

    @staticmethod
    def _health_url_from_public_mcp(public_url: str) -> str:
        value = public_url.strip().rstrip("/")
        if value.endswith("/mcp"):
            value = value[:-4]
        return f"{value}/health"

    @staticmethod
    def _probe_health_url(url: str, timeout_seconds: float) -> tuple[bool, float, int]:
        started = time.monotonic()
        status = 0
        try:
            req = urllib_request.Request(url, headers={"User-Agent": "MCPDevBridge-transport-health"})
            with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
                status = int(getattr(response, "status", 0) or response.getcode() or 0)
                response.read(1)
            elapsed = time.monotonic() - started
            return 200 <= status < 300, elapsed, status
        except (OSError, TimeoutError, urllib_error.URLError):
            return False, time.monotonic() - started, status

    def _write_transport_health(self, **fields: object) -> None:
        from . import constants as _c

        entry = {**fields, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            _c.LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = _c.LOG_DIR / f"transport-health-{time.strftime('%Y-%m-%d')}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _transport_health_tick(self) -> None:
        options = self._active_options
        if options is None or options.connection == ConnectionMethod.LOCAL or self.state != EngineState.READY:
            return
        local_url = f"http://127.0.0.1:{options.gateway_port}/health"
        local_ok, local_ttfb, local_status = self._transport_probe(
            local_url, TRANSPORT_HEALTH_TIMEOUT_SECONDS
        )
        if not local_ok:
            self._gateway_failures += 1
            self._write_transport_health(
                event="gateway_unhealthy",
                local_status=local_status,
                local_ttfb_ms=round(local_ttfb * 1000),
                consecutive_gateway_failures=self._gateway_failures,
            )
            self._transport_failures = 0
            if self._gateway_failures < GATEWAY_HEALTH_FAILURE_THRESHOLD:
                return
            now = time.monotonic()
            if now - self._last_gateway_restart < GATEWAY_RESTART_COOLDOWN_SECONDS:
                return
            self._restart_gateway()
            return
        self._gateway_failures = 0
        public_health = self._health_url_from_public_mcp(self.public_url)
        public_ok, public_ttfb, public_status = self._transport_probe(
            public_health, TRANSPORT_HEALTH_TIMEOUT_SECONDS
        )
        degraded = (not public_ok) or public_ttfb > TRANSPORT_HEALTH_TTFB_LIMIT_SECONDS
        self._transport_failures = self._transport_failures + 1 if degraded else 0
        self._write_transport_health(
            event="transport_probe",
            local_status=local_status,
            local_ttfb_ms=round(local_ttfb * 1000),
            public_status=public_status,
            public_ttfb_ms=round(public_ttfb * 1000),
            degraded=degraded,
            consecutive_failures=self._transport_failures,
        )
        if self._transport_failures < TRANSPORT_HEALTH_FAILURE_THRESHOLD:
            return
        now = time.monotonic()
        if now - self._last_transport_restart < TRANSPORT_RESTART_COOLDOWN_SECONDS:
            return
        self._restart_public_tunnel()

    def _restart_gateway(self) -> None:
        """Recover only the shared loopback Gateway; project engines/tunnel stay up."""
        with self._lifecycle_lock:
            options = self._active_options
            if options is None or self.state != EngineState.READY:
                return
            self._last_gateway_restart = time.monotonic()
            self._write_transport_health(event="gateway_restart", reason="consecutive_local_probe_failures")
            old_gateway = self.gateway
            try:
                if old_gateway is not None:
                    old_gateway.stop()
                self.gateway = None
                self._start_gateway(options)
                self._gateway_failures = 0
                self._write_transport_health(event="gateway_restart_ok")
            except Exception as exc:
                if self.gateway is not None:
                    with contextlib.suppress(Exception):
                        self.gateway.stop()
                self.gateway = None
                self._write_transport_health(
                    event="gateway_restart_failed",
                    error=type(exc).__name__,
                    detail=str(exc)[:500],
                )

    def _restart_public_tunnel(self) -> None:
        with self._lifecycle_lock:
            options = self._active_options
            if options is None or options.connection == ConnectionMethod.LOCAL or self.state != EngineState.READY:
                return
            self._last_transport_restart = time.monotonic()
            self._write_transport_health(event="tunnel_restart", reason="consecutive_public_probe_failures")
            try:
                if self.tunnel.is_running:
                    self.tunnel.stop()
                self.tunnel.port = options.gateway_port
                kwargs = {
                    "kind": options.connection,
                    "hostname": options.public_hostname,
                    "cloudflare_config": options.cloudflare_config,
                    "tunnel_" + "token": getattr(options, "tunnel_" + "token"),
                }
                self.tunnel.start(**kwargs)
                if not self.tunnel.wait_ready():
                    raise SpawnError("隧道自愈重建失败。")
                self._set_url(self.tunnel.public_url, options.url_mutable)
                self._transport_failures = 0
                self._write_transport_health(event="tunnel_restart_ok")
            except Exception as exc:
                self._write_transport_health(event="tunnel_restart_failed", error=type(exc).__name__)
                self._set_state(EngineState.ERROR, f"公网隧道自愈重建失败：{exc}")

    def _transport_monitor_loop(self) -> None:
        if self._transport_stop.wait(TRANSPORT_HEALTH_INITIAL_DELAY_SECONDS):
            return
        while not self._transport_stop.is_set():
            with contextlib.suppress(Exception):
                self._transport_health_tick()
            if self._transport_stop.wait(TRANSPORT_HEALTH_INTERVAL_SECONDS):
                return

    def _start_transport_monitor(self, options: StartOptions) -> None:
        if options.connection == ConnectionMethod.LOCAL:
            return
        self._stop_transport_monitor()
        self._transport_stop = threading.Event()
        self._transport_thread = threading.Thread(
            target=self._transport_monitor_loop,
            name="MCPDevBridge-transport-health",
            daemon=True,
        )
        self._transport_thread.start()

    def _stop_transport_monitor(self) -> None:
        thread = self._transport_thread
        if thread is None:
            return
        self._transport_stop.set()
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._transport_thread = None

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