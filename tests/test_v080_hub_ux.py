from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

import local_dev_mcp_bridge.desktop_main as dm
from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.platform_support import IS_WINDOWS
from local_dev_mcp_bridge.update_manager import bundled_upgrade_script, is_newer, version_tuple


def test_update_version_helpers_and_bundled_script() -> None:
    assert version_tuple("v0.8.0") == (0, 8, 0)
    assert is_newer("0.8.0", "0.7.2")
    assert not is_newer("0.7.2", "0.8.0")
    assert bundled_upgrade_script().name == ("live_upgrade.ps1" if IS_WINDOWS else "live_upgrade.sh")
    assert bundled_upgrade_script().is_file()


def test_ready_project_stop_button_waits_for_async_busy_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "fetch_latest_release", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    project_root = tmp_path / "project"
    project_root.mkdir()
    manager = dm.ProjectManager()
    project = manager.add(str(project_root), display_name="Demo")
    app = QApplication.instance() or QApplication([])
    window = dm.MainWindow()
    try:
        window._select_root(project.root_path)
        window._apply_selected_project()
        unit = window.pm.unit_for(project.id)
        assert unit is not None
        unit.codex._state = EngineState.READY
        window._busy_project_ids.add(project.id)
        window._poll_status()
        assert project.id in window._busy_project_ids
        row = window._row_of_root(project.root_path)
        button = window.project_table.cellWidget(row, 4)
        assert isinstance(button, QPushButton)
        assert button.text() == "停止服务"
        assert not button.isEnabled()
        window._set_project_busy(project.id, False)
        assert project.id not in window._busy_project_ids
        assert button.isEnabled()
    finally:
        window._force_exit = True
        window.coord._state = EngineState.IDLE
        unit = window.pm.unit(project.id)
        if unit is not None:
            unit.codex._state = EngineState.IDLE
            unit.windows._state = EngineState.IDLE
        window.close()
        app.processEvents()


def test_update_button_hidden_until_new_release(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    app = QApplication.instance() or QApplication([])
    window = dm.MainWindow()
    try:
        assert window._update_timer.interval() == 12 * 60 * 60 * 1000
        assert window.update_btn.isHidden()
        info = dm.ReleaseInfo(
            version="9.9.9",
            tag="v9.9.9",
            name="Future",
            notes="notes",
            download_url="https://example.invalid/setup.exe",
            size=1,
            sha256="",
        )
        monkeypatch.setattr(dm, "fetch_latest_release", lambda: info)
        monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
        window._check_for_updates()
        assert not window.update_btn.isHidden()
        assert window._latest_release == info
    finally:
        window._force_exit = True
        window.coord._state = EngineState.IDLE
        window.close()
        app.processEvents()
