from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import local_dev_mcp_bridge.app_state as app_state
from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.engines import EngineState, SpawnError
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod


class FakeTunnel:
    def __init__(
        self,
        *,
        wait_fails: bool = False,
        wait_results: list[bool] | None = None,
        failure_detail: str = "lookup argotunnel.com: i/o timeout",
    ) -> None:
        self.state = EngineState.IDLE
        self.port = 0
        self.public_url = ""
        self.wait_fails = wait_fails
        self.wait_results = list(wait_results or [])
        self.failure_detail = failure_detail
        self.error: str | None = None
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
        failed = self.wait_fails
        if self.wait_results:
            failed = not self.wait_results.pop(0)
        if failed:
            self.state = EngineState.ERROR
            self.error = "?????????"
            self.is_running = False
            return False
        self.state = EngineState.READY
        self.error = None
        return True

    def log_tail(self, count: int = 200) -> str:
        _ = count
        return self.failure_detail

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
    monkeypatch.setattr(ServiceCoordinator, "_start_transport_monitor", lambda self, options: None)


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


def test_second_start_with_same_transport_is_idempotent() -> None:
    coord = make_coord()
    options = StartOptions(gateway_port=19892)
    coord.start(options)
    coord.start(options)
    assert len(FakeGateway.instances) == 1


def test_second_start_with_different_transport_is_rejected() -> None:
    coord = make_coord()
    coord.start(StartOptions(gateway_port=19892))
    with pytest.raises(SpawnError):
        coord.start(StartOptions(gateway_port=19897))


def test_concurrent_public_start_is_single_flight() -> None:
    tunnel = FakeTunnel()
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19898,
    )
    entered = threading.Event()
    release = threading.Event()
    original_start = tunnel.start

    def slow_start(**kwargs) -> None:
        entered.set()
        assert release.wait(2)
        original_start(**kwargs)

    tunnel.start = slow_start  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            coord.start(options)
        except BaseException as exc:  # pragma: no cover - assertion aid
            errors.append(exc)

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(2)
    second.join(2)

    assert errors == []
    assert len(tunnel.starts) == 1
    assert len(FakeGateway.instances) == 1
    assert coord.state == EngineState.READY


def test_tunnel_failure_cleans_shared_transport_and_sets_error() -> None:
    tunnel = FakeTunnel(
        wait_fails=True,
        failure_detail="invalid tunnel configuration",
    )
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    coord.start(
        StartOptions(
            connection=ConnectionMethod.QUICK,
            gateway_port=19893,
        )
    )
    assert coord.state == EngineState.ERROR
    assert coord.gateway is None
    assert not coord.tunnel.is_running


def test_transient_tunnel_start_failure_retries_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel = FakeTunnel(wait_results=[False, True])
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    monkeypatch.setattr(app_state, "TUNNEL_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    coord.start(
        StartOptions(
            connection=ConnectionMethod.CLOUDFLARE,
            public_hostname="mcp.example.com",
            gateway_port=19910,
        )
    )

    assert coord.state == EngineState.READY
    assert len(tunnel.starts) == 2
    assert coord.public_url == "https://mcp.example.com/mcp"
    assert coord.message


def test_permanent_tunnel_configuration_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel = FakeTunnel(
        wait_results=[False, False, False],
        failure_detail="ERR invalid tunnel configuration: credentials rejected",
    )
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    monkeypatch.setattr(app_state, "TUNNEL_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))

    coord.start(
        StartOptions(
            connection=ConnectionMethod.CLOUDFLARE,
            public_hostname="mcp.example.com",
            gateway_port=19911,
        )
    )

    assert len(tunnel.starts) == 1
    assert coord.message


def test_runtime_tunnel_rebuild_retries_without_demoting_local_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tunnel = FakeTunnel(wait_results=[True, False, True])
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    monkeypatch.setattr(app_state, "TUNNEL_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19912,
    )
    coord.start(options)
    gateway = FakeGateway.instances[0]
    coord._write_transport_health = lambda **fields: None  # type: ignore[method-assign]

    coord._restart_public_tunnel()

    assert coord.state == EngineState.READY
    assert gateway.is_running
    assert len(tunnel.starts) == 3
    assert coord.message


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



def test_public_health_failures_restart_tunnel_only() -> None:
    tunnel = FakeTunnel()
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19899,
    )
    coord.start(options)
    gateway = FakeGateway.instances[0]
    coord._write_transport_health = lambda **fields: None  # type: ignore[method-assign]

    def probe(url: str, timeout_seconds: float) -> tuple[bool, float, int]:
        _ = timeout_seconds
        if url.startswith("http://127.0.0.1"):
            return True, 0.01, 200
        return False, 0.2, 0

    coord._transport_probe = probe
    coord._last_transport_restart = -1000
    for _ in range(app_state.TRANSPORT_HEALTH_FAILURE_THRESHOLD):
        coord._transport_health_tick()

    assert tunnel.stops == 1
    assert len(tunnel.starts) == 2
    assert FakeGateway.instances == [gateway]
    assert gateway.is_running
    assert coord.state == EngineState.READY


def test_dead_owned_tunnel_restarts_even_when_public_health_is_healthy() -> None:
    tunnel = FakeTunnel()
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19900,
    )
    coord.start(options)
    gateway = FakeGateway.instances[0]
    events: list[dict[str, object]] = []
    coord._write_transport_health = lambda **fields: events.append(fields)  # type: ignore[method-assign]
    coord._transport_probe = lambda url, timeout_seconds: (True, 0.01, 200)
    coord._last_transport_restart = -1000.0

    tunnel.is_running = False
    tunnel.state = EngineState.ERROR
    tunnel.error = "owned tunnel process exited"
    coord._transport_health_tick()

    assert len(tunnel.starts) == 2
    assert FakeGateway.instances == [gateway]
    assert gateway.is_running
    assert coord.state == EngineState.READY
    assert any(item.get("event") == "tunnel_process_unhealthy" for item in events)


def test_deep_mcp_failure_restarts_gateway_only_when_project_engine_is_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from local_dev_mcp_bridge import config_store

    tunnel = FakeTunnel()
    project_id = "project-a"
    project_port = 19901
    gateway_port = 19902
    monkeypatch.setattr(
        config_store,
        "load_projects",
        lambda: [SimpleNamespace(id=project_id, root_path="D:/project-a")],
    )
    coord = ServiceCoordinator(
        tunnel=tunnel,  # type: ignore[arg-type]
        workspace_registry=lambda item: (project_port, "D:/project-a") if item == project_id else None,
        workspace_credential_registry=lambda item: "x" * 32 if item == project_id else None,
    )
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=gateway_port,
    )
    coord.start(options)
    original_gateway = FakeGateway.instances[0]
    coord._write_transport_health = lambda **fields: None  # type: ignore[method-assign]
    coord._transport_probe = lambda url, timeout_seconds: (True, 0.01, 200)
    monkeypatch.setattr(app_state, "DEEP_MCP_PROBE_INTERVAL_SECONDS", 0.0, raising=False)

    calls: list[str] = []

    def deep_probe(url: str, token: str, route_workspace_id: str = "") -> tuple[bool, float, str]:
        assert token == "x" * 32
        calls.append(url)
        if f":{project_port}/mcp" in url:
            assert route_workspace_id == ""
            return True, 0.01, "ok"
        if f":{gateway_port}/mcp" in url:
            assert route_workspace_id == project_id
            return False, 0.02, "tools/list timed out"
        return True, 0.03, "ok"

    coord._mcp_probe = deep_probe  # type: ignore[attr-defined]
    coord._last_deep_mcp_probe = -1000.0  # type: ignore[attr-defined]
    coord._last_gateway_restart = -1000.0
    for _ in range(app_state.GATEWAY_HEALTH_FAILURE_THRESHOLD):
        coord._transport_health_tick()

    assert any(f":{project_port}/mcp" in url for url in calls)
    assert any(f":{gateway_port}/mcp" in url for url in calls)
    assert original_gateway.stops == 1
    assert len(FakeGateway.instances) == 2
    assert tunnel.stops == 0
    assert coord.state == EngineState.READY


def test_source_has_no_entry_project_start_contract() -> None:
    source = Path(app_state.__file__).read_text(encoding="utf-8")
    forbidden = ["project_root:", "codexpro_port:", "windows_mcp_port:", "self.codex", "self.windows"]
    for marker in forbidden:
        assert marker not in source


def test_dns_host_not_found_is_classified_as_transient() -> None:
    tunnel = FakeTunnel(
        wait_fails=True,
        failure_detail="lookup region1.v2.argotunnel.com: host not found",
    )
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]

    category, retryable, detail_hash = coord._tunnel_failure_classification()

    assert category == "transient_network_or_dns"
    assert retryable is True
    assert len(detail_hash) == 16


def test_stop_interrupts_tunnel_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_failure = threading.Event()

    class SignallingTunnel(FakeTunnel):
        def wait_ready(self) -> bool:
            result = super().wait_ready()
            if not result:
                first_failure.set()
            return result

    tunnel = SignallingTunnel(
        wait_results=[False, False, False],
        failure_detail="lookup argotunnel.com: i/o timeout",
    )
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    monkeypatch.setattr(app_state, "TUNNEL_RETRY_BACKOFF_SECONDS", (0.0, 0.6, 0.6))
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19913,
    )
    starter = threading.Thread(target=coord.start, args=(options,), daemon=True)
    starter.start()
    assert first_failure.wait(1.0)

    started = time.monotonic()
    coord.stop()
    elapsed = time.monotonic() - started
    starter.join(timeout=1.0)

    assert elapsed < 0.5
    assert not starter.is_alive()
    assert coord.state == EngineState.IDLE

def test_cloudflare_http2_hint_is_used_on_next_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AdaptiveTunnel(FakeTunnel):
        recommended_protocol = ""
        current_protocol = "auto"

        def start(self, **kwargs) -> None:
            super().start(**kwargs)
            self.current_protocol = str(kwargs.get("cloudflare_protocol") or "auto")

        def wait_ready(self) -> bool:
            if len(self.starts) == 1:
                self.recommended_protocol = "http2"
                self.state = EngineState.ERROR
                self.error = "cloudflare_protocol_fallback:http2"
                self.is_running = False
                return False
            if self.current_protocol != "http2":
                self.state = EngineState.ERROR
                self.error = "expected explicit http2 fallback"
                self.is_running = False
                return False
            self.state = EngineState.READY
            self.error = None
            self.is_running = True
            return True

    tunnel = AdaptiveTunnel()
    coord = ServiceCoordinator(tunnel=tunnel)  # type: ignore[arg-type]
    monkeypatch.setattr(app_state, "TUNNEL_RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    events: list[dict[str, object]] = []
    coord._write_transport_health = lambda **fields: events.append(fields)  # type: ignore[method-assign]
    options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=19914,
    )

    coord.start(options)

    assert len(tunnel.starts) == 2
    assert tunnel.starts[0].get("cloudflare_protocol") == "auto"
    assert tunnel.starts[1].get("cloudflare_protocol") == "http2"
    assert any(
        item.get("event") == "tunnel_protocol_fallback"
        and item.get("from_protocol") == "auto"
        and item.get("to_protocol") == "http2"
        for item in events
    )
    assert coord.state == EngineState.READY
