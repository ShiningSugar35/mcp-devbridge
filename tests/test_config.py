"""Config store and secret store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.config_store import (
    detect_project_features,
    get_project,
    load_app_config,
    load_projects,
    load_tunnel_state,
    save_app_config,
    save_tunnel_state,
    suggest_commands,
    upsert_project,
)
from local_dev_mcp_bridge.models import ProjectConfig, TunnelState, git_field_error
from local_dev_mcp_bridge.secrets import SecretsStore, generate_token


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("LOCALDEV_MCP_NO_CREDENTIAL_MANAGER", "0")
    return tmp_path


class TestConfigStore:
    def test_app_config_roundtrip(self, isolated_config: Path) -> None:
        cfg = load_app_config()
        assert cfg.auth_mode == "bearer"
        cfg.auth_mode = "anonymous"
        cfg.first_system_risk_accepted = True
        save_app_config(cfg)
        loaded = load_app_config()
        assert loaded.auth_mode == "anonymous"
        assert loaded.first_system_risk_accepted

    def test_projects_upsert_and_ordering(self, isolated_config: Path) -> None:
        p1 = ProjectConfig(root_path=r"D:\proj\甲", display_name="甲", last_used_at="2026-01-01T00:00:00")
        p2 = ProjectConfig(root_path=r"D:\proj\乙", display_name="乙", last_used_at="2026-02-01T00:00:00")
        upsert_project(p1)
        upsert_project(p2)
        assert len(load_projects()) == 2
        # updating an existing project keeps a single entry
        p1_updated = ProjectConfig(root_path=r"D:\proj\甲", display_name="甲2", last_used_at="2026-03-01T00:00:00")
        upsert_project(p1_updated)
        projects = load_projects()
        assert len(projects) == 2
        assert get_project(r"D:\proj\甲").display_name == "甲2"

    def test_corrupt_config_file(self, isolated_config: Path) -> None:
        (isolated_config / "config.json").write_text("{ not valid json", encoding="utf-8")
        assert load_app_config().version == 1
        (isolated_config / "projects.json").write_text("garbage", encoding="utf-8")
        assert load_projects() == []

    def test_tunnel_state_roundtrip(self, isolated_config: Path) -> None:
        state = TunnelState(tunnel_id="abc", tunnel_name="ldmb", hostname="mcp.example.com", origin_domain="example.com")
        save_tunnel_state(state)
        loaded = load_tunnel_state()
        assert loaded.hostname == "mcp.example.com"
        assert loaded.tunnel_id == "abc"

    def test_detect_features(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")
        features = detect_project_features(tmp_path)
        assert features["pyproject"] and features["uv_lock"]
        commands = suggest_commands(tmp_path)
        assert commands["test_command"] == "uv run pytest"


class TestGitFieldValidation:
    def test_empty_is_always_allowed(self) -> None:
        for kind in ("git_user_name", "git_user_email", "default_push_remote", "default_push_branch"):
            assert git_field_error(kind, "") is None

    def test_valid_values_pass(self) -> None:
        assert git_field_error("git_user_name", "johndoe") is None
        assert git_field_error("git_user_email", "john@example.com") is None
        assert git_field_error("default_push_remote", "origin") is None
        assert git_field_error("default_push_branch", "main") is None

    def test_spaces_rejected(self) -> None:
        assert git_field_error("git_user_name", "john doe") is not None
        assert git_field_error("git_user_email", "john @x.com") is not None
        assert git_field_error("default_push_branch", " main") is not None

    def test_quotes_and_metachars_rejected(self) -> None:
        for bad in ('j"ohn', "'x", "back\\slash", "a;b", "a|b", "a$b", "a<b"):
            assert git_field_error("git_user_name", bad) is not None

    def test_control_chars_rejected(self) -> None:
        assert git_field_error("git_user_name", "a\nb") is not None
        assert git_field_error("git_user_name", "a\tb") is not None

    def test_email_pattern_checked(self) -> None:
        assert git_field_error("git_user_email", "not-an-email") is not None
        assert git_field_error("git_user_email", "a@b") is not None

    def test_project_config_roundtrip_keeps_git_fields(self, isolated_config: Path) -> None:
        project = ProjectConfig(
            root_path=r"D:\proj\git",
            display_name="git",
            git_user_name="johndoe",
            git_user_email="john@example.com",
            default_push_remote="origin",
            default_push_branch="main",
        )
        upsert_project(project)
        loaded = get_project(r"D:\proj\git")
        assert loaded is not None
        assert loaded.git_user_name == "johndoe"
        assert loaded.git_user_email == "john@example.com"
        assert loaded.default_push_remote == "origin"
        assert loaded.default_push_branch == "main"

    def test_old_json_without_git_fields_loads(self, isolated_config: Path) -> None:
        (isolated_config / "projects.json").write_text(
            '{"projects": [{"display_name": "旧项目", "root_path": "D:\\\\old", "permission_mode": "workspace"}]}',
            encoding="utf-8",
        )
        projects = load_projects()
        assert len(projects) == 1
        assert projects[0].git_user_name == ""
        assert projects[0].default_push_branch == ""


class TestSecrets:
    def test_generate_token_length(self) -> None:
        token = generate_token(256)
        assert len(token) >= 40

    def test_roundtrip_and_rotation(self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(isolated_config))
        store = SecretsStore(use_credential_manager=False)
        key = constants.ACCESS_TOKEN_CRED_NAME
        store.set(key, "token-v1")
        assert store.get(key) == "token-v1"
        store.set(key, "token-v2")
        assert store.get(key) == "token-v2"
        store.delete(key)
        assert store.get(key) is None

    def test_secret_file_is_not_plaintext(self, isolated_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(isolated_config))
        store = SecretsStore(use_credential_manager=False)
        store.set(constants.ACCESS_TOKEN_CRED_NAME, "super-secret-token-value")
        raw = (isolated_config / "secrets.dpapi.json").read_bytes()
        assert b"super-secret-token-value" not in raw
