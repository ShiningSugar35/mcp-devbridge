from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
import pytest
from starlette.testclient import TestClient

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig


def _gateway_for_roots(
    tmp_path: Path,
    roots: dict[str, Path],
    *,
    permission_mode: Literal["read_only", "workspace", "system"] = "system",
) -> OAuthGateway:
    projects = [
        ProjectConfig(
            id=project_id,
            display_name=project_id,
            root_path=str(root),
            permission_mode=permission_mode,
        )
        for project_id, root in roots.items()
    ]
    save_projects(projects)

    def registry(project_id: str) -> tuple[int, str] | None:
        keys = list(roots)
        if project_id not in roots:
            return None
        return 19000 + keys.index(project_id), str(roots[project_id])

    return OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(next(iter(roots.values()))),
        upstream_legacy_token=lambda: "hub-credential",
        workspace_registry=registry,
        workspace_credential_registry=lambda project_id: f"credential-{project_id}",
    )


def test_longest_prefix_prefers_nested_running_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    parent = tmp_path / "disk"
    nested = parent / "Environment" / "mcp"
    nested.mkdir(parents=True)
    gateway = _gateway_for_roots(tmp_path, {"parent": parent, "nested": nested})

    assert gateway._workspace_for_path(str(nested / "src" / "file.py")) == "nested"
    assert gateway._workspace_for_path(str(parent / "other" / "file.txt")) == "parent"


def test_relative_existing_path_auto_matches_unique_running_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_b / "unique.txt").write_text("ok", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})

    assert gateway._infer_workspace_for_call("read", {"path": "unique.txt"}) == "b"


def test_workspace_handle_scopes_ambiguous_relative_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit CodexPro handle is stronger than cross-root relative ambiguity."""
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "same.md").write_text("a", encoding="utf-8")
    (root_b / "same.md").write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    gateway._workspace_handle_roots["ws-b-child"] = "b"

    assert (
        gateway._infer_workspace_for_call("read", {"workspace_id": "ws-b-child", "path": "same.md"})
        == "b"
    )


def test_soft_anchor_scopes_ambiguous_relative_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Legacy client affinity is a relative-path context, never an absolute-path fence."""
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "same.md").write_text("a", encoding="utf-8")
    (root_b / "same.md").write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})

    assert (
        gateway._infer_workspace_for_call("read", {"path": "same.md"}, preferred_workspace="b")
        == "b"
    )
    assert (
        gateway._infer_workspace_for_call(
            "read", {"path": str(root_a / "same.md")}, preferred_workspace="b"
        )
        == "a"
    )


def test_outside_path_is_not_misrouted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"allowed": root}, permission_mode="workspace")

    assert gateway._workspace_for_path(str(outside / "blocked.txt")) == ""
    with pytest.raises(ValueError, match="没有运行中的「完全访问」项目"):
        gateway._infer_workspace_for_call("read", {"path": str(outside / "blocked.txt")})


def test_relative_parent_escape_is_not_routed_or_used_as_local_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"allowed": root}, permission_mode="workspace")

    assert gateway._workspace_for_path("../outside") == ""
    assert (
        gateway._infer_workspace_for_call(
            "run_command", {"cwd": "../outside", "command": "echo nope"}
        )
        == ""
    )

    rpc = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "run_command",
            "arguments": {"cwd": "../outside", "command": "echo nope"},
        },
    }
    response = asyncio.run(
        gateway._exec_local_tool(
            "run_command", rpc, rpc["params"], workspace_id="allowed", session_id=""
        )
    )
    payload = json.loads(bytes(response.body))
    assert "error" in payload
    assert "cwd 超出目标工作区根目录" in payload["error"]["message"]


def test_system_access_routes_outside_active_roots_and_allows_local_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"system-root": root}, permission_mode="system")

    assert (
        gateway._infer_workspace_for_call("read", {"path": str(outside / "free.txt")})
        == "system-root"
    )
    assert gateway._local_tool_cwd(root, str(outside), system_access=True) == outside.resolve()
    with pytest.raises(ValueError, match="cwd 超出目标工作区根目录"):
        gateway._local_tool_cwd(root, str(outside), system_access=False)

    if os.name == "nt":
        from types import SimpleNamespace

        class FakeElevation:
            def is_registered(self) -> bool:
                return True

            def execute_command(self, command: str, cwd: Path, timeout: int):
                assert command == "echo system-ok"
                assert cwd == outside.resolve()
                assert timeout == 10
                return SimpleNamespace(
                    shell="powershell",
                    exit_code=0,
                    duration_seconds=0.01,
                    timed_out=False,
                    stdout="system-ok",
                    stderr="",
                )

        monkeypatch.setattr(
            "local_dev_mcp_bridge.elevation.get_elevation_controller",
            lambda: FakeElevation(),
        )

    rpc = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {
            "name": "run_command",
            "arguments": {"cwd": str(outside), "command": "echo system-ok"},
        },
    }
    response = asyncio.run(
        gateway._exec_local_tool(
            "run_command", rpc, rpc["params"], workspace_id="system-root", session_id=""
        )
    )
    payload = json.loads(bytes(response.body))
    assert "error" not in payload
    assert "system-ok" in payload["result"]["content"][0]["text"]


@pytest.mark.skipif(os.name != "nt", reason="Windows broker semantics only")
def test_system_local_command_does_not_silently_downgrade_without_broker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "allowed"
    root.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"system-root": root}, permission_mode="system")

    class MissingElevation:
        def is_registered(self) -> bool:
            return False

    monkeypatch.setattr(
        "local_dev_mcp_bridge.elevation.get_elevation_controller",
        lambda: MissingElevation(),
    )
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {
            "name": "run_command",
            "arguments": {"command": "echo should-not-run"},
        },
    }
    response = asyncio.run(
        gateway._exec_local_tool(
            "run_command", rpc, rpc["params"], workspace_id="system-root", session_id=""
        )
    )
    payload = json.loads(bytes(response.body))
    assert "error" in payload
    assert "administrator capability is not authorized" in payload["error"]["message"]


@pytest.mark.skipif(os.name == "nt", reason="symlink creation semantics differ on Windows")
def test_symlink_escape_is_not_considered_inside_running_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    gateway = _gateway_for_roots(tmp_path, {"allowed": root}, permission_mode="workspace")

    assert gateway._workspace_for_path(str(root / "link" / "secret.txt")) == ""
    assert gateway._workspace_for_path("link/secret.txt") == ""


def test_task_affinity_routes_followups_without_workspace_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    gateway._task_workspaces["task-b"] = "b"

    for tool_name in ("get_task", "wait_task", "cancel_task"):
        assert gateway._infer_workspace_for_call(tool_name, {"task_id": "task-b"}) == "b"


def test_task_and_path_affinity_override_stale_workspace_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    target_b = root_b / "target.txt"
    target_b.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    gateway._workspace_handle_roots["ws-a"] = "a"
    gateway._task_workspaces["task-b"] = "b"

    assert (
        gateway._infer_workspace_for_call("read", {"workspace_id": "ws-a", "path": str(target_b)})
        == "b"
    )
    assert (
        gateway._infer_workspace_for_call(
            "wait_task", {"workspace_id": "ws-a", "task_id": "task-b"}
        )
        == "b"
    )
    assert gateway._infer_workspace_for_call("show_changes", {"workspace_id": "ws-a"}) == "a"


def test_path_bearing_tools_cover_read_write_edit_search_shell_git_and_patch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    existing = root_b / "existing.txt"
    existing.write_text("before", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})

    cases = [
        ("read", {"path": str(existing)}),
        ("write", {"path": str(root_b / "created.txt")}),
        ("edit", {"path": str(existing)}),
        ("search", {"path": str(root_b), "query": "needle"}),
        ("bash", {"cwd": str(root_b), "command": "echo ok"}),
        ("git_status", {"path": str(root_b)}),
        ("git_diff", {"path": str(root_b)}),
        ("codex_context", {"target_path": str(root_b)}),
        (
            "apply_patch",
            {
                "patch": (
                    f"--- a/{root_b / 'existing.txt'}\n"
                    f"+++ b/{root_b / 'existing.txt'}\n"
                    "@@ -1 +1 @@\n-before\n+after\n"
                )
            },
        ),
    ]
    for tool_name, arguments in cases:
        assert gateway._infer_workspace_for_call(tool_name, arguments) == "b", tool_name


def test_shell_command_absolute_path_routes_without_explicit_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})

    target = root_b / "folder with spaces" / "script.py"
    command = f'python "{target}"'
    assert gateway._infer_workspace_for_call("bash", {"command": command}) == "b"
    assert gateway._infer_workspace_for_call("run_command", {"command": command}) == "b"


def test_shell_command_spanning_two_active_roots_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    command = f'copy "{root_a / "a.txt"}" "{root_b / "b.txt"}"'
    with pytest.raises(ValueError, match="多个运行中的工作区根目录"):
        gateway._infer_workspace_for_call("bash", {"command": command})


def test_path_routing_overrides_legacy_session_selection_without_switching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    target_a = root_a / "a.txt"
    target_b = root_b / "b.txt"
    target_a.write_text("a", encoding="utf-8")
    target_b.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    gateway._session_workspaces["session-1"] = "a"
    gateway._session_workspaces["session-2"] = "b"

    inferred_b = gateway._infer_workspace_for_call("read", {"path": str(target_b)})
    inferred_a = gateway._infer_workspace_for_call("read", {"path": str(target_a)})
    assert gateway._effective_workspace(inferred_b, "session-1", pinned=True) == "b"
    assert gateway._effective_workspace(inferred_a, "session-2", pinned=True) == "a"
    assert gateway._session_workspaces == {"session-1": "a", "session-2": "b"}


def test_gateway_routes_absolute_tool_path_without_route_argument(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    target = root_b / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    routed_ports: list[int] = []
    auth_headers: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routed_ports.append(urlparse(str(request.url)).port or 0)
        auth_headers.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"ok": True})

    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TestClient(gateway.app, raise_server_exceptions=False)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {"path": str(target)}},
        }
    )
    response = client.post(
        "/mcp",
        content=body,
        headers={"Authorization": "Bearer hub-credential", "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert routed_ports == [19001]
    assert auth_headers == ["Bearer credential-b"]


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-prefix routing regression")
def test_cross_drive_roots_route_c_and_d_independently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    gateway = _gateway_for_roots(tmp_path, {"c-root": Path("C:\\"), "d-root": Path("D:\\")})

    assert gateway._workspace_for_path(r"C:\Program Files (x86)\Demo\x.txt") == "c-root"
    assert gateway._workspace_for_path(r"D:\Environment\mcp\src\x.py") == "d-root"


def test_codexpro_supertool_preserves_nested_autorouting_affinity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    target_b = root_b / "nested" / "file.txt"
    target_b.parent.mkdir()
    target_b.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    gateway._workspace_handle_roots["ws-a"] = "a"
    gateway._task_workspaces["task-b"] = "b"

    assert (
        gateway._infer_workspace_for_call(
            "codexpro", {"action": "read", "args": {"path": str(target_b)}}
        )
        == "b"
    )
    assert (
        gateway._infer_workspace_for_call(
            "codexpro", {"action": "wait_task", "args": {"task_id": "task-b"}}
        )
        == "b"
    )
    assert (
        gateway._infer_workspace_for_call(
            "codexpro", {"action": "changes", "args": {"workspace_id": "ws-a"}}
        )
        == "a"
    )


def test_gateway_routes_codexpro_supertool_by_nested_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    target = root_b / "nested" / "file.txt"
    target.parent.mkdir()
    target.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    routed_ports: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routed_ports.append(urlparse(str(request.url)).port or 0)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TestClient(gateway.app, raise_server_exceptions=False)
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "codexpro",
                "arguments": {"action": "read", "args": {"path": str(target)}},
            },
        }
    )
    response = client.post(
        "/mcp",
        content=body,
        headers={"Authorization": "Bearer hub-credential", "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert routed_ports == [19001]


def test_single_hub_session_routes_two_equal_roots_without_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One Hub bearer/session can alternate roots without an entry/current project."""
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg-no-entry"))
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    target_a = root_a / "a.txt"
    target_b = root_b / "b.txt"
    target_a.write_text("a", encoding="utf-8")
    target_b.write_text("b", encoding="utf-8")
    gateway = _gateway_for_roots(tmp_path, {"a": root_a, "b": root_b})
    routed_ports: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        routed_ports.append(urlparse(str(request.url)).port or 0)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"content": []}})

    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TestClient(gateway.app, raise_server_exceptions=False)
    headers = {
        "Authorization": "Bearer hub-credential",
        "Content-Type": "application/json",
        "mcp-session-id": "one-hub-session",
    }
    for request_id, target in ((1, target_a), (2, target_b), (3, target_a)):
        response = client.post(
            "/mcp",
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "read", "arguments": {"path": str(target)}},
                }
            ),
            headers=headers,
        )
        assert response.status_code == 200, response.text

    assert routed_ports == [19000, 19001, 19000]
    assert "one-hub-session" not in gateway._session_workspaces
