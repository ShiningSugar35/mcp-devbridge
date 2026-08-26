"""ProjectManager / ProjectUnit tests: catalog CRUD, per-project ports,
parallel engine lifecycle (fake units), auto-restore and views.
Real dual-engine spawn verification lives in test_parallel_real_engines
(skipped automatically when node.exe or the CodexPro dist is absent).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.engines import EngineState, SpawnError, port_listening
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.project_manager import ProjectManager, ProjectView

TOKEN = "t" * 32


class _FakeCodex:
    def __init__(self, port: int) -> None:
        self.port = port
        self.started = False

    @property
    def is_running(self) -> bool:
        return self.started

    def stop(self, timeout_seconds: float = 8.0) -> None:
        self.started = False


class _FakeUnit:
    """Records start/stop calls instead of spawning real engines."""

    def __init__(self, project: ProjectConfig) -> None:
        self.project = project
        self.calls: list[dict[str, object]] = []
        self._state = EngineState.IDLE
        self.engine_pid = 1000 + hash(project.id) % 900
        self.codex = _FakeCodex(project.codexpro_port or constants.DEFAULT_CODEXPRO_PORT)
        self.windows = _FakeCodex(project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT)

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
        self.calls.append(
            {
                "permission_mode": permission_mode,
                "windows_enabled": windows_enabled,
                "windows_token": windows_token,
                "elevated": elevated,
            }
        )
        self.codex.started = True
        self.windows.started = windows_enabled
        if windows_enabled and self.windows.port == 0:
            self.windows.port = (
                self.project.windows_bridge_port or constants.DEFAULT_WINDOWS_MCP_PORT
            )
        self._state = EngineState.READY

    def wait_ready(self, timeout_seconds: float | None = None) -> bool:
        return self._state == EngineState.READY

    def data_plane_health(self, token: str, timeout_seconds: float = 2.0) -> tuple[bool, str]:
        _ = token, timeout_seconds
        return (
            self._state == EngineState.READY,
            "ok" if self._state == EngineState.READY else "not ready",
        )

    def stop(self, timeout_seconds: float = 8.0) -> None:
        self._state = EngineState.IDLE
        self.codex.started = False

    def log_tail(self, count: int = 200) -> str:
        return ""


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ProjectManager, Path]:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(config_dir))
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    return ProjectManager(unit_factory=lambda p: _FakeUnit(p)), tmp_path


@pytest.fixture()
def real_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ProjectManager, Path]:
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(config_dir))
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB").mkdir()
    return ProjectManager(), tmp_path


def test_add_assigns_unique_ports(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj_a = pm.add(str(tmp / "projA"))
    proj_b = pm.add(str(tmp / "projB"))
    assert proj_a.id and proj_b.id and proj_a.id != proj_b.id
    assert proj_a.codexpro_port and proj_b.codexpro_port
    assert proj_a.codexpro_port != proj_b.codexpro_port
    assert proj_a.windows_bridge_port != proj_b.windows_bridge_port
    assert proj_a.codexpro_port != proj_b.windows_bridge_port


def test_list_backfills_engine_ports_and_ids_for_legacy_configs(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    os.environ["LOCALDEV_MCP_CONFIG_DIR"] = str(config_dir)
    try:
        legacy = ProjectConfig(
            id="",
            display_name="legacy",
            root_path=str(tmp_path / "legacy"),
            permission_mode="workspace",
        )
        save_projects([legacy])
        projects = ProjectManager(unit_factory=lambda p: _FakeUnit(p)).list()
        assert len(projects) == 1
        assert projects[0].id
        assert projects[0].codexpro_port == constants.DEFAULT_CODEXPRO_PORT
        assert projects[0].windows_bridge_port == constants.DEFAULT_WINDOWS_MCP_PORT
        assert not hasattr(projects[0], "gateway_port")
    finally:
        os.environ.pop("LOCALDEV_MCP_CONFIG_DIR", None)


def test_legacy_per_project_gateway_port_is_ignored(tmp_path: Path) -> None:
    config_dir = tmp_path / "config-many"
    os.environ["LOCALDEV_MCP_CONFIG_DIR"] = str(config_dir)
    try:
        payload = {
            "projects": [
                {
                    "id": "legacy-a",
                    "display_name": "legacy-a",
                    "root_path": str(tmp_path / "legacy-a"),
                    "codexpro_port": 18787,
                    "windows_bridge_port": 28731,
                    "gateway_port": 18786,
                },
                {
                    "id": "legacy-b",
                    "display_name": "legacy-b",
                    "root_path": str(tmp_path / "legacy-b"),
                    "codexpro_port": 18788,
                    "windows_bridge_port": 28732,
                    "gateway_port": 18789,
                },
            ]
        }
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "projects.json").write_text(json.dumps(payload), encoding="utf-8")
        projects = ProjectManager(unit_factory=lambda p: _FakeUnit(p)).list()
        assert all(not hasattr(project, "gateway_port") for project in projects)
        claimed = {
            *(project.codexpro_port for project in projects),
            *(project.windows_bridge_port for project in projects),
        }
        assert len(claimed) == 4
    finally:
        os.environ.pop("LOCALDEV_MCP_CONFIG_DIR", None)


def test_duplicate_add_returns_existing(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj_a = pm.add(str(tmp / "projA"))
    again = pm.add(str(tmp / "projA"))
    assert again.id == proj_a.id
    assert len(pm.list()) == 1


def test_start_single_project(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj = pm.add(str(tmp / "projA"))
    view = pm.start(proj.id, codex_token=TOKEN)
    assert view.state == EngineState.READY.value
    unit: Any = pm.unit(proj.id)
    assert unit is not None
    assert unit.calls[0]["permission_mode"] == "system"
    assert unit.calls[0]["windows_enabled"] is False


def test_start_uses_project_permission(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj = pm.add(str(tmp / "projA"), permission_mode="system")
    pm.start(proj.id, codex_token=TOKEN, execution_profile="full_system")
    unit: Any = pm.unit(proj.id)
    assert unit is not None
    assert unit.calls[0]["permission_mode"] == "system"
    assert unit.windows.started is False


@pytest.mark.skipif(os.name != "nt", reason="Windows elevation manager only")
def test_project_unit_permission_downgrade_replaces_elevated_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_dev_mcp_bridge.elevation import ElevatedCodexProManager
    from local_dev_mcp_bridge.engines import CodexProManager
    from local_dev_mcp_bridge.project_manager import ProjectUnit

    root = tmp_path / "project"
    root.mkdir()
    project = ProjectConfig(
        id="downgrade-test",
        display_name="downgrade-test",
        root_path=str(root),
        permission_mode="workspace",
        codexpro_port=18787,
        windows_bridge_port=28731,
    )
    unit = ProjectUnit(project, log_dir=tmp_path / "logs")
    unit.codex = ElevatedCodexProManager(
        project.id,
        log_dir=tmp_path / "logs",
        port=project.codexpro_port,
    )
    monkeypatch.setattr(CodexProManager, "start", lambda self, *args, **kwargs: None)
    unit.start(TOKEN, permission_mode="workspace", elevated=False)
    assert isinstance(unit.codex, CodexProManager)
    assert not isinstance(unit.codex, ElevatedCodexProManager)


def test_windows_starts_only_when_enabled_and_token(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj = pm.add(str(tmp / "projA"))
    proj.windows_enabled = True
    pm.update(proj)
    pm.start(proj.id, codex_token=TOKEN, windows_token="w" * 32)
    unit: Any = pm.unit(proj.id)
    assert unit is not None
    assert unit.calls[0]["windows_enabled"] is True
    assert unit.windows.started is True


def test_parallel_lifecycle_isolation(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj_a = pm.add(str(tmp / "projA"))
    proj_b = pm.add(str(tmp / "projB"))
    view_a = pm.start(proj_a.id, codex_token=TOKEN)
    view_b = pm.start(proj_b.id, codex_token=TOKEN)
    assert view_a.state == EngineState.READY.value
    assert view_b.state == EngineState.READY.value
    assert view_a.codexpro_port != view_b.codexpro_port
    pm.stop(proj_a.id)
    unit_a: Any = pm.unit(proj_a.id)
    unit_b: Any = pm.unit(proj_b.id)
    assert unit_a is not None and unit_b is not None
    assert unit_a.state == EngineState.IDLE
    assert unit_b.state == EngineState.READY
    pm.stop_all()
    assert unit_b.state == EngineState.IDLE


def test_start_enabled_auto_restore(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj_a = pm.add(str(tmp / "projA"))
    proj_b = pm.add(str(tmp / "projB"))
    proj_a.enabled = True
    proj_b.enabled = False
    pm.update(proj_a)
    pm.update(proj_b)
    started = pm.start_enabled(codex_token=TOKEN)
    assert [v.id for v in started] == [proj_a.id]
    unit_b = pm.unit(proj_b.id)
    assert unit_b is None or unit_b.state == EngineState.IDLE


def test_remove_stops_engines_and_drops_catalog(manager: tuple[ProjectManager, Path]) -> None:
    pm, tmp = manager
    proj = pm.add(str(tmp / "projA"))
    pm.start(proj.id, codex_token=TOKEN)
    unit = pm.unit(proj.id)
    pm.remove(proj.id)
    assert unit is not None and unit.state == EngineState.IDLE
    assert pm.get(proj.id) is None
    assert pm.views() == []


def test_missing_project_start_raises(manager: tuple[ProjectManager, Path]) -> None:
    pm, _tmp = manager
    with pytest.raises(SpawnError):
        pm.start("no-such-id", codex_token=TOKEN)


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node.exe not available",
)
def test_parallel_real_engines(real_manager: tuple[ProjectManager, Path]) -> None:
    """Live spawn: two real CodexPro engines on two projects' own ports."""
    pm, tmp = real_manager

    def _ready(pm: ProjectManager, project_id: str, seconds: float = 8.0) -> ProjectView:
        deadline = time.monotonic() + seconds
        last = pm.view(project_id)
        while time.monotonic() < deadline:
            last = pm.view(project_id)
            if last.state == EngineState.READY.value:
                return last
            time.sleep(0.25)
        return last

    proj_a = pm.add(str(tmp / "projA"))
    proj_b = pm.add(str(tmp / "projB"))
    # The desktop bridge may be running on production ports while tests execute.
    # Use isolated test ports so the suite never interferes with the active MCP session.
    proj_a.codexpro_port = 19787
    proj_b.codexpro_port = 19788
    proj_a.windows_bridge_port = 29731
    proj_b.windows_bridge_port = 29732
    pm.reconfigure(proj_a)
    pm.reconfigure(proj_b)
    try:
        pm.start(proj_a.id, codex_token=TOKEN)
        pm.start(proj_b.id, codex_token=TOKEN)
        a = _ready(pm, proj_a.id)
        b = _ready(pm, proj_b.id)
        assert a.state == EngineState.READY.value
        assert b.state == EngineState.READY.value
        assert a.codexpro_port != b.codexpro_port
        assert port_listening(a.codexpro_port)
        assert port_listening(b.codexpro_port)
    finally:
        pm.stop_all()
    time.sleep(0.5)
    assert not port_listening(a.codexpro_port)
    assert not port_listening(b.codexpro_port)


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-root naming regression")
def test_v081_drive_root_gets_nonempty_display_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_dev_mcp_bridge.config_store import load_projects

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg_drive"))
    save_projects(
        [
            ProjectConfig(
                id="drive-root",
                display_name="",
                root_path="C:\\",
                codexpro_port=19001,
                windows_bridge_port=29001,
            )
        ]
    )
    projects = ProjectManager(unit_factory=lambda p: _FakeUnit(p)).list()
    assert projects[0].display_name == "C:"
    assert load_projects()[0].display_name == "C:"
