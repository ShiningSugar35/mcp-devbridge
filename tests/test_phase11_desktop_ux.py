"""v0.6 desktop interaction regressions."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

import local_dev_mcp_bridge.desktop_main as dm


def _window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    manager = dm.ProjectManager()
    project_a = manager.add(str(first), display_name="First")
    project_b = manager.add(str(second), display_name="Second")
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = dm.MainWindow()
    app.processEvents()
    return app, window, project_a, project_b


def _close(app: QApplication, window: dm.MainWindow) -> None:
    window._force_exit = True
    window.coord._state = dm.EngineState.IDLE
    for project in window.pm.list():
        unit = window.pm.unit(project.id)
        if unit is not None:
            unit.codex._state = dm.EngineState.IDLE
            unit.windows._state = dm.EngineState.IDLE
    window.close()
    app.processEvents()


def test_running_project_does_not_disable_other_project(tmp_path: Path, monkeypatch) -> None:
    app, window, project_a, project_b = _window(tmp_path, monkeypatch)
    try:
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
            "工作台", "设备", "项目设置", "诊断", "日志", "使用手册", "设置"
        ]
        assert [window.log_tabs.tabText(i) for i in range(window.log_tabs.count())] == [
            "运行情况", "操作记录", "网络连接"
        ]
        unit_a = window.pm.unit_for(project_a.id)
        assert unit_a is not None
        unit_a.codex._state = dm.EngineState.READY
        window.coord._state = dm.EngineState.READY
        window._refresh_project_list()

        row_a = window._row_of_root(project_a.root_path)
        row_b = window._row_of_root(project_b.root_path)
        button_a = window.project_table.cellWidget(row_a, 4)
        button_b = window.project_table.cellWidget(row_b, 4)
        assert isinstance(button_a, QPushButton)
        assert isinstance(button_b, QPushButton)
        assert button_a.text() == "停止服务"
        assert button_a.isEnabled()
        assert button_b.text() == "启动服务"
        assert button_b.isEnabled()

        window._select_root(project_b.root_path)
        window._apply_selected_project()
        window._poll_status()
        assert not hasattr(window, "start_btn")
        assert window.project_table.cellWidget(row_b, 4) is button_b
        assert button_b.text() == "启动服务"
        assert button_b.isEnabled()
        assert window.permission_combo.isEnabled()
        assert window.client_combo.isEnabled()
        assert window.connection_combo.isEnabled()
        assert window.add_project_btn.isEnabled()

        window._select_root(project_a.root_path)
        window._apply_selected_project()
        window._poll_status()
        assert window.project_table.cellWidget(row_a, 4) is button_a
        assert button_a.text() == "停止服务"
        assert button_a.isEnabled()
        assert not window.permission_combo.isEnabled()
    finally:
        _close(app, window)


def test_admin_setup_failure_can_recover_with_workspace_mode(
    tmp_path: Path, monkeypatch
) -> None:
    import local_dev_mcp_bridge.elevation as elevation

    app, window, project_a, project_b = _window(tmp_path, monkeypatch)
    try:
        window._app_config.first_system_risk_accepted = True
        window._app_config.full_system_risk_accepted = True
        monkeypatch.setattr(dm, "IS_WINDOWS", True)

        class FakeController:
            def ensure_registered(self, *, interactive: bool) -> bool:
                assert interactive is True
                return False

            def ensure_running(self, *, interactive_registration: bool = False):
                raise AssertionError("must not start when registration failed")

        monkeypatch.setattr(elevation, "get_elevation_controller", lambda: FakeController())
        monkeypatch.setattr(window, "_admin_setup_choice", lambda: "workspace")
        projects = window.pm.list()
        assert window._require_start_confirmations(projects) is False
        assert all(project.permission_mode == "workspace" for project in projects)
        stored_a = window.pm.get(project_a.id)
        stored_b = window.pm.get(project_b.id)
        assert stored_a is not None
        assert stored_b is not None
        assert stored_a.permission_mode == "workspace"
        assert stored_b.permission_mode == "workspace"
    finally:
        _close(app, window)


def test_admin_setup_success_continues_without_failure_dialog(
    tmp_path: Path, monkeypatch
) -> None:
    import local_dev_mcp_bridge.elevation as elevation

    app, window, _a, _b = _window(tmp_path, monkeypatch)
    try:
        window._app_config.first_system_risk_accepted = True
        window._app_config.full_system_risk_accepted = True
        monkeypatch.setattr(dm, "IS_WINDOWS", True)
        calls = {"running": 0}

        class FakeController:
            def ensure_registered(self, *, interactive: bool) -> bool:
                assert interactive is True
                return True

            def ensure_running(self, *, interactive_registration: bool = False):
                assert interactive_registration is True
                calls["running"] += 1
                return {"ok": True, "elevated": True}

        monkeypatch.setattr(elevation, "get_elevation_controller", lambda: FakeController())
        monkeypatch.setattr(
            window,
            "_admin_setup_choice",
            lambda: (_ for _ in ()).throw(AssertionError("failure dialog must not open")),
        )
        assert window._require_start_confirmations(window.pm.list()) is False
        assert calls["running"] == 1
    finally:
        _close(app, window)


def test_workbench_hides_internal_port_and_uses_plain_language(
    tmp_path: Path, monkeypatch
) -> None:
    app, window, _a, _b = _window(tmp_path, monkeypatch)
    try:
        assert window.project_table.isColumnHidden(3)
        assert window.token_copy_btn.text() == "复制访问码"
        assert window.service_url_copy_btn.text() == "复制本机连接地址"
        assert "Gateway" not in window._service_url_text()
        forbidden = ("UAC", "broker", "full_system", "token", "Gateway")
        plain_text = dm.ADMIN_SETUP_TITLE + dm.ADMIN_SETUP_TEXT
        assert all(term not in plain_text for term in forbidden)
    finally:
        _close(app, window)


def test_close_to_tray_hides_without_quitting(tmp_path: Path, monkeypatch) -> None:
    app, window, _a, _b = _window(tmp_path, monkeypatch)
    try:
        window.show()
        app.processEvents()
        window._app_config.close_behavior = "tray"
        menu = window.tray_icon.contextMenu()
        assert menu is not None
        menu_texts = [action.text() for action in menu.actions() if action.text()]
        assert "显示主窗口" in menu_texts
        assert "退出 MCP DevBridge" in menu_texts
        if window.tray_icon.isVisible():
            window.close()
            app.processEvents()
            assert not window.isVisible()
            window._show_from_tray()
            app.processEvents()
            assert window.isVisible()
    finally:
        _close(app, window)
