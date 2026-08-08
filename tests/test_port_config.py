"""Port configuration tests: models, persistence, migration, detection."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.backend_manager import port_in_use
from local_dev_mcp_bridge.config_store import (
    load_app_config,
    load_projects,
    save_app_config,
    save_projects,
)
from local_dev_mcp_bridge.models import (
    AppConfig,
    ProjectConfig,
    RuntimeConfig,
    gateway_service_url,
    validate_port,
)


class TestDefaultsCentralized:
    def test_default_ports_live_in_constants(self) -> None:
        assert constants.DEFAULT_GATEWAY_PORT == 8786
        assert constants.DEFAULT_CODEXPRO_PORT == 8787
        assert constants.DEFAULT_WINDOWS_MCP_PORT == 28731
        assert constants.DEFAULT_LEGACY_BACKEND_PORT == 8765

    def test_compat_aliases_point_at_defaults(self) -> None:
        from local_dev_mcp_bridge.engines import CODEXPRO_LOCAL_PORT, WINDOWS_BRIDGE_PORT

        assert CODEXPRO_LOCAL_PORT == constants.DEFAULT_CODEXPRO_PORT
        assert WINDOWS_BRIDGE_PORT == constants.DEFAULT_WINDOWS_MCP_PORT
        assert constants.DEFAULT_LOCAL_PORT == constants.DEFAULT_LEGACY_BACKEND_PORT
        assert constants.GATEWAY_PORT == constants.DEFAULT_GATEWAY_PORT

    def test_app_config_default_ports(self) -> None:
        cfg = AppConfig()
        assert cfg.gateway_port == constants.DEFAULT_GATEWAY_PORT
        assert cfg.codexpro_port == constants.DEFAULT_CODEXPRO_PORT
        assert cfg.windows_mcp_port == constants.DEFAULT_WINDOWS_MCP_PORT
        assert cfg.legacy_backend_port == constants.DEFAULT_LEGACY_BACKEND_PORT


class TestPortValidation:
    def test_invalid_port_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_port(0)
        with pytest.raises(ValueError):
            validate_port(65536)
        with pytest.raises(ValueError):
            validate_port(-1)
        with pytest.raises(ValueError):
            validate_port(True)

    def test_invalid_field_values_rejected(self) -> None:
        for bad in (0, 65536, -5):
            with pytest.raises(ValidationError):
                AppConfig(gateway_port=bad)
        with pytest.raises(ValidationError):
            RuntimeConfig(workspace="C:/x", legacy_backend_port=0)

    def test_gateway_service_url(self) -> None:
        assert gateway_service_url(8786) == "http://localhost:8786"
        assert gateway_service_url(9090) == "http://localhost:9090"
        with pytest.raises(ValueError):
            gateway_service_url(0)


class TestPersistence:
    def test_save_restore_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        cfg = AppConfig()
        cfg.gateway_port = 9090
        cfg.codexpro_port = 9091
        save_app_config(cfg)
        loaded = load_app_config()
        assert loaded.gateway_port == 9090
        assert loaded.codexpro_port == 9091
        assert loaded.windows_mcp_port == constants.DEFAULT_WINDOWS_MCP_PORT

    def test_restart_preserves_ports(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        first = AppConfig()
        first.gateway_port = 10001
        save_app_config(first)
        # 模拟程序重启：重新 load（同一磁盘文件）
        second = load_app_config()
        assert second.gateway_port == 10001

    def test_legacy_config_without_ports_gets_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_workspace": "C:/proj",
                    "auth_mode": "bearer",
                    "require_public_bearer": True,
                    "allow_local_anonymous": True,
                    "log_retention_days": 14,
                    "tunnel_auto_reconnect": True,
                    "exit_stop_managed": False,
                    "first_system_risk_accepted": False,
                    "first_run_version": 0,
                }
            ),
            encoding="utf-8",
        )
        cfg = load_app_config()
        assert cfg.active_workspace == "C:/proj"
        assert cfg.gateway_port == constants.DEFAULT_GATEWAY_PORT
        assert cfg.codexpro_port == constants.DEFAULT_CODEXPRO_PORT

    def test_legacy_projects_ignore_local_port(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        save_projects(
            [
                ProjectConfig(
                    root_path=str(tmp_path / "p1"),
                    display_name="p1",
                    local_port=8899,  # type: ignore[arg-type]  # v0.1 字段（现在被忽略）
                )
            ]
        )
        projects = load_projects()
        assert len(projects) == 1
        assert not hasattr(projects[0], "local_port")

    def test_legacy_runtime_migrates_local_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(parents=True)
        runtime_path = cfg_dir / "runtime.json"
        runtime_path.write_text(
            json.dumps({"workspace": "C:/proj", "permission_mode": "workspace", "local_port": 9090}),
            encoding="utf-8",
        )
        from local_dev_mcp_bridge.config_store import load_runtime_config

        rc = load_runtime_config(runtime_path)
        assert rc is not None
        assert rc.legacy_backend_port == 9090
        assert rc.local_port == 9090  # 兼容属性
        rc.legacy_backend_port = 9111
        assert rc.local_port == 9111
        # 默认值统一为 8765（不再有 2865）
        assert RuntimeConfig(workspace="C:/x").legacy_backend_port == constants.DEFAULT_LEGACY_BACKEND_PORT


class TestPortInUseDetection:
    def test_occupied_port_reported(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            assert port_in_use(port) is True
        finally:
            listener.close()

    def test_free_port_reported(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        assert port_in_use(port) is False


class TestCoordinatorPorts:
    def test_start_options_defaults(self) -> None:
        from local_dev_mcp_bridge.app_state import StartOptions

        opts = StartOptions(project_root="C:/proj")
        assert opts.gateway_port == constants.DEFAULT_GATEWAY_PORT
        assert opts.codexpro_port == constants.DEFAULT_CODEXPRO_PORT
        assert opts.windows_mcp_port == constants.DEFAULT_WINDOWS_MCP_PORT

    def test_start_options_custom_ports_respected(self) -> None:
        from local_dev_mcp_bridge.app_state import StartOptions

        opts = StartOptions(project_root="C:/proj", gateway_port=9990, codexpro_port=9991, windows_mcp_port=9992)
        assert opts.gateway_port == 9990
        assert opts.codexpro_port == 9991
        assert opts.windows_mcp_port == 9992

    def test_engine_port_check_blocks_occupied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from local_dev_mcp_bridge.app_state import ServiceCoordinator, StartOptions
        from local_dev_mcp_bridge.engines import SpawnError

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        occupied = listener.getsockname()[1]

        class FakeManager:
            state = None
            is_running = False
            port = occupied
            error = None

            def start(self, *a, **k):
                raise AssertionError("pre-check must block before start")

            def stop(self):
                pass

            def wait_ready(self) -> bool:
                return True

        coord = ServiceCoordinator(  # type: ignore[reportArgumentType]
            codex=FakeManager(), windows=FakeManager(), tunnel=FakeManager()  # type: ignore[reportArgumentType]
        )
        try:
            opts = StartOptions(project_root="C:/proj", codex_token="t" * 32, codexpro_port=occupied)
            with pytest.raises(SpawnError):
                coord.start(opts)
            assert coord.running is False
        finally:
            listener.close()