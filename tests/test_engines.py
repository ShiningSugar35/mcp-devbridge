"""Tests for engines.py: command/env building, redaction, readiness."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from local_dev_mcp_bridge.engines import (
    CODEXPRO_LOCAL_PORT,
    WINDOWS_BRIDGE_PORT,
    WINDOWS_MCP_PINNED_VERSION,
    CodexProManager,
    EngineState,
    ProcessLog,
    SpawnError,
    WindowsBridgeManager,
    build_codex_cmd,
    build_codex_env,
    find_node,
    find_uvx,
    port_listening,
    redact_line,
    sanitize_cmd_for_log,
)


class TestRedactLine:
    def test_masks_exact_secret_value(self) -> None:
        line = "CODEXPRO_HTTP_TOKEN=0123456789abcdef0123456789abcdef"
        masked = redact_line(line, ("0123456789abcdef0123456789abcdef",))
        assert "0123456789abcdef" not in masked
        assert "***" in masked

    def test_masks_key_value_pairs_without_explicit_secret(self) -> None:
        masked = redact_line("configured with token=xXyYzZ1234567890secret")
        assert "xXyYzZ1234567890secret" not in masked
        assert "token=" in masked

    def test_masks_bearer_values(self) -> None:
        masked = redact_line("using Authorization: Bearer abcdefgh12345678XYZ")
        assert "abcdefgh12345678XYZ" not in masked
        assert "bearer" in masked.lower()

    def test_masks_auth_key_cli_flag(self) -> None:
        masked = redact_line("running windows-mcp --auth-key supersecret-24byte-token-here")
        assert "supersecret-24byte-token-here" not in masked

    def test_leaves_plain_lines_untouched(self) -> None:
        line = "[CodexPro] HTTP MCP listening on http://127.0.0.1:8787/mcp"
        assert redact_line(line) == line


class TestSanitizeCmdForLog:
    def test_hides_token_flag_values(self) -> None:
        cmd = ["uvx", "windows-mcp", "serve", "--auth-key", "24byte-token-secret-abcdef"]
        display = sanitize_cmd_for_log(cmd)
        assert "24byte-token-secret-abcdef" not in display
        assert "***" in display

    def test_hides_secret_values_passed_in_list(self) -> None:
        cmd = ["node", "http.js", "--token", "tok"]
        display = sanitize_cmd_for_log(cmd, ("tok",))
        assert "tok" not in display
        assert display == "node http.js *** ***"  # flag and value both masked


class TestEnvAndCmd:
    def test_read_only_maps_to_off_writes_and_minimal_tools(self) -> None:
        env = build_codex_env("C:/repo", permission_mode="read_only", token="x" * 32)
        assert env["CODEXPRO_WRITE_MODE"] == "off"
        assert env["CODEXPRO_BASH_MODE"] == "off"
        assert env["CODEXPRO_TOOL_MODE"] == "minimal"

    def test_workspace_maps_to_developer_bash(self) -> None:
        env = build_codex_env("C:/repo", permission_mode="workspace", token="x" * 32)
        assert env["CODEXPRO_WRITE_MODE"] == "workspace"
        assert env["CODEXPRO_BASH_MODE"] == "developer"

    def test_workspace_safe_profile_keeps_safe_bash(self) -> None:
        env = build_codex_env(
            "C:/repo", permission_mode="workspace", token="x" * 32, execution_profile="safe"
        )
        assert env["CODEXPRO_BASH_MODE"] == "safe"

    def test_system_maps_to_full(self) -> None:
        env = build_codex_env("C:/repo", permission_mode="system", token="x" * 32)
        assert env["CODEXPRO_BASH_MODE"] == "full"
        assert env["CODEXPRO_TOOL_MODE"] == "full"

    def test_windows_token_absent_by_default(self) -> None:
        env = build_codex_env("C:/repo", permission_mode="workspace", token="x" * 32)
        assert "CODEXPRO_WINDOWS_BRIDGE_TOKEN" not in env

    def test_windows_token_included_when_passed(self) -> None:
        env = build_codex_env("C:/repo", permission_mode="workspace", token="x" * 32, windows_token="y" * 32)
        assert env["CODEXPRO_WINDOWS_BRIDGE_TOKEN"] == "y" * 32

    def test_windows_profile_maps_permission_modes(self) -> None:
        assert build_codex_env("C:/r", permission_mode="read_only", token="x" * 32)["CODEXPRO_WINDOWS_PROFILE"] == "desktop_ui"
        assert build_codex_env("C:/r", permission_mode="workspace", token="x" * 32)["CODEXPRO_WINDOWS_PROFILE"] == "desktop_ui"
        assert build_codex_env("C:/r", permission_mode="system", token="x" * 32)["CODEXPRO_WINDOWS_PROFILE"] == "system_full"

    def test_token_cap_length_can_be_shorter_than_secret_minimum(self) -> None:
        # this test documents that dummy tokens work for compile-only checks
        env = build_codex_env("C:/repo", permission_mode="workspace", token="short")
        assert env["CODEXPRO_HTTP_TOKEN"] == "short"

    def test_cmd_shape(self) -> None:
        cmd = build_codex_cmd(r"C:\node\node.exe", Path("dist/http.js"), "D:/proj", 8787)
        # Path str() may normalize separators; flag order is what matters
        assert cmd[0].replace("\\", "/") == "C:/node/node.exe"
        assert cmd[1].replace("\\", "/") == "dist/http.js"
        assert cmd[2:] == ["--root", "D:/proj", "--port", "8787"]

    def test_fixed_ports(self) -> None:
        assert CODEXPRO_LOCAL_PORT == 8787
        assert WINDOWS_BRIDGE_PORT == 28731


class TestProcessLog:
    def test_ring_buffer_keeps_recent_lines(self) -> None:
        log = ProcessLog()
        for i in range(50):
            log.append(f"line-{i}")
        log.append("line-50")
        tail = log.tail(3)
        assert "line-48" in tail and "line-50" in tail and "line-0" not in tail

    def test_tail_default_is_200(self) -> None:
        log = ProcessLog()
        for i in range(400):
            log.append(f"{i}\n")
        lines = log.tail().strip().splitlines()
        assert len(lines) == 200
        assert lines[0] == "200" and lines[-1] == "399"

    def test_clear(self) -> None:
        log = ProcessLog()
        log.append("hello")
        log.clear()
        assert log.tail() == ""


class TestBinaryDiscovery:
    def test_find_node_available(self) -> None:
        # Node is present in the dev environment; guard against missing PATH
        node = find_node()
        assert isinstance(node, str)

    def test_find_uvx_may_be_absent(self) -> None:
        assert isinstance(find_uvx(), str)


class TestReadiness:
    def test_port_listening_ports(self) -> None:
        # A random ephemeral port that is not ours should report False
        assert port_listening(43999) is False

    def test_port_listening_true_when_open(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert port_listening(port) is True
        listener.close()


class TestManagerErrors:
    def test_codex_dist_missing_raises(self, tmp_path: Path) -> None:
        manager = CodexProManager(node_exe="node", dist_dir=tmp_path / "nope")
        with pytest.raises(SpawnError):
            manager.start("C:/repo", "t" * 32)

    def test_codex_token_length_checked(self, tmp_path: Path) -> None:
        (tmp_path / "http.js").write_text("// fake", encoding="utf-8")
        manager = CodexProManager(node_exe="node", dist_dir=tmp_path)
        with pytest.raises(SpawnError):
            manager.start("C:/repo", "short")

    def test_windows_token_length_checked_before_spawn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = WindowsBridgeManager(uvx_exe="uvx")
        spawned: list[list[str]] = []
        monkeypatch.setattr(manager, "_spawn", lambda cmd, env, secrets, log_file: spawned.append(cmd))
        with pytest.raises(SpawnError):
            manager.start("short")
        assert spawned == []

    def test_windows_bridge_cmd_pins_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        manager = WindowsBridgeManager(uvx_exe="uvx")
        spawned: list[list[str]] = []
        monkeypatch.setattr(manager, "_spawn", lambda cmd, env, secrets, log_file: spawned.append(cmd))
        manager.start("t" * 32)
        assert spawned
        cmd = spawned[0]
        assert cmd[0] == "uvx"
        assert cmd[1] == "--from"
        assert cmd[2] == f"windows-mcp=={WINDOWS_MCP_PINNED_VERSION}"
        assert cmd[3] == "windows-mcp"
        assert "serve" in cmd

    def test_manager_state_initialized_idle(self) -> None:
        manager = WindowsBridgeManager(uvx_exe="uvx")
        assert manager.state == EngineState.IDLE
        assert manager.error is None

    def test_stop_when_never_started_is_idempotent(self) -> None:
        manager = CodexProManager(node_exe="node")
        manager.stop()
        assert manager.state == EngineState.IDLE