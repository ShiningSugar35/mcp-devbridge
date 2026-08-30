from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig


@pytest.mark.asyncio
async def test_workspace_handle_is_rehydrated_before_first_forward_after_gateway_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "d-root"
    root.mkdir()
    project = ProjectConfig(
        id="d",
        display_name="d",
        root_path=str(root),
        codexpro_port=19000,
        permission_mode="system",
    )
    save_projects([project])

    def registry(project_id: str):
        return (project.codexpro_port, project.root_path) if project_id == project.id else None

    first = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=registry,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"ok": True})),
    )
    first._remember_persistent_workspace_handle("ws_old_d", "d", str(root))
    first.stop()

    calls: list[str] = []
    hydrated = False

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal hydrated
        rpc = json.loads(request.content or b"{}")
        params = rpc.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if name == "open_workspace":
            calls.append(name)
            assert Path(str(arguments.get("root"))).resolve() == root.resolve()
            hydrated = True
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "content": [{"type": "text", "text": "opened"}],
                        "structuredContent": {"workspace_id": "ws_old_d"},
                    },
                },
            )
        if name == "show_changes":
            calls.append(name)
            if not hydrated:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc.get("id"),
                        "result": {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": "CodexProError: Unknown workspace_id: ws_old_d. Call open_workspace first.",
                                }
                            ],
                            "structuredContent": {
                                "error": "CodexProError: Unknown workspace_id: ws_old_d. Call open_workspace first."
                            },
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {"content": [{"type": "text", "text": "changes ok"}]},
                },
            )
        if name == "write":
            calls.append(name)
            if not hydrated:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": rpc.get("id"),
                        "result": {
                            "isError": True,
                            "content": [
                                {
                                    "type": "text",
                                    "text": "CodexProError: Unknown workspace_id: ws_old_d. Call open_workspace first.",
                                }
                            ],
                            "structuredContent": {
                                "error": "CodexProError: Unknown workspace_id: ws_old_d. Call open_workspace first."
                            },
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {"content": [{"type": "text", "text": "write ok"}]},
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    second = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=registry,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=second.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "show_changes", "arguments": {"workspace_id": "ws_old_d"}},
            },
        )
        first_calls = list(calls)
        calls.clear()
        hydrated = False
        restarted_response = await client.post(
            "/mcp",
            headers={"accept": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "show_changes", "arguments": {"workspace_id": "ws_old_d"}},
            },
        )
        read_only_restart_calls = list(calls)
        calls.clear()
        hydrated = False
        write_response = await client.post(
            "/mcp",
            headers={"accept": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "write",
                    "arguments": {"workspace_id": "ws_old_d", "path": "x.txt", "content": "x"},
                },
            },
        )
    second.stop()

    assert response.status_code == 200
    assert first_calls == ["open_workspace", "show_changes"]
    assert "changes ok" in response.text
    assert restarted_response.status_code == 200
    assert read_only_restart_calls == ["show_changes", "open_workspace", "show_changes"]
    assert "changes ok" in restarted_response.text
    assert write_response.status_code == 200
    assert calls == ["write", "open_workspace"]
