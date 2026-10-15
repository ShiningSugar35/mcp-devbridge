"""Truthful Hub effects: read-only discovery must not look like shell execution."""
from __future__ import annotations

import copy
import json

import pytest

from local_dev_mcp_bridge.gateway import (
    _PYTHON_TOOL_DEFS,
    _STABLE_HUB_READ_ONLY_TOOLS,
    _STABLE_HUB_TOOLS,
    _inject_tools,
    _rewrite_server_identity,
    _stable_tools_list_payload,
    _tools_response_summary,
)
from local_dev_mcp_bridge.hub_tool_contract import (
    HUB_TOOL_CONTRACT_FINGERPRINT,
    HUB_TOOL_COUNT,
    load_codexpro_full_tool_contract,
)

QUERIES = (
    "devbridge_list_workspaces", "devbridge_get_current_workspace",
    "devbridge_list_devices", "devbridge_get_current_device",
)
EXECUTORS = ("run_command", "run_program", "shell_self_test")
SWITCHES = ("devbridge_switch_workspace", "devbridge_switch_device")


@pytest.mark.parametrize("name", QUERIES)
def test_hub_queries_have_explicit_read_only_effects(name):
    tool = next(item for item in _STABLE_HUB_TOOLS if item["name"] == name)
    assert tool.get("annotations") == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert name in _STABLE_HUB_READ_ONLY_TOOLS


@pytest.mark.parametrize("name", EXECUTORS)
def test_shell_and_program_diagnostics_are_not_misrepresented_as_read_only(name):
    tool = next(item for item in _STABLE_HUB_TOOLS if item["name"] == name)
    # Even shell_self_test launches configurable external executables/modules.
    assert tool.get("annotations") == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    assert name not in _STABLE_HUB_READ_ONLY_TOOLS


@pytest.mark.parametrize("name", SWITCHES)
def test_session_switch_is_explicitly_state_changing(name):
    tool = next(item for item in _STABLE_HUB_TOOLS if item["name"] == name)
    assert tool.get("annotations") == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert name not in _STABLE_HUB_READ_ONLY_TOOLS


def test_all_fifty_tools_have_boolean_effect_annotations_without_schema_drift():
    payload = _stable_tools_list_payload(42)
    tools = json.loads(payload)["result"]["tools"]
    assert len(tools) == HUB_TOOL_COUNT == 50
    assert len({item["name"] for item in tools}) == 50
    assert _tools_response_summary(payload)["schema_fingerprint"] == HUB_TOOL_CONTRACT_FINGERPRINT
    for tool in tools:
        annotations = tool.get("annotations")
        assert isinstance(annotations, dict), tool["name"]
        for key in ("readOnlyHint", "destructiveHint", "openWorldHint"):
            assert type(annotations.get(key)) is bool, (tool["name"], key)
    for tool in tools:
        if tool["name"] in {"write", "edit", "apply_patch", "bash", "codexpro", "windows_call"}:
            assert tool["annotations"]["readOnlyHint"] is False
            assert tool["annotations"]["destructiveHint"] is True


@pytest.mark.parametrize("sse", [False, True])
def test_annotation_injection_is_deterministic_and_preserves_upstream_metadata(sse):
    source = load_codexpro_full_tool_contract()
    before = copy.deepcopy(source)
    local_before = copy.deepcopy(_PYTHON_TOOL_DEFS)
    raw = json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"tools": source}}).encode()
    if sse:
        raw = b"event: message\ndata: " + raw + b"\n\n"
    result = _inject_tools(raw)
    result = _inject_tools(result)
    document = next(line[6:] for line in result.splitlines() if line.startswith(b"data: ")) if sse else result
    actual = json.loads(document)["result"]["tools"]
    assert len(actual) == 50
    by_name = {item["name"]: item for item in actual}
    for tool in before:
        assert by_name[tool["name"]]["annotations"] == tool["annotations"]
        assert by_name[tool["name"]].get("_meta") == tool.get("_meta")
    for name in QUERIES:
        assert by_name[name]["annotations"]["readOnlyHint"] is True
    assert local_before == _PYTHON_TOOL_DEFS
    assert source == before


def test_unknown_tool_is_not_given_a_read_only_override():
    unknown = {"name": "vendor_unknown", "inputSchema": {"type": "object"}}
    raw = json.dumps({"result": {"tools": [unknown]}}).encode()
    tools = json.loads(_inject_tools(raw))["result"]["tools"]
    assert "annotations" not in tools[0]


@pytest.mark.parametrize("sse", [False, True])
def test_initialize_explains_raw_shell_and_direct_routing_without_duplicate_notes(sse):
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "serverInfo": {"name": "engine", "version": "fixture"},
        "instructions": "Retain upstream instructions.",
    }}).encode()
    if sse:
        raw = b"data: " + raw + b"\n\n"
    once = _rewrite_server_identity(raw)
    twice = _rewrite_server_identity(once)
    document = next(line[6:] for line in twice.splitlines() if line.startswith(b"data: ")) if sse else twice
    result = json.loads(document)["result"]
    instructions = result["instructions"]
    assert instructions.startswith("Retain upstream instructions.")
    assert instructions.count("MCP DevBridge command execution:") == 1
    assert "raw script" in instructions
    assert "absolute cwd" in instructions
    assert "client confirmation" in instructions
    assert "do not replay" in instructions
    assert result["serverInfo"]["version"] == "fixture"
    assert twice == once
