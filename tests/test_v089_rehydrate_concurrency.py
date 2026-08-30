from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

import local_dev_mcp_bridge.gateway as gateway_module
from local_dev_mcp_bridge.config_store import save_projects
from local_dev_mcp_bridge.gateway import OAuthGateway
from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.routing_state import load_workspace_routes, save_workspace_routes


def _project_gateway(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.AsyncBaseTransport,
) -> tuple[OAuthGateway, ProjectConfig, Path]:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "repo"
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

    gateway = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=registry,
        workspace_project_registry=lambda item: project.root_path if item == project.id else None,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=transport,
    )
    return gateway, project, root


def _workspace_response(request: httpx.Request, workspace_id: str) -> httpx.Response:
    rpc = json.loads(request.content or b"{}")
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": rpc.get("id"),
            "result": {
                "content": [{"type": "text", "text": "opened"}],
                "structuredContent": {"workspace_id": workspace_id},
            },
        },
    )


@pytest.mark.asyncio
async def test_same_handle_rehydrate_is_single_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal open_calls
        open_calls += 1
        await asyncio.sleep(0.05)
        return _workspace_response(request, "ws_shared")

    gateway, project, root = _project_gateway(
        tmp_path, monkeypatch, httpx.MockTransport(upstream)
    )
    gateway._remember_persistent_workspace_handle("ws_shared", project.id, str(root))
    gateway._workspace_hydrated_handles.clear()
    try:
        results = await asyncio.gather(
            *(
                gateway._ensure_workspace_handle_hydrated(
                    "ws_shared",
                    project.id,
                    "http://upstream.test",
                    "test-credential",
                )
                for _ in range(12)
            )
        )
        await asyncio.sleep(0)
    finally:
        gateway.stop()

    assert results == [""] * 12
    assert open_calls == 1
    assert gateway._workspace_rehydrate_inflight == {}
    assert "ws_shared" in gateway._workspace_hydrated_handles


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_shared_rehydrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    open_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal open_calls
        open_calls += 1
        started.set()
        await release.wait()
        return _workspace_response(request, "ws_cancel_safe")

    gateway, project, root = _project_gateway(
        tmp_path, monkeypatch, httpx.MockTransport(upstream)
    )
    gateway._remember_persistent_workspace_handle("ws_cancel_safe", project.id, str(root))
    gateway._workspace_hydrated_handles.clear()
    first = asyncio.create_task(
        gateway._ensure_workspace_handle_hydrated(
            "ws_cancel_safe", project.id, "http://upstream.test", "test-credential"
        )
    )
    await started.wait()
    second = asyncio.create_task(
        gateway._ensure_workspace_handle_hydrated(
            "ws_cancel_safe", project.id, "http://upstream.test", "test-credential"
        )
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    try:
        assert await second == ""
        await asyncio.sleep(0)
    finally:
        gateway.stop()

    assert open_calls == 1
    assert gateway._workspace_rehydrate_inflight == {}


@pytest.mark.asyncio
async def test_rehydrate_inflight_has_hard_cap_and_finally_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "_MAX_WORKSPACE_REHYDRATE_INFLIGHT", 2)
    started = asyncio.Event()
    release = asyncio.Event()
    root_to_handle: dict[str, str] = {}
    open_calls = 0

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal open_calls
        rpc = json.loads(request.content or b"{}")
        arguments = (rpc.get("params") or {}).get("arguments") or {}
        canonical = str(Path(str(arguments.get("root"))).resolve())
        open_calls += 1
        if open_calls == 2:
            started.set()
        await release.wait()
        return _workspace_response(request, root_to_handle[canonical])

    gateway, project, root = _project_gateway(
        tmp_path, monkeypatch, httpx.MockTransport(upstream)
    )
    handles: list[str] = []
    for index in range(3):
        child = root / f"child-{index}"
        child.mkdir()
        handle = f"ws_cap_{index}"
        handles.append(handle)
        root_to_handle[str(child.resolve())] = handle
        gateway._remember_persistent_workspace_handle(handle, project.id, str(child))
    gateway._workspace_hydrated_handles.clear()

    first = asyncio.create_task(
        gateway._ensure_workspace_handle_hydrated(
            handles[0], project.id, "http://upstream.test", "test-credential"
        )
    )
    second = asyncio.create_task(
        gateway._ensure_workspace_handle_hydrated(
            handles[1], project.id, "http://upstream.test", "test-credential"
        )
    )
    await started.wait()
    overflow = await gateway._ensure_workspace_handle_hydrated(
        handles[2], project.id, "http://upstream.test", "test-credential"
    )
    assert "并发已达上限" in overflow
    assert len(gateway._workspace_rehydrate_inflight) == 2
    release.set()
    try:
        assert await first == ""
        assert await second == ""
        await asyncio.sleep(0)
    finally:
        gateway.stop()

    assert open_calls == 2
    assert gateway._workspace_rehydrate_inflight == {}


def test_workspace_handle_and_persistent_route_maps_are_bounded_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "_MAX_WORKSPACE_ROUTE_ENTRIES", 3)
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    gateway, project, root = _project_gateway(tmp_path, monkeypatch, transport)
    try:
        for index in range(5):
            child = root / f"persistent-{index}"
            child.mkdir()
            gateway._remember_persistent_workspace_handle(
                f"ws_persistent_{index}", project.id, str(child)
            )
        assert list(gateway._workspace_route_records) == [
            "ws_persistent_2",
            "ws_persistent_3",
            "ws_persistent_4",
        ]
        assert len(gateway._workspace_handle_roots) == 3
        assert len(gateway._workspace_handle_paths) == 3
        assert len(gateway._workspace_hydrated_handles) == 3
        assert len(load_workspace_routes()) == 3

        for index in range(5):
            gateway._remember_persistent_workspace_handle(
                f"ws-legacy-{index}", project.id, str(root)
            )
    finally:
        gateway.stop()

    assert len(gateway._workspace_handle_roots) == 3
    assert len(gateway._workspace_handle_paths) == 3
    assert len(gateway._workspace_route_records) <= 3
    assert gateway._workspace_hydrated_handles <= set(gateway._workspace_route_records)
    assert len(load_workspace_routes()) == len(gateway._workspace_route_records)


def test_legacy_workspace_handle_keeps_only_in_process_project_affinity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    gateway, project, root = _project_gateway(tmp_path, monkeypatch, transport)
    missing_child = root / "opaque-child-not-present-locally"
    try:
        gateway._remember_persistent_workspace_handle(
            "ws-legacy-child", project.id, str(missing_child)
        )
        gateway._remember_persistent_workspace_handle(
            "ws-unknown-project", "missing-project", str(root)
        )
        gateway._remember_persistent_workspace_handle(
            "arbitrary-handle", project.id, str(root)
        )

        assert gateway._workspace_handle_roots["ws-legacy-child"] == project.id
        assert "ws-legacy-child" not in gateway._workspace_handle_paths
        assert "ws-legacy-child" not in gateway._workspace_route_records
        assert "ws-legacy-child" not in gateway._workspace_hydrated_handles
        assert "ws-unknown-project" not in gateway._workspace_handle_roots
        assert "arbitrary-handle" not in gateway._workspace_handle_roots
        assert load_workspace_routes() == []
    finally:
        gateway.stop()


@pytest.mark.asyncio
async def test_deleted_or_out_of_project_route_fails_closed_without_upstream_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_calls = 0

    def upstream(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(500)

    gateway, project, root = _project_gateway(
        tmp_path, monkeypatch, httpx.MockTransport(upstream)
    )
    child = root / "deleted"
    child.mkdir()
    gateway._remember_persistent_workspace_handle("ws_deleted", project.id, str(child))
    gateway._workspace_hydrated_handles.clear()
    child.rmdir()
    try:
        result = await gateway._ensure_workspace_handle_hydrated(
            "ws_deleted", project.id, "http://upstream.test", "test-credential"
        )
    finally:
        gateway.stop()

    assert "根目录已失效" in result
    assert upstream_calls == 0
    assert "ws_deleted" not in gateway._workspace_handle_roots
    assert "ws_deleted" not in gateway._workspace_handle_paths
    assert "ws_deleted" not in gateway._workspace_route_records
    assert "ws_deleted" not in gateway._workspace_hydrated_handles
    assert load_workspace_routes() == []


def test_stale_gateway_snapshots_merge_without_losing_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    gateway_a, project, root = _project_gateway(tmp_path, monkeypatch, transport)

    def registry(project_id: str):
        return (project.codexpro_port, project.root_path) if project_id == project.id else None

    gateway_b = OAuthGateway(
        public_hostname="mcp.example.test",
        workspace=str(root),
        upstream_url="http://upstream.test",
        allow_local_anonymous=True,
        workspace_registry=registry,
        workspace_project_registry=lambda item: project.root_path if item == project.id else None,
        workspace_credential_registry=lambda _project_id: "test-credential",
        transport=transport,
    )
    child_a = root / "writer-a"
    child_b = root / "writer-b"
    child_a.mkdir()
    child_b.mkdir()
    try:
        gateway_a._remember_persistent_workspace_handle("ws_writer_a", project.id, str(child_a))
        gateway_b._remember_persistent_workspace_handle("ws_writer_b", project.id, str(child_b))
        routes = {record["handle"]: record for record in load_workspace_routes()}
        assert set(routes) == {"ws_writer_a", "ws_writer_b"}
    finally:
        gateway_a.stop()
        gateway_b.stop()


def test_route_store_supports_explicit_atomic_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    first = {
        "handle": "ws_first",
        "project_id": "d",
        "root": str(tmp_path / "first"),
        "last_used": 1.0,
    }
    second = {
        "handle": "ws_second",
        "project_id": "d",
        "root": str(tmp_path / "second"),
        "last_used": 2.0,
    }
    save_workspace_routes([first])
    save_workspace_routes([second])
    assert {record["handle"] for record in load_workspace_routes()} == {
        "ws_first",
        "ws_second",
    }

    save_workspace_routes([], removed_handles={"ws_first"})
    assert [record["handle"] for record in load_workspace_routes()] == ["ws_second"]


def test_deleted_project_route_is_removed_from_memory_and_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    gateway, project, root = _project_gateway(tmp_path, monkeypatch, transport)
    child = root / "project-deleted"
    child.mkdir()
    gateway._remember_persistent_workspace_handle("ws_project_deleted", project.id, str(child))
    gateway._workspace_registry = lambda _project_id: None
    gateway._workspace_project_registry = lambda _project_id: None
    save_projects([])
    try:
        with pytest.raises(ValueError, match="上下文已失效"):
            gateway._infer_workspace_for_call(
                "show_changes", {"workspace_id": "ws_project_deleted"}
            )
        assert "ws_project_deleted" not in gateway._workspace_handle_roots
        assert "ws_project_deleted" not in gateway._workspace_handle_paths
        assert "ws_project_deleted" not in gateway._workspace_route_records
        assert "ws_project_deleted" not in gateway._workspace_hydrated_handles
        assert load_workspace_routes() == []
    finally:
        gateway.stop()


def test_removed_route_tombstone_blocks_stale_writer_resurrection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    stale = {
        "handle": "ws_stale",
        "project_id": "d",
        "root": str(tmp_path / "stale"),
        "last_used": 1.0,
    }
    save_workspace_routes([stale])
    save_workspace_routes([], removed_handles={"ws_stale"})
    save_workspace_routes([stale])
    assert load_workspace_routes() == []

    reopened = {**stale, "last_used": 9_999_999_999.0}
    save_workspace_routes([reopened])
    assert [record["handle"] for record in load_workspace_routes()] == ["ws_stale"]


def test_concurrent_route_writers_preserve_all_records_and_leave_no_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "cfg"
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(config_dir))

    def write_route(index: int) -> None:
        save_workspace_routes(
            [
                {
                    "handle": f"ws_thread_{index}",
                    "project_id": "d",
                    "root": str(tmp_path / f"thread-{index}"),
                    "last_used": float(index + 1),
                }
            ]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write_route, range(32)))

    assert {record["handle"] for record in load_workspace_routes()} == {
        f"ws_thread_{index}" for index in range(32)
    }
    assert not list(config_dir.glob("workspace-routes.json.*.tmp"))



def test_temporarily_unready_project_preserves_deterministic_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    gateway, project, root = _project_gateway(tmp_path, monkeypatch, transport)
    child = root / "temporarily-unready"
    child.mkdir()
    gateway._remember_persistent_workspace_handle("ws_transient", project.id, str(child))
    gateway._workspace_registry = lambda _project_id: None
    try:
        inferred = gateway._infer_workspace_for_call(
            "show_changes", {"workspace_id": "ws_transient"}
        )
        assert inferred == project.id
        assert gateway._workspace_handle_roots["ws_transient"] == project.id
        assert gateway._workspace_route_records["ws_transient"]["root"] == str(child.resolve())
        assert [record["handle"] for record in load_workspace_routes()] == ["ws_transient"]
    finally:
        gateway.stop()
