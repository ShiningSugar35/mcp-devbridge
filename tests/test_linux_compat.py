from __future__ import annotations

import os
import stat
from pathlib import Path

import local_dev_mcp_bridge.constants as constants
import local_dev_mcp_bridge.platform_support as ps
import local_dev_mcp_bridge.secrets as secret_store
import local_dev_mcp_bridge.shell as shell
import local_dev_mcp_bridge.update_manager as updates
from local_dev_mcp_bridge.secrets import SecretsStore
from local_dev_mcp_bridge.shell import ShellInfo


def test_linux_xdg_config_path(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LOCALDEV_MCP_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(constants, "IS_WINDOWS", False)
    assert constants._base_config_dir() == tmp_path / "xdg" / "LocalDevMCPBridge"


def test_linux_aesgcm_fallback_is_encrypted_and_user_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(secret_store, "IS_WINDOWS", False)
    monkeypatch.setattr(secret_store, "IS_LINUX", True)
    monkeypatch.setattr(secret_store, "_secret_tool_path", lambda: "")
    store = SecretsStore(use_credential_manager=False)
    store.set("demo", "sensitive-value")
    assert store.get("demo") == "sensitive-value"
    encrypted = tmp_path / "cfg" / "secrets.aesgcm"
    key = tmp_path / "cfg" / "secrets.key"
    assert encrypted.is_file() and key.is_file()
    assert b"sensitive-value" not in encrypted.read_bytes()
    assert encrypted.read_bytes().startswith(b"MCPDB1")
    if os.name != "nt":
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert stat.S_IMODE(encrypted.stat().st_mode) == 0o600
    store.delete("demo")
    assert store.get("demo") is None


def test_linux_shell_build_uses_bash_lc(monkeypatch) -> None:
    monkeypatch.setattr(shell, "IS_WINDOWS", False)
    monkeypatch.setattr(shell, "default_shell", lambda: ShellInfo("bash", "/bin/bash", "bash"))
    argv, display = shell.build_shell_command("printf hello")
    assert argv == ["/bin/bash", "-lc", "printf hello"]
    assert display == "/bin/bash"


def test_posix_popen_kwargs_do_not_emit_windows_creationflags(monkeypatch) -> None:
    monkeypatch.setattr(ps, "IS_WINDOWS", False)
    kwargs = ps.popen_platform_kwargs(new_session=True)
    assert kwargs == {"start_new_session": True}
    assert "creationflags" not in kwargs
    assert ps.run_platform_kwargs() == {}


def test_linux_release_asset_prefix(monkeypatch) -> None:
    monkeypatch.setattr(updates, "IS_WINDOWS", False)
    monkeypatch.setattr(updates, "IS_LINUX", True)
    monkeypatch.setattr(updates.platform, "machine", lambda: "x86_64")
    assert updates._release_asset_prefix() == "MCPDevBridge-Linux-x86_64-"


def test_linux_distribution_scripts_and_workflow_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    scripts = [
        root / "scripts" / "prepare_runtime_linux.sh",
        root / "scripts" / "install_linux.sh",
        root / "scripts" / "live_upgrade.sh",
        root / "scripts" / "build_linux.sh",
    ]
    for path in scripts:
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "linux-steamos-test" in ci
    assert "ubuntu-22.04" in ci
    assert "MCPDevBridge-Linux-x86_64" in release
    spec = (root / "packaging" / "local-dev-mcp-bridge.spec").read_text(encoding="utf-8")
    assert 'live_upgrade.sh' in spec
    assert 'cloudflared_name = "cloudflared.exe" if IS_WINDOWS else "cloudflared"' in spec
