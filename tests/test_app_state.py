from __future__ import annotations

from pathlib import Path

import pytest

import local_dev_mcp_bridge.app_state as app_state
from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.engines import EngineState, SpawnError
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod


class FakeTunnel:
    def __init__(self, *, wait_fails: bool = False) -> None:
        self.state = EngineState.IDLE
        self.port = 0
        self.public_url = ""
        self.wait_fails = wait_fails
        self.starts: list[dict[str, object]] = []
        self.stops = 0
        self.is_running = False

    def start(self, **kwargs) -> None:
        self.starts.append(kwargs)
        self.state = EngineState.STARTING
        self.is_running = True
        kind = kwargs.get("kind")
        if kind == ConnectionMethod.QUICK:
            self.public_url = "https://random.trycloudflare.com/mcp"
        else:
            host = str(kwargs.get("hostname") or "mcp.example.com")
            self.public_url = f"https://{host}/mcp"

    def wait_ready(self) -> bool:
        if self.wait_fails:
            self.state = EngineState.ERROR
            return False
        self.state = EngineState.READY
        return True

    def stop(self) -> None:
        self.stops += 1
        self.state = EngineState.IDLE
        self.is_running = False
        self.public_url = ""


class FakeGateway:
    instances: list[FakeGateway] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.port = 0
        self.is_running = False
        self.stops = 0
        FakeGateway.instances.append(self)

    def start(self, *, port: int) -> None:
        self.port = port
        self.is_running = True

    def stop(self) -> None:
        self.stops += 1
        self.is_running = False


@pytest.fixture(autouse=True)
def _fake_gateway(monkeypatch: pytest.MonkeyPatch):
    FakeGateway.instances.clear()
    monkeypatch.setattr(app_state, "OAuthGateway", FakeGateway)
    monkeypatch.setattr(ServiceCoordinator, "_wait_gateway_ready", staticmethod(lambda _port, timeout_seconds=20.0: True))
    monkeypatch.setattr(ServiceCoordinator, "_check_gateway_port", staticmethod(lambda _port: None))


def make_coord(*, tunnel_fails: bool = False) -> ServiceCoordinator:
    return ServiceCoordinator(tunnel=FakeTunnel(wait_fails=tunnel_fails))  # type: ignore[arg-type]


def test_initial_state_is_idle_and_has_no_project_engines() -> None:
    coord = make_coord()
    assert coord.state == EngineState.IDLE
    assert not hasattr(coord, "codex")
    assert not hasattr(coord, "windows")
    assert coord.component_states()["tunnel"] == EngineState.IDLE


def test_start_options_only_describe_shared_transport() -> None:
    opts = StartOptions()
    assert opts.gateway_port == constants.DEFAULT_GATEWAY_PORT
    assert not hasattr(opts, "project_root")
    assert not hasattr(opts, "codexpro_port")
    assert not hasattr(opts, "windows_mcp_port")


def test_local_start_uses_shared_gateway_and_no_tunnel() -> None:
    coord = make_coord()
    options = StartOptions(connection=ConnectionMethod.LOCAL, gateway_port=19886)
    coord.start(options)
    assert coord.state == EngineState.READY
    assert coord.public_url == "http://127.0.0.1:19886/mcp"
    assert coord.tunnel.port == 19886
    assert coord.tunnel.starts == []  # type: ignore[attr-defined]
    assert len(FakeGateway.instances) == 1
    gateway = FakeGateway.instances[0]
    assert gateway.port == 19886
    assert gateway.kwargs["workspace"] == ""
    assert gateway.kwargs["upstream_url"] == ""


def test_public_tunnel_targets_same_shared_gateway_port() -> None:
    coord = make_coord()
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19890,
    )
    coord.start(options)
    assert coord.state == EngineState.READY
    assert coord.tunnel.port == 19890
    assert coord.public_url == "https://mcp.example.com/mcp"
    assert FakeGateway.instances[0].port == 19890


def test_quick_tunnel_marks_url_mutable() -> None:
    coord = make_coord()
    coord.start(StartOptions(connection=ConnectionMethod.QUICK, gateway_port=19891))
    assert coord.state == EngineState.READY
    assert coord.url_mutable is True
    assert "trycloudflare.com" in coord.public_url


def test_second_start_while_running_is_rejected() -> None:
    coord = make_coord()
    options = StartOptions(gateway_port=19892)
    coord.start(options)
    with pytest.raises(SpawnError):
        coord.start(options)


def test_tunnel_failure_cleans_shared_transport_and_sets_error() -> None:
    coord = make_coord(tunnel_fails=True)
    coord.start(
        StartOptions(
            connection=ConnectionMethod.CLOUDFLARE,
            public_hostname="mcp.example.com",
            gateway_port=19893,
        )
    )
    assert coord.state == EngineState.ERROR
    assert coord.gateway is None
    assert not coord.tunnel.is_running


def test_stop_only_stops_gateway_and_tunnel() -> None:
    coord = make_coord()
    coord.start(
        StartOptions(
            connection=ConnectionMethod.CLOUDFLARE,
            public_hostname="mcp.example.com",
            gateway_port=19894,
        )
    )
    gateway = FakeGateway.instances[0]
    coord.stop()
    assert coord.state == EngineState.IDLE
    assert gateway.stops == 1
    assert coord.tunnel.stops == 1  # type: ignore[attr-defined]
    assert coord.public_url == ""


def test_idle_stop_is_noop() -> None:
    coord = make_coord()
    coord.stop()
    assert coord.state == EngineState.IDLE


def test_listeners_receive_shared_hub_transitions() -> None:
    coord = make_coord()
    seen: list[EngineState] = []
    coord.listen(lambda state, _message: seen.append(state))
    coord.start(StartOptions(gateway_port=19895))
    coord.stop()
    assert EngineState.STARTING in seen
    assert EngineState.READY in seen
    assert EngineState.STOPPING in seen
    assert seen[-1] == EngineState.IDLE


def test_restart_keeps_fixed_public_url() -> None:
    coord = make_coord()
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19896,
    )
    coord.start(options)
    first = coord.public_url
    coord.restart(options)
    assert coord.state == EngineState.READY
    assert coord.public_url == first


def test_source_has_no_entry_project_start_contract() -> None:
    source = Path(app_state.__file__).read_text(encoding="utf-8")
    forbidden = ["project_root:", "codexpro_port:", "windows_mcp_port:", "self.codex", "self.windows"]
    for marker in forbidden:
        assert marker not in source