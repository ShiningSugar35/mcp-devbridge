"""v0.6 desktop interaction regressions."""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QPushButton

import local_dev_mcp_bridge.desktop_main as dm


def _window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
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
        window._service_root = project_a.root_path
        window.coord._state = dm.EngineState.READY
        window._refresh_project_list()

        row_a = window._row_of_root(project_a.root_path)
        row_b = window._row_of_root(project_b.root_path)
        button_a = window.project_table.cellWidget(row_a, 5)
        button_b = window.project_table.cellWidget(row_b, 5)
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
        assert window.project_table.cellWidget(row_b, 5) is button_b
        assert button_b.text() == "启动服务"
        assert button_b.isEnabled()
        assert window.permission_combo.isEnabled()
        assert window.client_combo.isEnabled()
        assert window.connection_combo.isEnabled()
        assert window.add_project_btn.isEnabled()

        window._select_root(project_a.root_path)
        window._apply_selected_project()
        window._poll_status()
        assert window.project_table.cellWidget(row_a, 5) is button_a
        assert button_a.text() == "停止服务"
        assert button_a.isEnabled()
        assert not window.permission_combo.isEnabled()
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
