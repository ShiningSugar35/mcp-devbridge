"""v0.6 product-default regressions."""

from __future__ import annotations

from local_dev_mcp_bridge.config_store import load_app_config, load_projects
from local_dev_mcp_bridge.models import AppConfig, ProjectConfig


def test_window_close_defaults_to_system_tray() -> None:
    assert AppConfig().close_behavior == "tray"


def test_fresh_install_has_no_machine_specific_project_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "fresh-config"))
    config = load_app_config()
    assert config.active_workspace is None
    assert load_projects() == []


def test_new_project_public_and_optional_fields_start_clean() -> None:
    project = ProjectConfig(root_path=r"C:\user-selected-project")
    assert project.public_hostname == ""
    assert project.gemini_redirect_uri == ""
    assert project.git_user_name == ""
    assert project.git_user_email == ""
    assert project.default_push_remote == ""
    assert project.default_push_branch == ""
    assert project.windows_enabled is False
    assert project.connection == "local"
    assert project.codexpro_port == 0
    assert project.windows_bridge_port == 0
    assert not hasattr(project, "gateway_port")
