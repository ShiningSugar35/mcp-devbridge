"""Tests for the shell execution profile (safe/developer/full_system)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

from local_dev_mcp_bridge import engines
from local_dev_mcp_bridge.execution_profile import (
    DEFAULT_EXECUTION_PROFILE,
    ExecutionProfileError,
    check_execution,
    enforce_full_system_confirmation,
    normalize_first_word,
)
from local_dev_mcp_bridge.models import AppConfig, RuntimeConfig
from local_dev_mcp_bridge.permissions import PermissionError as PermissionDenied
from local_dev_mcp_bridge.shell import default_shell, detect_shells, get_shell_info
from local_dev_mcp_bridge.tools import LocalDevTools

WS = None


@pytest.fixture
def ws(tmp_path):
    git = tmp_path / "ws"
    git.mkdir()
    (git / "pyproject.toml").write_text("[project]\nname=\"ws\"\n", encoding="utf-8")
    return git


# ---------------------------------------------------------------------------
# Profile policy
# ---------------------------------------------------------------------------


def test_developer_is_default():
    assert DEFAULT_EXECUTION_PROFILE == "developer"


def test_developer_allows_core_tooling():
    for cmd in (
        "pytest tests/",
        "pyright src",
        "ruff check src",
        "git status",
        "git diff --stat",
        "git log --oneline -5",
        "npm run lint",
        "npm install",
        "npx tsc --noEmit",
        "uv run pytest",
        "python -m pytest -q",
        "pip install -r requirements.txt",
    ):
        allowed, _ = check_execution(cmd, "developer")
        assert allowed, f"应为允许: {cmd}"


def test_developer_blocks_non_tooling():
    for cmd in ("notepad", "start cmd", "taskkill /f /im chrome.exe", "regedit", "msiexec"):
        allowed, reason = check_execution(cmd, "developer")
        assert not allowed, f"应被拒绝: {cmd} ({reason})"


def test_dangerous_commands_blocked_in_all_profiles():
    for profile in ("safe", "developer", "full_system"):
        for cmd in (
            "format C:",
            "diskpart",
            "shutdown /s /t 0",
            "reboot",
            "reg delete HKLM\\Software\\X",
            "bcdedit /set {current} safeboot minimal",
            "del /s /q C:\\Users",
            "Remove-Item -Recurse C:\\Windows",
            "rm -rf /",
        ):
            allowed, reason = check_execution(cmd, profile)
            assert not allowed, f"{profile} 应拦截: {cmd}"


def test_safe_preserves_project_commands():
    for cmd in ("pytest tests", "git status", "随便什么命令", "python run.py"):
        allowed, _ = check_execution(cmd, "safe")
        assert allowed


def test_full_system_requires_confirmation_at_boundary():
    with pytest.raises(ExecutionProfileError):
        enforce_full_system_confirmation("full_system", confirmed=False)
    enforce_full_system_confirmation("full_system", confirmed=True)
    enforce_full_system_confirmation("developer", confirmed=False)


def test_check_full_system_pure():
    allowed, _ = check_execution("cmd /c dir", "full_system")
    assert allowed


def test_normalize_first_word():
    assert normalize_first_word(r"C:\Python312\python.exe -m pytest") == "python"
    assert normalize_first_word(" C:\\Python312\\python.exe -m pytest ") == "python"
    assert normalize_first_word('"uv" run x') == "uv"
    assert normalize_first_word("  pytest  ") == "pytest"


# ---------------------------------------------------------------------------
# Shell detection
# ---------------------------------------------------------------------------


def test_default_shell_never_wsl():
    shell = default_shell()
    assert shell.kind != "wsl"
    assert shell.executable, "windows powershell (or pwsh/cmd) must exist on CI"


def test_detect_reports_powershell_family():
    kinds = {s.kind for s in detect_shells()}
    assert "windows_powershell" in kinds or "pwsh" in kinds or "cmd" in kinds


def test_get_shell_info_json():
    info = get_shell_info()
    default = cast(dict, info["default"])
    assert default.get("executable") is True
    assert isinstance(info["detected"], list)


def test_powershell_runs():
    import local_dev_mcp_bridge.shell as s

    res = s.run_command("echo hi", cwd=Path("."), timeout_seconds=15)
    assert res.exit_code == 0
    assert "hi" in res.stdout


# ---------------------------------------------------------------------------
# Tool layer
# ---------------------------------------------------------------------------


@pytest.fixture
def tools_(ws):
    return LocalDevTools(ws, "workspace")


def test_run_command_pytest_ok(tools_):
    res = tools_.run_command("pytest --version")
    assert "pytest" in res


def test_run_command_blocks_dangerous(tools_):
    with pytest.raises(PermissionDenied):
        tools_.run_command("format D:")


def test_run_command_git_workflow(tools_, ws):
    res = tools_.run_command("git init", cwd=str(ws))
    assert "initialized" in res.lower()
    res = tools_.run_command("git status", cwd=str(ws))
    assert "branch" in res.lower() or "no commits" in res.lower()


def test_full_system_blocked_without_confirmation(tmp_path):
    tools = LocalDevTools(tmp_path, "workspace", execution_profile="full_system", full_system_confirmed=False)
    with pytest.raises(PermissionDenied):
        tools.run_command("pytest --version")


def test_full_system_allowed_after_confirmation(tmp_path):
    tools = LocalDevTools(tmp_path, "workspace", execution_profile="full_system", full_system_confirmed=True)
    out = tools.run_command("python --version")
    assert "Python" in out


def test_shell_program_runs_pyright(ws):
    tools = LocalDevTools(ws)
    res = tools.run_program(sys.executable, ["-m", "pyright", "--version"], cwd=str(ws))
    assert "pyright" in res.lower() or "version" in res.lower()


def test_shell_self_test(tools_):
    out = tools_.shell_self_test()
    assert "python" in out and "git" in out and "pytest" in out


# ---------------------------------------------------------------------------
# CodexPro engine mapping
# ---------------------------------------------------------------------------


def test_build_codex_env_developer(tmp_path):
    env = engines.build_codex_env(str(tmp_path), permission_mode="workspace", token="x" * 32, execution_profile="developer")
    assert env["CODEXPRO_BASH_MODE"] == "developer"


def test_build_codex_env_safe(tmp_path):
    env = engines.build_codex_env(str(tmp_path), permission_mode="workspace", token="x" * 32, execution_profile="safe")
    assert env["CODEXPRO_BASH_MODE"] == "safe"


def test_build_codex_env_system_still_full(tmp_path):
    env = engines.build_codex_env(str(tmp_path), permission_mode="system", token="x" * 32, execution_profile="developer")
    assert env["CODEXPRO_BASH_MODE"] == "full"
    assert env["CODEXPRO_WINDOWS_PROFILE"] == "system_full"


def test_build_codex_env_default_developer(tmp_path):
    env = engines.build_codex_env(str(tmp_path), permission_mode="workspace", token="x" * 32)
    assert env["CODEXPRO_BASH_MODE"] == "developer"


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_runtime_config_defaults():
    rc = RuntimeConfig(workspace="C:/x")
    assert rc.execution_profile == "developer"
    assert rc.full_system_confirmed is False


def test_app_config_roundtrip_keep(ws):
    cfg = AppConfig(active_workspace=str(ws), execution_profile="full_system", full_system_risk_accepted=True)
    dumped = json.loads(cfg.model_dump_json())
    assert dumped["execution_profile"] == "full_system"
    assert dumped["full_system_risk_accepted"] is True


def test_build_backend_forwards_profile(ws, tmp_path):
    from local_dev_mcp_bridge import server_factory
    rc = RuntimeConfig(workspace=str(ws), execution_profile="safe", full_system_confirmed=True)
    _mcp, _app, tools = server_factory.build_backend(rc)
    assert tools.execution_profile == "safe"
    assert tools.full_system_confirmed is True