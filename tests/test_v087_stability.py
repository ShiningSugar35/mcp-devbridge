from __future__ import annotations

from typing import Any, cast

import pytest

import local_dev_mcp_bridge.app_state as app_state
from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.engines import CodexProManager, EngineState
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.power_guard import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, SystemAwakeGuard
from local_dev_mcp_bridge.project_manager import ProjectManager
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod

TOKEN = "t" * 32


def test_ready_engine_state_detects_exited_child() -> None:
    class DeadProcess:
        def __init__(self) -> None:
            self.is_running = False
            self.returncode = 17
            self.pid = 4242
            self.message = ""

        def record_event(self, message: str) -> None:
            self.message = message

    manager = CodexProManager(node_exe="node")
    dead = DeadProcess()
    cast(Any, manager)._proc = dead
    cast(Any, manager)._set_state(EngineState.READY)

    assert manager.state == EngineState.ERROR
    assert manager.error == "CodexPro 进程已退出（exit=17）。"
    assert "engine_exit" in dead.message


def test_codexpro_ready_requires_real_mcp_canary_not_log_or_tcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ListeningButMcpDead:
        is_running = True
        returncode = None
        pid = 4243

        class Log:
            @staticmethod
            def tail(_count: int) -> str:
                return "[CodexPro] HTTP MCP listening on http://127.0.0.1:8788/mcp"

        log = Log()

    manager = CodexProManager(node_exe="node", timeout_seconds=0.03)
    cast(Any, manager)._proc = ListeningButMcpDead()
    cast(Any, manager)._mcp_ready_probe = lambda: (False, "tools/list timed out")
    monkeypatch.setattr("local_dev_mcp_bridge.engines.port_listening", lambda _port: True)

    assert manager.wait_ready(timeout_seconds=0.03) is False
    assert manager.state != EngineState.READY


def test_windows_awake_guard_uses_system_required_without_display_required() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int:
        calls.append(flags)
        return 1

    guard = SystemAwakeGuard(enabled=True, setter=setter, refresh_seconds=60)
    assert guard.start()
    assert guard.active
    guard.stop()

    assert calls[0] == ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    assert calls[-1] == ES_CONTINUOUS
    assert all((flags & 0x00000002) == 0 for flags in calls)  # ES_DISPLAY_REQUIRED is never requested.


class _RecoverableUnit:
    def __init__(self, project: ProjectConfig) -> None:
        self.project = project
        self._state = EngineState.IDLE
        self.message: str | None = None
        self.engine_pid = 1000
        self.healthy = True
        self.start_count = 0
        self.stop_count = 0

    @property
    def state(self) -> EngineState:
        return self._state

    def start(
        self,
        codex_token: str,
        *,
        permission_mode: str = "workspace",
        execution_profile: str = "developer",
        windows_token: str | None = None,
        windows_enabled: bool = False,
        elevated: bool = False,
    ) -> None:
        _ = (
            codex_token,
            permission_mode,
            execution_profile,
            windows_token,
            windows_enabled,
            elevated,
        )
        self.start_count += 1
        self.engine_pid += 1
        self.healthy = True
        self._state = EngineState.READY

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        _ = timeout_seconds
        return self._state == EngineState.READY

    def data_plane_health(self, token: str, timeout_seconds: float = 2.0) -> tuple[bool, str]:
        _ = token, timeout_seconds
        return self.healthy, "ok" if self.healthy else "injected failure"

    def stop(self, timeout_seconds: float = 8.0) -> None:
        _ = timeout_seconds
        self.stop_count += 1
        self._state = EngineState.IDLE


def test_project_supervisor_recovers_only_desired_running_project(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(config_dir))
    root = tmp_path / "project"
    root.mkdir()
    units: dict[str, _RecoverableUnit] = {}

    def factory(project: ProjectConfig) -> _RecoverableUnit:
        unit = _RecoverableUnit(project)
        units[project.id] = unit
        return unit

    manager = ProjectManager(unit_factory=factory, supervisor_enabled=False)
    project = manager.add(str(root), permission_mode="workspace")
    initial = manager.start(project.id, codex_token=TOKEN)
    unit = units[project.id]
    old_pid = initial.engine_pid
    assert old_pid is not None

    unit.healthy = False
    cast(Any, manager)._supervisor_tick()
    assert unit.engine_pid == old_pid
    cast(Any, manager)._supervisor_tick()

    assert unit.state == EngineState.READY
    assert unit.engine_pid != old_pid
    assert unit.start_count == 2
    assert unit.stop_count == 1

    manager.stop(project.id)
    stopped_pid = unit.engine_pid
    unit.healthy = False
    cast(Any, manager)._supervisor_tick()
    assert unit.engine_pid == stopped_pid


class _FakeTunnel:
    def __init__(self) -> None:
        self.state = EngineState.IDLE
        self.port = 0
        self.public_url = ""
        self.is_running = False
        self.starts = 0
        self.stops = 0

    def start(self, **kwargs: object) -> None:
        self.starts += 1
        self.state = EngineState.STARTING
        self.is_running = True
        hostname = str(kwargs.get("hostname") or "mcp.example.com")
        self.public_url = f"https://{hostname}/mcp"

    def wait_ready(self) -> bool:
        self.state = EngineState.READY
        return True

    def stop(self) -> None:
        self.stops += 1
        self.state = EngineState.IDLE
        self.is_running = False


class _FakeGateway:
    instances: list[_FakeGateway] = []

    def __init__(self, **kwargs: object) -> None:
        _ = kwargs
        self.is_running = False
        self.stops = 0
        _FakeGateway.instances.append(self)

    def start(self, *, port: int) -> None:
        _ = port
        self.is_running = True

    def stop(self) -> None:
        self.stops += 1
        self.is_running = False


class _FakeAwakeGuard:
    def __init__(self) -> None:
        self.last_error = ""
        self.starts = 0
        self.stops = 0

    def start(self) -> bool:
        self.starts += 1
        return True

    def stop(self) -> None:
        self.stops += 1


def _prepare_gateway_recovery_test(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ServiceCoordinator, _FakeTunnel, _FakeAwakeGuard]:
    _FakeGateway.instances.clear()
    monkeypatch.setattr(app_state, "OAuthGateway", _FakeGateway)
    monkeypatch.setattr(
        ServiceCoordinator,
        "_wait_gateway_ready",
        staticmethod(lambda _port, timeout_seconds=20.0: True),
    )
    monkeypatch.setattr(ServiceCoordinator, "_check_gateway_port", staticmethod(lambda _port: None))
    monkeypatch.setattr(ServiceCoordinator, "_start_transport_monitor", lambda self, options: None)

    tunnel = _FakeTunnel()
    awake = _FakeAwakeGuard()
    coord = ServiceCoordinator(tunnel=cast(Any, tunnel), awake_guard=cast(Any, awake))
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19886,
    )
    coord.start(options)
    cast(Any, coord)._write_transport_health = lambda **fields: None
    cast(Any, coord)._last_gateway_restart = -1000.0
    return coord, tunnel, awake


def test_gateway_health_failure_restarts_gateway_only(monkeypatch: pytest.MonkeyPatch) -> None:
    coord, tunnel, awake = _prepare_gateway_recovery_test(monkeypatch)
    old_gateway = _FakeGateway.instances[0]
    cast(Any, coord)._transport_probe = lambda url, timeout: (False, 0.01, 0)

    for _ in range(app_state.GATEWAY_HEALTH_FAILURE_THRESHOLD):
        cast(Any, coord)._transport_health_tick()

    assert len(_FakeGateway.instances) == 2
    assert old_gateway.stops == 1
    assert _FakeGateway.instances[-1].is_running
    assert tunnel.starts == 1
    assert tunnel.stops == 0
    assert coord.state == EngineState.READY
    assert awake.starts == 1


def test_gateway_health_success_resets_consecutive_failure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coord, _tunnel, _awake = _prepare_gateway_recovery_test(monkeypatch)
    outcomes = iter(
        [
            (False, 0.01, 0),
            (True, 0.01, 200),
            (True, 0.01, 200),
            (False, 0.01, 0),
        ]
    )
    cast(Any, coord)._transport_probe = lambda url, timeout: next(outcomes)

    cast(Any, coord)._transport_health_tick()
    assert cast(Any, coord)._gateway_failures == 1

    cast(Any, coord)._transport_health_tick()
    assert cast(Any, coord)._gateway_failures == 0

    cast(Any, coord)._transport_health_tick()
    assert cast(Any, coord)._gateway_failures == 1
    assert len(_FakeGateway.instances) == 1
