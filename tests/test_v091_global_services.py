from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QGroupBox, QMessageBox, QPushButton

import local_dev_mcp_bridge.desktop_main as dm
from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.project_secrets import (
    get_global_tunnel_token,
    get_project_tunnel_token,
    remember_global_tunnel_token,
)


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _window(tmp_path: Path, monkeypatch, count: int = 2):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "fetch_latest_release", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(dm, "get_global_tunnel_token", lambda: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "remember_global_tunnel_token", lambda _value: None)
    monkeypatch.setattr(dm, "remember_project_tunnel_token", lambda _project_id, _value: None)
    manager = dm.ProjectManager()
    projects = []
    for idx in range(count):
        root = tmp_path / f"project-{idx}"
        root.mkdir()
        projects.append(manager.add(str(root), display_name=f"Project {idx}"))
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = dm.MainWindow()
    app.processEvents()
    return app, window, projects


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


def test_global_tunnel_credential_is_project_fallback() -> None:
    store = MemoryStore()
    remember_global_tunnel_token("shared-credential", store=store)
    assert get_global_tunnel_token(store=store) == "shared-credential"
    assert get_project_tunnel_token("project-a", store=store) == "shared-credential"
    # The fallback is copied into the project slot, so later project reads remain stable.
    store.delete("LocalDevMCPBridge/GlobalCloudflareTunnelToken")
    assert get_project_tunnel_token("project-a", store=store, migrate_legacy=False) == "shared-credential"


def test_workbench_removes_duplicate_current_project_card(tmp_path: Path, monkeypatch) -> None:
    app, window, _projects = _window(tmp_path, monkeypatch)
    try:
        assert not hasattr(window, "start_btn")
        assert window.all_services_btn.text() == "一键启动所有服务"
        assert isinstance(window.advanced_btn, QPushButton)
        workbench_titles = [
            box.title()
            for box in window.ctrl_tab.findChildren(QGroupBox)
            if box.title()
        ]
        assert "当前项目" not in workbench_titles
        assert "连接信息" in workbench_titles
        assert "设备全局连接配置" in workbench_titles
    finally:
        _close(app, window)


def test_global_connection_syncs_existing_and_new_projects(tmp_path: Path, monkeypatch) -> None:
    applied: dict[str, str] = {}
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: QMessageBox.StandardButton.Ok)
    app, window, projects = _window(tmp_path, monkeypatch)
    monkeypatch.setattr(dm, "remember_global_tunnel_token", lambda value: applied.__setitem__("global", value))
    monkeypatch.setattr(
        dm,
        "remember_project_tunnel_token",
        lambda project_id, value: applied.__setitem__(project_id, value),
    )
    try:
        idx = window.global_connection_combo.findData(dm.ConnectionMethod.CLOUDFLARE.value)
        window.global_connection_combo.setCurrentIndex(idx)
        window.global_hostname_edit.setText("jerry.shiningsugar.shop")
        window.global_cf_token_edit.setText("shared-credential")
        window._save_global_connection_settings()

        assert window._app_config.device_connection == dm.ConnectionMethod.CLOUDFLARE.value
        assert window._app_config.device_public_hostname == "jerry.shiningsugar.shop"
        assert applied["global"] == "shared-credential"
        for project in window.pm.list():
            assert project.connection == dm.ConnectionMethod.CLOUDFLARE.value
            assert project.public_hostname == "jerry.shiningsugar.shop"
            assert applied[project.id] == "shared-credential"

        new_root = tmp_path / "new-project"
        new_root.mkdir()
        window._add_project_dir(str(new_root))
        created = window.pm.by_root(str(new_root))
        assert created is not None
        assert created.connection == dm.ConnectionMethod.CLOUDFLARE.value
        assert created.public_hostname == "jerry.shiningsugar.shop"
        assert applied[created.id] == "shared-credential"
        assert len(window.pm.list()) == len(projects) + 1
    finally:
        _close(app, window)


def test_project_row_button_identity_survives_polling(tmp_path: Path, monkeypatch) -> None:
    app, window, projects = _window(tmp_path, monkeypatch, count=1)
    try:
        project = projects[0]
        row = window._row_of_root(project.root_path)
        first = window.project_table.cellWidget(row, 5)
        assert isinstance(first, QPushButton)
        window._poll_status()
        second = window.project_table.cellWidget(row, 5)
        assert second is first
        window._poll_status()
        third = window.project_table.cellWidget(row, 5)
        assert third is first
    finally:
        _close(app, window)


def test_repeated_stops_do_not_lose_later_project_clicks(tmp_path: Path, monkeypatch) -> None:
    app, window, projects = _window(tmp_path, monkeypatch, count=4)
    monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))

    def fake_stop(project_id: str) -> None:
        unit = window.pm.unit(project_id)
        if unit is not None:
            unit.codex._state = EngineState.IDLE
            unit.windows._state = EngineState.IDLE

    monkeypatch.setattr(window.pm, "stop", fake_stop)
    try:
        for project in projects:
            unit = window.pm.unit_for(project.id)
            assert unit is not None
            unit.codex._state = EngineState.READY
        window._poll_status()

        for project in projects:
            window._toggle_service_for(project.root_path)
            unit = window.pm.unit(project.id)
            assert unit is not None
            assert unit.state == EngineState.IDLE
            assert project.id not in window._busy_project_ids

        assert all(window._project_state(project) == EngineState.IDLE for project in window.pm.list())
        assert window.all_services_btn.text() == "一键启动所有服务"
    finally:
        _close(app, window)


def test_bulk_stop_stops_entry_and_all_project_units(tmp_path: Path, monkeypatch) -> None:
    app, window, projects = _window(tmp_path, monkeypatch, count=4)
    monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))

    def fake_stop(project_id: str) -> None:
        unit = window.pm.unit(project_id)
        if unit is not None:
            unit.codex._state = EngineState.IDLE
            unit.windows._state = EngineState.IDLE

    monkeypatch.setattr(window.pm, "stop", fake_stop)
    try:
        entry = projects[0]
        window._service_root = entry.root_path
        window.coord._state = EngineState.READY
        entry_unit = window.pm.unit_for(entry.id)
        assert entry_unit is not None
        entry_unit.codex._state = EngineState.READY
        for project in projects[1:]:
            unit = window.pm.unit_for(project.id)
            assert unit is not None
            unit.codex._state = EngineState.READY

        def stop_entry() -> None:
            entry_unit.codex._state = EngineState.IDLE
            window.coord._state = EngineState.IDLE

        monkeypatch.setattr(window.coord, "stop_callable", stop_entry)
        window._stop_all_services()

        assert window.coord.state == EngineState.IDLE
        assert window._service_root == ""
        assert not window._all_services_busy
        assert not window._busy_project_ids
        for project in projects:
            assert window._project_state(project) == EngineState.IDLE
        assert window.all_services_btn.text() == "一键启动所有服务"
    finally:
        _close(app, window)


def test_bulk_start_uses_global_gateway_and_starts_all_projects(tmp_path: Path, monkeypatch) -> None:
    app, window, projects = _window(tmp_path, monkeypatch, count=4)
    monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
    monkeypatch.setattr(window, "_require_start_confirmations", lambda _mode=None: False)
    monkeypatch.setattr(window, "_save_project_settings", lambda **_kwargs: True)
    monkeypatch.setattr(window, "_ports_conflict", lambda _options: None)
    for project in projects:
        project.connection = dm.ConnectionMethod.LOCAL.value
        window.pm.update(project)

    # Select a project whose historical per-project gateway port differs from the device port.
    entry = window.pm.get(projects[-1].id)
    assert entry is not None
    window._select_root(entry.root_path)
    window._app_config.gateway_port = dm.constants.DEFAULT_GATEWAY_PORT
    captured: dict[str, object] = {}

    def fake_coord_start(options) -> None:
        captured["gateway_port"] = options.gateway_port
        unit = window.pm.unit_for(entry.id)
        assert unit is not None
        unit.codex._state = EngineState.READY
        window.coord._state = EngineState.READY

    def fake_project_start(project_id: str, **_kwargs):
        unit = window.pm.unit_for(project_id)
        assert unit is not None
        unit.codex._state = EngineState.READY
        return window.pm.view(project_id)

    monkeypatch.setattr(window.coord, "start", fake_coord_start)
    monkeypatch.setattr(window.pm, "start", fake_project_start)
    try:
        window._start_all_services()
        assert captured["gateway_port"] == dm.constants.DEFAULT_GATEWAY_PORT
        assert entry.gateway_port != dm.constants.DEFAULT_GATEWAY_PORT
        assert window._service_root == entry.root_path
        assert not window._all_services_busy
        assert not window._busy_project_ids
        assert all(window._project_state(project) == EngineState.READY for project in window.pm.list())
        assert window.all_services_btn.text() == "一键停止所有服务"
    finally:
        _close(app, window)
