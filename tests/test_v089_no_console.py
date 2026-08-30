from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _compact(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def _assert_hidden_after(source: str, marker: str, *, window: int = 450) -> None:
    start = source.find(marker)
    assert start >= 0, marker
    assert "windowsHide: true" in source[start : start + window], marker


def test_windows_backend_child_processes_are_hidden() -> None:
    search_ops = _compact("third_party/codexpro/src/searchOps.ts")
    git_ops = _compact("third_party/codexpro/src/gitOps.ts")
    server = _compact("third_party/codexpro/src/server.ts")

    _assert_hidden_after(search_ops, 'spawn("where", [command]')
    _assert_hidden_after(search_ops, 'spawn("rg", args')
    _assert_hidden_after(git_ops, 'spawnSync("git", args')
    _assert_hidden_after(git_ops, 'spawnSync("git", ["-C", probe, "rev-parse", "--show-toplevel"]')
    _assert_hidden_after(server, 'spawnSync("git", ["apply", "--check", "--whitespace=nowarn"]')
    _assert_hidden_after(server, 'spawnSync("git", ["apply", "--whitespace=nowarn"]')


def test_python_process_helpers_request_create_no_window() -> None:
    platform_support = (ROOT / "src/local_dev_mcp_bridge/platform_support.py").read_text(encoding="utf-8")
    assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in platform_support
    assert 'return {"creationflags": int(getattr(subprocess, "CREATE_NO_WINDOW", 0))}' in platform_support
