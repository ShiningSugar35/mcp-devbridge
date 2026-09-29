"""Scheduled continuation reuses canonical long-run Gateway policies."""

from local_dev_mcp_bridge import gateway


def test_continuation_write_reuses_canonical_long_run_update_policy() -> None:
    effective, nested = gateway.OAuthGateway._unwrap_codexpro_call(
        "codexpro",
        {
            "action": "long_run_update",
            "args": {
                "workspace_id": "ws_fixture",
                "run_id": "lr_fixture_12345678",
                "continuation": {"operation": "request_schedule", "expected_revision": 0},
            },
        },
    )
    assert effective == "long_run_update"
    assert "continuation" in nested
    assert effective in gateway._READ_ONLY_DISABLED_TOOLS
    assert effective in gateway._CODEXPRO_MINIMAL_TOOLS


def test_continuation_read_is_projected_by_canonical_long_run_status_policy() -> None:
    effective, nested = gateway.OAuthGateway._unwrap_codexpro_call(
        "codexpro",
        {
            "action": "long_run_status",
            "args": {"workspace_id": "ws_fixture", "run_id": "lr_fixture_12345678"},
        },
    )
    assert effective == "long_run_status"
    assert nested["run_id"] == "lr_fixture_12345678"
    assert effective not in gateway._READ_ONLY_DISABLED_TOOLS
    assert effective in gateway._CODEXPRO_MINIMAL_TOOLS


def test_gateway_needs_no_new_continuation_aliases() -> None:
    assert "continuation_update" not in gateway._CODEXPRO_ACTION_ALIASES
    assert "continuation_status" not in gateway._CODEXPRO_ACTION_ALIASES
