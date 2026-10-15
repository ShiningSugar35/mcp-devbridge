"""v0.9.0 shared-client connection UX and legacy-config regressions."""
from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

import local_dev_mcp_bridge.desktop_main as dm


def _window(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(dm, "get_project_access_token", lambda _project_id: None)
    monkeypatch.setattr(dm, "get_project_tunnel_token", lambda _project_id: None)
    monkeypatch.setattr(
        dm, "fetch_latest_release",
        lambda: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    root = tmp_path / "legacy-gemini"
    root.mkdir()
    manager = dm.ProjectManager()
    project = manager.add(str(root), display_name="Legacy Gemini")
    project.client_target = "gemini"
    project.gemini_redirect_uri = "https://old.example/callback"
    project.connection = dm.ConnectionMethod.CLOUDFLARE.value
    project.public_hostname = "mcp.example.test"
    manager.update(project)
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    window = dm.MainWindow()
    window._select_root(str(root))
    window._apply_selected_project()
    app.processEvents()
    return app, window, project


def _close(app: QApplication, window: dm.MainWindow) -> None:
    window._force_exit = True
    window.coord._state = dm.EngineState.IDLE
    for p in window.pm.list():
        unit = window.pm.unit(p.id)
        if unit is not None:
            unit.codex._state = dm.EngineState.IDLE
            unit.windows._state = dm.EngineState.IDLE
    window.close()
    app.processEvents()


def test_shared_connector_buttons_replace_per_project_client_selection(tmp_path, monkeypatch):
    app, window, _ = _window(tmp_path, monkeypatch)
    try:
        assert not hasattr(window, "client_combo")
        assert not hasattr(window, "gemini_box")
        assert "Gemini" in window.url_copy_btn.text()
        assert "ChatGPT" in window.no_auth_url_copy_btn.text()
        assert window._display_url() == "https://mcp.example.test/mcp"
        # The address shown in the textbox must itself be copy/paste-ready:
        # no status decoration may be appended to a valid MCP URL.
        window._refresh_url_ui()
        assert window.url_edit.text() == "https://mcp.example.test/mcp"
        window.url_edit.selectAll()
        window.url_edit.copy()
        assert QApplication.clipboard().text() == "https://mcp.example.test/mcp"
        assert window._no_auth_server_url().startswith("https://mcp.example.test/mcp?token=")
        assert "连接客户端" not in [
            label.text() for label in window.findChildren(QLabel)
        ]
    finally:
        _close(app, window)


def test_one_click_copy_works_for_both_clients_without_switching_project(tmp_path, monkeypatch):
    app, window, original = _window(tmp_path, monkeypatch)
    try:
        saved_before = window.pm.get(original.id)
        assert saved_before is not None
        before = saved_before.model_dump()
        window.url_copy_btn.click()
        assert QApplication.clipboard().text() == "https://mcp.example.test/mcp"
        window.no_auth_url_copy_btn.click()
        assert QApplication.clipboard().text() == window._no_auth_server_url()
        assert QApplication.clipboard().text().startswith("https://mcp.example.test/mcp?token=")
        saved_after = window.pm.get(original.id)
        assert saved_after is not None
        assert saved_after.model_dump() == before
    finally:
        _close(app, window)


def test_editing_legacy_gemini_project_does_not_rewrite_client_identity(tmp_path, monkeypatch):
    app, window, project = _window(tmp_path, monkeypatch)
    try:
        assert window._save_project_settings(show_errors=False) is True
        saved = window.pm.get(project.id)
        assert saved is not None
        assert saved.client_target == "gemini"
        assert saved.gemini_redirect_uri == "https://old.example/callback"
        assert saved.public_hostname == "mcp.example.test"
    finally:
        _close(app, window)
