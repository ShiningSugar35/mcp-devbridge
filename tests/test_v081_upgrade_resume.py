from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import local_dev_mcp_bridge.desktop_main as dm
import local_dev_mcp_bridge.device_hub as device_hub
from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.engines import EngineState


class _MemoryStore:
    values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_live_upgrade_script_snapshots_equal_running_roots_and_shared_gateway() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "live_upgrade.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "resume_project_roots" in script
    assert "$resumeProjectRoots" in script
    assert "Test-LoopbackPort -Port $candidatePort" in script
    assert "project_roots = @($resumeRoots)" in script
    assert "Get-ExpectedPort" in script
    assert "$global.gateway_port" in script
    assert "$project.gateway_port" not in script
    assert "public entry project" not in script
    assert "install_dir = $currentInstallDir" in script
    assert '/DIR="{0}"' in script
    assert 'Join-Path $installDir "MCPDevBridge.exe"' in script
    assert "$launcherElevated" in script
    assert 'launcher_elevated = [bool]$launcherElevated' in script
    assert '$createArgs += @("/RL", "HIGHEST")' in script


def test_upgrade_resume_restores_all_roots_without_entry_owner(tmp_path: Path, monkeypatch) -> None:
    _MemoryStore.values = {}
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "SecretsStore", _MemoryStore)
    monkeypatch.setattr(device_hub, "SecretsStore", _MemoryStore)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)

    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()
    manager = dm.ProjectManager()
    project_a = manager.add(str(root_a), display_name="A")
    project_b = manager.add(str(root_b), display_name="B")

    app = QApplication.instance() or QApplication([])
    window = dm.MainWindow()
    calls: list[str] = []
    try:
        window._app_config.first_system_risk_accepted = True
        window._app_config.full_system_risk_accepted = True
        monkeypatch.setattr(window, "_start_project_engine_for", lambda project: calls.append(project.root_path))
        request = constants.config_dir() / "upgrade-resume.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps({"project_roots": [project_a.root_path, project_b.root_path]}),
            encoding="utf-8",
        )

        window._resume_upgrade_if_requested()

        assert set(calls) == {project_a.root_path, project_b.root_path}
        assert len(calls) == 2
        assert not hasattr(window, "_service_root")
        assert not hasattr(window, "_start_service")
        assert not request.exists()
    finally:
        window._force_exit = True
        window.coord._state = EngineState.IDLE
        for project in window.pm.list():
            unit = window.pm.unit(project.id)
            if unit is not None:
                unit.codex._state = EngineState.IDLE
                unit.windows._state = EngineState.IDLE
        window.close()
        app.processEvents()