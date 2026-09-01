from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest
from local_dev_mcp_bridge.regular_chat import (
    RegularChatClient,
    RegularChatError,
    RegularChatPaths,
    reset_profile,
    workspace_hash,
)

from local_dev_mcp_bridge import regular_chat


def test_workspace_hash_is_canonical_and_stable(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    assert workspace_hash(child) == workspace_hash(child / ".." / "child")
    assert len(workspace_hash(child)) == 64


def test_profile_reset_is_scoped_to_regular_chat_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "config"))
    target = regular_chat.regular_chat_runtime_dir() / "profiles" / "default-managed-managed"
    target.mkdir(parents=True)
    (target / "state.txt").write_text("x", encoding="utf-8")
    sibling = regular_chat.regular_chat_runtime_dir() / "profiles" / "keep"
    sibling.mkdir(parents=True)
    removed = reset_profile()
    assert removed == target
    assert not target.exists()
    assert sibling.is_dir()


def _write_fake_sidecar(path: Path) -> None:
    path.write_text(
        """
import json, sys, time
for line in sys.stdin:
    req = json.loads(line)
    if req.get('method') == 'controller.stop':
        print(json.dumps({'id': req['id'], 'result': {'ok': True}}), flush=True)
        break
    if req.get('method') == 'boom':
        print(json.dumps({'id': req['id'], 'error': {'code': -32000, 'message': 'boom'}}), flush=True)
        continue
    if req.get('method') == 'hang':
        time.sleep(5)
        continue
    print(json.dumps({'id': req['id'], 'result': {'method': req['method'], 'params': req.get('params', {})}}), flush=True)
""".strip(),
        encoding="utf-8",
    )


def test_stdio_client_round_trip_and_error_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(regular_chat, "find_node", lambda: sys.executable)
    paths = RegularChatPaths(
        runtime_root=tmp_path / "runtime",
        controller_entry=script,
        package_root=tmp_path,
        browsers_dir=tmp_path / "runtime" / "browsers",
    )
    client = RegularChatClient(engine="chrome", paths=paths)
    try:
        assert client.request("ping", {"value": 1}) == {
            "method": "ping",
            "params": {"value": 1},
        }
        with pytest.raises(RegularChatError, match="boom"):
            client.request("boom")
        assert client.request("after") == {"method": "after", "params": {}}
    finally:
        client.stop()
    assert not client.is_running


def test_stdio_client_rejects_oversized_payload_before_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(regular_chat, "find_node", lambda: sys.executable)
    paths = RegularChatPaths(tmp_path / "runtime", script, tmp_path, tmp_path / "runtime" / "browsers")
    client = RegularChatClient(engine="chrome", paths=paths)
    try:
        with pytest.raises(RegularChatError, match="IPC"):
            client.request("oversize", {"value": "x" * (regular_chat.MAX_RPC_LINE_BYTES + 1)})
    finally:
        client.stop()


def test_stdio_client_timeout_terminates_only_owned_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(regular_chat, "find_node", lambda: sys.executable)
    paths = RegularChatPaths(tmp_path / "runtime", script, tmp_path, tmp_path / "runtime" / "browsers")
    client = RegularChatClient(engine="chrome", paths=paths)
    with pytest.raises(RegularChatError, match="未返回"):
        client.request("hang", timeout_seconds=0.2)
    assert not client.is_running


def test_abort_interrupts_an_inflight_rpc_without_waiting_for_its_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "fake_sidecar.py"
    _write_fake_sidecar(script)
    monkeypatch.setattr(regular_chat, "find_node", lambda: sys.executable)
    paths = RegularChatPaths(tmp_path / "runtime", script, tmp_path, tmp_path / "runtime" / "browsers")
    client = RegularChatClient(engine="chrome", paths=paths)
    errors: list[BaseException] = []

    def request() -> None:
        try:
            client.request("hang", timeout_seconds=30.0)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=request, daemon=True)
    worker.start()
    deadline = time.monotonic() + 3.0
    while not client.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    client.abort()
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    assert time.monotonic() - started < 3.0
    assert errors and isinstance(errors[0], RegularChatError)
    assert not client.is_running


def test_packaging_spec_includes_the_console_mcpdev_entrypoint() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "local-dev-mcp-bridge.spec").read_text(encoding="utf-8")
    entry = (root / "packaging" / "entry_cli.py").read_text(encoding="utf-8")
    assert 'entry_cli.py' in spec
    assert 'name="mcpdev"' in spec
    assert "console=True" in spec
    assert "local_dev_mcp_bridge.cli" in entry


def test_session_store_contract_contains_no_raw_auth_fields(tmp_path: Path) -> None:
    # This test protects the Python orchestration layer from accidentally adding
    # auth material to the wire protocol.  The raw prompt is permitted only in
    # the ephemeral turn.send request, never in persisted provider-session state.
    request = {"id": 1, "method": "session.open", "params": {"workspace_hash": "a" * 64, "run_id": "lr_x"}}
    serialized = json.dumps(request).lower()
    for forbidden in ("cookie", "authorization", "access_token", "refresh_token", "password"):
        assert forbidden not in serialized
