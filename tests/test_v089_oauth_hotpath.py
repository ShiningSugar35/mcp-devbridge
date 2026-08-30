from __future__ import annotations

import asyncio
import time
from pathlib import Path
from threading import Event
from typing import cast

import httpx
import pytest
from starlette.responses import JSONResponse

from local_dev_mcp_bridge import desktop_main
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider
from local_dev_mcp_bridge.secrets import SecretsStore


def _project(tmp_path: Path, project_id: str = "p1") -> ProjectConfig:
    root = tmp_path / project_id
    root.mkdir(parents=True, exist_ok=True)
    return ProjectConfig(id=project_id, display_name=project_id, root_path=str(root))


def _provider(store: SecretsStore) -> LocalOAuthProvider:
    return LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        workspace="",
        store=store,
    )


@pytest.mark.asyncio
async def test_persisted_oauth_bearer_skips_project_credential_scan_after_gateway_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    save_projects([_project(tmp_path, "p1"), _project(tmp_path, "p2")])
    store = SecretsStore()
    issuer = _provider(store)
    issued = issuer._issue_tokens("oauth-client", [OAUTH_SCOPE], "")
    bearer = str(issued.access_token)

    scans: list[str] = []

    def slow_project_credential(project_id: str) -> str | None:
        scans.append(project_id)
        time.sleep(0.15)
        return f"project-token-{project_id}"

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_legacy_token=lambda: "hub-token",
        provider=_provider(store),  # fresh provider: exercises persisted OAuth continuity
        workspace_credential_registry=slow_project_credential,
        allow_local_anonymous=False,
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("203.0.113.10", 12345))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            response = await client.post(
                "/mcp",
                headers={"authorization": f"Bearer {bearer}", "accept": "application/json"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 50
        assert scans == []
    finally:
        gateway.stop()


@pytest.mark.asyncio
async def test_persisted_oauth_store_lookup_does_not_block_gateway_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    store = SecretsStore()
    issuer = _provider(store)
    issued = issuer._issue_tokens("oauth-client", [OAUTH_SCOPE], "")
    bearer = str(issued.access_token)
    access_key = LocalOAuthProvider._access_key(bearer)
    original_get = store.get
    entered = Event()
    release = Event()

    def slow_get(key: str) -> str | None:
        if key == access_key:
            entered.set()
            release.wait(0.6)
        return original_get(key)

    monkeypatch.setattr(store, "get", slow_get)
    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_legacy_token=lambda: "hub-token",
        provider=_provider(store),
        allow_local_anonymous=False,
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("203.0.113.12", 12345))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            started = time.perf_counter()
            request_task = asyncio.create_task(
                client.post(
                    "/mcp",
                    headers={"authorization": f"Bearer {bearer}", "accept": "application/json"},
                    json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                )
            )
            await asyncio.sleep(0.05)
            loop_resume_seconds = time.perf_counter() - started
            assert entered.is_set()
            assert loop_resume_seconds < 0.25
            health = await asyncio.wait_for(client.get("/health"), timeout=0.25)
            assert health.status_code == 200
            release.set()
            response = await asyncio.wait_for(request_task, timeout=1.0)
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 50
    finally:
        release.set()
        gateway.stop()


@pytest.mark.asyncio
async def test_project_bearer_credential_scan_does_not_block_gateway_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    project = _project(tmp_path, "project-a")
    save_projects([project])
    entered = Event()
    release = Event()

    def slow_project_credential(project_id: str) -> str | None:
        entered.set()
        release.wait(0.6)
        return "project-bearer" if project_id == project.id else None

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_legacy_token=lambda: "hub-token",
        provider=_provider(SecretsStore()),
        workspace_credential_registry=slow_project_credential,
        allow_local_anonymous=False,
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("203.0.113.11", 12345))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            started = time.perf_counter()
            request_task = asyncio.create_task(
                client.post(
                    "/mcp",
                    headers={"authorization": "Bearer project-bearer", "accept": "application/json"},
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
            )
            await asyncio.sleep(0.05)
            loop_resume_seconds = time.perf_counter() - started
            assert entered.is_set()
            assert loop_resume_seconds < 0.25
            health = await asyncio.wait_for(client.get("/health"), timeout=0.25)
            assert health.status_code == 200
            release.set()
            response = await asyncio.wait_for(request_task, timeout=1.0)
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) == 50
    finally:
        release.set()
        gateway.stop()


@pytest.mark.asyncio
async def test_routed_oauth_project_credential_lookup_does_not_block_gateway_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A routed OAuth tools/call must not run the native project-secret lookup on the loop."""

    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    project = _project(tmp_path, "project-routed")
    store = SecretsStore()
    provider = _provider(store)
    issued = provider._issue_tokens("oauth-client", [OAUTH_SCOPE], project.id)
    bearer = str(issued.access_token)
    entered = Event()
    release = Event()

    def slow_project_credential(project_id: str) -> str | None:
        entered.set()
        release.wait(0.6)
        return "project-upstream-token" if project_id == project.id else None

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_legacy_token=lambda: "hub-token",
        provider=provider,
        workspace_registry=lambda project_id: (
            (8788, project.root_path) if project_id == project.id else None
        ),
        workspace_credential_registry=slow_project_credential,
        allow_local_anonymous=False,
    )

    async def fake_proxy(*_args: object, **_kwargs: object) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": 4, "result": {"ok": True}})

    monkeypatch.setattr(gateway, "_proxy", fake_proxy)
    transport = httpx.ASGITransport(app=gateway.app, client=("203.0.113.13", 12345))
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="https://mcp.example.test"
        ) as client:
            started = time.perf_counter()
            request_task = asyncio.create_task(
                client.post(
                    "/mcp",
                    headers={"authorization": f"Bearer {bearer}", "accept": "application/json"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "server_config",
                            "arguments": {"devbridge_workspace_id": project.id},
                        },
                    },
                )
            )
            await asyncio.sleep(0.05)
            loop_resume_seconds = time.perf_counter() - started
            assert entered.is_set()
            assert loop_resume_seconds < 0.25
            health = await asyncio.wait_for(client.get("/health"), timeout=0.25)
            assert health.status_code == 200
            release.set()
            response = await asyncio.wait_for(request_task, timeout=1.0)
        assert response.status_code == 200
    finally:
        release.set()
        gateway.stop()


class _CredentialCacheDummy:
    def __init__(self) -> None:
        self._workspace_credentials: dict[str, str] = {}


def test_desktop_project_credential_is_warmed_once_then_read_from_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy = cast(desktop_main.MainWindow, _CredentialCacheDummy())
    calls: list[str] = []

    def fake_ensure(project_id: str) -> str:
        calls.append(project_id)
        return f"upstream-{project_id}"

    monkeypatch.setattr(desktop_main, "ensure_project_access_token", fake_ensure)
    first = desktop_main.MainWindow._ensure_workspace_credential(dummy, "project-a")
    second = desktop_main.MainWindow._ensure_workspace_credential(dummy, "project-a")
    looked_up = desktop_main.MainWindow._lookup_workspace_credential(dummy, "project-a")

    assert first == "upstream-project-a"
    assert second == first
    assert looked_up == first
    assert calls == ["project-a"]
