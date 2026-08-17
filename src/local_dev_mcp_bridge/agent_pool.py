"""Bounded local implementation-agent pool with optional Git worktree isolation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from . import constants
from .shell import CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, kill_process_tree

AgentState = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]

_DEFAULT_MAX_PARALLEL = 4
_HARD_MAX_PARALLEL = 16
_MAX_BATCH = 64
_MAX_HISTORY = 200
_MAX_TAIL_CHARS = 24_000
_MAX_DIFF_CHARS = 80_000


@dataclass
class AgentTask:
    id: str
    title: str
    executor: str
    model: str
    workspace: str
    write: bool
    state: AgentState
    created_at: float
    prompt_sha256: str
    started_at: float = 0.0
    finished_at: float = 0.0
    exit_code: int | None = None
    worktree: str = ""
    branch: str = ""
    repo_root: str = ""
    base_sha: str = ""
    log_path: str = ""
    error: str = ""
    cleaned: bool = False


CommandBuilder = Callable[[AgentTask, str, Path], list[str]]


def _bounded_int(value: int | str | None, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(parsed, high))


def _tail_text(path: Path, max_chars: int = _MAX_TAIL_CHARS) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if len(data) > max_chars * 4:
        data = data[-max_chars * 4 :]
    text = data.decode("utf-8", errors="replace")
    return text[-max_chars:]


def _run_capture(argv: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )


class AgentPool:
    """Process-scoped queue of local implementation-agent runs.

    The queue can hold many logical tasks, while ``max_parallel`` bounds actual
    concurrent model processes. Write-capable tasks run in a dedicated Git
    worktree/branch so concurrent agents never edit the user's primary checkout.
    """

    def __init__(
        self,
        *,
        root_dir: Path | None = None,
        max_parallel: int | None = None,
        command_builder: CommandBuilder | None = None,
    ) -> None:
        configured = max_parallel
        if configured is None:
            configured = _bounded_int(
                os.environ.get("MCP_DEVBRIDGE_AGENT_POOL_MAX"),
                _DEFAULT_MAX_PARALLEL,
                1,
                _HARD_MAX_PARALLEL,
            )
        self.max_parallel = _bounded_int(configured, _DEFAULT_MAX_PARALLEL, 1, _HARD_MAX_PARALLEL)
        self.root_dir = (root_dir or (constants.config_dir() / "agent-pool")).resolve()
        self.task_dir = self.root_dir / "tasks"
        self.log_dir = self.root_dir / "logs"
        self.worktree_dir = self.root_dir / "worktrees"
        for directory in (self.task_dir, self.log_dir, self.worktree_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._command_builder = command_builder
        self._opencode_executable = "" if command_builder is not None else self._discover_opencode_path()
        self._claude_executable = "" if command_builder is not None else self._discover_claude_path()
        preferred = os.environ.get("MCP_DEVBRIDGE_AGENT_EXECUTOR", "").strip().lower()
        self.preferred_executor = preferred if preferred in {"opencode", "claude"} else ""
        self._lock = threading.RLock()
        self._tasks: dict[str, AgentTask] = {}
        self._prompts: dict[str, str] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._futures: dict[str, Future[None]] = {}
        self._executor = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="mcp-agent")
        self._load_history()

    # --------------------------------------------------------------- discovery
    @staticmethod
    def _probe_opencode(candidate: str) -> bool:
        try:
            result = subprocess.run(
                [candidate, "run", "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        text = f"{result.stdout}\n{result.stderr}".lower()
        return result.returncode == 0 and "opencode run" in text and "message to send" in text

    @classmethod
    def _discover_opencode_path(cls) -> str:
        candidates = [
            shutil.which("opencode.cmd"),
            shutil.which("opencode"),
            shutil.which("opencode.exe"),
        ]
        seen: set[str] = set()
        for raw in candidates:
            if not raw:
                continue
            candidate = str(Path(raw).resolve())
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            if cls._probe_opencode(candidate):
                return candidate
        return ""

    @staticmethod
    def _probe_claude(candidate: str) -> bool:
        try:
            result = subprocess.run(
                [candidate, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        text = f"{result.stdout}\n{result.stderr}".lower()
        return result.returncode == 0 and "claude code" in text and "--print" in text

    @classmethod
    def _discover_claude_path(cls) -> str:
        candidates = [shutil.which("claude.exe"), shutil.which("claude")]
        seen: set[str] = set()
        for raw in candidates:
            if not raw:
                continue
            candidate = str(Path(raw).resolve())
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            if cls._probe_claude(candidate):
                return candidate
        return ""

    def capabilities(self) -> dict[str, object]:
        opencode = self._opencode_executable
        claude = self._claude_executable
        executors = [name for name, path in (("opencode", opencode), ("claude", claude)) if path]
        return {
            "available": bool(executors or self._command_builder),
            "executors": executors or (["test"] if self._command_builder else []),
            "opencode_path": opencode,
            "claude_path": claude,
            "preferred_executor": self.preferred_executor or "auto",
            "provider_runtime_verified": False,
            "provider_runtime_note": (
                "CLI discovery only verifies a non-interactive command surface. "
                "Provider authentication, quota and service health are validated when each task runs."
            ),
            "max_parallel": self.max_parallel,
            "max_batch": _MAX_BATCH,
            "worktree_isolation": True,
            "write_requires_git": True,
            "running_tasks_survive_restart": False,
        }

    # -------------------------------------------------------------- persistence
    def _task_file(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def _persist(self, task: AgentTask) -> None:
        target = self._task_file(task.id)
        tmp = target.with_suffix(".tmp")
        payload = json.dumps(asdict(task), ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)

    def _load_history(self) -> None:
        records: list[AgentTask] = []
        for path in self.task_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                task = AgentTask(**raw)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if task.state in {"queued", "running"}:
                task.state = "interrupted"
                task.finished_at = time.time()
                task.error = "MCP DevBridge restarted before the agent process finished."
                self._persist(task)
            records.append(task)
        records.sort(key=lambda item: item.created_at, reverse=True)
        self._tasks = {task.id: task for task in records[:_MAX_HISTORY]}

    # ------------------------------------------------------------------ helpers
    def _default_command(self, task: AgentTask, prompt: str, workdir: Path) -> list[str]:
        if task.executor == "claude":
            executable = self._claude_executable
            if not executable:
                raise RuntimeError("未检测到可非交互运行的 Claude Code CLI。")
            command = [
                executable,
                "-p",
                "--verbose",
                "--output-format",
                "stream-json",
                "--no-session-persistence",
            ]
            if task.model:
                command.extend(["--model", task.model])
            if task.write:
                command.extend(
                    [
                        "--permission-mode",
                        "acceptEdits",
                        "--tools",
                        "Read,Glob,Grep,Edit,Write,Bash",
                        "--allowedTools",
                        "Read,Glob,Grep,Edit,Write,Bash",
                    ]
                )
            else:
                command.extend(["--permission-mode", "plan", "--tools", "Read,Glob,Grep"])
            command.append(prompt)
            return command

        executable = self._opencode_executable
        if not executable:
            raise RuntimeError("未检测到可非交互运行的 OpenCode CLI。请安装 CLI 版并确保 `opencode run --help` 可正常退出。")
        command = [
            executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(workdir),
            "--title",
            task.title,
        ]
        if task.model:
            command.extend(["--model", task.model])
        if task.write:
            command.append("--auto")
        command.append(prompt)
        return command

    @staticmethod
    def _git_repo_root(workspace: Path) -> tuple[Path, str]:
        root = _run_capture(["git", "-C", str(workspace), "rev-parse", "--show-toplevel"])
        if root.returncode != 0 or not root.stdout.strip():
            raise RuntimeError("写入型 Agent 需要 Git 仓库，以便使用独立 worktree 隔离并发修改。")
        repo = Path(root.stdout.strip()).resolve()
        sha = _run_capture(["git", "-C", str(repo), "rev-parse", "HEAD"])
        if sha.returncode != 0 or not sha.stdout.strip():
            raise RuntimeError("无法读取 Git HEAD，不能创建 Agent worktree。")
        return repo, sha.stdout.strip()

    def _prepare_worktree(self, task: AgentTask) -> Path:
        workspace = Path(task.workspace).resolve()
        if not task.write:
            return workspace
        repo, base_sha = self._git_repo_root(workspace)
        branch = f"mcp-agent/{task.id[:12]}"
        worktree = (self.worktree_dir / task.id).resolve()
        worktree.parent.mkdir(parents=True, exist_ok=True)
        result = _run_capture(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(worktree), base_sha],
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"创建 Agent worktree 失败：{(result.stderr or result.stdout).strip()[:800]}")
        task.repo_root = str(repo)
        task.base_sha = base_sha
        task.branch = branch
        task.worktree = str(worktree)
        self._persist(task)
        return worktree

    @staticmethod
    def _agent_prompt(task: AgentTask, user_prompt: str) -> str:
        isolation = (
            "You are running inside a dedicated Git worktree. Work only inside the current worktree. "
            "Do not push, do not alter other worktrees, and do not touch the user's primary checkout. "
            if task.write
            else "This is a read-only/analysis assignment. Do not modify files. "
        )
        return (
            f"{isolation}Read and obey all applicable AGENTS.md instructions before acting. "
            "Complete only the assigned task, run relevant tests when practical, and finish with a concise result summary.\n\n"
            f"TASK:\n{user_prompt.strip()}"
        )

    # ---------------------------------------------------------------- lifecycle
    def spawn(
        self,
        *,
        workspace: Path,
        prompt: str,
        title: str = "",
        executor: str = "auto",
        model: str = "",
        write: bool = True,
    ) -> dict[str, object]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空。")
        requested_executor = executor.strip().lower()
        if requested_executor in {"", "auto"}:
            if self.preferred_executor == "claude" and self._claude_executable:
                selected_executor = "claude"
            elif self.preferred_executor == "opencode" and self._opencode_executable:
                selected_executor = "opencode"
            else:
                selected_executor = (
                    "opencode"
                    if self._opencode_executable
                    else "claude"
                    if self._claude_executable
                    else "test"
                    if self._command_builder is not None
                    else ""
                )
        else:
            selected_executor = requested_executor
        if self._command_builder is None and selected_executor not in {"opencode", "claude"}:
            raise ValueError("当前 Agent Pool 支持 opencode / claude 执行器。")
        if self._command_builder is None and selected_executor == "opencode" and not self._opencode_executable:
            raise RuntimeError("未检测到可非交互运行的 OpenCode CLI。")
        if self._command_builder is None and selected_executor == "claude" and not self._claude_executable:
            raise RuntimeError("未检测到可非交互运行的 Claude Code CLI。")
        task_id = uuid.uuid4().hex
        safe_title = " ".join((title.strip() or f"Agent {task_id[:8]}").split())[:120]
        task = AgentTask(
            id=task_id,
            title=safe_title,
            executor=selected_executor,
            model=model.strip(),
            workspace=str(workspace.resolve()),
            write=bool(write),
            state="queued",
            created_at=time.time(),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            log_path=str((self.log_dir / f"{task_id}.log").resolve()),
        )
        with self._lock:
            self._tasks[task.id] = task
            self._prompts[task.id] = prompt
            self._persist(task)
            future = self._executor.submit(self._run_task, task.id)
            self._futures[task.id] = future
        return self.get(task.id)

    def spawn_batch(
        self,
        *,
        workspace: Path,
        tasks: list[dict[str, object]],
    ) -> dict[str, object]:
        if not tasks:
            raise ValueError("tasks 不能为空。")
        if len(tasks) > _MAX_BATCH:
            raise ValueError(f"单次最多提交 {_MAX_BATCH} 个 Agent 任务。")
        created: list[dict[str, object]] = []
        for item in tasks:
            if not isinstance(item, dict):
                raise ValueError("tasks 中每一项都必须是对象。")
            created.append(
                self.spawn(
                    workspace=workspace,
                    prompt=str(item.get("prompt") or ""),
                    title=str(item.get("title") or ""),
                    executor=str(item.get("executor") or "auto"),
                    model=str(item.get("model") or ""),
                    write=bool(item.get("write", True)),
                )
            )
        return {"submitted": len(created), "max_parallel": self.max_parallel, "tasks": created}

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            prompt = self._prompts.pop(task_id, "")
            if task.state == "cancelled":
                return
            task.state = "running"
            task.started_at = time.time()
            self._persist(task)
        try:
            workdir = self._prepare_worktree(task)
            full_prompt = self._agent_prompt(task, prompt)
            builder = self._command_builder or self._default_command
            command = builder(task, full_prompt, workdir)
            log_path = Path(task.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ)
            environment.setdefault("PYTHONIOENCODING", "utf-8")
            environment.setdefault("PYTHONUTF8", "1")
            with log_path.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
                )
                with self._lock:
                    self._processes[task_id] = process
                exit_code = process.wait()
            with self._lock:
                if task.state != "cancelled":
                    task.exit_code = exit_code
                    task.state = "completed" if exit_code == 0 else "failed"
                    if exit_code != 0:
                        tail = _tail_text(log_path, 1600).strip()
                        task.error = f"Agent executor exited with code {exit_code}."
                        if tail:
                            task.error += f" Output tail: {tail}"
        except Exception as exc:
            with self._lock:
                if task.state != "cancelled":
                    task.state = "failed"
                    task.error = str(exc)[:1200]
                    task.exit_code = -1
        finally:
            with self._lock:
                task.finished_at = time.time()
                self._processes.pop(task_id, None)
                self._persist(task)

    def _snapshot(self, task: AgentTask, *, include_tail: bool = True) -> dict[str, object]:
        row = asdict(task)
        row["duration_seconds"] = round(
            max(0.0, (task.finished_at or time.time()) - (task.started_at or task.created_at)), 3
        )
        if include_tail:
            row["output_tail"] = _tail_text(Path(task.log_path))
        return row

    def get(self, task_id: str) -> dict[str, object]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"找不到 Agent 任务：{task_id}")
            return self._snapshot(task)

    def list(self) -> dict[str, object]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            running = sum(item.state == "running" for item in tasks)
            queued = sum(item.state == "queued" for item in tasks)
            return {
                "max_parallel": self.max_parallel,
                "running": running,
                "queued": queued,
                "tasks": [self._snapshot(item, include_tail=False) for item in tasks[:100]],
            }

    def wait(self, task_id: str, wait_seconds: int = 15) -> dict[str, object]:
        wait_seconds = _bounded_int(wait_seconds, 15, 1, 30)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            row = self.get(task_id)
            if row["state"] not in {"queued", "running"}:
                return row
            time.sleep(min(0.25, max(0.01, deadline - time.monotonic())))
        return self.get(task_id)

    def cancel(self, task_id: str) -> dict[str, object]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"找不到 Agent 任务：{task_id}")
            if task.state not in {"queued", "running"}:
                return self._snapshot(task)
            task.state = "cancelled"
            task.finished_at = time.time()
            future = self._futures.get(task_id)
            process = self._processes.get(task_id)
            if future is not None:
                future.cancel()
            self._persist(task)
        if process is not None and process.poll() is None:
            kill_process_tree(process.pid)
        return self.get(task_id)

    def collect(self, task_id: str, *, include_diff: bool = True) -> dict[str, object]:
        row = self.get(task_id)
        task = self._tasks[task_id]
        result: dict[str, object] = {"task": row}
        if task.worktree and task.base_sha:
            worktree = Path(task.worktree)
            if worktree.is_dir():
                status = _run_capture(["git", "-C", str(worktree), "status", "--short"], timeout=20)
                stat = _run_capture(
                    ["git", "-C", str(worktree), "diff", "--stat", task.base_sha, "--"], timeout=30
                )
                result["git_status"] = status.stdout[-12_000:]
                result["diff_stat"] = stat.stdout[-12_000:]
                if include_diff:
                    diff = _run_capture(
                        ["git", "-C", str(worktree), "diff", task.base_sha, "--"], timeout=60
                    )
                    text = diff.stdout
                    untracked = _run_capture(
                        ["git", "-C", str(worktree), "ls-files", "--others", "--exclude-standard"],
                        timeout=20,
                    )
                    for relative in untracked.stdout.splitlines():
                        candidate = (worktree / relative).resolve()
                        if not candidate.is_file() or not candidate.is_relative_to(worktree):
                            continue
                        try:
                            data = candidate.read_bytes()
                        except OSError:
                            continue
                        if b"\x00" in data[:4096]:
                            snippet = "[binary/unreadable new file]"
                        else:
                            snippet = data.decode("utf-8", errors="replace")[:20_000]
                        text += f"\n--- /dev/null\n+++ b/{relative}\n@@ new file @@\n{snippet}\n"
                    result["diff"] = text[:_MAX_DIFF_CHARS] + (
                        "\n...[diff truncated]" if len(text) > _MAX_DIFF_CHARS else ""
                    )
        return result

    def cleanup(self, task_id: str, *, remove_branch: bool = False) -> dict[str, object]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"找不到 Agent 任务：{task_id}")
            if task.state in {"queued", "running"}:
                raise ValueError("Agent 仍在运行，不能清理。请先等待或取消。")
        if task.worktree and task.repo_root:
            worktree = Path(task.worktree)
            repo = Path(task.repo_root)
            if worktree.exists():
                result = _run_capture(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
                    timeout=60,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"清理 worktree 失败：{(result.stderr or result.stdout).strip()[:800]}")
            if remove_branch and task.branch:
                deleted = _run_capture(
                    ["git", "-C", str(repo), "branch", "-D", task.branch], timeout=30
                )
                if deleted.returncode != 0:
                    raise RuntimeError(f"删除 Agent 分支失败：{(deleted.stderr or deleted.stdout).strip()[:800]}")
        with self._lock:
            task.cleaned = True
            self._persist(task)
            return self._snapshot(task)


__all__ = ["AgentPool", "AgentTask", "AgentState", "_HARD_MAX_PARALLEL", "_MAX_BATCH"]
