from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

from local_dev_mcp_bridge.agent_pool import AgentPool, AgentTask


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Agent Pool Test")
    _git(repo, "config", "user.email", "agent-pool@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _wait_terminal(pool: AgentPool, task_id: str, timeout: float = 10.0) -> dict[str, object]:
    deadline = time.time() + timeout
    last: dict[str, object] = {}
    while time.time() < deadline:
        last = pool.wait(task_id, 1)
        if last["state"] not in {"queued", "running"}:
            return last
    raise AssertionError(f"task did not finish: {last}")


def test_agent_pool_capabilities_and_bounded_parallel_queue(tmp_path: Path) -> None:
    script = (
        "import sys,time; time.sleep(0.35); "
        "print('worker:'+sys.argv[1], flush=True)"
    )

    def builder(_task: AgentTask, prompt: str, _workdir: Path) -> list[str]:
        return [sys.executable, "-c", script, prompt[-20:]]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=2, command_builder=builder)
    caps = pool.capabilities()
    assert caps["available"] is True
    assert caps["max_parallel"] == 2
    first = pool.spawn(workspace=tmp_path, prompt="first", write=False)
    second = pool.spawn(workspace=tmp_path, prompt="second", write=False)
    a = _wait_terminal(pool, str(first["id"]))
    b = _wait_terminal(pool, str(second["id"]))
    assert a["state"] == "completed"
    assert b["state"] == "completed"
    assert abs(cast(float, a["started_at"]) - cast(float, b["started_at"])) < 0.30
    assert "worker:" in str(a["output_tail"])
    listed = pool.list()
    assert listed["max_parallel"] == 2
    assert listed["running"] == 0


def test_write_agent_uses_isolated_worktree_and_collects_diff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def builder(_task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        code = "from pathlib import Path; Path('README.md').write_text('agent change\\n', encoding='utf-8'); Path('NEW.md').write_text('new file\\n', encoding='utf-8')"
        return [sys.executable, "-c", code]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    created = pool.spawn(workspace=repo, prompt="change readme", title="writer", write=True)
    result = _wait_terminal(pool, str(created["id"]))
    assert result["state"] == "completed"
    assert result["worktree"]
    assert cast(str, result["branch"]).startswith("mcp-agent/")
    assert (repo / "README.md").read_text(encoding="utf-8") == "base\n"
    worktree = Path(str(result["worktree"]))
    assert (worktree / "README.md").read_text(encoding="utf-8") == "agent change\n"
    collected = pool.collect(str(created["id"]))
    assert "README.md" in str(collected["git_status"])
    assert "agent change" in str(collected["diff"])
    assert "NEW.md" in str(collected["diff"])
    assert "new file" in str(collected["diff"])
    cleaned = pool.cleanup(str(created["id"]), remove_branch=True)
    assert cleaned["cleaned"] is True
    assert not worktree.exists()


def test_write_agent_rejects_non_git_workspace(tmp_path: Path) -> None:
    pool = AgentPool(
        root_dir=tmp_path / "pool",
        max_parallel=1,
        command_builder=lambda _task, _prompt, _workdir: [sys.executable, "-c", "print('x')"],
    )
    created = pool.spawn(workspace=tmp_path, prompt="write", write=True)
    result = _wait_terminal(pool, str(created["id"]))
    assert result["state"] == "failed"
    assert "Git" in str(result["error"])


def test_agent_pool_cancel_running_process(tmp_path: Path) -> None:
    def builder(_task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        return [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(30)"]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    created = pool.spawn(workspace=tmp_path, prompt="sleep", write=False)
    task_id = str(created["id"])
    deadline = time.time() + 5
    while time.time() < deadline:
        current = pool.get(task_id)
        if current["state"] == "running" and "started" in str(current["output_tail"]):
            break
        time.sleep(0.05)
    cancelled = pool.cancel(task_id)
    assert cancelled["state"] == "cancelled"
    final = pool.wait(task_id, 2)
    assert final["state"] == "cancelled"


def test_agent_pool_restart_marks_running_metadata_interrupted(tmp_path: Path) -> None:
    root = tmp_path / "pool"
    task_dir = root / "tasks"
    task_dir.mkdir(parents=True)
    payload = {
        "id": "old-task",
        "title": "old",
        "executor": "opencode",
        "model": "",
        "workspace": str(tmp_path),
        "write": False,
        "state": "running",
        "created_at": time.time() - 20,
        "prompt_sha256": "0" * 64,
        "started_at": time.time() - 19,
        "finished_at": 0.0,
        "exit_code": None,
        "worktree": "",
        "branch": "",
        "repo_root": "",
        "base_sha": "",
        "log_path": str(root / "logs" / "old-task.log"),
        "error": "",
        "cleaned": False,
    }
    (task_dir / "old-task.json").write_text(json.dumps(payload), encoding="utf-8")
    pool = AgentPool(root_dir=root, max_parallel=1, command_builder=lambda *_args: [])
    restored = pool.get("old-task")
    assert restored["state"] == "interrupted"
    assert "restarted" in str(restored["error"])


def test_agent_pool_batch_limit(tmp_path: Path) -> None:
    pool = AgentPool(
        root_dir=tmp_path / "pool",
        max_parallel=1,
        command_builder=lambda _task, _prompt, _workdir: [sys.executable, "-c", "print('ok')"],
    )
    with pytest.raises(ValueError, match="64"):
        pool.spawn_batch(workspace=tmp_path, tasks=[{"prompt": "x", "write": False}] * 65)



def test_claude_executor_command_is_noninteractive(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(AgentPool, "_discover_opencode_path", classmethod(lambda cls: ""))
    monkeypatch.setattr(
        AgentPool,
        "_discover_claude_path",
        classmethod(lambda cls: str(tmp_path / "claude.exe")),
    )
    monkeypatch.setenv("MCP_DEVBRIDGE_AGENT_EXECUTOR", "claude")
    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1)
    caps = pool.capabilities()
    assert caps["executors"] == ["claude"]
    assert caps["preferred_executor"] == "claude"
    assert caps["provider_runtime_verified"] is False
    task = AgentTask(
        id="claude-task",
        title="claude",
        executor="claude",
        model="",
        workspace=str(tmp_path),
        write=False,
        state="queued",
        created_at=time.time(),
        prompt_sha256="0" * 64,
    )
    command = pool._default_command(task, "hello", tmp_path)
    assert command[0].endswith("claude.exe")
    assert "-p" in command
    assert "stream-json" in command
    assert "plan" in command
    assert command[-1] == "hello"


def test_agent_executor_failure_surfaces_provider_output(tmp_path: Path) -> None:
    def builder(_task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        code = "import sys; print('provider quota exhausted', flush=True); raise SystemExit(7)"
        return [sys.executable, "-c", code]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    created = pool.spawn(workspace=tmp_path, prompt="fail clearly", write=False)
    result = _wait_terminal(pool, str(created["id"]))
    assert result["state"] == "failed"
    assert result["exit_code"] == 7
    assert "provider quota exhausted" in str(result["error"])
