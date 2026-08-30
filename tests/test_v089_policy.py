from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig


def _policy_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    upstream_calls: list[str],
) -> tuple[OAuthGateway, str]:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / mode
    root.mkdir(parents=True, exist_ok=True)
    project_id = f"p-{mode}"
    project = ProjectConfig(
        id=project_id,
        display_name=project_id,
        root_path=str(root),
        codexpro_port=19041,
        permission_mode=mode,  # type: ignore[arg-type]
    )
    save_projects([project])

    def upstream(request: httpx.Request) -> httpx.Response:
        rpc = json.loads(request.content or b"{}")
        name = str((rpc.get("params") or {}).get("name") or "")
        upstream_calls.append(name)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc.get("id"),
                "result": {"content": [{"type": "text", "text": "upstream ok"}]},
            },
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=lambda pid: (project.codexpro_port, project.root_path)
        if pid == project_id
        else None,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=httpx.MockTransport(upstream),
    )
    return gateway, project_id


async def _call(gateway: OAuthGateway, project_id: str, name: str, arguments: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    payload = dict(arguments)
    payload["devbridge_workspace_id"] = project_id
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        return await client.post(
            "/mcp",
            headers={"accept": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": name, "arguments": payload},
            },
        )


@pytest.mark.asyncio
async def test_read_only_project_denies_write_at_gateway_before_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "read_only", calls)
    response = await _call(gateway, project_id, "write", {"path": "x.txt", "content": "x"})
    gateway.stop()
    assert response.status_code == 200
    assert calls == []
    assert "只读" in response.text or "permission" in response.text.lower()


@pytest.mark.asyncio
async def test_workspace_project_denies_full_mode_only_tool_at_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "workspace", calls)
    response = await _call(gateway, project_id, "git_status", {})
    gateway.stop()
    assert response.status_code == 200
    assert calls == []
    assert "完全访问" in response.text or "permission" in response.text.lower()


@pytest.mark.asyncio
async def test_system_project_keeps_full_mode_tool_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "system", calls)
    response = await _call(gateway, project_id, "git_status", {})
    gateway.stop()
    assert response.status_code == 200
    assert calls == ["git_status"]
    assert "upstream ok" in response.text


@pytest.mark.asyncio
async def test_read_only_supertool_write_cannot_bypass_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "read_only", calls)
    response = await _call(
        gateway,
        project_id,
        "codexpro",
        {"action": "write", "args": {"path": "x.txt", "content": "x"}},
    )
    gateway.stop()
    assert response.status_code == 200
    assert calls == []
    assert "permission" in response.text.lower()


@pytest.mark.asyncio
async def test_workspace_supertool_full_action_cannot_bypass_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "workspace", calls)
    response = await _call(
        gateway,
        project_id,
        "codexpro",
        {"action": "git_status", "args": {}},
    )
    gateway.stop()
    assert response.status_code == 200
    assert calls == []
    assert "完全访问" in response.text


@pytest.mark.asyncio
async def test_read_only_safe_read_remains_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "read_only", calls)
    response = await _call(gateway, project_id, "read", {"path": "x.txt"})
    gateway.stop()
    assert response.status_code == 200
    assert calls == ["read"]
    assert "upstream ok" in response.text


@pytest.mark.asyncio
async def test_read_only_standard_only_search_is_feature_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "read_only", calls)
    response = await _call(gateway, project_id, "search", {"query": "x"})
    gateway.stop()
    assert response.status_code == 200
    assert calls == []
    assert "未在只读项目中启用" in response.text


@pytest.mark.asyncio
async def test_workspace_standard_search_remains_callable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    gateway, project_id = _policy_gateway(tmp_path, monkeypatch, "workspace", calls)
    response = await _call(gateway, project_id, "search", {"query": "x"})
    gateway.stop()
    assert response.status_code == 200
    assert calls == ["search"]
