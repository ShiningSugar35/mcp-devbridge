"""Project lifecycle always tells both plain/elevated engines whether UI is enabled."""
from __future__ import annotations

import pytest

from local_dev_mcp_bridge.engines import build_codex_env


@pytest.mark.parametrize("mode", ["read_only", "workspace", "system"])
@pytest.mark.parametrize("enabled", [False, True])
def test_windows_enablement_is_explicit_for_every_permission_mode(mode: str, enabled: bool) -> None:
    env = build_codex_env(
        "D:/project",
        permission_mode=mode,
        token="x" * 32,
        windows_token="y" * 32 if enabled else None,
    )
    assert env["CODEXPRO_WINDOWS_ENABLED"] == ("1" if enabled else "0")
    assert ("CODEXPRO_WINDOWS_BRIDGE_TOKEN" in env) is enabled
    assert env["CODEXPRO_WINDOWS_PROFILE"] == ("system_full" if mode == "system" else "desktop_ui")
