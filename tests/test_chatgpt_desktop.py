from __future__ import annotations

import json
from pathlib import Path

import pytest

import local_dev_mcp_bridge.chatgpt_desktop as chat


def test_relative_to_route_uses_forward_slashes_and_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "drive"
    target = root / "repo" / "sub"
    target.mkdir(parents=True)
    bridge = chat.ChatGPTDesktopBridge()
    assert bridge._relative_to_route(root, target) == "repo/sub"
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RuntimeError, match="routed MCP root"):
        bridge._relative_to_route(root, outside)


def test_prepare_bridge_without_restart_uses_existing_loopback_cdp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    executable = tmp_path / "ChatGPT.exe"
    executable.write_bytes(b"stub")
    monkeypatch.setattr(chat, "IS_WINDOWS", True)
    monkeypatch.setattr(chat, "find_chatgpt_executable", lambda: str(executable))
    monkeypatch.setattr(chat, "_cdp_ready", lambda port: port == 19333)
    result = chat.prepare_chatgpt_bridge(restart=False, debug_port=19333)
    assert result["enabled"] is True
    assert result["ready"] is True
    assert result["loopback_only"] is True
    assert result["debug_port"] == 19333
    persisted = json.loads((tmp_path / "cfg" / "chatgpt-desktop-bridge.json").read_text(encoding="utf-8"))
    assert persisted["enabled"] is True
    assert persisted["debug_port"] == 19333


def test_prepare_bridge_refuses_implicit_restart(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    executable = tmp_path / "ChatGPT.exe"
    executable.write_bytes(b"stub")
    monkeypatch.setattr(chat, "IS_WINDOWS", True)
    monkeypatch.setattr(chat, "find_chatgpt_executable", lambda: str(executable))
    monkeypatch.setattr(chat, "_cdp_ready", lambda _port: False)
    with pytest.raises(RuntimeError, match="显式允许重启"):
        chat.prepare_chatgpt_bridge(restart=False, debug_port=19334)


def test_run_task_requires_external_verified_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    route = tmp_path / "route"
    target = route / "project"
    target.mkdir(parents=True)
    bridge = chat.ChatGPTDesktopBridge()
    monkeypatch.setattr(chat.ChatGPTDesktopBridge, "ready", property(lambda self: True))
    captured: dict[str, str] = {}

    def fake_launch(prompt: str, task_id: str) -> str:
        captured["prompt"] = prompt
        receipt = route / ".mcp-devbridge-chat-agent-receipts" / f"{task_id}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps({"task_id": task_id, "status": "success", "summary": "verified"}),
            encoding="utf-8",
        )
        return "chatgpt:test-conversation"

    monkeypatch.setattr(bridge, "_launch_and_send", fake_launch)
    receipt, conversation_id, receipt_path = bridge.run_task(
        task_id="task-123",
        assignment="Create one test file.",
        route_root=route,
        target_workspace=target,
        write=True,
        route_workspace_id="ws-route-123",
        timeout_seconds=2,
    )
    assert receipt["status"] == "success"
    assert conversation_id == "chatgpt:test-conversation"
    assert receipt_path.is_file()
    assert "Stay in Chat mode; DO NOT hand off to Work or Codex" in captured["prompt"]
    assert "Do NOT call open_workspace, switch_workspace" in captured["prompt"]
    assert "Your target workspace is exactly project" in captured["prompt"]
    assert "devbridge_workspace_id='ws-route-123'" in captured["prompt"]
    assert ".mcp-devbridge-chat-agent-receipts/task-123.json" in captured["prompt"]
