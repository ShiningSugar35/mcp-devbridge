"""Shared fixtures for the test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
