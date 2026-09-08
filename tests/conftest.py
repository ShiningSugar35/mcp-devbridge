"""Shared fixtures for the test suite."""

from __future__ import annotations

import atexit
import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TEST_STATE_PARENT = _ROOT / ".ai-bridge"
_TEST_STATE_PARENT.mkdir(exist_ok=True)
_TEST_STATE = tempfile.TemporaryDirectory(
    prefix="pytest-session-", dir=_TEST_STATE_PARENT, ignore_cleanup_errors=True
)
_PREVIOUS_CONFIG = os.environ.get("LOCALDEV_MCP_CONFIG_DIR")
# Set before product imports: static aliases and dynamic paths must share a sandbox.
# Every pytest process gets its own directory, including concurrent/xdist workers.
os.environ["LOCALDEV_MCP_CONFIG_DIR"] = _TEST_STATE.name


def _cleanup_test_state() -> None:
    _TEST_STATE.cleanup()
    if _PREVIOUS_CONFIG is None:
        os.environ.pop("LOCALDEV_MCP_CONFIG_DIR", None)
    else:
        os.environ["LOCALDEV_MCP_CONFIG_DIR"] = _PREVIOUS_CONFIG


atexit.register(_cleanup_test_state)
sys.path.insert(0, str(_ROOT / "src"))

from local_dev_mcp_bridge.tools import LocalDevTools  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "工作区 空间"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture()
def tools(workspace: Path) -> LocalDevTools:
    return LocalDevTools(workspace, "workspace")


@pytest.fixture()
def read_only_tools(workspace: Path) -> LocalDevTools:
    return LocalDevTools(workspace, "read_only")


@pytest.fixture()
def system_tools(workspace: Path) -> LocalDevTools:
    return LocalDevTools(workspace, "system")
