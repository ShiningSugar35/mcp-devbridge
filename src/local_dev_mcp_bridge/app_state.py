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
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from . import constants
from .device_hub import DeviceRegistry
from .engines import EngineState, SpawnError, port_listening
from .flight_recorder import FlightRecorder
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
DEEP_MCP_PROBE_INTERVAL_SECONDS = 60.0
DEEP_MCP_PROBE_TIMEOUT_SECONDS = 5.0
TUNNEL_RETRY_BACKOFF_SECONDS = (0.0, 2.0, 8.0)
_TUNNEL_PERMANENT_MARKERS = (
    "not configured",
    "invalid tunnel",
    "credentials rejected",
    "authentication failed",
    "unauthorized",
    "forbidden",
    "unknown connection",
    "未配置",
)
_TUNNEL_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "i/o timeout",
    "lookup",
    "host not found",
    "no such host",
    "dns",
    "network",
    "connection refused",
    "connection reset",
    "connection closed",
    "tls handshake",
    "quic",
    "udp",
    "api request failed",
    "cloudflare_protocol_fallback",
    "temporary",
    "unreachable",
    "context canceled",
    "eof",
)


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
        workspace_project_registry: Callable[[str], str | None] | None = None,
        workspace_credential_registry: Callable[[str], str | None] | None = None,
        project_runtime_registry: Callable[[], list[dict[str, object]]] | None = None,
        device_registry: DeviceRegistry | None = None,
        local_device_id: str = "",
        flight_recorder: FlightRecorder | None = None,
    ) -> None:
        self.tunnel = tunnel or TunnelManager()
        self.awake_guard = awake_guard or SystemAwakeGuard()
        self.gateway: OAuthGateway | None = None
        self._workspace_registry = workspace_registry
        self._workspace_project_registry = workspace_project_registry
        self._workspace_credential_registry = workspace_credential_registry
        self._project_runtime_registry = project_runtime_registry
        self._device_registry = device_registry
        self.flight_recorder = flight_recorder or FlightRecorder(constants.log_dir())
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
        self._tunnel_retry_stop = threading.Event()
        self._transport_thread: threading.Thread | None = None
        self._transport_failures = 0
        self._last_transport_restart = 0.0
        self._gateway_failures = 0
        self._last_gateway_restart = 0.0
        self._gateway_mcp_failures = 0
        self._public_mcp_failures = 0
        self._last_deep_mcp_probe = 0.0
        self._probe_state_lock = threading.Lock()
        self._last_local_success_ts = ""
        self._last_local_success_mono_ns = 0
        self._last_public_success_ts = ""
        self._last_public_success_mono_ns = 0
        self._last_project_success: dict[str, tuple[str, int]] = {}
        self._transport_probe = self._probe_health_url
        self._mcp_probe = self._probe_mcp_url

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

    def recovery_snapshot(self) -> dict[str, float | int | None]:
        """Return bounded, non-secret recovery telemetry for the desktop diagnostic UI."""
        now = time.monotonic()
        with self._lifecycle_lock:
            gateway_age = (
                max(0.0, now - self._last_gateway_restart)
                if self._last_gateway_restart > 0
                else None
            )
            public_age = (
                max(0.0, now - self._last_transport_restart)
                if self._last_transport_restart > 0
                else None
            )
            request_snapshot = self.flight_recorder.snapshot()
            return {
                "gateway_restart_seconds_ago": gateway_age,
                "public_restart_seconds_ago": public_age,
                "gateway_failures": self._gateway_failures + self._gateway_mcp_failures,
                "public_failures": self._transport_failures + self._public_mcp_failures,
                "active_requests": request_snapshot["active_requests"],
                "oldest_request_age_ms": request_snapshot["oldest_request_age_ms"],
            }

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
            workspace_project_registry=self._workspace_project_registry,
            workspace_credential_registry=self._workspace_credential_registry,
            device_registry=self._device_registry,
            local_device_id=self._local_device_id,
            flight_recorder=self.flight_recorder,
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

    @staticmethod
    def _fixed_public_url(options: StartOptions) -> str:
        host = options.public_hostname.strip().rstrip("/")
        if not host:
            return ""
        if host.lower().startswith(("http://", "https://")):
            return f"{host}/mcp"
        return f"https://{host}/mcp"

    def _tunnel_failure_classification(self, extra: str = "") -> tuple[str, bool, str]:
        parts = [str(extra or ""), str(getattr(self.tunnel, "error", "") or "")]
        with contextlib.suppress(Exception):
            parts.append(str(self.tunnel.log_tail(160) or ""))
        detail = "\n".join(parts).strip()
        lowered = detail.casefold()
        digest = hashlib.sha256(detail.encode("utf-8", errors="ignore")).hexdigest()[:16]
        if any(marker in lowered for marker in _TUNNEL_PERMANENT_MARKERS):
            return "configuration_auth", False, digest
        if any(marker in lowered for marker in _TUNNEL_TRANSIENT_MARKERS):
            return "transient_network_or_dns", True, digest
        return "unknown_process_failure", True, digest

    def _start_public_tunnel_with_retry(
        self, options: StartOptions, *, phase: str
    ) -> tuple[bool, bool, str]:
        last_category = "unknown_process_failure"
        retried = False
        cloudflare_protocol = "auto"
        for attempt, delay in enumerate(TUNNEL_RETRY_BACKOFF_SECONDS, start=1):
            if self._tunnel_retry_stop.is_set():
                return False, retried, "cancelled"
            if delay > 0:
                self._set_state(
                    EngineState.STARTING,
                    f"公网隧道暂时不可用，正在进行第 {attempt} 次有界重试…",
                )
                if self._tunnel_retry_stop.wait(delay):
                    self._write_transport_health(
                        event="tunnel_retry_cancelled",
                        phase=phase,
                        attempt=attempt,
                    )
                    return False, retried, "cancelled"
            try:
                if self.tunnel.is_running:
                    self.tunnel.stop()
                self.tunnel.port = options.gateway_port
                start_kwargs = {
                    "kind": options.connection,
                    "hostname": options.public_hostname,
                    "cloudflare_config": options.cloudflare_config,
                }
                start_kwargs["tunnel_" + "token"] = getattr(options, "tunnel_" + "token")
                if options.connection == ConnectionMethod.CLOUDFLARE:
                    start_kwargs["cloudflare_protocol"] = cloudflare_protocol
                self.tunnel.start(**start_kwargs)
                if self.tunnel.wait_ready():
                    self._write_transport_health(
                        event="tunnel_start_attempt_ok",
                        phase=phase,
                        attempt=attempt,
                        retried=attempt > 1,
                        protocol=getattr(self.tunnel, "current_protocol", "") or cloudflare_protocol,
                    )
                    return True, attempt > 1, "ok"
                if self._tunnel_retry_stop.is_set():
                    return False, retried, "cancelled"
                category, retryable, detail_hash = self._tunnel_failure_classification()
            except Exception as exc:
                category, retryable, detail_hash = self._tunnel_failure_classification(
                    f"{type(exc).__name__}: {exc}"
                )
            current_protocol = str(
                getattr(self.tunnel, "current_protocol", "") or cloudflare_protocol
            ).strip().casefold()
            recommended_protocol = str(
                getattr(self.tunnel, "recommended_protocol", "") or ""
            ).strip().casefold()
            last_category = category
            self._write_transport_health(
                event="tunnel_start_attempt_failed",
                phase=phase,
                attempt=attempt,
                category=category,
                retryable=retryable,
                detail_hash=detail_hash,
                tunnel_pid=getattr(self.tunnel, "pid", None),
                protocol=current_protocol,
                recommended_protocol=recommended_protocol,
            )
            if (
                options.connection == ConnectionMethod.CLOUDFLARE
                and recommended_protocol == "http2"
                and cloudflare_protocol != "http2"
                and attempt < len(TUNNEL_RETRY_BACKOFF_SECONDS)
            ):
                self._write_transport_health(
                    event="tunnel_protocol_fallback",
                    phase=phase,
                    attempt=attempt,
                    from_protocol=current_protocol or "auto",
                    to_protocol="http2",
                )
                cloudflare_protocol = "http2"
                retryable = True
            if not retryable or attempt >= len(TUNNEL_RETRY_BACKOFF_SECONDS):
                break
            retried = True
        return False, retried, last_category

    def _mark_probe_success(self, component: str, *, project_id: str = "") -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        mono_ns = time.monotonic_ns()
        with self._probe_state_lock:
            if component == "local":
                self._last_local_success_ts = stamp
                self._last_local_success_mono_ns = mono_ns
            elif component == "public":
                self._last_public_success_ts = stamp
                self._last_public_success_mono_ns = mono_ns
            elif component == "project" and project_id:
                self._last_project_success[project_id] = (stamp, mono_ns)
                while len(self._last_project_success) > 64:
                    self._last_project_success.pop(next(iter(self._last_project_success)))

    def _record_component_snapshot(self, *, reason: str) -> None:
        raw_projects: list[dict[str, object]] = []
        if self._project_runtime_registry is not None:
            try:
                raw_projects = list(self._project_runtime_registry())[:32]
            except Exception as exc:
                raw_projects = [{"snapshot_error": type(exc).__name__}]
        with self._probe_state_lock:
            last_local_success_ts = self._last_local_success_ts
            last_local_success_mono_ns = self._last_local_success_mono_ns
            last_public_success_ts = self._last_public_success_ts
            last_public_success_mono_ns = self._last_public_success_mono_ns
            project_success = dict(self._last_project_success)
        projects: list[dict[str, object]] = []
        for raw_project in raw_projects:
            project = dict(raw_project)
            project_id = str(project.get("project_id") or "")
            try:
                engine_port = int(str(project.get("engine_port") or "0"))
            except (TypeError, ValueError):
                engine_port = 0
            project["engine_listener"] = bool(
                engine_port and port_listening(engine_port, timeout=0.05)
            )
            success = project_success.get(project_id)
            if success is not None:
                project["last_success_ts"] = success[0]
                project["last_success_mono_ns"] = success[1]
            projects.append(project)
        options = self._active_options
        gateway_port = int(options.gateway_port) if options is not None else 0
        gateway_running = bool(self.gateway and self.gateway.is_running)
        try:
            tunnel_recent_output = str(self.tunnel.log_tail(8) or "")
        except Exception:
            tunnel_recent_output = ""
        active = self.flight_recorder.snapshot()
        self.flight_recorder.record(
            "component_snapshot",
            reason=reason,
            process_pid=os.getpid(),
            coordinator_state=str(self.state),
            coordinator_message=self.message or "",
            gateway_running=gateway_running,
            gateway_pid=os.getpid() if gateway_running else None,
            gateway_port=gateway_port,
            gateway_listener=bool(
                gateway_port and port_listening(gateway_port, timeout=0.05)
            ),
            tunnel_state=str(getattr(self.tunnel, "state", "")),
            tunnel_pid=getattr(self.tunnel, "last_pid", None)
            or getattr(self.tunnel, "pid", None),
            tunnel_exit_code=getattr(self.tunnel, "last_exit_code", None),
            tunnel_recent_output=tunnel_recent_output,
            last_local_success_ts=last_local_success_ts,
            last_local_success_mono_ns=last_local_success_mono_ns,
            last_public_success_ts=last_public_success_ts,
            last_public_success_mono_ns=last_public_success_mono_ns,
            gateway_failures=self._gateway_failures + self._gateway_mcp_failures,
            public_failures=self._transport_failures + self._public_mcp_failures,
            public_endpoint_hash=hashlib.sha256(
                self.public_url.encode("utf-8", errors="ignore")
            ).hexdigest()[:16]
            if self.public_url
            else "",
            projects=projects,
            **active,
        )

    def start(self, options: StartOptions) -> None:
        with self._lifecycle_lock:
            if self.running:
                if self._same_transport_options(self._active_options, options):
                    return
                raise SpawnError("Hub 已使用不同连接配置启动；请先停止后再切换连接配置。")
            self._tunnel_retry_stop.clear()
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
        self.tunnel.port = options.gateway_port
        self._set_state(EngineState.STARTING, "正在启动共享 Hub…")
        fixed_public_url = (
            self._fixed_public_url(options)
            if options.connection in (ConnectionMethod.CLOUDFLARE, ConnectionMethod.NGROK)
            else ""
        )
        try:
            if options.connection == ConnectionMethod.LOCAL:
                self._set_url(
                    f"http://127.0.0.1:{options.gateway_port}{constants.DEFAULT_MCP_PATH}",
                    False,
                )
                self._start_gateway(options)
                self._set_state(EngineState.READY, "共享 Hub 已启动。")
                return

            if fixed_public_url:
                self._set_url(fixed_public_url, False)
                self._start_gateway(options)

            tunnel_ok, retried, category = self._start_public_tunnel_with_retry(
                options, phase="initial_start"
            )
            if not tunnel_ok and not fixed_public_url:
                if category == "configuration_auth":
                    raise SpawnError("公网隧道配置或凭据错误，无法启动。")
                raise SpawnError("隧道建立失败；有界重试已耗尽。")
            if tunnel_ok:
                self._set_url(self.tunnel.public_url or fixed_public_url, options.url_mutable)
            if self.gateway is None:
                self._start_gateway(options)

            awake_ok = self.awake_guard.start()
            self._write_transport_health(
                event="awake_guard_started" if awake_ok else "awake_guard_failed",
                error=self.awake_guard.last_error if not awake_ok else "",
            )
            if tunnel_ok:
                message = (
                    "公网隧道重试后已恢复，共享 Hub 已启动。"
                    if retried
                    else "共享 Hub 已启动。"
                )
                self._set_state(EngineState.READY, message)
            else:
                self._transport_failures = TRANSPORT_HEALTH_FAILURE_THRESHOLD - 1
                if category == "configuration_auth":
                    message = (
                        "公网隧道配置或凭据错误；本地 Hub 与项目引擎保持可用，"
                        "请修正配置后重新启动连接。"
                    )
                else:
                    message = (
                        "公网隧道暂时不可用；本地 Hub 与项目引擎保持可用，"
                        "探针将继续检测并自动重建公网连接。"
                    )
                self._set_state(EngineState.READY, message)
                self._write_transport_health(
                    event="tunnel_start_degraded",
                    category=category,
                    local_gateway_kept=True,
                )
        except Exception as exc:
            message = str(exc)
            self._cleanup_after_failure()
            self._set_state(EngineState.ERROR, message)

    def _cancel_tunnel_start_wait(self) -> None:
        self._tunnel_retry_stop.set()
        cancel_wait = getattr(self.tunnel, "cancel_wait_ready", None)
        if callable(cancel_wait):
            with contextlib.suppress(Exception):
                cancel_wait()

    def stop(self, message: str = "正在停止共享 Hub…") -> None:
        self._cancel_tunnel_start_wait()
        with self._lifecycle_lock:
            if self.state == EngineState.IDLE:
                return
            self._set_state(EngineState.STOPPING, message)
            self.stop_callable()

    def stop_callable(self) -> None:
        """Stop shared transport only; project engines are owned elsewhere."""
        self._cancel_tunnel_start_wait()
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
        self._gateway_mcp_failures = 0
        self._public_mcp_failures = 0
        self._last_deep_mcp_probe = 0.0
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
        self._transport_failures = 0
        self._gateway_failures = 0
        self._gateway_mcp_failures = 0
        self._public_mcp_failures = 0
        self._last_deep_mcp_probe = 0.0
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

    @staticmethod
    def _probe_mcp_url(
        url: str, token: str, route_workspace_id: str = ""
    ) -> tuple[bool, float, str]:
        started = time.monotonic()
        try:
            from .selftest import run_selftest

            result = run_selftest(
                url,
                token or None,
                timeout=DEEP_MCP_PROBE_TIMEOUT_SECONDS,
                route_workspace_id=route_workspace_id,
            )
        except Exception as exc:
            return False, time.monotonic() - started, f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started
        if result.ok:
            return True, elapsed, "ok"
        return False, elapsed, result.error or "MCP canary failed"

    def _write_transport_health(self, **fields: object) -> None:
        entry = {**fields, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        recorder_fields = dict(fields)
        transport_event = str(recorder_fields.pop("event", "health"))
        self.flight_recorder.record(
            "transport_health",
            transport_event=transport_event,
            **recorder_fields,
        )
        try:
            log_dir = constants.log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"transport-health-{time.strftime('%Y-%m-%d')}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass

    def _deep_mcp_health_tick(self, options: StartOptions) -> None:
        now = time.monotonic()
        if now - self._last_deep_mcp_probe < DEEP_MCP_PROBE_INTERVAL_SECONDS:
            return
        self._last_deep_mcp_probe = now
        if self._workspace_registry is None or self._workspace_credential_registry is None:
            return

        try:
            from .config_store import load_projects

            projects = load_projects()
        except Exception as exc:
            self._write_transport_health(
                event="deep_mcp_project_catalog_failed",
                error=type(exc).__name__,
            )
            return

        healthy_project: tuple[str, str] | None = None
        for project in projects:
            project_id = str(getattr(project, "id", "") or "").strip()
            if not project_id:
                continue
            target = self._workspace_registry(project_id)
            token = self._workspace_credential_registry(project_id)
            if not target or not token:
                continue
            project_url = f"http://127.0.0.1:{int(target[0])}/mcp"
            project_ok, project_elapsed, project_detail = self._mcp_probe(project_url, token, "")
            self._write_transport_health(
                event="project_mcp_probe",
                project_id=project_id,
                ok=project_ok,
                elapsed_ms=round(project_elapsed * 1000),
                detail="ok" if project_ok else project_detail[:300],
            )
            if project_ok:
                self._mark_probe_success("project", project_id=project_id)
                if healthy_project is None:
                    healthy_project = (project_id, token)

        if healthy_project is None:
            self._gateway_mcp_failures = 0
            self._public_mcp_failures = 0
            self._write_transport_health(event="deep_mcp_no_healthy_project")
            return

        project_id, token = healthy_project
        local_mcp_url = f"http://127.0.0.1:{options.gateway_port}/mcp"
        local_ok, local_elapsed, local_detail = self._mcp_probe(
            local_mcp_url, token, project_id
        )
        if not local_ok:
            self._gateway_mcp_failures += 1
            self._public_mcp_failures = 0
            self._write_transport_health(
                event="gateway_mcp_unhealthy",
                project_id=project_id,
                elapsed_ms=round(local_elapsed * 1000),
                detail=local_detail[:300],
                consecutive_gateway_mcp_failures=self._gateway_mcp_failures,
            )
            if (
                self._gateway_mcp_failures >= GATEWAY_HEALTH_FAILURE_THRESHOLD
                and now - self._last_gateway_restart >= GATEWAY_RESTART_COOLDOWN_SECONDS
            ):
                self._restart_gateway()
            return

        self._gateway_mcp_failures = 0
        self._mark_probe_success("local")
        if options.connection == ConnectionMethod.LOCAL:
            self._public_mcp_failures = 0
            self._write_transport_health(
                event="mcp_transport_probe",
                project_id=project_id,
                local_ok=True,
                local_elapsed_ms=round(local_elapsed * 1000),
                public_skipped=True,
            )
            return

        public_ok, public_elapsed, public_detail = self._mcp_probe(
            self.public_url, token, project_id
        )
        self._public_mcp_failures = 0 if public_ok else self._public_mcp_failures + 1
        if public_ok:
            self._mark_probe_success("public")
        self._write_transport_health(
            event="mcp_transport_probe",
            project_id=project_id,
            local_ok=True,
            local_elapsed_ms=round(local_elapsed * 1000),
            public_ok=public_ok,
            public_elapsed_ms=round(public_elapsed * 1000),
            detail="ok" if public_ok else public_detail[:300],
            consecutive_public_mcp_failures=self._public_mcp_failures,
        )
        if (
            not public_ok
            and self._public_mcp_failures >= TRANSPORT_HEALTH_FAILURE_THRESHOLD
            and now - self._last_transport_restart >= TRANSPORT_RESTART_COOLDOWN_SECONDS
        ):
            self._restart_public_tunnel()

    def _transport_health_tick(self) -> None:
        options = self._active_options
        if options is None or self.state != EngineState.READY:
            return
        self._record_component_snapshot(reason="periodic_transport_probe")
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
        self._mark_probe_success("local")
        if options.connection == ConnectionMethod.LOCAL:
            self._transport_failures = 0
            self._deep_mcp_health_tick(options)
            return
        if not self.tunnel.is_running:
            self._transport_failures = max(
                self._transport_failures,
                TRANSPORT_HEALTH_FAILURE_THRESHOLD,
            )
            self._write_transport_health(
                event="tunnel_process_unhealthy",
                reason="owned_tunnel_process_not_running",
                tunnel_pid=getattr(self.tunnel, "last_pid", None)
                or getattr(self.tunnel, "pid", None),
                tunnel_exit_code=getattr(self.tunnel, "last_exit_code", None),
                external_public_health_ignored=True,
            )
            now = time.monotonic()
            if now - self._last_transport_restart >= TRANSPORT_RESTART_COOLDOWN_SECONDS:
                self._restart_public_tunnel()
            return
        public_health = self._health_url_from_public_mcp(self.public_url)
        public_ok, public_ttfb, public_status = self._transport_probe(
            public_health, TRANSPORT_HEALTH_TIMEOUT_SECONDS
        )
        degraded = (not public_ok) or public_ttfb > TRANSPORT_HEALTH_TTFB_LIMIT_SECONDS
        self._transport_failures = self._transport_failures + 1 if degraded else 0
        if public_ok:
            self._mark_probe_success("public")
        self._write_transport_health(
            event="transport_probe",
            local_status=local_status,
            local_ttfb_ms=round(local_ttfb * 1000),
            public_status=public_status,
            public_ttfb_ms=round(public_ttfb * 1000),
            degraded=degraded,
            consecutive_failures=self._transport_failures,
        )
        if self._transport_failures:
            if self._transport_failures < TRANSPORT_HEALTH_FAILURE_THRESHOLD:
                return
            now = time.monotonic()
            if now - self._last_transport_restart < TRANSPORT_RESTART_COOLDOWN_SECONDS:
                return
            self._restart_public_tunnel()
            return
        self._deep_mcp_health_tick(options)

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
                self._gateway_mcp_failures = 0
                self._last_deep_mcp_probe = 0.0
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
            self._write_transport_health(
                event="tunnel_restart",
                reason="consecutive_public_probe_failures",
            )
            tunnel_ok, retried, category = self._start_public_tunnel_with_retry(
                options, phase="runtime_rebuild"
            )
            if tunnel_ok:
                self._set_url(
                    self.tunnel.public_url or self._fixed_public_url(options),
                    options.url_mutable,
                )
                self._transport_failures = 0
                self._public_mcp_failures = 0
                self._last_deep_mcp_probe = 0.0
                self._write_transport_health(
                    event="tunnel_restart_ok",
                    retried=retried,
                )
                self._set_state(EngineState.READY, "公网隧道重试后已恢复。")
                return

            self._transport_failures = 0
            self._public_mcp_failures = 0
            self._write_transport_health(
                event="tunnel_restart_failed",
                category=category,
                local_gateway_kept=bool(self.gateway and self.gateway.is_running),
            )
            if category == "configuration_auth":
                message = (
                    "公网隧道配置或凭据错误；本地 Hub 与项目引擎仍可用，"
                    "请修正配置后重新启动连接。"
                )
            else:
                message = (
                    "公网隧道重建暂未成功；本地 Hub 与项目引擎仍可用，"
                    "探针会在冷却窗口后继续检测。"
                )
            self._set_state(EngineState.READY, message)

    def _transport_monitor_loop(self) -> None:
        if self._transport_stop.wait(TRANSPORT_HEALTH_INITIAL_DELAY_SECONDS):
            return
        while not self._transport_stop.is_set():
            with contextlib.suppress(Exception):
                self._transport_health_tick()
            if self._transport_stop.wait(TRANSPORT_HEALTH_INTERVAL_SECONDS):
                return

    def _start_transport_monitor(self, options: StartOptions) -> None:
        self._stop_transport_monitor()
        self._transport_stop = threading.Event()
        self._record_component_snapshot(reason="transport_monitor_started")
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
        self.flight_recorder.record(
            "coordinator_state",
            state=str(state),
            message=message or "",
            gateway_running=bool(self.gateway and self.gateway.is_running),
            tunnel_state=str(getattr(self.tunnel, "state", "")),
            tunnel_pid=getattr(self.tunnel, "pid", None),
        )
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
