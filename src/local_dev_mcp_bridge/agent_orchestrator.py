"""High-level multi-agent orchestrator built on top of :mod:`agent_pool`.

The AgentPool is intentionally a low-level bounded process queue.  This module
adds the layer expected from a full coding-agent system:

* logical agents that may receive follow-up messages across multiple executor
  turns while staying on the same isolated Git branch/worktree;
* teams of parallel workers;
* an automatic read-only Reviewer turn after workers finish;
* an isolated integration branch/worktree and a Merger agent that combines the
  worker branches, resolves conflicts and runs validation;
* persistent agent/team metadata, cancellation and short polling waits.

OpenCode/Claude CLI invocations are one-shot processes, not interactive sockets.
``message_agent`` therefore appends to a task that is still queued, or queues a
continuation turn on the same worktree once the running turn exits.  The API does
not pretend that a running one-shot CLI supports live stdin when it does not.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from . import constants
from .agent_pool import AgentPool
from .agent_runtime import AgentRuntimeLoop, TaskState
from .platform_support import run_platform_kwargs

AgentRole = Literal["worker", "reviewer", "merger"]
_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_MAX_AGENTS = 256
_MAX_TEAM_WORKERS = 64
_MAX_MESSAGE_CHARS = 20_000
_MAX_REVIEW_CONTEXT_CHARS = 100_000


@dataclass
class AgentRecord:
    id: str
    title: str
    role: AgentRole
    workspace: str
    executor: str
    model: str
    write: bool
    state: str
    created_at: float
    current_task_id: str
    objective: str = ""
    route_root: str = ""
    route_workspace_id: str = ""
    isolation_mode: str = "auto"
    completion_verified: bool = False
    completion_receipt: dict[str, Any] = field(default_factory=dict)
    task_ids: list[str] = field(default_factory=list)
    team_id: str = ""
    pending_messages: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    committed_task_ids: list[str] = field(default_factory=list)
    worktree: str = ""
    branch: str = ""
    repo_root: str = ""
    base_sha: str = ""
    isolate: bool | None = None
    output_tail: str = ""
    error: str = ""
    finished_at: float = 0.0


@dataclass
class TeamRecord:
    id: str
    title: str
    objective: str
    workspace: str
    state: str
    stage: str
    created_at: float
    route_root: str = ""
    route_workspace_id: str = ""
    worker_ids: list[str] = field(default_factory=list)
    reviewer_id: str = ""
    merger_id: str = ""
    reviewer_enabled: bool = True
    merger_enabled: bool = True
    reviewer_prompt: str = ""
    merger_prompt: str = ""
    reviewer_executor: str = "auto"
    reviewer_model: str = ""
    merger_executor: str = "auto"
    merger_model: str = ""
    repo_root: str = ""
    base_sha: str = ""
    integration_worktree: str = ""
    integration_branch: str = ""
    success_policy: str = "all_required"
    worker_failures: list[str] = field(default_factory=list)
    warning: str = ""
    error: str = ""
    finished_at: float = 0.0


def _run_git(argv: list[str], *, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **run_platform_kwargs(),
    )


class AgentOrchestrator:
    """Persistent logical-agent and team controller backed by one :class:`AgentPool`."""

    def __init__(
        self,
        pool: AgentPool,
        *,
        root_dir: Path | None = None,
        monitor: bool = True,
        tick_seconds: float = 0.35,
    ) -> None:
        self.pool = pool
        self.root_dir = (root_dir or (constants.config_dir() / "agent-orchestrator")).resolve()
        self.agent_dir = self.root_dir / "agents"
        self.team_dir = self.root_dir / "teams"
        self.integration_dir = self.root_dir / "integrations"
        for directory in (self.agent_dir, self.team_dir, self.integration_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._agents: dict[str, AgentRecord] = {}
        self._teams: dict[str, TeamRecord] = {}
        self._tick_seconds = max(0.1, min(float(tick_seconds), 2.0))
        self._stop = threading.Event()
        self._load_history()
        self.runtime = AgentRuntimeLoop(
            self.root_dir / "runtime",
            turn_spawner=self._spawn_runtime_turn,
            turn_getter=self._get_runtime_turn,
            turn_canceller=getattr(self.pool, "cancel", None),
        )
        self._migrate_runtime_tasks()
        self.runtime.resume_incomplete()
        self._monitor_thread: threading.Thread | None = None
        if monitor:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="mcp-agent-orchestrator",
                daemon=True,
            )
            self._monitor_thread.start()

    # --------------------------------------------------------------- persistence
    def _agent_file(self, agent_id: str) -> Path:
        return self.agent_dir / f"{agent_id}.json"

    def _team_file(self, team_id: str) -> Path:
        return self.team_dir / f"{team_id}.json"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _persist_agent(self, agent: AgentRecord) -> None:
        with self._lock:
            self._atomic_json(self._agent_file(agent.id), asdict(agent))

    def _persist_team(self, team: TeamRecord) -> None:
        with self._lock:
            self._atomic_json(self._team_file(team.id), asdict(team))

    def _load_history(self) -> None:
        agents: list[AgentRecord] = []
        for path in self.agent_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                agents.append(AgentRecord(**raw))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
        agents.sort(key=lambda item: item.created_at, reverse=True)
        self._agents = {item.id: item for item in agents[:_MAX_AGENTS]}

        teams: list[TeamRecord] = []
        for path in self.team_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                team = TeamRecord(**raw)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if team.state not in _TERMINAL:
                team.state = "running"
                if team.stage == "reviewer_spawning":
                    team.stage = "workers"
                elif team.stage == "merger_spawning":
                    team.stage = "merge_preparing"
                team.error = ""
                team.finished_at = 0.0
                self._persist_team(team)
            teams.append(team)
        teams.sort(key=lambda item: item.created_at, reverse=True)
        self._teams = {item.id: item for item in teams[:100]}

    def _migrate_runtime_tasks(self) -> None:
        """Attach pre-runtime nonterminal Agent metadata to the durable loop."""
        with self._lock:
            records = [item for item in self._agents.values() if item.state not in _TERMINAL]
        for agent in records:
            if self.runtime.has_task(agent.id):
                continue
            self.runtime.create_task(
                task_id=agent.id,
                objective=agent.objective or agent.title,
                workspace=Path(agent.workspace),
                title=agent.title,
                role=agent.role,
                route_root=Path(agent.route_root or agent.workspace),
                route_workspace_id=agent.route_workspace_id,
                executor=agent.executor,
                model=agent.model,
                write=agent.write,
                isolation_mode=agent.isolation_mode,
                initial_isolate=agent.isolate,
                current_turn_id=agent.current_task_id,
                turn_ids=agent.task_ids,
                worktree=agent.worktree,
                branch=agent.branch,
                repo_root=agent.repo_root,
                base_sha=agent.base_sha,
                start=False,
            )

    def _spawn_runtime_turn(self, state: TaskState, prompt: str) -> dict[str, Any]:
        with self._lock:
            agent = self._agents.get(state.task_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{state.task_id}")
            continuation = bool(agent.task_ids)
            workspace = Path(agent.worktree or state.worktree or agent.workspace)
        task = self.pool.spawn(
            workspace=workspace,
            route_root=Path(agent.route_root or agent.workspace),
            route_workspace_id=agent.route_workspace_id,
            prompt=prompt,
            title=f"{agent.title} · continuation" if continuation else agent.title,
            executor=agent.executor,
            model=agent.model,
            write=agent.write,
            isolate=(
                False
                if continuation and (agent.worktree or state.worktree)
                else state.initial_isolate
            ),
            isolation_mode=(
                agent.isolation_mode
                if agent.isolation_mode in {"auto", "git_worktree", "direct"}
                else "auto"
            ),
        )
        task_id = str(task.get("id") or "")
        with self._lock:
            agent.current_task_id = task_id
            if task_id and task_id not in agent.task_ids:
                agent.task_ids.append(task_id)
            agent.executor = str(task.get("executor") or agent.executor)
            agent.model = str(task.get("model") or agent.model)
            agent.state = str(task.get("state") or "queued")
            self._persist_agent(agent)
        return cast(dict[str, Any], task)

    def _get_runtime_turn(self, turn_id: str) -> dict[str, Any]:
        task = cast(dict[str, Any], self.pool.get(turn_id))
        if str(task.get("state") or "") != "completed":
            return task
        with self._lock:
            agent = next((item for item in self._agents.values() if item.current_task_id == turn_id), None)
        if agent is not None and not self._commit_agent_turn(agent, task):
            task = dict(task)
            task["state"] = "failed"
            task["error"] = agent.error or "Agent turn could not be committed."
        return task

    # -------------------------------------------------------------- git helpers
    @staticmethod
    def _repo_base(workspace: Path) -> tuple[Path, str]:
        root = _run_git(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
        if root.returncode != 0 or not root.stdout.strip():
            raise ValueError("目标目录不属于 Git 仓库。")
        repo = Path(root.stdout.strip()).resolve()
        sha = _run_git(["git", "-C", str(repo), "rev-parse", "HEAD"])
        if sha.returncode != 0 or not sha.stdout.strip():
            raise RuntimeError("无法读取 Git HEAD。")
        return repo, sha.stdout.strip()

    @classmethod
    def _try_repo_base(cls, workspace: Path) -> tuple[Path, str] | None:
        try:
            return cls._repo_base(workspace)
        except (ValueError, RuntimeError):
            return None

    def _commit_agent_turn(self, agent: AgentRecord, task: dict[str, Any]) -> bool:
        task_id = str(task.get("id") or "")
        if not task_id or task_id in agent.committed_task_ids or not agent.write:
            return True
        if str(task.get("isolation_mode") or agent.isolation_mode) != "git_worktree":
            agent.committed_task_ids.append(task_id)
            self._persist_agent(agent)
            return True
        worktree_raw = str(task.get("worktree") or agent.worktree or "")
        if not worktree_raw:
            return True
        worktree = Path(worktree_raw)
        if not worktree.is_dir():
            return False
        status = _run_git(["git", "-C", str(worktree), "status", "--porcelain"], timeout=20)
        if status.returncode != 0:
            return False
        unmerged = _run_git(
            ["git", "-C", str(worktree), "diff", "--name-only", "--diff-filter=U"],
            timeout=20,
        )
        if unmerged.stdout.strip():
            agent.error = "Agent 退出时仍存在未解决的 Git 冲突：" + unmerged.stdout.strip()[:800]
            self._persist_agent(agent)
            return False
        if status.stdout.strip():
            staged = _run_git(["git", "-C", str(worktree), "add", "-A"], timeout=30)
            if staged.returncode != 0:
                agent.error = f"自动暂存 Agent 修改失败：{(staged.stderr or staged.stdout).strip()[:800]}"
                self._persist_agent(agent)
                return False
            message = f"agent({agent.id[:8]}): {agent.title}"[:180]
            committed = _run_git(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "-c",
                    "user.name=MCP DevBridge Agent",
                    "-c",
                    "user.email=mcp-devbridge@local",
                    "commit",
                    "-m",
                    message,
                ],
                timeout=60,
            )
            if committed.returncode != 0:
                agent.error = f"自动提交 Agent 修改失败：{(committed.stderr or committed.stdout).strip()[:800]}"
                self._persist_agent(agent)
                return False
        agent.committed_task_ids.append(task_id)
        self._persist_agent(agent)
        return True

    # -------------------------------------------------------------- agent sync
    def _sync_runtime_agent(self, agent_id: str) -> dict[str, Any]:
        runtime = self.runtime.advance(agent_id)
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{agent_id}")
            agent.current_task_id = str(runtime.get("current_turn_id") or agent.current_task_id)
            agent.task_ids = [str(item) for item in runtime.get("turn_ids", agent.task_ids)]
            agent.state = str(runtime.get("status") or agent.state)
            agent.worktree = str(runtime.get("worktree") or agent.worktree)
            agent.branch = str(runtime.get("branch") or agent.branch)
            agent.repo_root = str(runtime.get("repo_root") or agent.repo_root)
            agent.base_sha = str(runtime.get("base_sha") or agent.base_sha)
            agent.isolation_mode = str(runtime.get("isolation_mode") or agent.isolation_mode)
            agent.completion_verified = bool(runtime.get("completion_verified"))
            receipt = runtime.get("completion_receipt")
            agent.completion_receipt = dict(receipt) if isinstance(receipt, dict) else {}
            agent.output_tail = str(runtime.get("previous_output") or agent.output_tail)[-24_000:]
            agent.error = str(runtime.get("error") or runtime.get("waiting_reason") or "")
            if bool(runtime.get("terminal")):
                agent.finished_at = float(runtime.get("updated_at") or time.time())
            else:
                agent.finished_at = 0.0
            self._persist_agent(agent)
            return self._agent_snapshot(agent, runtime=runtime)

    def _sync_agent(self, agent_id: str) -> dict[str, Any]:
        if hasattr(self, "runtime") and self.runtime.has_task(agent_id):
            return self._sync_runtime_agent(agent_id)
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{agent_id}")
            task_id = agent.current_task_id
        try:
            task = self.pool.get(task_id)
        except ValueError as exc:
            with self._lock:
                agent.state = "interrupted"
                agent.error = str(exc)
                agent.finished_at = time.time()
                self._persist_agent(agent)
                return self._agent_snapshot(agent)

        with self._lock:
            agent.worktree = str(task.get("worktree") or agent.worktree or "")
            agent.branch = str(task.get("branch") or agent.branch or "")
            agent.repo_root = str(task.get("repo_root") or agent.repo_root or "")
            agent.base_sha = agent.base_sha or str(task.get("base_sha") or "")
            agent.isolation_mode = str(task.get("isolation_mode") or agent.isolation_mode or "auto")
            agent.completion_verified = bool(task.get("completion_verified", False))
            receipt = task.get("completion_receipt")
            agent.completion_receipt = dict(receipt) if isinstance(receipt, dict) else {}
            agent.output_tail = str(task.get("output_tail") or "")[-24_000:]
            low_state = str(task.get("state") or "")
            if low_state in {"queued", "running"}:
                agent.state = low_state
                self._persist_agent(agent)
                return self._agent_snapshot(agent)

            if low_state == "completed" and not self._commit_agent_turn(agent, task):
                agent.state = "failed"
                agent.finished_at = time.time()
                self._persist_agent(agent)
                return self._agent_snapshot(agent)

            if agent.pending_messages and low_state != "cancelled":
                pending = agent.pending_messages[:]
                agent.pending_messages.clear()
                followup = "\n\n".join(pending)
                prior_tail = agent.output_tail[-5000:]
                continuation_prompt = (
                    "Continue the same logical Agent on the existing branch/worktree. "
                    "Review the previous turn and implement the new instruction.\n\n"
                    f"PREVIOUS TURN OUTPUT TAIL:\n{prior_tail}\n\n"
                    f"NEW MESSAGE:\n{followup}"
                )
                workspace = Path(agent.worktree or agent.workspace)
                isolate = not bool(agent.worktree and agent.write)
                try:
                    next_task = self.pool.spawn(
                        workspace=workspace,
                        route_root=Path(agent.route_root or agent.workspace),
                        route_workspace_id=agent.route_workspace_id,
                        prompt=continuation_prompt,
                        title=f"{agent.title} · continuation",
                        executor=agent.executor,
                        model=agent.model,
                        write=agent.write,
                        isolate=isolate,
                    )
                except Exception as exc:  # noqa: BLE001 - persisted Agent failure
                    agent.state = "failed"
                    agent.error = f"无法启动 Agent continuation：{exc}"
                    agent.finished_at = time.time()
                    self._persist_agent(agent)
                    return self._agent_snapshot(agent)
                next_id = str(next_task.get("id") or "")
                agent.current_task_id = next_id
                agent.task_ids.append(next_id)
                agent.state = str(next_task.get("state") or "queued")
                agent.completion_verified = False
                agent.completion_receipt = {}
                agent.finished_at = 0.0
                agent.error = ""
                self._persist_agent(agent)
                return self._agent_snapshot(agent)

            agent.state = low_state if low_state in _TERMINAL else "failed"
            agent.error = str(task.get("error") or agent.error or "")
            raw_finished = task.get("finished_at")
            agent.finished_at = (
                float(raw_finished) if isinstance(raw_finished, (int, float)) and raw_finished else time.time()
            )
            self._persist_agent(agent)
            return self._agent_snapshot(agent)

    def _agent_snapshot(
        self,
        agent: AgentRecord,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = asdict(agent)
        if runtime is not None:
            row.update(
                {
                    key: runtime[key]
                    for key in (
                        "objective",
                        "checklist",
                        "completed_items",
                        "current_stage",
                        "iteration",
                        "retry_count",
                        "last_checkpoint",
                        "status",
                        "validation",
                        "waiting_reason",
                        "next_plan",
                        "modified_files",
                        "recent_events",
                    )
                    if key in runtime
                }
            )
            row["state"] = str(runtime.get("status") or agent.state)
            row["terminal"] = bool(runtime.get("terminal"))
            row["objective_complete"] = bool(runtime.get("objective_complete"))
            row["turn_count"] = int(runtime.get("turn_count") or len(agent.task_ids))
        else:
            row["terminal"] = agent.state in _TERMINAL
            row["turn_count"] = len(agent.task_ids)
        return row

    def _team_snapshot(self, team: TeamRecord) -> dict[str, Any]:
        row = asdict(team)
        row["terminal"] = team.state in _TERMINAL
        return row

    # --------------------------------------------------------------- public API
    def spawn_agent(
        self,
        *,
        workspace: Path,
        prompt: str,
        title: str = "",
        route_root: Path | None = None,
        route_workspace_id: str = "",
        executor: str = "auto",
        model: str = "",
        write: bool = True,
        role: str = "worker",
        team_id: str = "",
        isolate: bool | None = None,
        isolation_mode: str = "auto",
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空。")
        if role not in {"worker", "reviewer", "merger"}:
            raise ValueError("role 必须是 worker / reviewer / merger。")
        normalized_role = cast(AgentRole, role)
        agent_id = uuid.uuid4().hex
        safe_title = " ".join((title.strip() or f"{role.title()} {agent_id[:8]}").split())[:120]
        record = AgentRecord(
            id=agent_id,
            title=safe_title,
            role=normalized_role,
            workspace=str(workspace.resolve()),
            executor=executor,
            model=model.strip(),
            write=bool(write),
            state="queued",
            created_at=time.time(),
            current_task_id="",
            objective=prompt,
            route_root=str((route_root or workspace).resolve()),
            route_workspace_id=route_workspace_id.strip(),
            isolation_mode=isolation_mode or "auto",
            task_ids=[],
            team_id=team_id,
            isolate=isolate,
        )
        with self._lock:
            self._agents[record.id] = record
            self._persist_agent(record)
        try:
            self.runtime.create_task(
                task_id=record.id,
                objective=prompt,
                workspace=workspace,
                title=safe_title,
                role=role,
                route_root=route_root,
                route_workspace_id=route_workspace_id,
                executor=executor,
                model=model,
                write=write,
                isolation_mode=isolation_mode,
                initial_isolate=isolate,
            )
        except Exception:
            with self._lock:
                self._agents.pop(record.id, None)
                with suppress(OSError):
                    self._agent_file(record.id).unlink(missing_ok=True)
            raise
        return self.get_agent(record.id)

    def spawn_agent_team(
        self,
        *,
        workspace: Path,
        objective: str,
        tasks: list[dict[str, Any]],
        route_root: Path | None = None,
        route_workspace_id: str = "",
        title: str = "",
        executor: str = "auto",
        model: str = "",
        reviewer: bool = True,
        merger: bool = True,
        reviewer_prompt: str = "",
        merger_prompt: str = "",
        reviewer_executor: str = "auto",
        reviewer_model: str = "",
        merger_executor: str = "auto",
        merger_model: str = "",
        success_policy: str = "all_required",
        isolation_mode: str = "auto",
    ) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ValueError("objective 不能为空。")
        if not tasks:
            raise ValueError("tasks 不能为空。")
        if len(tasks) > _MAX_TEAM_WORKERS:
            raise ValueError(f"单个 Team 最多 {_MAX_TEAM_WORKERS} 个 worker。")
        if any(not isinstance(item, dict) or not str(item.get("prompt") or "").strip() for item in tasks):
            raise ValueError("tasks 中每项都必须包含非空 prompt。")
        normalized_policy = success_policy.strip().lower() or "all_required"
        if normalized_policy not in {"all_required", "allow_partial"}:
            raise ValueError("success_policy 必须是 all_required / allow_partial。")

        any_write = any(bool(item.get("write", True)) for item in tasks)
        repo_root = ""
        base_sha = ""
        repo_info = self._try_repo_base(workspace) if any_write or merger else None
        if repo_info is not None:
            repo, base_sha = repo_info
            repo_root = str(repo)

        team_id = uuid.uuid4().hex
        record = TeamRecord(
            id=team_id,
            title=" ".join((title.strip() or f"Agent Team {team_id[:8]}").split())[:120],
            objective=objective,
            workspace=str(workspace.resolve()),
            state="running",
            stage="workers",
            created_at=time.time(),
            route_root=str((route_root or workspace).resolve()),
            route_workspace_id=route_workspace_id.strip(),
            reviewer_enabled=bool(reviewer),
            merger_enabled=bool(merger and any_write and repo_root),
            reviewer_prompt=reviewer_prompt.strip(),
            merger_prompt=merger_prompt.strip(),
            reviewer_executor=reviewer_executor or executor,
            reviewer_model=reviewer_model or model,
            merger_executor=merger_executor or executor,
            merger_model=merger_model or model,
            repo_root=repo_root,
            base_sha=base_sha,
            success_policy=normalized_policy,
            warning=(
                "目标目录不是 Git 仓库：写型 worker 将使用 direct 模式；自动 Merger 已关闭。"
                if any_write and not repo_root
                else ""
            ),
        )
        with self._lock:
            self._teams[team_id] = record
            self._persist_team(record)

        created: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(tasks, start=1):
                worker = self.spawn_agent(
                    workspace=workspace,
                    route_root=Path(record.route_root or record.workspace),
                    route_workspace_id=record.route_workspace_id,
                    prompt=str(item.get("prompt") or ""),
                    title=str(item.get("title") or f"Worker {index}"),
                    executor=str(item.get("executor") or executor),
                    model=str(item.get("model") or model),
                    write=bool(item.get("write", True)),
                    role="worker",
                    team_id=team_id,
                    isolate=None,
                    isolation_mode=str(item.get("isolation_mode") or isolation_mode or "auto"),
                )
                created.append(worker)
                with self._lock:
                    record.worker_ids.append(str(worker["id"]))
                    self._persist_team(record)
        except Exception as exc:
            with self._lock:
                record.state = "failed"
                record.stage = "done"
                record.error = f"Team worker spawn failed: {exc}"
                record.finished_at = time.time()
                self._persist_team(record)
            raise

        return {"team": self._team_snapshot(record), "workers": created}

    def list_agents(self, *, team_id: str = "") -> dict[str, Any]:
        with self._lock:
            agent_ids = [
                item.id
                for item in sorted(self._agents.values(), key=lambda row: row.created_at, reverse=True)
                if not team_id or item.team_id == team_id
            ][:200]
            teams = [
                self._team_snapshot(item)
                for item in sorted(self._teams.values(), key=lambda row: row.created_at, reverse=True)
                if not team_id or item.id == team_id
            ][:100]
        agents = [self._sync_agent(agent_id) for agent_id in agent_ids]
        return {
            "agents": agents,
            "teams": teams,
            "running": sum(item["state"] == "running" for item in agents),
            "queued": sum(item["state"] == "queued" for item in agents),
            "max_parallel": self.pool.max_parallel,
            "persistent_runtime": self.runtime_capabilities(),
        }

    def runtime_capabilities(self) -> dict[str, Any]:
        return {
            "automatic_continuation": True,
            "durable_checkpoints": True,
            "restart_resume": True,
            "completion_validator": True,
            "waiting_human_resume_same_task": True,
            "trace_directory": str(self.runtime.log_dir),
        }

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._sync_agent(agent_id)

    def get_team(self, team_id: str) -> dict[str, Any]:
        self._advance_team(team_id)
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                raise ValueError(f"找不到 Agent Team：{team_id}")
            snapshot = self._team_snapshot(team)
            agent_ids = [*team.worker_ids]
            if team.reviewer_id:
                agent_ids.append(team.reviewer_id)
            if team.merger_id:
                agent_ids.append(team.merger_id)
        snapshot["agents"] = [self._sync_agent(item) for item in agent_ids]
        return snapshot

    def message_agent(self, agent_id: str, message: str) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("message 不能为空。")
        if len(message) > _MAX_MESSAGE_CHARS:
            raise ValueError(f"message 最多 {_MAX_MESSAGE_CHARS} 字符。")
        self._sync_agent(agent_id)
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{agent_id}")
            if agent.state == "cancelled":
                raise ValueError("Agent 已取消，不能继续发送消息。")
            message_record = {"at": time.time(), "message": message}
            agent.messages.append(message_record)
            task_id = agent.current_task_id

        applied = False
        if task_id:
            try:
                applied = self.pool.append_prompt(task_id, message)
            except ValueError:
                applied = False
        if self.runtime.has_task(agent_id):
            self.runtime.add_instruction(
                agent_id,
                message,
                applied_to_current_turn=applied,
            )
            with self._lock:
                self._persist_agent(agent)
            result = self.get_agent(agent_id)
            result["message_delivery"] = (
                "appended_to_queued_turn"
                if applied
                else "queued_as_continuation_on_same_worktree"
            )
            return result
        with self._lock:
            if not applied:
                agent.pending_messages.append(message)
            self._persist_agent(agent)
        result = self.get_agent(agent_id)
        result["message_delivery"] = (
            "appended_to_queued_turn"
            if applied
            else "queued_as_continuation_on_same_worktree"
        )
        return result

    def cancel_agent(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{agent_id}")
            task_id = agent.current_task_id
            agent.pending_messages.clear()
        if self.runtime.has_task(agent_id):
            self.runtime.cancel(agent_id)
            return self._sync_agent(agent_id)
        try:
            self.pool.cancel(task_id)
        finally:
            with self._lock:
                agent.state = "cancelled"
                agent.finished_at = time.time()
                self._persist_agent(agent)
        return self._agent_snapshot(agent)

    def wait_agents(
        self,
        *,
        agent_ids: list[str] | None = None,
        team_id: str = "",
        wait_seconds: int = 15,
    ) -> dict[str, Any]:
        wait_seconds = max(1, min(int(wait_seconds), 30))
        if team_id:
            with self._lock:
                team = self._teams.get(team_id)
                if team is None:
                    raise ValueError(f"找不到 Agent Team：{team_id}")
                ids = list(team.worker_ids)
                if team.reviewer_id:
                    ids.append(team.reviewer_id)
                if team.merger_id:
                    ids.append(team.merger_id)
        else:
            ids = [str(item).strip() for item in (agent_ids or []) if str(item).strip()]
            if not ids:
                raise ValueError("请提供 agent_ids 或 team_id。")

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if team_id:
                self._advance_team(team_id)
                with self._lock:
                    team_state = self._teams[team_id].state
                if team_state in _TERMINAL:
                    break
            snapshots = [self._sync_agent(item) for item in ids]
            if snapshots and all(bool(item.get("terminal")) for item in snapshots) and not team_id:
                break
            time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))

        if team_id:
            return {"team": self.get_team(team_id)}
        snapshots = [self._sync_agent(item) for item in ids]
        return {
            "agents": snapshots,
            "all_terminal": bool(snapshots) and all(bool(item.get("terminal")) for item in snapshots),
        }

    def cleanup_agent(self, agent_id: str, *, remove_branch: bool = True) -> dict[str, Any]:
        """Delete terminal logical-Agent metadata and any isolated executor worktrees."""
        snapshot = self._sync_agent(agent_id)
        if not bool(snapshot.get("terminal")):
            raise ValueError("Agent 仍在运行，不能清理。请先等待或取消。")
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise ValueError(f"找不到 Agent：{agent_id}")
            task_ids = list(agent.task_ids)
        cleaned_tasks: list[str] = []
        for task_id in task_ids:
            try:
                task = self.pool.get(task_id)
                if not bool(task.get("cleaned")):
                    self.pool.cleanup(task_id, remove_branch=remove_branch)
                cleaned_tasks.append(task_id)
            except ValueError:
                continue
        with self._lock:
            self._agents.pop(agent_id, None)
            with suppress(OSError):
                self._agent_file(agent_id).unlink(missing_ok=True)
        if self.runtime.has_task(agent_id):
            self.runtime.delete(agent_id)
        return {"agent_id": agent_id, "cleaned": True, "task_ids": cleaned_tasks}

    def cleanup_team(self, team_id: str, *, remove_branches: bool = True) -> dict[str, Any]:
        """Delete a terminal team, its logical agents, and its integration worktree/branch."""
        team_snapshot = self.get_team(team_id)
        if not bool(team_snapshot.get("terminal")):
            raise ValueError("Agent Team 仍在运行，不能清理。请先等待或取消相关 Agent。")
        with self._lock:
            team = self._teams.get(team_id)
            if team is None:
                raise ValueError(f"找不到 Agent Team：{team_id}")
            agent_ids = [*team.worker_ids]
            if team.reviewer_id:
                agent_ids.append(team.reviewer_id)
            if team.merger_id:
                agent_ids.append(team.merger_id)
            integration_worktree = team.integration_worktree
            integration_branch = team.integration_branch
            repo_root = team.repo_root

        cleaned_agents: list[str] = []
        for agent_id in agent_ids:
            if agent_id not in self._agents:
                continue
            self.cleanup_agent(agent_id, remove_branch=remove_branches)
            cleaned_agents.append(agent_id)

        if integration_worktree and repo_root:
            worktree = Path(integration_worktree)
            repo = Path(repo_root)
            if worktree.exists():
                removed = _run_git(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                    timeout=60,
                )
                if removed.returncode != 0:
                    raise RuntimeError(
                        f"清理 Team integration worktree 失败：{(removed.stderr or removed.stdout).strip()[:800]}"
                    )
            if remove_branches and integration_branch:
                deleted = _run_git(
                    ["git", "-C", str(repo), "branch", "-D", integration_branch], timeout=30
                )
                if deleted.returncode != 0 and "not found" not in (deleted.stderr or "").lower():
                    raise RuntimeError(
                        f"删除 Team integration 分支失败：{(deleted.stderr or deleted.stdout).strip()[:800]}"
                    )

        with self._lock:
            self._teams.pop(team_id, None)
            with suppress(OSError):
                self._team_file(team_id).unlink(missing_ok=True)
        return {"team_id": team_id, "cleaned": True, "agent_ids": cleaned_agents}

    # -------------------------------------------------------------- team stages
    def _review_context(self, team: TeamRecord) -> str:
        lines = [
            f"TEAM OBJECTIVE:\n{team.objective}",
            f"BASE SHA: {team.base_sha}",
            "WORKER BRANCHES AND RESULTS:",
        ]
        remaining = _MAX_REVIEW_CONTEXT_CHARS
        for agent_id in team.worker_ids:
            agent = self._agents[agent_id]
            snapshot = self._sync_agent(agent_id)
            lines.append(
                f"\n- {agent.title} [{snapshot['state']}] branch={agent.branch or '(none)'} "
                f"verified={agent.completion_verified} error={agent.error or '(none)'}"
            )
            if agent.output_tail and remaining > 0:
                chunk = ("WORKER OUTPUT TAIL:\n" + agent.output_tail[-6000:])[:remaining]
                lines.append(chunk)
                remaining -= len(chunk)
            if agent.branch and team.repo_root:
                diff = _run_git(
                    ["git", "-C", team.repo_root, "diff", f"{team.base_sha}..{agent.branch}", "--"],
                    timeout=60,
                )
                text = diff.stdout
                if text and remaining > 0:
                    chunk = text[:remaining]
                    lines.append(chunk)
                    remaining -= len(chunk)
        custom = team.reviewer_prompt or (
            "Review the worker branches adversarially. Identify correctness issues, conflicts, missing tests, "
            "security/regression risks and concrete merge guidance. Do not modify files."
        )
        lines.append(f"\nREVIEW ASSIGNMENT:\n{custom}")
        return "\n".join(lines)

    def _create_integration_worktree(self, team: TeamRecord) -> Path:
        if not team.repo_root or not team.base_sha:
            raise RuntimeError("Team 缺少 Git repo/base，无法创建 Merger worktree。")
        integration_base = self.integration_dir
        if team.merger_executor == "chatgpt" and team.route_root:
            integration_base = Path(team.route_root).resolve() / ".mcp-devbridge-team-worktrees"
        path = (integration_base / team.id).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"mcp-team/{team.id[:12]}"
        if path.exists():
            raise RuntimeError(f"Team integration worktree 已存在：{path}")
        result = _run_git(
            ["git", "-C", team.repo_root, "worktree", "add", "-b", branch, str(path), team.base_sha],
            timeout=90,
        )
        if result.returncode != 0:
            raise RuntimeError(f"创建 Team integration worktree 失败：{(result.stderr or result.stdout).strip()[:1200]}")
        team.integration_worktree = str(path)
        team.integration_branch = branch
        self._persist_team(team)
        return path

    def _merger_assignment(self, team: TeamRecord) -> str:
        branches = [
            self._agents[item].branch
            for item in team.worker_ids
            if self._agents[item].state == "completed" and self._agents[item].branch
        ]
        reviewer = self._agents.get(team.reviewer_id) if team.reviewer_id else None
        review_tail = reviewer.output_tail[-12_000:] if reviewer else "(reviewer disabled)"
        custom = team.merger_prompt or (
            "Merge every successful worker branch into the current integration branch. Resolve conflicts by "
            "understanding intent rather than dropping either side, run relevant tests/lint/typecheck, and leave "
            "the integration worktree in a coherent reviewable state. Do not push."
        )
        branch_lines = "\n".join(f"- {item}" for item in branches) or "- (no changed worker branches)"
        return (
            f"TEAM OBJECTIVE:\n{team.objective}\n\n"
            f"CURRENT INTEGRATION BRANCH: {team.integration_branch}\n"
            f"WORKER BRANCHES TO INTEGRATE:\n{branch_lines}\n\n"
            f"REVIEWER OUTPUT TAIL:\n{review_tail}\n\n"
            f"MERGER ASSIGNMENT:\n{custom}\n\n"
            "You are already inside the dedicated Team integration worktree. Use git merge/cherry-pick/diff as "
            "needed. If a branch has no commits beyond base, skip it. Resolve all conflict markers before finishing."
        )

    def _advance_team(self, team_id: str) -> None:
        with self._lock:
            team = self._teams.get(team_id)
            if team is None or team.state in _TERMINAL:
                return
            stage = team.stage
            worker_ids = list(team.worker_ids)

        if stage == "workers":
            workers = [self._sync_agent(item) for item in worker_ids]
            waiting = [item for item in workers if item.get("state") == "waiting_human"]
            if waiting:
                with self._lock:
                    team.state = "waiting_human"
                    team.worker_failures = [
                        f"{item.get('title')}: {item.get('waiting_reason') or item.get('error')}"
                        for item in waiting
                    ]
                    team.error = "一个或多个 worker 连续失败，正在等待人工处理。"
                    self._persist_team(team)
                return
            if team.state == "waiting_human":
                with self._lock:
                    team.state = "running"
                    team.error = ""
                    self._persist_team(team)
            if not workers or any(not bool(item.get("terminal")) for item in workers):
                return
            completed = [
                item
                for item in workers
                if item.get("state") == "completed" and bool(item.get("completion_verified"))
            ]
            failures = [
                f"{item.get('title')}: {item.get('error') or item.get('state')}"
                for item in workers
                if item not in completed
            ]
            if not completed:
                with self._lock:
                    team.state = "failed"
                    team.stage = "done"
                    team.worker_failures = failures
                    team.error = "所有 worker 均未成功完成或缺少可验证的成功回执。"
                    team.finished_at = time.time()
                    self._persist_team(team)
                return
            with self._lock:
                team.worker_failures = failures
                team.stage = "reviewer_spawning" if team.reviewer_enabled else "merge_preparing"
                self._persist_team(team)
            if team.reviewer_enabled:
                try:
                    reviewer = self.spawn_agent(
                        workspace=Path(team.workspace),
                        route_root=Path(team.route_root or team.workspace),
                        route_workspace_id=team.route_workspace_id,
                        prompt=self._review_context(team),
                        title=f"{team.title} · Reviewer",
                        executor=team.reviewer_executor,
                        model=team.reviewer_model,
                        write=False,
                        role="reviewer",
                        team_id=team.id,
                        isolate=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        team.error = f"Reviewer spawn failed: {exc}"
                        team.stage = "merge_preparing"
                        self._persist_team(team)
                    return
                with self._lock:
                    team.reviewer_id = str(reviewer["id"])
                    team.stage = "reviewer"
                    self._persist_team(team)
            return

        if stage == "reviewer":
            reviewer = self._sync_agent(team.reviewer_id)
            if reviewer.get("state") == "waiting_human":
                with self._lock:
                    team.state = "waiting_human"
                    team.error = "Reviewer 连续失败，正在等待人工处理。"
                    self._persist_team(team)
                return
            if not bool(reviewer.get("terminal")):
                return
            with self._lock:
                team.state = "running"
                if reviewer.get("state") != "completed" or not bool(reviewer.get("completion_verified")):
                    team.state = "failed"
                    team.stage = "done"
                    team.error = (
                        (team.error + " | ") if team.error else ""
                    ) + f"Reviewer 未成功完成或缺少成功回执：{reviewer.get('error') or reviewer.get('state')}"
                    team.finished_at = time.time()
                    self._persist_team(team)
                    return
                if team.success_policy == "all_required" and team.worker_failures:
                    team.state = "failed"
                    team.stage = "done"
                    team.error = "存在失败 worker；all_required 策略禁止把部分成功误判为团队完成。"
                    team.finished_at = time.time()
                    self._persist_team(team)
                    return
                team.stage = "merge_preparing"
                self._persist_team(team)
            return

        if stage == "merge_preparing":
            if team.success_policy == "all_required" and team.worker_failures:
                with self._lock:
                    team.state = "failed"
                    team.stage = "done"
                    team.error = "存在失败 worker；all_required 策略禁止把部分成功误判为团队完成。"
                    team.finished_at = time.time()
                    self._persist_team(team)
                return
            if not team.merger_enabled:
                with self._lock:
                    team.state = "completed"
                    team.stage = "done"
                    team.finished_at = time.time()
                    self._persist_team(team)
                return
            with self._lock:
                team.stage = "merger_spawning"
                self._persist_team(team)
            try:
                integration = self._create_integration_worktree(team)
                merger = self.spawn_agent(
                    workspace=integration,
                    route_root=Path(team.route_root or team.workspace),
                    route_workspace_id=team.route_workspace_id,
                    prompt=self._merger_assignment(team),
                    title=f"{team.title} · Merger",
                    executor=team.merger_executor,
                    model=team.merger_model,
                    write=True,
                    role="merger",
                    team_id=team.id,
                    isolate=False,
                )
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    team.state = "failed"
                    team.stage = "done"
                    team.error = ((team.error + " | ") if team.error else "") + f"Merger setup failed: {exc}"
                    team.finished_at = time.time()
                    self._persist_team(team)
                return
            with self._lock:
                team.merger_id = str(merger["id"])
                team.stage = "merger"
                self._persist_team(team)
            return

        if stage == "merger":
            merger = self._sync_agent(team.merger_id)
            if merger.get("state") == "waiting_human":
                with self._lock:
                    team.state = "waiting_human"
                    team.error = "Merger 连续失败，正在等待人工处理。"
                    self._persist_team(team)
                return
            if not bool(merger.get("terminal")):
                return
            with self._lock:
                team.state = "running"
                if merger.get("state") == "completed" and bool(merger.get("completion_verified")):
                    team.state = "completed"
                else:
                    team.state = "failed"
                    team.error = ((team.error + " | ") if team.error else "") + (
                        f"Merger 未成功完成：{merger.get('error') or merger.get('state')}"
                    )
                team.stage = "done"
                team.finished_at = time.time()
                self._persist_team(team)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            self.runtime.tick_all()
            with self._lock:
                agent_ids = list(self._agents)
                team_ids = list(self._teams)
            for agent_id in agent_ids:
                try:
                    self._sync_agent(agent_id)
                except Exception:
                    continue
            for team_id in team_ids:
                try:
                    self._advance_team(team_id)
                except Exception:
                    continue

    def shutdown(self) -> None:
        self._stop.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)


__all__ = ["AgentOrchestrator", "AgentRecord", "TeamRecord", "AgentRole"]
