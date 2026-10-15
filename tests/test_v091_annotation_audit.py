"""Annotation diagnostics retain effects, never arbitrary tool metadata or secrets."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "annotation_audit", Path(__file__).resolve().parents[1] / "scripts" / "audit_tool_annotations.py"
)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


@pytest.mark.parametrize("sse", [False, True])
def test_decodes_catalog_transport_without_changing_content(sse):
    source = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read"}]}}
    raw = json.dumps(source).encode()
    if sse:
        raw = b": keepalive\nevent: message\ndata: " + raw + b"\n\n"
    assert AUDIT.decode_response(raw) == source


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"error":{"message":"private-fixture"}}'])
def test_protocol_errors_never_echo_original_payload(raw):
    with pytest.raises(ValueError) as caught:
        AUDIT.decode_response(raw)
    assert "private-fixture" not in str(caught.value)


def test_summary_ignores_arbitrary_metadata_and_non_boolean_flags():
    source = [{"name": "read", "description": "do-not-export", "_meta": {"private": "hidden-fixture"},
               "annotations": {"readOnlyHint": "true", "destructiveHint": False,
                               "title": "private-title", "openWorldHint": 1}}]
    result = AUDIT.summarize(source)
    encoded = json.dumps(result)
    for marker in ("do-not-export", "hidden-fixture", "private-title"):
        assert marker not in encoded
    row = result["tools"][0]
    assert row["annotations_present"] is True
    assert row["destructiveHint"] is False
    assert "readOnlyHint" not in row and "openWorldHint" not in row


def test_fingerprint_tracks_effects_not_only_names_and_schema():
    source = [{"name": "read", "annotations": {"readOnlyHint": True}}]
    before = AUDIT.summarize(source)["annotation_fingerprint"]
    source[0]["annotations"]["readOnlyHint"] = False
    assert AUDIT.summarize(source)["annotation_fingerprint"] != before


def test_summary_is_order_invariant_and_reports_missing_annotations():
    source = [{"name": "a"}, {"name": "b", "annotations": {"readOnlyHint": False}}]
    assert AUDIT.summarize(source) == AUDIT.summarize(list(reversed(source)))
    assert AUDIT.summarize(source)["missing_annotations"] == ["a"]


@pytest.mark.parametrize("endpoint", [
    "https://example.org/mcp", "https://mcp.shiningsugar.shop/mcp?unexpected=value",
    "http://mcp.shiningsugar.shop/mcp", "https://mcp.shiningsugar.shop.example.org/mcp",
])
def test_unapproved_endpoint_is_rejected_before_credential_access(endpoint):
    with pytest.raises(ValueError, match="existing endpoints"):
        AUDIT.fetch_catalog(endpoint)
