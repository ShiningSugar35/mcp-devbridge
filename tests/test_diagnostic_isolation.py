"""Test diagnostics must not be mistaken for incidents from the running desktop."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from local_dev_mcp_bridge import audit, constants, gateway, processes

ROOT = Path(__file__).resolve().parents[1]


def assert_isolated(path: Path) -> None:
    relative = path.resolve().relative_to(ROOT / ".ai-bridge")
    assert relative.parts[0].startswith("pytest-session-"), str(relative)


@pytest.mark.parametrize("path", [
    constants.CONFIG_DIR, constants.LOG_DIR, constants.PROCESS_LOG_DIR,
    audit.LOG_DIR, gateway._LOG_DIR, processes.PROCESS_LOG_DIR,
])
def test_static_default_directories_are_session_isolated(path: Path) -> None:
    assert_isolated(path)


def test_dynamic_config_matches_static_default() -> None:
    assert_isolated(constants.log_dir())
    assert constants.log_dir() == constants.LOG_DIR
    assert Path(os.environ["LOCALDEV_MCP_CONFIG_DIR"]) == constants.CONFIG_DIR


def test_real_diagnostic_write_stays_in_test_directory() -> None:
    # Fail before any write on the old, unsafe configuration.
    assert_isolated(gateway._LOG_DIR)
    gateway._write_diag_entry(event="test_isolation_sentinel", duration_ms=0)
    files = list(gateway._LOG_DIR.glob("gateway-*.jsonl"))
    assert files and any("test_isolation_sentinel" in path.read_text(encoding="utf-8") for path in files)
    assert audit.AuditLogger().directory == constants.LOG_DIR


def test_explicit_per_test_override_remains_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert_isolated(constants.CONFIG_DIR)
    original = constants.CONFIG_DIR
    chosen = tmp_path / "own-config"
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(chosen))
    assert constants.log_dir() == chosen / "logs"
    assert original == constants.CONFIG_DIR  # Existing static aliases stay sandboxed.
