from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from local_dev_mcp_bridge.agent_gateway import execute_agent_tool
from local_dev_mcp_bridge.agent_orchestrator import AgentOrchestrator
from local_dev_mcp_bridge.agent_pool import AgentPool, AgentTask
from local_dev_mcp_bridge.gateway import _PYTHON_TOOL_DEFS


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Orchestrator Test")
    _git(repo, "config", "user.email", "orchestrator@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _wait_agent(orchestrator: AgentOrchestrator, agent_id: str, timeout: float = 15) -> dict[str, object]:
    deadline = time.time() + timeout
    last: dict[str, object] = {}
    while time.time() < deadline:
        last = orchestrator.get_agent(agent_id)
        if bool(last.get("terminal")):
            return last
        time.sleep(0.05)
    raise AssertionError(f"agent did not finish: {last}")


def test_message_agent_runs_continuation_on_same_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def builder(task: AgentTask, prompt: str, _workdir: Path) -> list[str]:
        phase = "followup" if "NEW MESSAGE:" in prompt else "initial"
        code = (
            "from pathlib import Path; import sys,time; "
            "p=Path('history.txt'); old=p.read_text(encoding='utf-8') if p.exists() else ''; "
            "p.write_text(old+sys.argv[1]+'\\n',encoding='utf-8'); "
            "print(sys.argv[1],flush=True); time.sleep(0.25)"
        )
        return [sys.executable, "-c", code, phase]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent(
            workspace=repo,
            prompt="make initial change",
            title="Persistent Worker",
            write=True,
        )
        agent_id = str(created["id"])
        deadline = time.time() + 5
        while time.time() < deadline:
            current = orchestrator.get_agent(agent_id)
            if current["state"] == "running":
                break
            time.sleep(0.02)
        messaged = orchestrator.message_agent(agent_id, "add the second phase")
        assert messaged["message_delivery"] == "queued_as_continuation_on_same_worktree"

        final = _wait_agent(orchestrator, agent_id)
        assert final["state"] == "completed"
        assert final["turn_count"] == 2
        worktree = Path(str(final["worktree"]))
        assert worktree.is_dir()
        assert str(final["branch"]).startswith("mcp-agent/")
        assert (worktree / "history.txt").read_text(encoding="utf-8") == "initial\nfollowup\n"
        assert not (repo / "history.txt").exists()
        assert len(cast(list[str], final["committed_task_ids"])) == 2
    finally:
        orchestrator.shutdown()


def test_logical_agent_automatically_continues_without_message_agent(tmp_path: Path) -> None:
    prompts: list[str] = []

    def builder(_task: AgentTask, prompt: str, _workdir: Path) -> list[str]:
        prompts.append(prompt)
        full_line = next(line for line in prompt.splitlines() if line.startswith("FULL CHECKLIST: "))
        checklist = json.loads(full_line.removeprefix("FULL CHECKLIST: "))
        receipt = (
            {
                "status": "success",
                "summary": "analysis checkpoint",
                "completed_items": ["analyze_objective"],
                "objective_complete": False,
                "evidence": [],
            }
            if len(prompts) == 1
            else {
                "status": "success",
                "summary": "objective verified",
                "completed_items": checklist,
                "objective_complete": True,
                "evidence": [],
            }
        )
        code = "import sys; print('MCP_AGENT_RESULT: ' + sys.argv[1], flush=True)"
        return [sys.executable, "-c", code, json.dumps(receipt)]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent(
            workspace=tmp_path,
            route_root=tmp_path,
            route_workspace_id="workspace-D",
            prompt="Analyze a long objective and verify the final result",
            write=False,
        )
        final = _wait_agent(orchestrator, str(created["id"]))

        assert final["state"] == "completed"
        assert final["turn_count"] == 2
        assert final["iteration"] == 2
        assert len(prompts) == 2
        assert "PREVIOUS TURN OUTPUT TAIL" in prompts[1]
        for task_id in cast(list[str], final["task_ids"]):
            assert pool.get(task_id)["route_workspace_id"] == "workspace-D"
    finally:
        orchestrator.shutdown()


def test_gateway_capabilities_report_logical_restart_resume(tmp_path: Path) -> None:
    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        result = execute_agent_tool(
            name="agent_pool_capabilities",
            arguments={},
            workspace=tmp_path,
            workspace_id="workspace-D",
            pool=pool,
            orchestrator=orchestrator,
        )

        assert result["running_tasks_survive_restart"] is True
        assert result["execution_processes_survive_restart"] is False
        assert cast(dict[str, object], result["persistent_runtime"])["restart_resume"] is True
    finally:
        orchestrator.shutdown()


def test_spawn_agent_team_runs_workers_reviewer_and_merger(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def builder(task: AgentTask, prompt: str, _workdir: Path) -> list[str]:
        if "Reviewer" in task.title:
            return [sys.executable, "-c", "print('REVIEW_OK: branches inspected', flush=True)"]
        if "Merger" in task.title:
            code = r'''
import pathlib, subprocess, sys
prompt = sys.argv[1]
branches = []
inside = False
for line in prompt.splitlines():
    if line.startswith("WORKER BRANCHES TO INTEGRATE:"):
        inside = True
        continue
    if inside and line.startswith("REVIEWER OUTPUT TAIL:"):
        break
    if inside and line.startswith("- mcp-agent/"):
        branches.append(line[2:].strip())
for branch in branches:
    result = subprocess.run(["git", "merge", "--no-ff", "--no-edit", branch], text=True, capture_output=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
pathlib.Path("MERGER.txt").write_text("merged:" + ",".join(branches) + "\n", encoding="utf-8")
print("MERGE_OK", flush=True)
'''
            return [sys.executable, "-c", code, prompt]
        filename = f"worker-{task.id[:8]}.txt"
        code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2]+'\\n',encoding='utf-8'); print(sys.argv[1],flush=True)"
        return [sys.executable, "-c", code, filename, task.title]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=2, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent_team(
            workspace=repo,
            objective="Implement two independent files and integrate them safely.",
            title="Integration Team",
            tasks=[
                {"title": "Worker Alpha", "prompt": "create alpha", "write": True},
                {"title": "Worker Beta", "prompt": "create beta", "write": True},
            ],
            reviewer=True,
            merger=True,
        )
        team_id = str(cast(dict[str, object], created["team"])["id"])
        deadline = time.time() + 25
        team: dict[str, object] = {}
        while time.time() < deadline:
            team = orchestrator.get_team(team_id)
            if bool(team.get("terminal")):
                break
            time.sleep(0.05)
        assert team["state"] == "completed", team
        assert team["reviewer_id"]
        assert team["merger_id"]
        integration = Path(str(team["integration_worktree"]))
        assert integration.is_dir()
        assert str(team["integration_branch"]).startswith("mcp-team/")
        assert len(list(integration.glob("worker-*.txt"))) == 2
        assert (integration / "MERGER.txt").is_file()
        assert not list(repo.glob("worker-*.txt"))
        assert not (repo / "MERGER.txt").exists()

        agents = cast(list[dict[str, object]], team["agents"])
        roles = {str(item["role"]) for item in agents}
        assert roles == {"worker", "reviewer", "merger"}
        reviewer = next(item for item in agents if item["role"] == "reviewer")
        assert "REVIEW_OK" in str(reviewer["output_tail"])
        merger = next(item for item in agents if item["role"] == "merger")
        assert "MERGE_OK" in str(merger["output_tail"])
    finally:
        orchestrator.shutdown()



def test_non_git_team_uses_direct_write_and_disables_merger(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(AgentPool, "_try_git_repo_root", classmethod(lambda cls, workspace: None))
    monkeypatch.setattr(AgentOrchestrator, "_try_repo_base", classmethod(lambda cls, workspace: None))

    def builder(task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        if "Reviewer" in task.title:
            return [sys.executable, "-c", "print('DIRECT_REVIEW_OK', flush=True)"]
        filename = "alpha.txt" if "Alpha" in task.title else "beta.txt"
        return [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path('{filename}').write_text('ok\\n', encoding='utf-8')",
        ]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=2, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent_team(
            workspace=tmp_path,
            objective="Write two independent local files outside Git.",
            title="Direct Team",
            tasks=[
                {"title": "Alpha", "prompt": "write alpha", "write": True},
                {"title": "Beta", "prompt": "write beta", "write": True},
            ],
            reviewer=True,
            merger=True,
        )
        team_id = str(cast(dict[str, object], created["team"])["id"])
        deadline = time.time() + 10
        team: dict[str, object] = {}
        while time.time() < deadline:
            team = orchestrator.get_team(team_id)
            if bool(team.get("terminal")):
                break
            time.sleep(0.05)
        assert team["state"] == "completed", team
        assert team["merger_id"] == ""
        assert team["integration_worktree"] == ""
        assert "direct" in str(team["warning"])
        assert (tmp_path / "alpha.txt").read_text(encoding="utf-8") == "ok\n"
        assert (tmp_path / "beta.txt").read_text(encoding="utf-8") == "ok\n"
        workers = [item for item in cast(list[dict[str, object]], team["agents"]) if item["role"] == "worker"]
        assert workers and all(item["isolation_mode"] == "direct" for item in workers)
        cleaned = orchestrator.cleanup_team(team_id)
        assert cleaned["cleaned"] is True
        assert not (tmp_path / "orchestrator" / "teams" / f"{team_id}.json").exists()
    finally:
        orchestrator.shutdown()


def test_team_all_required_pauses_for_human_after_repeated_worker_failure(tmp_path: Path) -> None:
    def builder(task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        if "Reviewer" in task.title:
            return [sys.executable, "-c", "print('reviewed partial failure', flush=True)"]
        if "Fail" in task.title:
            return [sys.executable, "-c", "raise SystemExit(7)"]
        return [sys.executable, "-c", "print('worker ok', flush=True)"]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=2, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent_team(
            workspace=tmp_path,
            objective="One worker intentionally fails.",
            tasks=[
                {"title": "Pass", "prompt": "pass", "write": False},
                {"title": "Fail", "prompt": "fail", "write": False},
            ],
            reviewer=True,
            merger=False,
        )
        team_id = str(cast(dict[str, object], created["team"])["id"])
        deadline = time.time() + 10
        team: dict[str, object] = {}
        while time.time() < deadline:
            team = orchestrator.get_team(team_id)
            if bool(team.get("terminal")):
                break
            time.sleep(0.05)
        assert team["state"] == "waiting_human", team
        assert team["worker_failures"]
        assert "人工" in str(team["error"])
    finally:
        orchestrator.shutdown()

def test_wait_and_cancel_logical_agent(tmp_path: Path) -> None:
    def builder(_task: AgentTask, _prompt: str, _workdir: Path) -> list[str]:
        return [sys.executable, "-c", "import time; print('running',flush=True); time.sleep(30)"]

    pool = AgentPool(root_dir=tmp_path / "pool", max_parallel=1, command_builder=builder)
    orchestrator = AgentOrchestrator(pool, root_dir=tmp_path / "orchestrator", tick_seconds=0.05)
    try:
        created = orchestrator.spawn_agent(workspace=tmp_path, prompt="sleep", write=False)
        agent_id = str(created["id"])
        short = orchestrator.wait_agents(agent_ids=[agent_id], wait_seconds=1)
        assert short["all_terminal"] is False
        cancelled = orchestrator.cancel_agent(agent_id)
        assert cancelled["state"] == "cancelled"
        final = orchestrator.wait_agents(agent_ids=[agent_id], wait_seconds=2)
        assert final["all_terminal"] is True
    finally:
        orchestrator.shutdown()


def test_gateway_exposes_full_orchestrator_tool_surface() -> None:
    defs = {str(item["name"]): item for item in _PYTHON_TOOL_DEFS}
    expected = {
        "spawn_agent",
        "spawn_agent_team",
        "list_agents",
        "get_agent",
        "get_agent_team",
        "message_agent",
        "cancel_agent",
        "wait_agents",
        "cleanup_agent",
        "cleanup_agent_team",
    }
    assert expected.issubset(defs)
    assert "device_id" in defs["spawn_agent"]["inputSchema"]["properties"]
    assert "target_path" in defs["spawn_agent"]["inputSchema"]["properties"]
    assert "isolation_mode" in defs["spawn_agent"]["inputSchema"]["properties"]
    assert "project_id" in defs["spawn_agent_team"]["inputSchema"]["properties"]
    assert "success_policy" in defs["spawn_agent_team"]["inputSchema"]["properties"]
    assert defs["spawn_agent_team"]["inputSchema"]["properties"]["tasks"]["maxItems"] == 64
