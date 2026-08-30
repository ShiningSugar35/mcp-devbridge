from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import local_dev_mcp_bridge.app_state as app_state
from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.flight_recorder import FlightRecorder
from local_dev_mcp_bridge.gateway import _DiagnosticMiddleware
from local_dev_mcp_bridge.tunnel_manager import ConnectionMethod, TunnelManager


def _rows(log_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(log_dir.glob("flight-recorder-*.jsonl*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                assert isinstance(value, dict)
                rows.append(value)
    return rows


class _SnapshotTunnel:
    state = EngineState.READY
    pid: int | None = 4242
    last_pid = 4242
    last_exit_code = 17
    is_running = True

    def log_tail(self, count: int = 200) -> str:
        _ = count
        return "Authorization: Bearer super-secret\nlookup argotunnel.com: i/o timeout\n"


def test_component_snapshot_preserves_bounded_disconnect_forensics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = FlightRecorder(tmp_path / "logs")
    snapshot_tunnel = _SnapshotTunnel()
    coord = ServiceCoordinator(
        tunnel=snapshot_tunnel,  # type: ignore[arg-type]
        project_runtime_registry=lambda: [
            {
                "project_id": "project-secret",
                "state": str(EngineState.READY),
                "engine_pid": 31337,
                "engine_port": 18888,
            }
        ],
        flight_recorder=recorder,
    )
    coord.gateway = type("Gateway", (), {"is_running": True})()  # type: ignore[assignment]
    coord._active_options = StartOptions(
        connection=ConnectionMethod.CLOUDFLARE,
        public_hostname="mcp.example.com",
        gateway_port=18886,
    )
    coord._set_url("https://mcp.example.com/mcp", False)
    coord._set_state(EngineState.READY, "test-ready")
    monkeypatch.setattr(app_state, "port_listening", lambda port, timeout=0.3: port in {18886, 18888})
    coord._transport_probe = lambda url, timeout_seconds: (True, 0.025, 200)
    coord._deep_mcp_health_tick = lambda options: None  # type: ignore[method-assign]

    coord._transport_health_tick()
    snapshot_tunnel.state = EngineState.ERROR
    snapshot_tunnel.pid = None
    snapshot_tunnel.is_running = False
    coord._record_component_snapshot(reason="post-success")

    snapshots = [row for row in _rows(tmp_path / "logs") if row.get("event") == "component_snapshot"]
    assert snapshots
    row = snapshots[-1]
    assert row["gateway_port"] == 18886
    assert row["gateway_listener"] is True
    assert row["tunnel_pid"] == 4242
    assert row["tunnel_exit_code"] == 17
    assert row["last_local_success_ts"]
    assert row["last_public_success_ts"]
    assert int(str(row["last_local_success_mono_ns"])) > 0
    assert int(str(row["last_public_success_mono_ns"])) > 0
    assert "super-secret" not in str(row["tunnel_recent_output"])
    assert "Bearer ***" in str(row["tunnel_recent_output"])
    projects = row["projects"]
    assert isinstance(projects, list) and projects
    project = projects[0]
    assert isinstance(project, dict)
    assert project["engine_listener"] is True
    assert project["engine_pid"] == 31337
    assert str(project["project_id"]) != "project-secret"


def test_tunnel_remembers_exit_code_after_failed_readiness() -> None:
    class Log:
        def tail(self, count: int = 200) -> str:
            _ = count
            return "lookup argotunnel.com: i/o timeout\n"

    class Proc:
        pid = 9911
        is_running = False
        returncode = 23
        log = Log()

        def stop(self, timeout_seconds: float = 8.0) -> None:
            _ = timeout_seconds

    tunnel = TunnelManager(cloudflared_exe="cloudflared")
    tunnel.kind = ConnectionMethod.CLOUDFLARE
    tunnel._proc = Proc()  # type: ignore[assignment]

    assert tunnel.wait_ready(timeout_seconds=0.1) is False
    assert tunnel.last_pid == 9911
    assert tunnel.last_exit_code == 23


def test_http_terminal_records_mcp_sse_and_oauth_component(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path / "logs")

    async def invoke(path: str, content_type: bytes) -> None:
        async def app(scope, receive, send):
            _ = scope, receive
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", content_type)],
                }
            )
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            _ = message

        middleware = _DiagnosticMiddleware(app, recorder)
        await middleware({"type": "http", "method": "POST", "path": path}, receive, send)

    asyncio.run(invoke("/mcp", b"text/event-stream; charset=utf-8"))
    asyncio.run(invoke("/token", b"application/json"))

    terminals = [row for row in _rows(tmp_path / "logs") if row.get("event") == "request_terminal"]
    assert len(terminals) == 2
    mcp, oauth = terminals
    assert mcp["component"] == "mcp"
    assert mcp["streaming"] is True
    assert "text/event-stream" in str(mcp["content_type"])
    assert oauth["component"] == "oauth"
    assert oauth["streaming"] is False
    assert oauth["content_type"] == "application/json"
