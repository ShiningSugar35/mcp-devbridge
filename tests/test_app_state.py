"""Tests for app_state.py: coordinator state machine, ordering, cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.engines import EngineState, SpawnError
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod


@dataclass
class FakeEngine:
    """Minimal fake matching the EngineManager surface the coordinator uses."""

    label: str
    wait_fails: bool = False
    state: EngineState = EngineState.IDLE
    error: str | None = None
    started: bool = False
    stopped: bool = False
    public_url: str = ""
    last_hostname: str = ""
    last_kind: ConnectionMethod | None = None
    stop_order: list[str] = field(default_factory=list)
    port: int = 0

    @property
    def is_running(self) -> bool:
        return self.started and not self.stopped

    def fake_start(self, *args, **kwargs) -> None:
        self.started = True
        self.state = EngineState.STARTING
        if self.label == "tunnel":
            self.last_kind = kwargs.get("kind", ConnectionMethod.LOCAL)
            self.last_hostname = kwargs.get("hostname", "")
            if self.last_kind == ConnectionMethod.QUICK:
                self.public_url = "https://quick-abc123.trycloudflare.com/mcp"
            elif self.last_hostname:
                self.public_url = f"https://{self.last_hostname}"
            else:
                self.public_url = ""

    def fake_wait_ready(self, *args, **kwargs) -> bool:
        if self.wait_fails:
            self.error = f"{self.label} 启动失败"
            self.state = EngineState.ERROR
            return False
        self.state = EngineState.READY
        return True

    def fake_stop(self, timeout_seconds: float = 8.0) -> None:
        self.stopped = True
        self.state = EngineState.IDLE

    def stop(self, timeout_seconds: float = 8.0) -> None:
        self.stopped = True
        self.state = EngineState.IDLE


@dataclass
class Rig:
    coordinator: ServiceCoordinator
    tunnel: FakeEngine
    codex: FakeEngine
    windows: FakeEngine


def make_rig(windows_wait_fails: bool = False) -> Rig:
    tunnel = FakeEngine("tunnel")
    codex = FakeEngine("codex")
    windows = FakeEngine("windows", wait_fails=windows_wait_fails)
    for engine in (tunnel, codex, windows):
        engine.start = engine.fake_start  # type: ignore[method-assign]
        engine.wait_ready = engine.fake_wait_ready  # type: ignore[method-assign]
        engine.stop = engine.fake_stop  # type: ignore[method-assign]
    coordinator = ServiceCoordinator(
        tunnel=tunnel, codex=codex, windows=windows  # type: ignore[arg-type]
    )

    class _FakeGateway:
        def stop(self) -> None:
            return None

    def fake_start_gateway(options: StartOptions) -> None:  # noqa: ARG001
        coordinator.gateway = _FakeGateway()  # type: ignore[assignment]

    coordinator._start_gateway = fake_start_gateway  # type: ignore[method-assign]
    return Rig(coordinator, tunnel, codex, windows)


def base_options(tmp_path: Path, **overrides: object) -> StartOptions:
    values: dict[str, object] = {
        "project_root": str(tmp_path),
        "codex_token": "t" * 32,
        "windows_enabled": False,
        "windows_token": "w" * 32,
        "connection": ConnectionMethod.LOCAL,
        "gateway_port": 19886,
        "codexpro_port": 19887,
        "windows_mcp_port": 29831,
    }
    values.update(overrides)
    return StartOptions(**values)  # type: ignore[arg-type]


class TestStartValidation:
    def test_initial_state_idle(self, tmp_path: Path) -> None:
        rig = make_rig()
        assert rig.coordinator.state == EngineState.IDLE
        assert not rig.coordinator.running

    def test_missing_project_rejected_without_state_change(self, tmp_path: Path) -> None:
        rig = make_rig()
        with pytest.raises(SpawnError):
            rig.coordinator.start(base_options(tmp_path, project_root=""))
        assert rig.coordinator.state == EngineState.IDLE

    def test_nonexistent_project_rejected(self, tmp_path: Path) -> None:
        rig = make_rig()
        with pytest.raises(SpawnError):
            rig.coordinator.start(base_options(tmp_path, project_root=str(tmp_path / "nope")))
        assert rig.coordinator.state == EngineState.IDLE

    def test_short_token_rejected_without_spawning(self, tmp_path: Path) -> None:
        rig = make_rig()
        with pytest.raises(SpawnError):
            rig.coordinator.start(base_options(tmp_path, codex_token="short"))
        assert not rig.tunnel.started and not rig.codex.started

    def test_short_windows_token_rejected(self, tmp_path: Path) -> None:
        rig = make_rig()
        with pytest.raises(SpawnError):
            rig.coordinator.start(base_options(tmp_path, windows_enabled=True, windows_token="short"))
        assert rig.coordinator.state == EngineState.IDLE

    def test_second_start_while_running_rejected(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path))
        with pytest.raises(SpawnError):
            rig.coordinator.start(base_options(tmp_path))
        assert rig.coordinator.state == EngineState.READY


class TestLocalStart:
    def test_local_reaches_ready(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path))
        assert rig.coordinator.state == EngineState.READY
        assert rig.coordinator.public_url == ""
        assert rig.coordinator.url_mutable is False

    def test_windows_disabled_not_started(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path))
        assert rig.windows.started is False
        assert rig.tunnel.started is False

    def test_windows_enabled_starts_bridge(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path, windows_enabled=True))
        assert rig.windows.started is True
        assert rig.coordinator.state == EngineState.READY


class TestTunnelStart:
    def test_named_tunnel_url_computed_once(self, tmp_path: Path) -> None:
        rig = make_rig()
        opts = base_options(
            tmp_path, connection=ConnectionMethod.CLOUDFLARE, public_hostname="bridge.example.com"
        )
        rig.coordinator.start(opts)
        assert rig.coordinator.public_url == "https://bridge.example.com"
        assert rig.coordinator.url_mutable is False

    def test_quick_tunnel_url_is_mutable(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path, connection=ConnectionMethod.QUICK))
        assert rig.coordinator.public_url == "https://quick-abc123.trycloudflare.com/mcp"
        assert rig.coordinator.url_mutable is True

    def test_public_tunnel_targets_gateway_port(self, tmp_path: Path) -> None:
        rig = make_rig()
        options = base_options(tmp_path, connection=ConnectionMethod.QUICK)
        rig.coordinator.start(options)
        assert rig.tunnel.port == options.gateway_port

    def test_local_mode_uses_codex_port(self, tmp_path: Path) -> None:
        rig = make_rig()
        options = base_options(tmp_path, connection=ConnectionMethod.LOCAL)
        rig.coordinator.start(options)
        assert rig.tunnel.port == options.codexpro_port

    def test_tunnel_failure_takes_service_to_error(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.tunnel.wait_fails = True
        rig.coordinator.start(base_options(tmp_path, connection=ConnectionMethod.QUICK))
        assert rig.coordinator.state == EngineState.ERROR
        assert rig.tunnel.stopped


class TestWindowsFailure:
    def test_windows_failure_errors_and_cleans_up(self, tmp_path: Path) -> None:
        rig = make_rig(windows_wait_fails=True)
        rig.coordinator.start(base_options(tmp_path, windows_enabled=True))
        assert rig.coordinator.state == EngineState.ERROR
        assert rig.codex.stopped is True
        assert rig.tunnel.stopped is False
        assert "启动失败" in (rig.windows.error or "")


class TestStopAndListeners:
    def test_local_stop_returns_to_idle(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path))
        rig.coordinator.stop()
        assert rig.coordinator.state == EngineState.IDLE
        assert rig.codex.stopped is True

    def test_stop_order_codex_windows_tunnel(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(base_options(tmp_path, windows_enabled=True))
        order: list[str] = []
        for engine in (rig.codex, rig.windows, rig.tunnel):
            orig = engine.stop
            engine.stop = lambda e=engine, o=orig: (order.append(e.label), o())  # type: ignore[method-assign]
        rig.coordinator.stop()
        assert order == ["codex", "windows"]  # tunnel never started in local mode

    def test_stop_includes_tunnel_when_named(self, tmp_path: Path) -> None:
        rig = make_rig()
        rig.coordinator.start(
            base_options(tmp_path, connection=ConnectionMethod.CLOUDFLARE, public_hostname="bridge.example.com")
        )
        order: list[str] = []
        for engine in (rig.codex, rig.windows, rig.tunnel):
            orig = engine.stop
            engine.stop = lambda e=engine, o=orig: (order.append(e.label), o())  # type: ignore[method-assign]
        rig.coordinator.stop()
        assert order == ["codex", "tunnel"]

    def test_idle_stop_is_noop(self) -> None:
        rig = make_rig()
        rig.coordinator.stop()
        assert rig.coordinator.state == EngineState.IDLE

    def test_listeners_receive_state_transitions(self, tmp_path: Path) -> None:
        rig = make_rig()
        seen: list[str] = []
        rig.coordinator.listen(lambda state, msg: seen.append(state.value))
        rig.coordinator.start(base_options(tmp_path))
        rig.coordinator.stop()
        assert "启动中" in seen
        assert "已连接" in seen
        assert "未启动" in seen


class TestRestartKeepsUrl:
    def test_fixed_hostname_url_survives_restart(self, tmp_path: Path) -> None:
        rig = make_rig()
        opts = base_options(
            tmp_path, connection=ConnectionMethod.CLOUDFLARE, public_hostname="bridge.example.com"
        )
        rig.coordinator.start(opts)
        first = rig.coordinator.public_url
        rig.coordinator.stop()
        rig.coordinator.start(opts)
        assert rig.coordinator.public_url == first == "https://bridge.example.com"
        assert rig.coordinator.url_mutable is False