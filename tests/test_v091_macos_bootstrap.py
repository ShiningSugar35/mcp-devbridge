"""Finder/bootstrap edge cases, isolated from real applications and user settings."""
from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from local_dev_mcp_bridge import macos_app as mac


def _bundle(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "Applications with spaces" / mac.BUNDLE_NAME
    executable = app / "Contents" / "MacOS" / "MCPDevBridge"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test-fixture-not-executable")
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": mac.BUNDLE_ID})
    )
    runtime = app / "Contents" / "Frameworks" / "runtime"
    runtime.mkdir(parents=True)
    for name in ("node", "cloudflared"):
        (runtime / name).write_bytes(b"fixture")
    return executable, runtime


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_profile_override_uses_native_configuration(tmp_path: Path, empty: str):
    env = {"LOCALDEV_MCP_CONFIG_DIR": empty, "PATH": "/usr/bin:/bin"}
    mac.prepare_environment(env=env, home=tmp_path, system="darwin", executable=tmp_path / "python")
    assert env["LOCALDEV_MCP_CONFIG_DIR"] == str(mac.native_config_dir(tmp_path))


@pytest.mark.parametrize("payload", [[], ["not-a-dictionary"], "text", 42])
def test_invalid_plist_root_does_not_crash_bootstrap(tmp_path: Path, payload):
    executable, _runtime = _bundle(tmp_path)
    (executable.parent.parent / "Info.plist").write_bytes(plistlib.dumps(payload))
    assert mac.bundle_for_executable(executable) is None


def test_repeated_bootstrap_keeps_private_runtime_path_single(tmp_path: Path):
    executable, runtime = _bundle(tmp_path)
    env = {"PATH": os.pathsep.join(("/usr/bin", "/bin"))}
    mac.prepare_environment(env=env, home=tmp_path, system="darwin", executable=executable)
    before = dict(env)
    mac.prepare_environment(env=env, home=tmp_path, system="darwin", executable=executable)
    assert env == before
    assert env["PATH"].split(os.pathsep).count(str(runtime)) == 1
    assert env["MCPDEVBRIDGE_NODE_EXE"] == str(runtime / "node")


def test_existing_explicit_profile_and_node_override_are_preserved(tmp_path: Path):
    executable, _runtime = _bundle(tmp_path)
    env = {"LOCALDEV_MCP_CONFIG_DIR": str(tmp_path / "selected"),
           "MCPDEVBRIDGE_NODE_EXE": str(tmp_path / "selected-node"), "PATH": "/usr/bin"}
    mac.prepare_environment(env=env, home=tmp_path, system="darwin", executable=executable)
    assert env["LOCALDEV_MCP_CONFIG_DIR"] == str(tmp_path / "selected")
    assert env["MCPDEVBRIDGE_NODE_EXE"] == str(tmp_path / "selected-node")


def test_non_mac_startup_does_not_change_environment(tmp_path: Path):
    env = {"LOCALDEV_MCP_CONFIG_DIR": "", "PATH": "original"}
    mac.prepare_environment(env=env, home=tmp_path, system="win32")
    assert env == {"LOCALDEV_MCP_CONFIG_DIR": "", "PATH": "original"}
