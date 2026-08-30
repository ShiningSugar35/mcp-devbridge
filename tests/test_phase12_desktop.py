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


def test_ready_diagnostics_are_project_bound_and_show_connection_layers(
    tmp_path: Path, monkeypatch
) -> None:
    app, window, project = _window(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_selftest(url: str, _access: str | None = None, **kwargs: Any):
        calls.append({"url": url, **kwargs})
        result = dm.SelftestResult(
            ok=True,
            tool_count=50,
            schema_fingerprint="a" * 64,
            hub_contract_match=True,
        )
        for step in ("initialize", "streamable_http", "list_tools", "server_config"):
            result.add(step, True, step)
        return result

    try:
        project.connection = dm.ConnectionMethod.LOCAL.value
        window.pm.update(project)
        monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
        monkeypatch.setattr(dm, "_hub_access_token", lambda **_kwargs: "diagnostic-access")
        monkeypatch.setattr(dm, "run_selftest", fake_selftest)
        monkeypatch.setattr(window, "_project_state", lambda _project: dm.EngineState.READY)
        monkeypatch.setattr(
            window.coord,
            "recovery_snapshot",
            lambda: {"gateway_restart_seconds_ago": None, "public_restart_seconds_ago": None},
        )

        window._run_diagnostics()
        output = window.diag_output.toPlainText()

        assert len(calls) == 1
        assert calls[0]["route_workspace_id"] == project.id
        assert calls[0]["expect_hub_contract"] is True
        assert calls[0]["timeout"] == 15.0
        assert "分层连接状态" in output
        assert "公开工具数量：正常" in output
        assert "Schema 指纹：正常" in output
        assert "工作区连续性：正常" in output
        assert "ChatGPT 会话边界：边界清晰" in output
    finally:
        _close(app, window)


def test_public_diagnostics_checks_both_legs_before_session_blame(
    tmp_path: Path, monkeypatch
) -> None:
    app, window, project = _window(tmp_path, monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_selftest(url: str, _access: str | None = None, **kwargs: Any):
        calls.append({"url": url, **kwargs})
        if url.startswith("http://127.0.0.1"):
            result = dm.SelftestResult(
                ok=True,
                tool_count=50,
                schema_fingerprint="a" * 64,
                hub_contract_match=True,
            )
            for step in ("initialize", "streamable_http", "list_tools", "server_config"):
                result.add(step, True, step)
            return result
        result = dm.SelftestResult(
            ok=False,
            tool_count=25,
            schema_fingerprint="b" * 64,
            hub_contract_match=False,
            error="public contract mismatch",
        )
        for step in ("initialize", "streamable_http", "list_tools"):
            result.add(step, True, step)
        result.add("hub_contract", False, "contract mismatch")
        return result

    try:
        project.connection = dm.ConnectionMethod.QUICK.value
        window.pm.update(project)
        window.coord._public_url = "https://mcp.example.test/mcp"
        monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
        monkeypatch.setattr(dm, "_hub_access_token", lambda **_kwargs: "diagnostic-access")
        monkeypatch.setattr(dm, "run_selftest", fake_selftest)
        monkeypatch.setattr(window, "_project_state", lambda _project: dm.EngineState.READY)
        monkeypatch.setattr(
            window.coord,
            "recovery_snapshot",
            lambda: {"gateway_restart_seconds_ago": None, "public_restart_seconds_ago": None},
        )

        window._run_diagnostics()
        output = window.diag_output.toPlainText()

        assert len(calls) == 2
        assert all(call["route_workspace_id"] == project.id for call in calls)
        assert "本机连接服务：正常" in output
        assert "公网入口：异常" in output
        assert "公开工具数量：异常" in output
        assert "ChatGPT 会话边界：未判定" in output
    finally:
        _close(app, window)


def test_ready_diagnostics_surface_missing_access_authorization(
    tmp_path: Path, monkeypatch
) -> None:
    app, window, project = _window(tmp_path, monkeypatch)

    def fake_selftest(_url: str, _access: str | None = None, **_kwargs: Any):
        result = dm.SelftestResult(
            ok=True,
            tool_count=50,
            schema_fingerprint="a" * 64,
            hub_contract_match=True,
        )
        for step in ("initialize", "streamable_http", "list_tools", "server_config"):
            result.add(step, True, step)
        return result

    try:
        project.connection = dm.ConnectionMethod.LOCAL.value
        window.pm.update(project)
        monkeypatch.setattr(dm, "_run_async", lambda fn, callback: callback(fn()))
        monkeypatch.setattr(dm, "_hub_access_token", lambda **_kwargs: "")
        monkeypatch.setattr(dm, "run_selftest", fake_selftest)
        monkeypatch.setattr(window, "_project_state", lambda _project: dm.EngineState.READY)
        monkeypatch.setattr(
            window.coord,
            "recovery_snapshot",
            lambda: {"gateway_restart_seconds_ago": None, "public_restart_seconds_ago": None},
        )

        window._run_diagnostics()
        output = window.diag_output.toPlainText()

        assert "还没有连接访问码" in output
        assert "访问授权：异常" in output
    finally:
        _close(app, window)


def test_v081_run_async_keeps_completion_signal_alive() -> None:
    import time

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    received: list[str] = []
    guards_before = set(dm._ASYNC_SIGNAL_GUARDS)
    dm._run_async(lambda: "ready", lambda result: received.append(str(result)))
    created_guards = dm._ASYNC_SIGNAL_GUARDS - guards_before
    assert len(created_guards) == 1
    deadline = time.monotonic() + 3.0
    while not received and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert received == ["ready"]
    app.processEvents()
    assert created_guards.isdisjoint(dm._ASYNC_SIGNAL_GUARDS)
