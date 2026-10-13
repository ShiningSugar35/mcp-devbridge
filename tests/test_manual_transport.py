from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from local_dev_mcp_bridge.app_state import EngineState, ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod


def rig(tmp_path, monkeypatch, mode: Literal["direct", "relay"] = "relay"):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    events = []
    tunnel = SimpleNamespace(state=EngineState.IDLE, is_running=mode == "direct", public_url="https://example.com/mcp")
    relay = SimpleNamespace(provisioned=True, configured=True, is_running=mode == "relay", validate=lambda: None)
    def local_start(**kwargs):
        events.append("direct.start")
        tunnel.is_running = True
    def local_stop():
        events.append("direct.stop")
        tunnel.is_running = False
    def relay_start():
        events.append("relay.start")
        relay.is_running = True
    def relay_stop():
        events.append("relay.stop")
        relay.is_running = False
    tunnel.start = local_start
    tunnel.stop = local_stop
    tunnel.wait_ready = lambda **kwargs: True
    relay.start = relay_start
    relay.stop = relay_stop
    c = ServiceCoordinator(tunnel=cast(Any, tunnel), relay=cast(Any, relay), workspace_registry=lambda _: (8789, "D:\\"), workspace_credential_registry=lambda _: "synthetic")
    c.gateway = cast(Any, SimpleNamespace(is_running=True))
    c._state = EngineState.READY
    c._public_url = "https://example.com/mcp"
    c._active_options = StartOptions(connection=ConnectionMethod.CLOUDFLARE, public_hostname="example.com", cloudflare_transport=mode)
    monkeypatch.setattr("local_dev_mcp_bridge.config_store.load_projects", lambda: [SimpleNamespace(id="D")])
    monkeypatch.setattr(c, "_mcp_probe", lambda url, token, route: (True, 0.01, "ok"))
    return c, events


def test_relay_start_does_not_launch_local_connector(tmp_path, monkeypatch):
    c, events = rig(tmp_path, monkeypatch)
    assert c._active_options is not None
    assert c._start_public_tunnel_with_retry(c._active_options, phase="initial_start")[0]
    assert events == ["relay.start"]


def test_relay_health_uses_relay_liveness_not_local_process(tmp_path, monkeypatch):
    c, events = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(c, "_transport_probe", lambda *args: (True, 0.01, 200))
    c._transport_health_tick()
    assert not events


def test_switch_retains_gateway_and_stops_other_route_before_verification(tmp_path, monkeypatch):
    c, events = rig(tmp_path, monkeypatch)
    gateway = c.gateway
    def probe(url, token, route):
        assert route == "D"
        assert not c.relay.is_running
        assert c.tunnel.is_running
        events.append("verify")
        return True, 0.01, "ok"
    monkeypatch.setattr(c, "_mcp_probe", probe)
    ok, _ = c.switch_cloudflare_transport("direct")
    assert ok
    assert events[:3] == ["relay.stop", "direct.start", "verify"]
    assert c.gateway is gateway
    assert c.connection_snapshot()["verified"]


@pytest.mark.parametrize("rollback_ok", [True, False])
def test_failed_switch_restores_original_mode_without_false_recovery(tmp_path, monkeypatch, rollback_ok):
    c, events = rig(tmp_path, monkeypatch)
    monkeypatch.setattr(c, "_mcp_probe", lambda *args: (rollback_ok if getattr(c._active_options, "cloudflare_transport", "") == "relay" else False, 0.01, "failed"))
    ok, message = c.switch_cloudflare_transport("direct")
    assert not ok
    assert c.connection_snapshot()["mode"] == "relay"
    assert c.connection_snapshot()["verified"] is rollback_ok
    assert "relay.start" in events
    assert ("已恢复" in message) is rollback_ok


def test_unknown_relay_must_not_launch_direct(tmp_path, monkeypatch):
    c, events = rig(tmp_path, monkeypatch)
    def refuse():
        raise RuntimeError("ownership mismatch")
    c.relay.stop = refuse
    ok, _ = c.switch_cloudflare_transport("direct")
    assert not ok
    assert "direct.start" not in events


def test_target_preflight_failure_keeps_working_direct_connector(tmp_path, monkeypatch):
    c, events = rig(tmp_path, monkeypatch, "direct")
    c._public_mcp_verified = True
    def refuse():
        raise RuntimeError("foreign unit")
    c.relay.validate = refuse
    ok, _ = c.switch_cloudflare_transport("relay")
    assert not ok
    assert not events
    assert c.tunnel.is_running
    assert c.connection_snapshot()["verified"]
