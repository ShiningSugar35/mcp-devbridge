"""Independent lifecycle, HTTP and diagnostic-budget regressions; no real commands."""
from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import local_dev_mcp_bridge.gateway as gm
import local_dev_mcp_bridge.gateway_shell_probe as probe
import local_dev_mcp_bridge.gateway_workers as workers


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(gm, "_LOG_DIR", tmp_path / "logs")
    gw = gm.OAuthGateway(public_hostname="fixture.test", workspace=str(tmp_path))
    monkeypatch.setattr(gw, "_workspace_permission_mode", lambda _id: "workspace")
    yield gw
    gw._local_executor.close()


def output() -> Any:
    return SimpleNamespace(shell="fixture", command="fixture", exit_code=0,
                           duration_seconds=0.1, timed_out=False, stdout="ok", stderr="")


def call(name: str, identity: str = "http-1", **arguments: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identity, "method": "tools/call", "params": {
        "name": name, "arguments": {"command": "echo fixture", "executable": "fixture", **arguments},
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["run_command", "run_program", "shell_self_test"])
async def test_http_health_and_catalog_during_slow_call(gateway, monkeypatch, name):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    audit: list[dict[str, Any]] = []

    def slow(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)
        return "probe ok" if name == "shell_self_test" else output()

    monkeypatch.setattr(gm, name, slow)
    monkeypatch.setattr(gateway, "_audit_gateway_tool", lambda *args, **kw: audit.append({"success": args[5], **kw}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1") as client:
        task = asyncio.create_task(client.post("/mcp", json=call(name)))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert not task.done()
            health = await asyncio.wait_for(client.get("/health"), 0.5)
            catalog = await asyncio.wait_for(client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), 0.5)
            assert health.status_code == 200
            assert len(catalog.json()["result"]["tools"]) == 50
            release.set()
            response = await task
            assert response.json()["id"] == "http-1"
            assert len(audit) == 1 and audit[0]["success"] is True
        finally:
            release.set()
            await task
            await gateway._http.aclose()


@pytest.mark.asyncio
async def test_http_error_audit_is_not_success(gateway, monkeypatch):
    audit: list[dict[str, Any]] = []
    monkeypatch.setattr(gateway, "_audit_gateway_tool", lambda *args, **kw: audit.append({"success": args[5], **kw}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1") as client:
        response = await client.post("/mcp", json=call("run_command", command=""))
    assert response.json()["error"]["code"] == -32602
    assert len(audit) == 1 and audit[0]["success"] is False
    assert audit[0]["error_type"] == "local_tool_error"
    await gateway._http.aclose()


@pytest.mark.asyncio
async def test_http_cancel_records_uncertainty_without_freeing_worker(gateway, monkeypatch):
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    audit: list[dict[str, Any]] = []

    def slow(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)
        return output()

    monkeypatch.setattr(gm, "run_command", slow)
    monkeypatch.setattr(gateway, "_audit_gateway_tool", lambda *args, **kw: audit.append({"success": args[5], **kw}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1") as client:
        task = asyncio.create_task(client.post("/mcp", json=call("run_command")))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(gateway._local_tool_workers) == 1
            assert len(audit) == 1 and audit[0]["success"] is False
            assert audit[0]["error_type"] == "waiter_cancelled_execution_unknown"
        finally:
            active = gateway._local_tool_workers
            release.set()
            await asyncio.gather(*(asyncio.wrap_future(f) for f in active))
            await gateway._http.aclose()


@pytest.mark.asyncio
async def test_shutdown_and_replacement_retain_process_wide_capacity():
    old, new = workers.LocalToolWorkers(), workers.LocalToolWorkers()
    release = threading.Event()
    futures = [old.submit(lambda: release.wait(5)) for _ in range(4)]
    try:
        started = time.monotonic()
        old.close()
        old.close()
        assert time.monotonic() - started < 0.5
        assert len(old.active) == 4
        with pytest.raises(workers.LocalToolBusy):
            old.submit(lambda: "not submitted")
        with pytest.raises(workers.LocalToolBusy):
            new.submit(lambda: "not submitted")
        assert new._executor is None
        release.set()
        await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
        assert not old.active
        assert await new.run(lambda: 42) == 42
    finally:
        release.set()
        await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
        old.close()
        new.close()


@pytest.mark.asyncio
async def test_submit_failure_releases_admission(monkeypatch):
    class BrokenExecutor:
        def __init__(self, **_kw):
            pass

        def submit(self, _fn):
            raise RuntimeError("fixture submit failed")

        def shutdown(self, **_kw):
            pass

    failed = workers.LocalToolWorkers()
    with monkeypatch.context() as patch:
        patch.setattr(workers, "ThreadPoolExecutor", BrokenExecutor)
        with pytest.raises(RuntimeError, match="fixture submit"):
            failed.submit(lambda: 0)
        assert not failed.active
        failed.close()
    healthy = workers.LocalToolWorkers()
    release = threading.Event()
    futures = [healthy.submit(lambda: release.wait(5)) for _ in range(4)]
    try:
        assert len(healthy.active) == 4
    finally:
        release.set()
        await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
        healthy.close()


@pytest.mark.asyncio
async def test_detached_exception_is_consumed_and_capacity_released():
    pool = workers.LocalToolWorkers()
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    errors: list[Any] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    entered, release = asyncio.Event(), threading.Event()

    def fail():
        loop.call_soon_threadsafe(entered.set)
        release.wait(5)
        raise RuntimeError("detached fixture")

    task = asyncio.create_task(pool.run(fail))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(pool.active) == 1
        release.set()
        for _ in range(100):
            if not pool.active:
                break
            await asyncio.sleep(0.01)
        for _ in range(3):
            await asyncio.sleep(0)
        assert not pool.active and not errors
        assert await pool.run(lambda: 1) == 1
    finally:
        release.set()
        pool.close()
        loop.set_exception_handler(old_handler)


@pytest.mark.asyncio
async def test_immediate_completion_has_no_registry_growth():
    pool = workers.LocalToolWorkers()
    try:
        for n in range(200):
            assert await pool.run(lambda n=n: n) == n
            assert not pool.active
    finally:
        pool.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout, expected", [(0, 10), (-1, 1), (999, 20)])
async def test_command_budget_and_cwd_are_preserved(gateway, monkeypatch, timeout, expected):
    observed = []

    def execute(*_args, **kw):
        observed.append(kw)
        return output()

    monkeypatch.setattr(gm, "run_command", execute)
    rpc = call("run_command", timeout_seconds=timeout)
    response = await gateway._exec_local_tool("run_command", rpc, rpc["params"])
    assert "result" in json.loads(response.body)
    assert observed[0]["timeout_seconds"] == expected
    assert observed[0]["cwd"] == gateway._workspace
    await gateway._http.aclose()


@pytest.mark.asyncio
async def test_denied_cwd_never_executes(gateway, monkeypatch):
    observed = []
    monkeypatch.setattr(gm, "run_command", lambda *_a, **_kw: observed.append(True))
    rpc = call("run_command", cwd="..")
    response = await gateway._exec_local_tool("run_command", rpc, rpc["params"])
    assert json.loads(response.body)["error"]["code"] == -32602
    assert not observed and not gateway._local_tool_workers
    await gateway._http.aclose()


@pytest.mark.asyncio
async def test_gateway_stop_closes_admission(gateway):
    await gateway._http.aclose()
    gateway.stop()
    rpc = call("run_command")
    response = await gateway._exec_local_tool("run_command", rpc, rpc["params"])
    assert json.loads(response.body)["error"]["code"] == -32005
    assert not gateway._local_tool_workers


def test_probe_has_single_total_budget(monkeypatch):
    clock = [0.0]
    timeouts = []
    shell = SimpleNamespace(executable=True, name="fixture", path="fixture", kind="pwsh")
    monkeypatch.setattr(probe, "default_shell", lambda: shell)
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    def timeout(argv, **kw):
        timeouts.append(kw["timeout"])
        clock[0] += kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(probe.subprocess, "run", timeout)
    report = probe.shell_self_test()
    assert sum(timeouts) == 20 and max(timeouts) == 3
    assert len(timeouts) == 7 and report.count("检测超时") == 7
    assert report.count("[✓]") == 1


@pytest.mark.parametrize("code, stdout", [(1, b"failed"), (0, b"")])
def test_probe_does_not_claim_failed_binary_is_installed(monkeypatch, code, stdout):
    shell = SimpleNamespace(executable=True, name="fixture", path="fixture", kind="sh")
    monkeypatch.setattr(probe, "default_shell", lambda: shell)
    monkeypatch.setattr(probe.subprocess, "run", lambda *_a, **_kw: SimpleNamespace(returncode=code, stdout=stdout))
    report = probe.shell_self_test()
    assert report.count("[✗]") == 6
    assert report.count("[✓]") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["run_command", "run_program"])
@pytest.mark.parametrize("timed_out, code", [(False, 1), (True, 0)])
async def test_failed_command_is_error_and_audited(gateway, monkeypatch, name, timed_out, code):
    result = output()
    result.timed_out, result.exit_code = timed_out, code
    monkeypatch.setattr(gm, name, lambda *_a, **_kw: result)
    audit = []
    monkeypatch.setattr(gateway, "_audit_gateway_tool", lambda *args, **kw: audit.append(args[5]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1") as client:
        response = await client.post("/mcp", json=call(name))
    assert response.json()["result"]["isError"] is True
    assert audit == [False]
    await gateway._http.aclose()


@pytest.mark.asyncio
@pytest.mark.skipif(gm.os.name != "nt", reason="Windows administrator broker")
@pytest.mark.parametrize("name", ["run_command", "run_program"])
@pytest.mark.parametrize("registered", [False, True])
async def test_elevation_stays_authorized_and_off_loop(gateway, monkeypatch, name, registered):
    import local_dev_mcp_bridge.elevation as elevation

    calls = []
    main_thread = threading.get_ident()

    def execute(*args):
        calls.append((threading.get_ident(), args))
        return output()

    controller = SimpleNamespace(is_registered=lambda: registered,
                                 execute_command=execute, execute_program=execute)
    monkeypatch.setattr(elevation, "get_elevation_controller", lambda: controller)
    monkeypatch.setattr(gateway, "_workspace_permission_mode", lambda _id: "system")

    def no_downgrade(*_args, **_kwargs):
        raise AssertionError("administrator execution must not downgrade")

    monkeypatch.setattr(gm, name, no_downgrade)
    rpc = call(name, timeout_seconds=999)
    response = await gateway._exec_local_tool(name, rpc, rpc["params"])
    body = json.loads(response.body)
    if registered:
        assert "result" in body and len(calls) == 1
        assert calls[0][0] != main_thread and calls[0][1][-1] == 20
    else:
        assert body["error"]["code"] == -32602 and not calls
    await gateway._http.aclose()


@pytest.mark.asyncio
async def test_real_subprocess_can_callback_same_http_gateway(gateway):
    """A real child GET to its parent Hub previously deadlocked the event loop."""
    import sys

    gateway.start(port=0)
    try:
        for _ in range(100):
            if gateway._server.started:
                break
            await asyncio.sleep(0.02)
        assert gateway._server.started
        port = gateway._server.servers[0].sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        script = (
            "import urllib.request; "
            f"r=urllib.request.urlopen('{url}/health',timeout=2); "
            "print('callback', r.status)"
        )
        async with httpx.AsyncClient(base_url=url, timeout=8) as client:
            response = await client.post("/mcp", json=call("run_program", executable=sys.executable,
                                                         args=["-c", script], timeout_seconds=5))
        body = response.json()
        assert body["id"] == "http-1" and body["result"]["isError"] is False
        assert "callback 200" in body["result"]["content"][0]["text"]
    finally:
        gateway.stop()
    assert not gateway.is_running
    assert not gateway._local_tool_workers


@pytest.mark.asyncio
async def test_permission_is_rechecked_at_worker_execution(gateway, monkeypatch):
    """A permission change after HTTP admission must prevent command execution."""
    checks, executions = [], []

    def policy(*args):
        checks.append(args)
        if len(checks) == 1:
            return None  # HTTP admission passed before the permission change.
        return ("permission_denied", -32004, "permission changed before execution")

    monkeypatch.setattr(gateway, "_workspace_tool_policy_error", policy)
    monkeypatch.setattr(gm, "run_command", lambda *_a, **_kw: executions.append(True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://127.0.0.1") as client:
        response = await client.post("/mcp", json=call("run_command", identity="policy-change"))
    assert response.json()["id"] == "policy-change"
    assert response.json()["error"]["code"] == -32004
    assert len(checks) == 2 and not executions
    await gateway._http.aclose()
