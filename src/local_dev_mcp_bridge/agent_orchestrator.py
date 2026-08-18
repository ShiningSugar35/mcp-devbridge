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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from . import constants
from .agent_pool import AgentPool
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
    task_ids: list[str] = field(default_factory=list)
    team_id: str = ""
    pending_messages: list[str] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    committed_task_ids: list[str] = field(default_factory=list)
    worktree: str = ""
    branch: str = ""
    repo_root: str = ""
    base_sha: str = ""
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
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _persist_agent(self, agent: AgentRecord) -> None:
        self._atomic_json(self._agent_file(agent.id), asdict(agent))

    def _persist_team(self, team: TeamRecord) -> None:
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
                team.state = "interrupted"
                team.stage = "done"
                team.error = "MCP DevBridge restarted before team orchestration finished."
                team.finished_at = time.time()
                self._persist_team(team)
            teams.append(team)
        teams.sort(key=lambda item: item.created_at, reverse=True)
        self._teams = {item.id: item for item in teams[:100]}

    # -------------------------------------------------------------- git helpers
    @staticmethod
    def _repo_base(workspace: Path) -> tuple[Path, str]:
        root = _run_git(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
        if root.returncode != 0 or not root.stdout.strip():
            raise ValueError("Agent Team 的写入/合并需要 Git 仓库。")
        repo = Path(root.stdout.strip()).resolve()
        sha = _run_git(["git", "-C", str(repo), "rev-parse", "HEAD"])
        if sha.returncode != 0 or not sha.stdout.strip():
            raise RuntimeError("无法读取 Git HEAD。")
        return repo, sha.stdout.strip()

    def _commit_agent_turn(self, agent: AgentRecord, task: dict[str, Any]) -> bool:
        task_id = str(task.get("id") or "")
        if not task_id or task_id in agent.committed_task_ids or not agent.write:
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
    def _sync_agent(self, agent_id: str) -> dict[str, Any]:
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

    def _agent_snapshot(self, agent: AgentRecord) -> dict[str, Any]:
        row = asdict(agent)
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
        executor: str = "auto",
        model: str = "",
        write: bool = True,
        role: str = "worker",
        team_id: str = "",
        isolate: bool = True,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空。")
        if role not in {"worker", "reviewer", "merger"}:
            raise ValueError("role 必须是 worker / reviewer / merger。")
        normalized_role = cast(AgentRole, role)
        agent_id = uuid.uuid4().hex
        safe_title = " ".join((title.strip() or f"{role.title()} {agent_id[:8]}").split())[:120]
        task = self.pool.spawn(
            workspace=workspace,
            prompt=prompt,
            title=safe_title,
            executor=executor,
            model=model,
            write=write,
            isolate=isolate,
        )
        task_id = str(task.get("id") or "")
        record = AgentRecord(
            id=agent_id,
            title=safe_title,
            role=normalized_role,
            workspace=str(workspace.resolve()),
            executor=str(task.get("executor") or executor),
            model=model.strip(),
            write=bool(write),
            state=str(task.get("state") or "queued"),
            created_at=time.time(),
            current_task_id=task_id,
            task_ids=[task_id],
            team_id=team_id,
        )
        with self._lock:
            self._agents[record.id] = record
            self._persist_agent(record)
        return self.get_agent(record.id)

    def spawn_agent_team(
        self,
        *,
        workspace: Path,
        objective: str,
        tasks: list[dict[str, Any]],
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

        any_write = any(bool(item.get("write", True)) for item in tasks)
        repo_root = ""
        base_sha = ""
        if any_write or merger:
            repo, base_sha = self._repo_base(workspace)
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
            reviewer_enabled=bool(reviewer),
            merger_enabled=bool(merger and any_write),
            reviewer_prompt=reviewer_prompt.strip(),
            merger_prompt=merger_prompt.strip(),
            reviewer_executor=reviewer_executor or executor,
            reviewer_model=reviewer_model or model,
            merger_executor=merger_executor or executor,
            merger_model=merger_model or model,
            repo_root=repo_root,
            base_sha=base_sha,
        )
        with self._lock:
            self._teams[team_id] = record
            self._persist_team(record)

        created: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(tasks, start=1):
                worker = self.spawn_agent(
                    workspace=workspace,
                    prompt=str(item.get("prompt") or ""),
                    title=str(item.get("title") or f"Worker {index}"),
                    executor=str(item.get("executor") or executor),
                    model=str(item.get("model") or model),
                    write=bool(item.get("write", True)),
                    role="worker",
                    team_id=team_id,
                    isolate=bool(item.get("write", True)),
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
        try:
            applied = self.pool.append_prompt(task_id, message)
        except ValueError:
            applied = False
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
                f"error={agent.error or '(none)'}"
            )
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
        path = (self.integration_dir / team.id).resolve()
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
            if not workers or any(not bool(item.get("terminal")) for item in workers):
                return
            completed = [item for item in workers if item.get("state") == "completed"]
            if not completed:
                with self._lock:
                    team.state = "failed"
                    team.stage = "done"
                    team.error = "所有 worker 均未成功完成。"
                    team.finished_at = time.time()
                    self._persist_team(team)
                return
            with self._lock:
                team.stage = "reviewer_spawning" if team.reviewer_enabled else "merge_preparing"
                self._persist_team(team)
            if team.reviewer_enabled:
                try:
                    reviewer = self.spawn_agent(
                        workspace=Path(team.workspace),
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
            if not bool(reviewer.get("terminal")):
                return
            with self._lock:
                if reviewer.get("state") != "completed":
                    team.error = (
                        (team.error + " | ") if team.error else ""
                    ) + f"Reviewer 未成功完成：{reviewer.get('error') or reviewer.get('state')}"
                team.stage = "merge_preparing"
                self._persist_team(team)
            return

        if stage == "merge_preparing":
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
            if not bool(merger.get("terminal")):
                return
            with self._lock:
                if merger.get("state") == "completed":
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


__all__ = ["AgentOrchestrator", "AgentRecord", "TeamRecord", "AgentRole"]
