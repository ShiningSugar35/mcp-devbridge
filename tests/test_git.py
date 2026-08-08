"""Git tool tests against a temporary throwaway repository."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_mcp_bridge.tools import LocalDevTools


def _init_repo(tmp_path: Path) -> tuple[Path, LocalDevTools]:
    repo = tmp_path / "repo"
    repo.mkdir()
    sub = repo / "sub"
    sub.mkdir()
    tools = LocalDevTools(repo, "workspace")
    tools.run_program("git", ["init", "-b", "main"], cwd=str(repo), timeout_seconds=60)
    tools.run_program(
        "git", ["config", "user.email", "test@example.com"], cwd=str(repo), timeout_seconds=30
    )
    tools.run_program("git", ["config", "user.name", "Tester"], cwd=str(repo), timeout_seconds=30)
    return repo, tools


class TestGit:
    def test_status_commit_branch_flow(self, tmp_path: Path) -> None:
        repo, tools = _init_repo(tmp_path)
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")

        out = tools.git_status()
        assert "?? a.txt" in out

        tools.git_add(["a.txt"])
        out = tools.git_status()
        assert "A  a.txt" in out

        out = tools.git_commit("initial commit")
        assert "退出码: 0" in out

        tools.git_branch("create", "feature/中文")
        out = tools.git_branch("list")
        assert "feature/中文" in out

        out = tools.git_checkout("feature/中文")
        assert "退出码: 0" in out
        out = tools.git_status()
        assert "feature/中文" in out

        out = tools.git_log()
        assert "initial commit" in out

    def test_diff_and_restore(self, tmp_path: Path) -> None:
        repo, tools = _init_repo(tmp_path)
        (repo / "b.txt").write_text("one\n", encoding="utf-8")
        tools.git_add(["b.txt"])
        tools.git_commit("add b")
        (repo / "b.txt").write_text("one\nchanged\n", encoding="utf-8")

        diff = tools.git_diff("b.txt")
        assert "changed" in diff

        out = tools.git_restore(["b.txt"])
        assert "退出码: 0" in out
        assert (repo / "b.txt").read_text(encoding="utf-8") == "one\n"

    def test_git_commit_requires_message(self, tmp_path: Path) -> None:
        repo, tools = _init_repo(tmp_path)
        with pytest.raises(ValueError):
            tools.git_commit("  ")
