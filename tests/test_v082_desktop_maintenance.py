from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGroupBox, QMessageBox, QPushButton

import local_dev_mcp_bridge.desktop_main as dm
from local_dev_mcp_bridge.engines import EngineState


def _window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "fetch_latest_release", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    manager = dm.ProjectManager()
    project_a = manager.add(str(first), display_name="First")
    project_b = manager.add(str(second), display_name="Second")
    project_b.git_user_name = "keep-me"
    manager.update(project_b)

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = dm.MainWindow()
    app.processEvents()
    return app, window, project_a, project_b


def _close(app: QApplication, window: dm.MainWindow) -> None:
    window._force_exit = True
    window.coord._state = EngineState.IDLE
    for project in window.pm.list():
        unit = window.pm.unit(project.id)
        if unit is not None:
            unit.codex._state = EngineState.IDLE
            unit.windows._state = EngineState.IDLE
    window.close()
    app.processEvents()


def test_project_row_button_identity_survives_status_polls_and_duplicate_card_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    app, window, project_a, _project_b = _window(tmp_path, monkeypatch)
    try:
        row = window._row_of_root(project_a.root_path)
        button = window.project_table.cellWidget(row, 4)
        assert isinstance(button, QPushButton)
        for _ in range(5):
            window._poll_status()
            assert window.project_table.cellWidget(row, 4) is button

        titles = [box.title() for box in window.findChildren(QGroupBox)]
        assert "当前项目" not in titles
        assert not hasattr(window, "start_btn")
        assert window.project_table.columnCount() == 5
        headers: list[str] = []
        for i in range(window.project_table.columnCount()):
            header = window.project_table.horizontalHeaderItem(i)
            assert header is not None
            headers.append(header.text())
        assert headers == ["名称", "路径", "状态", "端口", "操作"]
        assert window.all_projects_btn.text() == "启动所有项目"
    finally:
        _close(app, window)


def test_save_connection_and_permission_card_to_all_projects(tmp_path: Path, monkeypatch) -> None:
    app, window, project_a, project_b = _window(tmp_path, monkeypatch)
    remembered: dict[str, str] = {}
    cleared: list[str] = []
    monkeypatch.setattr(dm, "remember_project_tunnel_token", lambda project_id, value: remembered.__setitem__(project_id, value))
    monkeypatch.setattr(dm, "clear_project_tunnel_token", lambda project_id: cleared.append(project_id))
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    try:
        window._select_root(project_a.root_path)
        window._apply_selected_project()
        window.permission_combo.setCurrentIndex(1)
        window.client_combo.setCurrentIndex(window.client_combo.findData("gemini"))
        window.connection_combo.setCurrentIndex(
            window.connection_combo.findData(dm.ConnectionMethod.CLOUDFLARE.value)
        )
        window.hostname_edit.setText("mcp.example.com")
        window.bridge_check.setChecked(True)
        window.gemini_uri_edit.setText("https://example.com/oauth/callback")
        window.cf_token_edit.setText("placeholder-value")

        window._save_settings_for_all_projects()

        for project_id in (project_a.id, project_b.id):
            saved = window.pm.get(project_id)
            assert saved is not None
            assert saved.permission_mode == "workspace"
            assert saved.client_target == "gemini"
            assert saved.connection == dm.ConnectionMethod.CLOUDFLARE.value
            assert saved.public_hostname == "mcp.example.com"
            assert saved.windows_enabled is True
            assert saved.gemini_redirect_uri == "https://example.com/oauth/callback"
            assert remembered[project_id] == "placeholder-value"
        saved_b = window.pm.get(project_b.id)
        assert saved_b is not None and saved_b.git_user_name == "keep-me"
        assert not cleared
    finally:
        _close(app, window)


def test_stopping_one_running_root_keeps_hub_until_last_root_stops(tmp_path: Path, monkeypatch) -> None:
    app, window, project_a, project_b = _window(tmp_path, monkeypatch)
    stopped_hub: list[bool] = []
    monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))

    unit_a = window.pm.unit_for(project_a.id)
    unit_b = window.pm.unit_for(project_b.id)
    assert unit_a is not None
    assert unit_b is not None
    unit_a.codex._state = EngineState.READY
    unit_b.codex._state = EngineState.READY
    window.coord._state = EngineState.READY
    window._service_root = project_a.root_path

    def fake_pm_stop(project_id: str) -> None:
        unit = window.pm.unit_for(project_id)
        assert unit is not None
        unit.codex._state = EngineState.IDLE
        unit.windows._state = EngineState.IDLE

    def fake_hub_stop() -> None:
        stopped_hub.append(True)
        window.coord._state = EngineState.IDLE

    monkeypatch.setattr(window.pm, "stop", fake_pm_stop)
    monkeypatch.setattr(window.coord, "stop", fake_hub_stop)

    try:
        window._stop_project_engine_for(project_a)
        assert window._project_state(project_a) == EngineState.IDLE
        assert window._project_state(project_b) == EngineState.READY
        assert window.coord.state == EngineState.READY
        assert not stopped_hub

        window._stop_project_engine_for(project_b)
        assert window._project_state(project_b) == EngineState.IDLE
        assert window.coord.state == EngineState.IDLE
        assert stopped_hub == [True]
    finally:
        _close(app, window)


def test_start_and_stop_all_projects_state_machine(tmp_path: Path, monkeypatch) -> None:
    app, window, project_a, project_b = _window(tmp_path, monkeypatch)
    started: list[str] = []
    stopped: list[str] = []
    access_value = "x" * 32
    bridge_value = "y" * 32

    monkeypatch.setattr(dm, "ensure_project_access_token", lambda _project_id: access_value)
    monkeypatch.setattr(dm, "_bridge_token", lambda ensure=False: bridge_value)
    monkeypatch.setattr(window, "_save_project_settings", lambda *args, **kwargs: True)
    monkeypatch.setattr(window, "_ports_conflict", lambda _options: None)
    monkeypatch.setattr(window, "_bind_coord_engines", lambda _project_id=None: None)
    monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
    monkeypatch.setattr(window.pm, "start", lambda project_id, **_kwargs: started.append(project_id))
    monkeypatch.setattr(window.pm, "stop", lambda project_id: stopped.append(project_id))

    def fake_coord_start(_options) -> None:
        window.coord._state = EngineState.READY

    def fake_coord_stop() -> None:
        window.coord._state = EngineState.IDLE

    monkeypatch.setattr(window.coord, "start", fake_coord_start)
    monkeypatch.setattr(window.coord, "stop", fake_coord_stop)
    window._app_config.first_system_risk_accepted = True
    window._app_config.full_system_risk_accepted = True

    try:
        window._select_root(project_a.root_path)
        window._apply_selected_project()
        window._start_all_projects()
        assert started == [project_b.id]
        assert window._bulk_project_action is None
        assert not window._busy_project_ids
        assert window.all_projects_btn.text() == "停止所有项目"

        window._stop_all_projects()
        assert set(stopped) == {project_a.id, project_b.id}
        assert window._bulk_project_action is None
        assert not window._busy_project_ids
        assert window.all_projects_btn.text() == "启动所有项目"
    finally:
        _close(app, window)
