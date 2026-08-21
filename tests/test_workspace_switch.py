"""Multi-session workspace switching on the tools layer.

Two sessions (``mcp-session-id`` headers) operate on two different projects in
the same backend process; sessions never leak into each other, and requests
without a session id keep the engine-local default root.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from local_dev_mcp_bridge.models import ProjectConfig
from local_dev_mcp_bridge.tools import LocalDevTools, WorkspaceCatalog

PROJ_A_ID = "aaaa1111"
PROJ_B_ID = "bbbb2222"


def _ctx(session_id: str | None) -> Any:
    headers: dict[str, str] = {}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return SimpleNamespace(headers=headers)


@pytest.fixture()
def projects(tmp_path: Path) -> tuple[Path, Path, list[ProjectConfig]]:
    dir_a = tmp_path / "projA"
    dir_b = tmp_path / "projB"
    dir_a.mkdir()
    dir_b.mkdir()
    (dir_a / "marker_A.txt").write_text("AAA", encoding="utf-8")
    (dir_b / "marker_B.txt").write_text("BBB", encoding="utf-8")
    catalog = [
        ProjectConfig(id=PROJ_A_ID, display_name="项目A", root_path=str(dir_a), permission_mode="workspace"),
        ProjectConfig(id=PROJ_B_ID, display_name="项目B", root_path=str(dir_b), permission_mode="workspace"),
    ]
    return dir_a, dir_b, catalog


@pytest.fixture()
def tools(projects: tuple[Path, Path, list[ProjectConfig]]) -> tuple[LocalDevTools, Path, Path]:
    dir_a, dir_b, catalog = projects
    return LocalDevTools(dir_a, "workspace", projects=catalog, default_project_id=PROJ_A_ID), dir_a, dir_b


def test_session_key_parsing() -> None:
    assert WorkspaceCatalog.session_key(None) == WorkspaceCatalog.DEFAULT_BUCKET
    assert WorkspaceCatalog.session_key(_ctx("s1")) == "s1"
    assert WorkspaceCatalog.session_key(SimpleNamespace(headers={"MCP-Session-Id": "s2"})) == "s2"
    assert WorkspaceCatalog.session_key(SimpleNamespace(headers={})) == WorkspaceCatalog.DEFAULT_BUCKET
    assert WorkspaceCatalog.session_key(
        SimpleNamespace(headers={"mcp-session-id": "s3", "authorization": "Bearer x"})
    ) == "s3"


def test_default_session_uses_engine_default_root(tools: tuple[LocalDevTools, Path, Path]) -> None:
    dev, dir_a, _dir_b = tools
    listing = dev.list_directory("")

    assert "marker_A.txt" in listing
    assert "marker_B.txt" not in listing


def test_switch_changes_only_calling_session(tools: tuple[LocalDevTools, Path, Path]) -> None:
    dev, dir_a, dir_b = tools
    ctx_a = _ctx("sess-A")
    ctx_b = _ctx("sess-B")
    result = dev.switch_workspace(PROJ_B_ID, ctx=ctx_a)
    assert "项目B" in result
    # session A 现在看项目 B
    assert "marker_B.txt" in dev.list_directory("", ctx=ctx_a)
    assert "marker_A.txt" not in dev.list_directory("", ctx=ctx_a)
    # session B 仍是默认项目 A
    assert "marker_A.txt" in dev.list_directory("", ctx=ctx_b)
    assert "marker_B.txt" not in dev.list_directory("", ctx=ctx_b)
    # 无上下文的调用也保持不变（默认项目 A）
    assert "marker_A.txt" in dev.list_directory("")
    # 项目工作区根必须随 session 切换（相对路径解析到新根）
    assert "marker_B.txt" in dev.read_file("marker_B.txt", ctx=ctx_a)
    with pytest.raises(ValueError, match="文件不存在"):
        dev.read_file("marker_A.txt", ctx=ctx_a)


def test_switch_unknown_project_keeps_workspace(tools: tuple[LocalDevTools, Path, Path]) -> None:
    dev, _dir_a, _dir_b = tools
    ctx = _ctx("sess-X")
    with pytest.raises(ValueError, match="未找到项目"):
        dev.switch_workspace("no-such-id", ctx=ctx)
    with pytest.raises(ValueError, match="未找到项目"):
        dev.switch_workspace("no-such-id", ctx=ctx)
    assert "marker_A.txt" in dev.list_directory("", ctx=ctx)


def test_list_projects_mentions_all_and_current(tools: tuple[LocalDevTools, Path, Path]) -> None:
    dev, _a, _b = tools
    text = dev.list_projects()
    assert "项目A" in text
    assert "项目B" in text
    dev.switch_workspace(PROJ_B_ID, ctx=_ctx("sess-1"))
    text_b = dev.list_projects(ctx=_ctx("sess-1"))
    assert "项目B" in text_b
    text_default = dev.list_projects()
    assert "项目A" in text_default


def test_switch_unknown_id_keeps_binding(tools: tuple[LocalDevTools, Path, Path]) -> None:
    dev, _a, _b = tools
    ctx = _ctx("sess-Y")
    dev.switch_workspace(PROJ_B_ID, ctx=ctx)
    with pytest.raises(ValueError, match="未找到项目"):
        dev.switch_workspace("no-such", ctx=ctx)
    assert "marker_B.txt" in dev.list_directory("", ctx=ctx)


def test_shell_info_shape() -> None:
    tools_shell = LocalDevTools(Path(".").resolve())
    info = tools_shell.shell_info()
    assert isinstance(info, dict)
    assert "default" in info
    assert isinstance(info["default"], dict)
    assert "name" in info["default"] and "path" in info["default"]
    detected = [str(d.get("type", "")) for d in info.get("detected", [])]
    assert detected, "至少应检测到一种 shell"
    # WSL 可以检测但绝不能被自动选为默认
    if "wsl" in detected:
        assert info["default"].get("type") != "wsl"