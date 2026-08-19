"""v0.7 desktop UX: devices, manual, contextual help, logs and diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import local_dev_mcp_bridge.desktop_main as dm
import local_dev_mcp_bridge.device_hub as device_hub


class MemoryStore:
    values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _window(tmp_path: Path, monkeypatch):
    MemoryStore.values = {}
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "SecretsStore", MemoryStore)
    monkeypatch.setattr(device_hub, "SecretsStore", MemoryStore)
    monkeypatch.setattr(dm, "load_project_ui_secrets", lambda _project_id: ("", ""))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    project_root = tmp_path / "project"
    project_root.mkdir()
    manager = dm.ProjectManager()
    project = manager.add(str(project_root), display_name="Demo")
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = dm.MainWindow()
    app.processEvents()
    return app, window, project


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


def _item_text(table: Any, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None
    return item.text()


def test_manual_search_help_and_device_page(tmp_path: Path, monkeypatch) -> None:
    app, window, _project = _window(tmp_path, monkeypatch)
    try:
        assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
            "工作台", "设备", "项目设置", "诊断", "日志", "使用手册", "设置"
        ]
        assert window.device_table.rowCount() == 1
        assert "本机" in _item_text(window.device_table, 0, 0)

        window.manual_search.setText("Quick")
        app.processEvents()
        assert window.manual_list.count() >= 1
        visible_titles = [window.manual_list.item(i).text() for i in range(window.manual_list.count())]
        assert any("Quick" in title or "连接方式" in title for title in visible_titles)
        assert "Quick" in window.manual_browser.toPlainText()

        helps = window.findChildren(dm.HelpButton)
        assert len(helps) >= 5
        assert any("四种方式" in help_button.toolTip() for help_button in helps)
    finally:
        _close(app, window)


def test_process_log_uses_selected_project_and_is_friendly(tmp_path: Path, monkeypatch) -> None:
    app, window, project = _window(tmp_path, monkeypatch)
    try:
        unit = window.pm.unit_for(project.id)
        assert unit is not None
        monkeypatch.setattr(unit.codex, "log_tail", lambda _count=200: "server starting\nserver ready\n")
        window.proc_combo.setCurrentIndex(window.proc_combo.findData("service"))
        window._refresh_process_log()
        assert window.proc_view.rowCount() == 2
        assert _item_text(window.proc_view, 0, 1) == "项目服务"
        assert "正在启动" in _item_text(window.proc_view, 0, 2)
        assert "准备好" in _item_text(window.proc_view, 1, 2)
    finally:
        _close(app, window)


def test_audit_log_is_human_readable(tmp_path: Path, monkeypatch) -> None:
    app, window, _project = _window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(dm, "available_tool_names", lambda: ["read_file"])
        record: dict[str, Any] = {
            "timestamp": "2026-08-13T09:00:01+08:00",
            "tool_name": "read_file",
            "success": True,
            "duration_ms": 18,
            "client_name": "ChatGPT-Test",
            "parameter_summary": {"path": "README.md"},
        }
        monkeypatch.setattr(dm, "query_logs", lambda _query: [record])
        window._refresh_audit_tool_combo()
        window._refresh_audit_log()
        assert window.audit_view.rowCount() == 1
        assert _item_text(window.audit_view, 0, 1) == "读取文件"
        assert _item_text(window.audit_view, 0, 2) == "成功"
        assert _item_text(window.audit_view, 0, 4) == "ChatGPT"
        assert _item_text(window.audit_view, 0, 5) == "文件：README.md"
    finally:
        _close(app, window)


def test_diagnostics_gives_conclusion_and_next_action(tmp_path: Path, monkeypatch) -> None:
    app, window, _project = _window(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
        window._run_diagnostics()
        output = window.diag_output.toPlainText()
        assert output.startswith("需要处理后再使用")
        assert "怎么做：" in output
        assert "项目还没有启动" in output
        # The component line exists only on Diagnostics, not Workbench.
        assert window.component_status.parentWidget() is not window.ctrl_tab
    finally:
        _close(app, window)



def test_v081_run_async_keeps_completion_signal_alive() -> None:
    import time

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    received: list[str] = []
    dm._run_async(lambda: "ready", lambda result: received.append(str(result)))
    deadline = time.monotonic() + 3.0
    while not received and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert received == ["ready"]
    app.processEvents()
    assert not dm._ASYNC_SIGNAL_GUARDS

def test_agent_panel_surface_and_shortcut(tmp_path: Path, monkeypatch) -> None:
    app, window, _project = _window(tmp_path, monkeypatch)

    class FakeOrchestrator:
        def list_agents(self):
            return {
                "agents": [
                    {
                        "id": "agent-1",
                        "title": "Worker A",
                        "role": "worker",
                        "state": "running",
                        "executor": "opencode",
                        "model": "opencode/nemotron-3-ultra-free",
                        "workspace": str(tmp_path),
                        "isolation_mode": "direct",
                        "branch": "",
                        "duration_seconds": 1.25,
                        "terminal": False,
                        "output_tail": "working",
                    }
                ],
                "teams": [
                    {
                        "id": "team-1",
                        "title": "Team A",
                        "stage": "workers",
                        "state": "running",
                        "workspace": str(tmp_path),
                        "integration_branch": "",
                        "terminal": False,
                    }
                ],
                "running": 1,
                "queued": 0,
                "max_parallel": 4,
            }

        def get_agent(self, _agent_id: str):
            return self.list_agents()["agents"][0]

        def get_team(self, _team_id: str):
            return self.list_agents()["teams"][0]

    try:
        monkeypatch.setattr(
            dm,
            "bridge_status",
            lambda: {"enabled": True, "ready": True, "debug_port": 19222},
        )
        fake = FakeOrchestrator()
        panel = dm.AgentPanel(lambda: fake, window)
        app.processEvents()
        assert panel.table.rowCount() == 2
        assert "并发上限 4" in panel.summary.text()
        assert "普通 Chat" in panel.chatgpt_bridge_label.text()
        assert "19222" in panel.chatgpt_bridge_label.text()
        assert panel.prepare_chatgpt_btn.isEnabled() is False
        assert panel.restore_chatgpt_btn.isEnabled() is True
        model_item = panel.table.item(1, 4)
        assert model_item is not None
        assert "nemotron-3-ultra-free" in model_item.text()
        assert window.agent_action.shortcut().toString() == "Ctrl+Shift+A"
        assert "Ctrl+Shift+A" in window.agent_btn.toolTip()
        panel.close()
    finally:
        _close(app, window)
