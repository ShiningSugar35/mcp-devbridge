"""BackendManager (desktop-side lifecycle) tests using the real server_main."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from local_dev_mcp_bridge.backend_manager import (
    BackendError,
    BackendManager,
    backend_health,
    port_in_use,
)
from local_dev_mcp_bridge.models import RuntimeConfig


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_workspace(root: Path) -> Path:
    ws = root / "工作区"
    ws.mkdir()
    return ws


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALDEV_MCP_NO_CREDENTIAL_MANAGER", "1")
    return tmp_path


class TestBackendManager:
    def test_start_health_stop(self, _isolate: Path) -> None:
        workspace = _make_workspace(_isolate)
        mgr = BackendManager(config_dir=_isolate / "cfg")
        rc = RuntimeConfig(workspace=str(workspace), local_port=_free_port())
        assert not mgr.is_running

        mgr.start(rc, wait_ready=True)
        assert mgr.is_running
        health = mgr.health()
        assert health is not None
        assert health["workspace"] == str(workspace)
        assert health["permission_mode"] == "workspace"
        assert backend_health(f"http://127.0.0.1:{rc.local_port}") is not None

        mgr.stop()
        assert not mgr.is_running
        assert backend_health(f"http://127.0.0.1:{rc.local_port}") is None

    def test_port_blocked_raises(self, _isolate: Path) -> None:
        workspace = _make_workspace(_isolate)
        busy = _free_port()
        mgr = BackendManager(config_dir=_isolate / "cfg")
        rc = RuntimeConfig(workspace=str(workspace), local_port=busy)
        mgr.start(rc, wait_ready=True)
        assert port_in_use(busy)

        other = BackendManager(config_dir=_isolate / "cfg")
        other_rc = RuntimeConfig(workspace=str(workspace), local_port=busy)
        with pytest.raises(BackendError, match="已被占用"):
            other.start(other_rc)
        other.stop()
        mgr.stop()

    def test_stop_idempotent(self, _isolate: Path) -> None:
        workspace = _make_workspace(_isolate)
        mgr = BackendManager(config_dir=_isolate / "cfg")
        rc = RuntimeConfig(workspace=str(workspace), local_port=_free_port())
        mgr.start(rc, wait_ready=True)
        mgr.stop()
        mgr.stop()
        assert not mgr.is_running

    def test_current_config_roundtrip(self, _isolate: Path) -> None:
        workspace = _make_workspace(_isolate)
        mgr = BackendManager(config_dir=_isolate / "cfg")
        rc = RuntimeConfig(workspace=str(workspace), local_port=_free_port(), test_command="uv run pytest")
        mgr.start(rc, wait_ready=False)
        loaded = mgr.current_config()
        assert loaded is not None
        assert loaded.test_command == "uv run pytest"
        mgr.stop()

    def test_start_missing_workspace_fails(self, _isolate: Path) -> None:
        mgr = BackendManager(config_dir=_isolate / "cfg")
        rc = RuntimeConfig(workspace=str(_isolate / "不存在"), local_port=_free_port())
        with pytest.raises(BackendError, match="项目目录不存在"):
            mgr.start(rc, wait_ready=True)
