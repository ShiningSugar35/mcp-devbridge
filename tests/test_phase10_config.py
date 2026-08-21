"""Phase 10 regression tests: new defaults and per-project protected values."""

from __future__ import annotations

from typing import Any

from local_dev_mcp_bridge import constants
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.project_secrets import (
    ensure_project_access_token,
    get_project_access_token,
    get_project_tunnel_token,
    remember_project_tunnel_token,
)


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


def test_new_project_defaults_to_full_access_chatgpt() -> None:
    project = ProjectConfig(root_path=r"C:\project")
    assert project.permission_mode == "system"
    assert project.client_target == "chatgpt"
    assert project.enabled is False


def test_project_access_values_are_isolated_from_shared_hub_credential() -> None:
    store: Any = MemoryStore()
    hub_value = "hub-access-value"
    store.set(constants.ACCESS_TOKEN_CRED_NAME, hub_value)

    assert get_project_access_token("project-a", store=store) is None
    assert get_project_access_token("project-b", store=store) is None
    project_a_value = ensure_project_access_token("project-a", store=store)
    project_b_value = ensure_project_access_token("project-b", store=store)
    assert project_a_value != hub_value
    assert project_b_value != hub_value
    assert project_a_value != project_b_value
    assert get_project_access_token("project-a", store=store) == project_a_value
    assert get_project_access_token("project-b", store=store) == project_b_value

def test_project_tunnel_values_are_isolated() -> None:
    store: Any = MemoryStore()
    remember_project_tunnel_token("project-a", "value-a", store=store)
    remember_project_tunnel_token("project-b", "value-b", store=store)
    assert get_project_tunnel_token("project-a", store=store, migrate_legacy=False) == "value-a"
    assert get_project_tunnel_token("project-b", store=store, migrate_legacy=False) == "value-b"
