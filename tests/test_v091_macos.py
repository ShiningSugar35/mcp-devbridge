"""macOS platform contract, runnable without accessing a real user profile."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import local_dev_mcp_bridge.platform_support as ps
import local_dev_mcp_bridge.update_manager as updates


def _mac(monkeypatch):
    monkeypatch.setattr(ps, "IS_WINDOWS", False)
    monkeypatch.setattr(ps, "IS_LINUX", False)
    monkeypatch.setattr(ps, "IS_MACOS", True)
    monkeypatch.setattr(updates, "IS_WINDOWS", False)
    monkeypatch.setattr(updates, "IS_LINUX", False)
    monkeypatch.setattr(updates, "IS_MACOS", True, raising=False)


def test_macos_config_is_native_and_does_not_follow_xdg(monkeypatch, tmp_path):
    _mac(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "wrong-xdg"))
    assert ps.macos_config_dir() == tmp_path / "Library" / "Application Support" / "LocalDevMCPBridge"


def test_macos_desktop_ignores_linux_user_dirs(monkeypatch, tmp_path):
    _mac(monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / "xdg"
    config.mkdir()
    (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/wrong-desktop"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    assert ps.desktop_dir() == tmp_path / "Desktop"


@pytest.mark.parametrize("machine, expected", [("arm64", "arm64"), ("aarch64", "arm64"), ("x86_64", "x86_64"), ("AMD64", "x86_64")])
def test_mac_update_asset_is_architecture_specific(monkeypatch, machine, expected):
    _mac(monkeypatch)
    monkeypatch.setattr(updates.platform, "machine", lambda: machine)
    assert updates._release_asset_prefix() == f"MCPDevBridge-macOS-{expected}-"


def test_unsupported_mac_architecture_is_explicit(monkeypatch):
    _mac(monkeypatch)
    monkeypatch.setattr(updates.platform, "machine", lambda: "mips")
    with pytest.raises(RuntimeError):
        updates._release_asset_prefix()


def test_mac_update_does_not_pick_windows_or_other_arch(monkeypatch):
    _mac(monkeypatch)
    monkeypatch.setattr(updates.platform, "machine", lambda: "arm64")
    payload = {"tag_name": "v0.9.1", "assets": [
        {"name": "MCPDevBridge-Setup-0.9.1.exe", "size": 1},
        {"name": "MCPDevBridge-macOS-x86_64-0.9.1.zip", "size": 1},
        {"name": "MCPDevBridge-macOS-arm64-0.9.1.zip", "size": 1,
         "browser_download_url": "https://example.invalid/package.zip", "digest": "sha256:" + "a" * 64},
    ]}
    info = updates._release_info_from_payload(payload)
    assert info is not None
    assert info.asset_name == "MCPDevBridge-macOS-arm64-0.9.1.zip"
    assert info.platform == "macos"


def test_mac_finder_uses_argument_array_without_shell(monkeypatch, tmp_path):
    _mac(monkeypatch)
    calls = []
    monkeypatch.setattr(ps.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    target = tmp_path / "folder with space;name"
    assert ps.open_in_file_manager(target)
    assert calls[0][0] == ["open", str(target.resolve())]
    assert not calls[0][1].get("shell", False)
    assert "creationflags" not in calls[0][1]


def test_mac_entry_config_does_not_overwrite_explicit_profile(monkeypatch, tmp_path):
    from local_dev_mcp_bridge.macos_app import prepare_environment

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "explicit-profile"))
    prepare_environment()
    assert os.environ["LOCALDEV_MCP_CONFIG_DIR"] == str(tmp_path / "explicit-profile")


def test_mac_entry_preserves_existing_legacy_configuration(monkeypatch, tmp_path):
    from local_dev_mcp_bridge.macos_app import prepare_environment

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALDEV_MCP_CONFIG_DIR", raising=False)
    old = tmp_path / ".config" / "LocalDevMCPBridge"
    old.mkdir(parents=True)
    (old / "projects.json").write_text("[]", encoding="utf-8")
    prepare_environment()
    assert os.environ["LOCALDEV_MCP_CONFIG_DIR"] == str(old)


def test_mac_entry_new_profile_uses_application_support(monkeypatch, tmp_path):
    from local_dev_mcp_bridge.macos_app import prepare_environment

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALDEV_MCP_CONFIG_DIR", raising=False)
    prepare_environment()
    assert os.environ["LOCALDEV_MCP_CONFIG_DIR"] == str(tmp_path / "Library" / "Application Support" / "LocalDevMCPBridge")
