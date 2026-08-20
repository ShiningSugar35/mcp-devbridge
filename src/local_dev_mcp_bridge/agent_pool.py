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
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from . import constants
from .chatgpt_desktop import ChatGPTDesktopBridge
from .platform_support import popen_platform_kwargs, run_platform_kwargs
from .shell import kill_process_tree

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
_RESULT_MARKER = "MCP_AGENT_RESULT:"
_DEFAULT_OPENCODE_FREE_MODEL = "opencode/nemotron-3-ultra-free"


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
    route_root: str = ""
    route_workspace_id: str = ""
    external_id: str = ""
    receipt_path: str = ""
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
    isolate: bool = True
    isolation_mode: str = "auto"


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


def _completion_receipt(text: str) -> dict[str, object] | None:
    """Parse the last receipt from plain text or JSON/stream-JSON executor output."""

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            result: list[str] = []
            for item in value.values():
                result.extend(strings(item))
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                result.extend(strings(item))
            return result
        return []

    decoder = json.JSONDecoder()
    for line in reversed(text.splitlines()):
        candidates = [line]
        with suppress(json.JSONDecodeError):
            candidates.extend(strings(json.loads(line)))
        for candidate in reversed(candidates):
            marker_index = candidate.rfind(_RESULT_MARKER)
            if marker_index < 0:
                continue
            raw = candidate[marker_index + len(_RESULT_MARKER) :].strip()
            try:
                value, _end = decoder.raw_decode(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return None

def _run_capture(argv: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **run_platform_kwargs(),
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
        self.preferred_executor = preferred if preferred in {"chatgpt", "opencode", "claude"} else ""
        self._chatgpt_bridge = ChatGPTDesktopBridge()
        self.default_opencode_model = (
            os.environ.get("MCP_DEVBRIDGE_OPENCODE_FREE_MODEL", "").strip()
            or _DEFAULT_OPENCODE_FREE_MODEL
        )
        self._lock = threading.RLock()
        self._tasks: dict[str, AgentTask] = {}
        self._prompts: dict[str, str] = {}
        self._processes: dict[str, subprocess.Popen[Any]] = {}
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
                **run_platform_kwargs(),
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
                **run_platform_kwargs(),
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
        chatgpt = self._chatgpt_bridge.capabilities()
        chatgpt_available = self._command_builder is None and bool(chatgpt.get("ready"))
        executors: list[str] = []
        if chatgpt_available:
            executors.append("chatgpt")
        executors.extend(name for name, path in (("opencode", opencode), ("claude", claude)) if path)
        return {
            "available": bool(executors or self._command_builder),
            "executors": executors or (["test"] if self._command_builder else []),
            "chatgpt_desktop": chatgpt,
            "opencode_path": opencode,
            "claude_path": claude,
            "preferred_executor": self.preferred_executor or ("chatgpt" if chatgpt_available else "auto"),
            "default_opencode_model": self.default_opencode_model,
            "opencode_default_is_free": self.default_opencode_model.startswith("opencode/")
            and (self.default_opencode_model.endswith("-free") or self.default_opencode_model == "opencode/big-pickle"),
            "provider_runtime_verified": False,
            "provider_runtime_note": (
                "CLI discovery only verifies a non-interactive command surface. "
                "Provider authentication, quota and service health are validated when each task runs."
            ),
            "max_parallel": self.max_parallel,
            "max_batch": _MAX_BATCH,
            "worktree_isolation": True,
            "write_requires_git": False,
            "non_git_direct_write": True,
            "isolation_modes": ["auto", "git_worktree", "direct"],
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
            "--pure",
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
            raise RuntimeError("目标目录不属于 Git 仓库。")
        repo = Path(root.stdout.strip()).resolve()
        sha = _run_capture(["git", "-C", str(repo), "rev-parse", "HEAD"])
        if sha.returncode != 0 or not sha.stdout.strip():
            raise RuntimeError("无法读取 Git HEAD，不能创建 Agent worktree。")
        return repo, sha.stdout.strip()

    @classmethod
    def _try_git_repo_root(cls, workspace: Path) -> tuple[Path, str] | None:
        try:
            return cls._git_repo_root(workspace)
        except RuntimeError:
            return None

    def _prepare_worktree(self, task: AgentTask) -> Path:
        workspace = Path(task.workspace).resolve()
        if not task.write:
            task.isolation_mode = "read_only"
            self._persist(task)
            return workspace
        if task.isolation_mode == "direct":
            task.isolate = False
            task.isolation_mode = "direct"
            task.worktree = str(workspace)
            self._persist(task)
            return workspace

        if not task.isolate:
            repo_info = self._try_git_repo_root(workspace)
            if repo_info is None:
                task.isolation_mode = "direct"
                task.worktree = str(workspace)
                self._persist(task)
                return workspace
            repo, base_sha = repo_info
            branch_result = _run_capture(
                ["git", "-C", str(workspace), "branch", "--show-current"], timeout=20
            )
            task.repo_root = str(repo)
            task.base_sha = base_sha
            task.branch = branch_result.stdout.strip()
            task.worktree = str(workspace)
            task.isolation_mode = "git_worktree"
            self._persist(task)
            return workspace

        repo_info = self._try_git_repo_root(workspace)
        if repo_info is None:
            if task.isolation_mode == "git_worktree":
                raise RuntimeError("该 Agent 明确要求 Git worktree 隔离，但目标目录不属于 Git 仓库。")
            task.isolate = False
            task.isolation_mode = "direct"
            task.worktree = str(workspace)
            self._persist(task)
            return workspace

        repo, base_sha = repo_info
        task.isolation_mode = "git_worktree"
        branch = f"mcp-agent/{task.id[:12]}"
        worktree_base = self.worktree_dir
        if task.executor == "chatgpt" and task.route_root:
            worktree_base = Path(task.route_root).resolve() / ".mcp-devbridge-agent-worktrees"
        worktree = (worktree_base / task.id).resolve()
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
        if task.write and task.isolation_mode == "git_worktree":
            isolation = (
                "You are running inside a dedicated Git worktree. Work only inside the current worktree. "
                "Do not push, do not alter other worktrees, and do not touch the user's primary checkout. "
            )
        elif task.write:
            isolation = (
                "You are in direct local-write mode. Work only inside the current target workspace and "
                "do not touch paths outside it. "
            )
        else:
            isolation = "This is a read-only/analysis assignment. Do not modify files. "
        return (
            f"{isolation}Read and obey all applicable AGENTS.md instructions before acting. "
            "Operate as a persistent implementation agent: do not stop after analysis, partial fixes, or an intermediate milestone. Continue through all required phases of the assignment until the original objective is actually satisfied. Before reporting success, verify the resulting state instead of only describing intended changes. "
            "Your final output MUST end with exactly one machine-readable line like "
            'MCP_AGENT_RESULT: {"status":"success","summary":"short result"}. '
            "Use status=failed when the assignment was not actually completed.\n\n"
            f"TASK (do not omit or reinterpret):\n{user_prompt.strip()}"
        )

    # ---------------------------------------------------------------- lifecycle
    def spawn(
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
        isolate: bool | None = None,
        isolation_mode: str = "auto",
    ) -> dict[str, object]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt 不能为空。")
        requested_executor = executor.strip().lower()
        chatgpt_ready = self._command_builder is None and self._chatgpt_bridge.ready
        if requested_executor in {"", "auto"}:
            if self.preferred_executor == "chatgpt" and chatgpt_ready:
                selected_executor = "chatgpt"
            elif self.preferred_executor == "claude" and self._claude_executable:
                selected_executor = "claude"
            elif self.preferred_executor == "opencode" and self._opencode_executable:
                selected_executor = "opencode"
            else:
                selected_executor = (
                    "chatgpt"
                    if chatgpt_ready
                    else "opencode"
                    if self._opencode_executable
                    else "claude"
                    if self._claude_executable
                    else "test"
                    if self._command_builder is not None
                    else ""
                )
        else:
            selected_executor = requested_executor
        if self._command_builder is None and selected_executor not in {"chatgpt", "opencode", "claude"}:
            raise ValueError("当前 Agent Pool 支持 chatgpt / opencode / claude 执行器。")
        if self._command_builder is None and selected_executor == "chatgpt" and not chatgpt_ready:
            raise RuntimeError("ChatGPT Desktop Chat Agent bridge 尚未准备好。")
        if self._command_builder is None and selected_executor == "opencode" and not self._opencode_executable:
            raise RuntimeError("未检测到可非交互运行的 OpenCode CLI。")
        if self._command_builder is None and selected_executor == "claude" and not self._claude_executable:
            raise RuntimeError("未检测到可非交互运行的 Claude Code CLI。")
        requested_isolation = isolation_mode.strip().lower() or "auto"
        if requested_isolation not in {"auto", "git_worktree", "direct"}:
            raise ValueError("isolation_mode 必须是 auto / git_worktree / direct。")
        selected_model = model.strip()
        if selected_executor == "opencode" and not selected_model:
            selected_model = self.default_opencode_model
        task_id = uuid.uuid4().hex
        safe_title = " ".join((title.strip() or f"Agent {task_id[:8]}").split())[:120]
        task = AgentTask(
            id=task_id,
            title=safe_title,
            executor=selected_executor,
            model=selected_model,
            workspace=str(workspace.resolve()),
            write=bool(write),
            state="queued",
            created_at=time.time(),
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            route_root=str((route_root or workspace).resolve()),
            route_workspace_id=route_workspace_id.strip(),
            log_path=str((self.log_dir / f"{task_id}.log").resolve()),
            isolate=(requested_isolation != "direct" and bool(write)) if isolate is None else bool(isolate),
            isolation_mode=requested_isolation if write else "read_only",
        )
        with self._lock:
            self._tasks[task.id] = task
            self._prompts[task.id] = prompt
            self._persist(task)
            future = self._executor.submit(self._run_task, task.id)
            self._futures[task.id] = future
        return self.get(task.id)

    def append_prompt(self, task_id: str, message: str) -> bool:
        """Append an instruction to a queued task before its executor starts.

        Returns False once the executor has consumed the prompt.  The Agent
        Orchestrator uses that signal to schedule a continuation turn instead of
        pretending one-shot CLIs support live stdin.
        """
        message = message.strip()
        if not message:
            raise ValueError("message 不能为空。")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ValueError(f"找不到 Agent 任务：{task_id}")
            prompt = self._prompts.get(task_id)
            if task.state != "queued" or prompt is None:
                return False
            self._prompts[task_id] = (
                prompt.rstrip()
                + "\n\nFOLLOW-UP INSTRUCTION FROM ORCHESTRATOR:\n"
                + message
            )
            return True

    def spawn_batch(
        self,
        *,
        workspace: Path,
        tasks: list[dict[str, object]],
        route_root: Path | None = None,
        route_workspace_id: str = "",
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
                    route_root=route_root,
                    route_workspace_id=route_workspace_id,
                    prompt=str(item.get("prompt") or ""),
                    title=str(item.get("title") or ""),
                    executor=str(item.get("executor") or "auto"),
                    model=str(item.get("model") or ""),
                    write=bool(item.get("write", True)),
                    isolation_mode=str(item.get("isolation_mode") or "auto"),
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
            log_path = Path(task.log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if task.executor == "chatgpt" and self._command_builder is None:
                def mark_started(conversation_id: str) -> None:
                    with self._lock:
                        if task.state == "cancelled":
                            return
                        task.external_id = conversation_id
                        self._persist(task)

                receipt, conversation_id, receipt_path = self._chatgpt_bridge.run_task(
                    task_id=task.id,
                    assignment=full_prompt,
                    route_root=Path(task.route_root or task.workspace),
                    target_workspace=workdir,
                    write=task.write,
                    route_workspace_id=task.route_workspace_id,
                    on_started=mark_started,
                )
                status = str(receipt.get("status") or "").lower()
                log_path.write_text(
                    "ChatGPT Desktop ordinary-Chat executor completed.\n"
                    + f"conversation_id={conversation_id}\n"
                    + _RESULT_MARKER
                    + " "
                    + json.dumps(receipt, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
                with self._lock:
                    if task.state != "cancelled":
                        task.external_id = conversation_id
                        task.receipt_path = str(receipt_path)
                        task.exit_code = 0 if status == "success" else 1
                        if status == "success":
                            task.state = "completed"
                        else:
                            task.state = "failed"
                            task.error = str(receipt.get("error") or receipt.get("summary") or "ChatGPT child agent reported failure.")[:1200]
            else:
                builder = self._command_builder or self._default_command
                command = builder(task, full_prompt, workdir)
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
                        text=False,
                        **popen_platform_kwargs(new_session=True),
                    )
                    with self._lock:
                        self._processes[task_id] = process
                    exit_code = process.wait()
                with self._lock:
                    if task.state != "cancelled":
                        task.exit_code = exit_code
                        tail = _tail_text(log_path, 2400).strip()
                        if exit_code != 0:
                            task.state = "failed"
                            task.error = f"Agent executor exited with code {exit_code}."
                            if tail:
                                task.error += f" Output tail: {tail}"
                        elif self._command_builder is None:
                            receipt = _completion_receipt(tail)
                            if receipt and str(receipt.get("status", "")).lower() == "success":
                                task.state = "completed"
                            else:
                                task.state = "failed"
                                task.error = (
                                    "Agent process exited successfully but did not provide a verified success receipt. "
                                    "The task may have been skipped or misunderstood."
                                )
                                if receipt and receipt.get("summary"):
                                    task.error += f" Receipt summary: {receipt.get('summary')}"
                        else:
                            task.state = "completed"
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
            tail = _tail_text(Path(task.log_path))
            row["output_tail"] = tail
            receipt = _completion_receipt(tail)
            row["completion_receipt"] = receipt or {}
            row["completion_verified"] = bool(
                task.executor == "test"
                or (receipt and str(receipt.get("status", "")).lower() == "success")
            )
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
            executor = task.executor
            external_id = task.external_id
            if future is not None:
                future.cancel()
            self._persist(task)
        if process is not None and process.poll() is None:
            kill_process_tree(process.pid)
        if executor == "chatgpt" and external_id:
            with suppress(Exception):
                self._chatgpt_bridge.stop_conversation(external_id)
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
        if task.isolate and task.worktree and task.repo_root:
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
        if task.receipt_path:
            with suppress(OSError):
                Path(task.receipt_path).unlink(missing_ok=True)
        with self._lock:
            task.cleaned = True
            self._persist(task)
            return self._snapshot(task)


__all__ = ["AgentPool", "AgentTask", "AgentState", "_HARD_MAX_PARALLEL", "_MAX_BATCH"]
