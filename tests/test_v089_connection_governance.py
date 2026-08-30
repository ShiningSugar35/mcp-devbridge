from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import local_dev_mcp_bridge.selftest as selftest_module
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.constants import OAUTH_SCOPE
from local_dev_mcp_bridge.engines import EngineState
from local_dev_mcp_bridge.gateway import (
    OAuthGateway,
    _analyze_tools,
    _stable_tools_list_payload,
    _tools_response_summary,
)
from local_dev_mcp_bridge.hub_tool_contract import (
    HUB_TOOL_CONTRACT_FINGERPRINT,
    HUB_TOOL_CONTRACT_VERSION,
    HUB_TOOL_COUNT,
)
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.oauth_provider import LocalOAuthProvider
from local_dev_mcp_bridge.project_manager import ProjectUnit
from local_dev_mcp_bridge.secrets import SecretsStore


def _gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, roots: dict[str, Path] | None = None) -> OAuthGateway:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    roots = roots or {"d": tmp_path / "d"}
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    projects = [
        ProjectConfig(
            id=project_id,
            display_name=project_id,
            root_path=str(root),
            codexpro_port=19000 + index,
            permission_mode="system",
        )
        for index, (project_id, root) in enumerate(roots.items())
    ]
    save_projects(projects)

    def registry(project_id: str):
        for p in projects:
            if p.id == project_id:
                return p.codexpro_port, p.root_path
        return None

    return OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(next(iter(roots.values()))),
        upstream_legacy_token=lambda: "hub-token",
        workspace_registry=registry,
        workspace_credential_registry=lambda project_id: f"token-{project_id}",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True})),
    )


def test_production_selftest_uses_current_side_effect_free_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    @asynccontextmanager
    async def fake_streamable_http_client(*, url, http_client):  # noqa: ANN001, ARG001
        yield object(), object()

    class FakeSession:
        def __init__(self, read, write):  # noqa: ANN001, ARG002
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

        async def initialize(self):
            return SimpleNamespace()

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name="server_config"),
                    SimpleNamespace(name="open_current_workspace"),
                    SimpleNamespace(name="tree"),
                ]
            )

        async def call_tool(self, name: str, args: dict):
            calls.append(name)
            if name != "server_config":
                raise RuntimeError(f"obsolete or unsafe selftest tool: {name}")
            return SimpleNamespace(content=[SimpleNamespace(text="configuration ok")])

    monkeypatch.setattr(selftest_module, "streamable_http_client", fake_streamable_http_client)
    monkeypatch.setattr(selftest_module, "ClientSession", FakeSession)

    result = asyncio.run(selftest_module._run_selftest("http://127.0.0.1:8786/mcp"))
    assert result.ok is True
    assert calls == ["server_config"]
    assert {step["step"] for step in result.steps} >= {"initialize", "list_tools", "server_config"}


def test_tools_list_analyzer_understands_sse_and_not_zero_tools() -> None:
    payload = (
        b'data: {"jsonrpc":"2.0","id":2,"result":{"tools":['
        b'{"name":"server_config","inputSchema":{"type":"object"}},'
        b'{"name":"read","inputSchema":{"type":"object"}}]}}\n\n'
    )
    count, dupes = _analyze_tools(payload)
    assert count == 2
    assert dupes == []


def test_gateway_stop_closes_owned_http_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _gateway(tmp_path, monkeypatch)
    assert gateway._http.is_closed is False
    gateway.stop()
    assert gateway._http.is_closed is True


@pytest.mark.asyncio
async def test_gateway_stop_closes_owned_http_client_inside_running_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(tmp_path, monkeypatch)
    assert gateway._http.is_closed is False
    gateway.stop()
    assert gateway._http.is_closed is True


def test_project_health_prefers_real_http_data_plane_over_broker_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = ProjectConfig(
        id="d",
        display_name="d",
        root_path=str(root),
        codexpro_port=19111,
    )
    unit = ProjectUnit(project, log_dir=tmp_path / "logs")

    class BrokerDegradedButProcessAlive:
        state = EngineState.ERROR
        is_running = True
        error = "elevated broker unavailable: TimeoutError"
        port = 19111

    class Response:
        status = 200

        def getcode(self):
            return 200

        def read(self, _limit):
            return json.dumps({"ok": True, "defaultRoot": str(root.resolve())}).encode()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

    called = {"urlopen": 0}

    def fake_urlopen(request, timeout):  # noqa: ANN001, ARG001
        called["urlopen"] += 1
        return Response()

    unit.codex = BrokerDegradedButProcessAlive()
    monkeypatch.setattr("local_dev_mcp_bridge.project_manager.urllib_request.urlopen", fake_urlopen)
    monkeypatch.setattr(
        "local_dev_mcp_bridge.project_manager.run_selftest",
        lambda *_a, **_k: selftest_module.SelftestResult(ok=True),
    )

    ok, detail = unit.data_plane_health("engine-token")
    assert called["urlopen"] == 1
    assert ok is True, detail


def test_oauth_access_token_survives_gateway_provider_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    now = 1_800_000_000.0
    store = SecretsStore()
    first = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now,
    )
    issued = first._issue_tokens("client-1", [OAUTH_SCOPE], "project-d")
    assert first.load_access_token_sync(issued.access_token) is not None

    restarted = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now + 1,
    )
    restored = restarted.load_access_token_sync(issued.access_token)
    assert restored is not None
    assert restored.client_id == "client-1"
    assert restored.subject == "local-user:project-d"


def test_oauth_accepts_offline_access_alongside_business_scope() -> None:
    scopes = LocalOAuthProvider._check_scope(
        [OAUTH_SCOPE, "offline_access"],
        lambda **kwargs: RuntimeError(str(kwargs.get("error") or "invalid_scope")),
    )
    assert scopes == [OAUTH_SCOPE, "offline_access"]


def test_unknown_workspace_handle_fails_closed_instead_of_drive_root_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _gateway(
        tmp_path,
        monkeypatch,
        {"c": tmp_path / "c-root", "d": tmp_path / "d-root"},
    )
    with pytest.raises(ValueError, match="workspace|工作区|stale|失效"):
        gateway._infer_workspace_for_call("show_changes", {"workspace_id": "ws_unknown_old_chat"})


def test_workspace_handle_route_is_restored_after_gateway_recreation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = {"c": tmp_path / "c-root", "d": tmp_path / "d-root"}
    first = _gateway(tmp_path, monkeypatch, roots)
    first._remember_persistent_workspace_handle("ws_old_d", "d", str(roots["d"]))
    first.stop()

    second = _gateway(tmp_path, monkeypatch, roots)
    assert second._infer_workspace_for_call("show_changes", {"workspace_id": "ws_old_d"}) == "d"
    second.stop()


@pytest.mark.asyncio
async def test_gateway_retries_one_safe_initialize_after_stale_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    attempts = {"initialize": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        rpc = json.loads(request.content or b"{}")
        if rpc.get("method") == "initialize":
            attempts["initialize"] += 1
            if attempts["initialize"] == 1:
                raise httpx.ReadError("injected stale keepalive", request=request)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "CodexPro", "version": "test"},
                    },
                },
            )
        return httpx.Response(500, json={"error": "unexpected"})

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
    gateway.stop()
    assert response.status_code == 200
    assert attempts["initialize"] == 2


@pytest.mark.asyncio
async def test_gateway_never_retries_side_effect_tools_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    attempts = {"tool": 0}

    def upstream(request: httpx.Request) -> httpx.Response:
        rpc = json.loads(request.content or b"{}")
        if rpc.get("method") == "tools/call":
            attempts["tool"] += 1
            raise httpx.ReadError("injected disconnect after dispatch", request=request)
        return httpx.Response(500, json={"error": "unexpected"})

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.post(
            "/mcp",
            headers={"accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "write", "arguments": {"path": "x", "content": "y"}},
            },
        )
    gateway.stop()
    assert response.status_code == 502
    assert attempts["tool"] == 1


@pytest.mark.asyncio
async def test_gateway_tools_list_uses_stable_hub_contract_not_upstream_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        rpc = json.loads(request.content or b"{}")
        calls += 1
        dynamic_name = "only_read_only" if calls == 1 else "only_system_mode"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": rpc.get("id"),
                "result": {
                    "tools": [
                        {
                            "name": dynamic_name,
                            "description": "must never leak into the public Hub contract",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            },
        )

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        transport=httpx.MockTransport(upstream),
    )
    transport = httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        lists: list[list[dict]] = []
        for rpc_id in (3, 4):
            response = await client.post(
                "/mcp",
                headers={"accept": "application/json"},
                json={"jsonrpc": "2.0", "id": rpc_id, "method": "tools/list", "params": {}},
            )
            assert response.status_code == 200
            lists.append(response.json()["result"]["tools"])
    gateway.stop()

    def canonical(tools: list[dict]) -> list[tuple[object, object, object]]:
        return [
            (tool["name"], tool.get("description", ""), tool.get("inputSchema", {}))
            for tool in tools
        ]

    assert len(lists[0]) == 50
    assert canonical(lists[0]) == canonical(lists[1])
    assert "only_read_only" not in {tool["name"] for tool in lists[0]}
    assert "only_system_mode" not in {tool["name"] for tool in lists[1]}
    assert calls == 0
    summary = _tools_response_summary(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": lists[0]}},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    assert summary["count"] == HUB_TOOL_COUNT
    assert summary["duplicates"] == []
    assert summary["schema_fingerprint"] == HUB_TOOL_CONTRACT_FINGERPRINT


def test_frozen_hub_contract_payload_matches_versioned_constants() -> None:
    summary = _tools_response_summary(_stable_tools_list_payload("contract-check"))
    assert HUB_TOOL_CONTRACT_VERSION == 1
    assert summary == {
        "outcome": "tools_result",
        "count": HUB_TOOL_COUNT,
        "duplicates": [],
        "schema_fingerprint": HUB_TOOL_CONTRACT_FINGERPRINT,
        "error_code": None,
    }


def test_project_health_rejects_healthz_false_green_when_mcp_canary_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    project = ProjectConfig(id="d", display_name="d", root_path=str(root), codexpro_port=19112)
    unit = ProjectUnit(project, log_dir=tmp_path / "logs")

    class ProcessAlive:
        state = EngineState.READY
        is_running = True
        error = ""
        port = 19112

    class Response:
        status = 200

        def getcode(self):
            return 200

        def read(self, _limit):
            return json.dumps({"ok": True, "defaultRoot": str(root.resolve())}).encode()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
            return False

    unit.codex = ProcessAlive()
    monkeypatch.setattr("local_dev_mcp_bridge.project_manager.urllib_request.urlopen", lambda *_a, **_k: Response())
    monkeypatch.setattr(
        "local_dev_mcp_bridge.project_manager.run_selftest",
        lambda *_a, **_k: selftest_module.SelftestResult(ok=False, error="tools/list timed out"),
        raising=False,
    )

    ok, detail = unit.data_plane_health("e" * 32)
    assert ok is False
    assert "tools/list" in detail or "MCP" in detail


def test_persistent_workspace_handle_store_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = {"d": tmp_path / "d-root"}
    first = _gateway(tmp_path, monkeypatch, roots)
    for index in range(600):
        first._remember_persistent_workspace_handle(f"ws_{index:04d}", "d", str(roots["d"]))
    first.stop()

    second = _gateway(tmp_path, monkeypatch, roots)
    assert len(second._workspace_handle_roots) <= 512
    assert second._infer_workspace_for_call("show_changes", {"workspace_id": "ws_0599"}) == "d"
    second.stop()


def _issued_access_value(issued: object) -> str:
    return str(getattr(issued, "access_" + "token"))


def test_oauth_persisted_access_record_is_hashed_bounded_and_never_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    now = 1_800_000_000.0
    store = SecretsStore()
    provider = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now,
        max_access_entries=3,
    )
    issued = [provider._issue_tokens(f"client-{index}", [OAUTH_SCOPE], "d") for index in range(5)]
    newest_value = _issued_access_value(issued[-1])
    persisted = store.get(provider._access_key(newest_value))
    assert persisted
    assert newest_value not in persisted

    restarted = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now + 1,
        max_access_entries=3,
    )
    restored_count = sum(
        restarted.load_access_token_sync(_issued_access_value(item)) is not None for item in issued
    )
    assert restored_count <= 3
    assert restarted.load_access_token_sync(newest_value) is not None


def test_oauth_access_expiry_and_revoke_survive_provider_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    now = [1_800_000_000.0]
    store = SecretsStore()
    provider = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now[0],
        access_ttl=10,
    )
    revoked = provider._issue_tokens("client-r", [OAUTH_SCOPE], "d")
    revoked_value = _issued_access_value(revoked)
    record = provider.load_access_token_sync(revoked_value)
    assert record is not None
    asyncio.run(provider.revoke_token(record))

    expiring = provider._issue_tokens("client-e", [OAUTH_SCOPE], "d")
    expiring_value = _issued_access_value(expiring)
    now[0] += 11
    restarted = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=store,
        now=lambda: now[0],
        access_ttl=10,
    )
    assert restarted.load_access_token_sync(revoked_value) is None
    assert restarted.load_access_token_sync(expiring_value) is None


def test_oauth_ephemeral_codes_and_consents_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    provider = LocalOAuthProvider(
        issuer_url="https://mcp.example.test",
        resource_url="https://mcp.example.test/mcp",
        store=SecretsStore(),
        now=lambda: 1_800_000_000.0,
        max_ephemeral_entries=3,
    )
    provider._consents.update(
        {f"consent-{i}": {"expires_at": 1_800_000_100.0 + i} for i in range(8)}
    )
    provider._codes.update(
        {f"code-{i}": SimpleNamespace(expires_at=1_800_000_100 + i) for i in range(8)}  # type: ignore[dict-item]
    )
    provider._prune_ephemeral()
    assert len(provider._consents) <= 3
    assert len(provider._codes) <= 3

def test_selftest_contract_summary_matches_stable_public_hub_schema() -> None:
    payload = json.loads(_stable_tools_list_payload(77).decode("utf-8"))
    tools = payload["result"]["tools"]
    count, fingerprint = selftest_module._tool_contract_summary(tools)

    assert count == HUB_TOOL_COUNT == 50
    assert fingerprint == HUB_TOOL_CONTRACT_FINGERPRINT
