"""Durable execution loop for persistent logical Agents.

The low-level :mod:`agent_pool` owns one executor process at a time. This
module owns the objective across those process lifetimes: planning, durable
checkpoints, validation, retries, automatic continuation and human pauses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from .platform_support import run_platform_kwargs

TaskStatus = Literal[
    "queued",
    "starting",
    "running",
    "validating",
    "waiting_human",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]

_TERMINAL = {"completed", "failed", "cancelled"}
_RESUMABLE = {"queued", "starting", "running", "validating", "interrupted"}
_MAX_OUTPUT = 24_000
_MAX_TOOL_RESULTS = 100
_MAX_EVIDENCE = 200
_SENSITIVE = re.compile(
    r"(?i)(bearer\s+|(?:token|secret|password|api[_-]?key)\s*[:=]\s*)[^\s,;]+"
)


def _now() -> float:
    return time.time()


def _safe_text(value: object, limit: int = 2_000) -> str:
    text = str(value or "")
    text = _SENSITIVE.sub(lambda match: match.group(1) + "***", text)
    return text[-limit:]


def _run_git(argv: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        **run_platform_kwargs(),
    )


@dataclass
class TaskState:
    """Durable state for one user objective, not one executor process."""

    task_id: str
    objective: str
    checklist: list[str]
    completed_items: list[str]
    current_stage: str
    iteration: int
    retry_count: int
    last_checkpoint: str
    created_at: float
    updated_at: float
    status: TaskStatus
    workspace: str
    title: str = ""
    role: str = "worker"
    route_root: str = ""
    route_workspace_id: str = ""
    executor: str = "auto"
    model: str = ""
    write: bool = True
    isolation_mode: str = "auto"
    initial_isolate: bool | None = None
    current_turn_id: str = ""
    turn_ids: list[str] = field(default_factory=list)
    active_checklist: list[str] = field(default_factory=list)
    last_processed_turn_id: str = ""
    previous_output: str = ""
    agent_output_summary: str = ""
    next_plan: str = ""
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    pending_instructions: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    completion_verified: bool = False
    completion_receipt: dict[str, Any] = field(default_factory=dict)
    worktree: str = ""
    branch: str = ""
    repo_root: str = ""
    base_sha: str = ""
    error: str = ""
    waiting_reason: str = ""
    next_attempt_at: float = 0.0
    max_retries: int = 3
    max_iterations: int = 100

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskState:
        allowed = {item.name for item in fields(cls)}
        values = {key: value for key, value in payload.items() if key in allowed}
        return cls(**values)


@dataclass
class Checkpoint:
    checkpoint_id: str
    task_id: str
    iteration: int
    current_stage: str
    turn_id: str
    tool_results: list[dict[str, Any]]
    modified_files: list[str]
    agent_output_summary: str
    next_plan: str
    validation: dict[str, Any]
    created_at: float


@dataclass
class ValidationResult:
    complete: bool
    missing_items: list[str]
    failures: list[str]
    checks: dict[str, bool]
    modified_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObjectivePlanner:
    """Create a deterministic first checklist that the executor may refine."""

    _TEST_WORDS = ("test", "pytest", "测试", "回归", "验证用例")
    _BUILD_WORDS = ("build", "构建", "编译", "打包", "installer", "安装包")
    _RELEASE_WORDS = ("release", "发布", "部署", "替换程序", "安装新版")
    _RESTART_WORDS = ("restart", "重启", "启动新版", "启动服务")
    _COMMIT_WORDS = ("commit", "提交")
    _PUSH_WORDS = ("push", "推送")
    _ARCH_WORDS = ("architecture", "架构", "runtime", "运行时")
    _MCP_WORDS = ("mcp", "连接正常", "connection")

    @staticmethod
    def _contains(text: str, words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    def plan(self, objective: str, *, write: bool, role: str = "worker") -> list[str]:
        text = objective.casefold()
        checklist = ["analyze_objective"]
        if self._contains(text, self._ARCH_WORDS) and write:
            checklist.append("modify_architecture")
        checklist.append("implement_changes" if write else "perform_analysis")
        if self._contains(text, self._TEST_WORDS):
            checklist.append("run_tests")
        if self._contains(text, self._BUILD_WORDS) or self._contains(text, self._RELEASE_WORDS):
            checklist.append("build_artifacts")
        if self._contains(text, self._RELEASE_WORDS):
            checklist.append("replace_program")
        if self._contains(text, self._RESTART_WORDS) or self._contains(text, self._RELEASE_WORDS):
            checklist.append("restart_service")
        if self._contains(text, self._MCP_WORDS) and self._contains(text, self._RELEASE_WORDS):
            checklist.append("validate_mcp_connection")
        checklist.append("validate_objective")
        if self._contains(text, self._COMMIT_WORDS):
            checklist.append("commit_changes")
        if self._contains(text, self._PUSH_WORDS):
            checklist.append("push_changes")
        if role == "reviewer":
            checklist = ["analyze_objective", "perform_analysis", "validate_objective"]
        return list(dict.fromkeys(checklist))


class RetryPolicy:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay_seconds: float = 0.5,
        max_delay_seconds: float = 30.0,
    ) -> None:
        self.max_retries = max(1, int(max_retries))
        self.base_delay_seconds = max(0.0, float(base_delay_seconds))
        self.max_delay_seconds = max(self.base_delay_seconds, float(max_delay_seconds))

    def delay(self, retry_count: int) -> float:
        exponent = max(0, retry_count - 1)
        return min(self.max_delay_seconds, self.base_delay_seconds * (2**exponent))


class CompletionValidator:
    """Independent completion gate; natural-language success is not sufficient."""

    @staticmethod
    def _evidence_ok(evidence: list[dict[str, Any]], kind: str) -> bool:
        for item in reversed(evidence):
            if str(item.get("kind") or "").casefold() != kind:
                continue
            exit_code = item.get("exit_code")
            if exit_code is not None:
                return exit_code == 0
            if "success" in item:
                return bool(item.get("success"))
            return bool(item.get("ok", True))
        return False

    @staticmethod
    def _git_changes(state: TaskState) -> tuple[bool, list[str]]:
        worktree = Path(state.worktree or state.workspace)
        if not worktree.is_dir():
            return False, []
        changed: list[str] = []
        status = _run_git(["git", "-C", str(worktree), "status", "--porcelain"], timeout=20)
        if status.returncode == 0:
            changed.extend(line[3:].strip() for line in status.stdout.splitlines() if len(line) > 3)
        if state.base_sha:
            diff = _run_git(
                ["git", "-C", str(worktree), "diff", "--name-only", f"{state.base_sha}..HEAD", "--"],
                timeout=30,
            )
            if diff.returncode == 0:
                changed.extend(line.strip() for line in diff.stdout.splitlines() if line.strip())
        unique = list(dict.fromkeys(changed))
        return bool(unique), unique

    @staticmethod
    def _file_evidence_valid(state: TaskState) -> bool:
        root = Path(state.worktree or state.workspace).resolve()
        for item in reversed(state.evidence):
            if str(item.get("kind") or "").casefold() not in {"file_exists", "exe_exists", "executable"}:
                continue
            raw = str(item.get("path") or "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            if path.exists():
                return True
        return False

    def validate(self, state: TaskState, turn: dict[str, Any] | None = None) -> ValidationResult:
        missing = [item for item in state.checklist if item not in state.completed_items]
        failures: list[str] = []
        checks: dict[str, bool] = {}
        turn = turn or {}
        test_executor = str(turn.get("executor") or state.executor) == "test"

        verified = bool(turn.get("completion_verified", state.completion_verified))
        checks["verified_executor_receipt"] = verified
        if not verified:
            failures.append("latest executor turn lacks a verified machine-readable success receipt")

        objective_complete = bool(state.completion_receipt.get("objective_complete")) or test_executor
        checks["objective_declared_complete"] = objective_complete
        if not objective_complete:
            failures.append("executor did not declare objective_complete=true")

        git_changed, git_files = self._git_changes(state) if state.write else (False, [])
        modified = list(dict.fromkeys([*state.modified_files, *git_files]))
        if state.write and "implement_changes" in state.checklist:
            code_ok = git_changed or bool(modified) or self._file_evidence_valid(state) or test_executor
            checks["code_changes_present"] = code_ok
            if not code_ok:
                failures.append("no git diff, modified-file record, or existing output file proves code changes")

        requirements = {
            "run_tests": ("tests_passed", "test"),
            "build_artifacts": ("build_passed", "build"),
            "replace_program": ("executable_exists", "exe_exists"),
            "restart_service": ("service_running", "service_running"),
            "validate_mcp_connection": ("mcp_connected", "mcp_connected"),
            "push_changes": ("push_succeeded", "push"),
        }
        for checklist_item, (check_name, evidence_kind) in requirements.items():
            if checklist_item not in state.checklist:
                continue
            ok = self._evidence_ok(state.evidence, evidence_kind) or test_executor
            if checklist_item == "replace_program" and ok:
                ok = self._file_evidence_valid(state) or test_executor
            checks[check_name] = ok
            if not ok:
                failures.append(f"missing successful {evidence_kind} evidence")

        if "commit_changes" in state.checklist:
            commit_ok = self._evidence_ok(state.evidence, "commit") or git_changed or test_executor
            checks["commit_exists"] = commit_ok
            if not commit_ok:
                failures.append("git commit was requested but no commit can be verified")

        checks["checklist_complete"] = not missing
        return ValidationResult(
            complete=not missing and not failures,
            missing_items=missing,
            failures=failures,
            checks=checks,
            modified_files=modified,
        )


TurnSpawner = Callable[[TaskState, str], dict[str, Any]]
TurnGetter = Callable[[str], dict[str, Any]]
TurnCanceller = Callable[[str], dict[str, Any]]


class AgentRuntimeLoop:
    """Persistent objective loop spanning any number of executor turns."""

    def __init__(
        self,
        root_dir: Path,
        *,
        turn_spawner: TurnSpawner | None = None,
        turn_getter: TurnGetter | None = None,
        turn_canceller: TurnCanceller | None = None,
        planner: ObjectivePlanner | None = None,
        validator: CompletionValidator | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.root_dir = root_dir.resolve()
        self.task_dir = self.root_dir / "tasks"
        self.checkpoint_dir = self.root_dir / "checkpoints"
        self.log_dir = self.root_dir / "agent_runtime_logs"
        for directory in (self.task_dir, self.checkpoint_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.turn_spawner = turn_spawner
        self.turn_getter = turn_getter
        self.turn_canceller = turn_canceller
        self.planner = planner or ObjectivePlanner()
        self.validator = validator or CompletionValidator()
        self.retry_policy = retry_policy or RetryPolicy()
        self._lock = threading.RLock()
        self._advancing: set[str] = set()
        self._tasks: dict[str, TaskState] = {}
        self._load()

    @staticmethod
    def has_persisted_incomplete(root_dir: Path) -> bool:
        """Return whether startup must eagerly restore a durable objective."""
        task_dir = root_dir.resolve() / "tasks"
        for path in task_dir.glob("*.json") if task_dir.is_dir() else ():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if str(payload.get("status") or "") in _RESUMABLE:
                return True
        return False

    def _task_file(self, task_id: str) -> Path:
        return self.task_dir / f"{task_id}.json"

    def _log_file(self, task_id: str) -> Path:
        return self.log_dir / f"{task_id}.jsonl"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _persist(self, state: TaskState) -> None:
        state.updated_at = _now()
        self._atomic_json(self._task_file(state.task_id), asdict(state))

    def _log(self, state: TaskState, event: str, **details: Any) -> None:
        payload = {
            "at": _now(),
            "event": event,
            "task_id": state.task_id,
            "status": state.status,
            "iteration": state.iteration,
            "stage": state.current_stage,
        }
        for key, value in details.items():
            payload[key] = _safe_text(value) if isinstance(value, str) else value
        with self._log_file(state.task_id).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        for path in self.task_dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                state = TaskState.from_dict(raw)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if state.status in {"starting", "running", "validating"}:
                state.status = "interrupted"
                state.error = "MCP DevBridge restarted; resuming from the last durable checkpoint."
                self._persist(state)
                self._log(state, "restart_detected", turn_id=state.current_turn_id)
            self._tasks[state.task_id] = state

    def create_task(
        self,
        *,
        task_id: str,
        objective: str,
        workspace: Path,
        title: str = "",
        role: str = "worker",
        route_root: Path | None = None,
        route_workspace_id: str = "",
        executor: str = "auto",
        model: str = "",
        write: bool = True,
        isolation_mode: str = "auto",
        initial_isolate: bool | None = None,
        checklist: list[str] | None = None,
        current_turn_id: str = "",
        turn_ids: list[str] | None = None,
        worktree: str = "",
        branch: str = "",
        repo_root: str = "",
        base_sha: str = "",
        start: bool = True,
    ) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ValueError("objective 不能为空。")
        with self._lock:
            if task_id in self._tasks:
                return self.snapshot(task_id)
            planned = checklist or self.planner.plan(objective, write=write, role=role)
            now = _now()
            state = TaskState(
                task_id=task_id,
                objective=objective,
                checklist=list(dict.fromkeys(item.strip() for item in planned if item.strip())),
                completed_items=[],
                current_stage="planner",
                iteration=0,
                retry_count=0,
                last_checkpoint="",
                created_at=now,
                updated_at=now,
                status="interrupted" if current_turn_id else "queued",
                workspace=str(workspace.resolve()),
                title=title.strip(),
                role=role,
                route_root=str((route_root or workspace).resolve()),
                route_workspace_id=route_workspace_id.strip(),
                executor=executor,
                model=model,
                write=write,
                isolation_mode=isolation_mode,
                initial_isolate=initial_isolate,
                current_turn_id=current_turn_id,
                turn_ids=list(turn_ids or ([current_turn_id] if current_turn_id else [])),
                worktree=worktree,
                branch=branch,
                repo_root=repo_root,
                base_sha=base_sha,
                max_retries=self.retry_policy.max_retries,
            )
            self._tasks[task_id] = state
            self._persist(state)
            self._log(state, "task_started", objective=_safe_text(objective), checklist=state.checklist)
            self._checkpoint(state, next_plan="Start the first execution turn from the objective checklist.")
        if start:
            return self.advance(task_id)
        return self.snapshot(task_id)

    def has_task(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def _checkpoint(self, state: TaskState, *, next_plan: str = "") -> Checkpoint:
        checkpoint_id = f"{state.iteration:04d}-{uuid.uuid4().hex[:12]}"
        checkpoint = Checkpoint(
            checkpoint_id=checkpoint_id,
            task_id=state.task_id,
            iteration=state.iteration,
            current_stage=state.current_stage,
            turn_id=state.current_turn_id,
            tool_results=state.tool_results[-_MAX_TOOL_RESULTS:],
            modified_files=state.modified_files,
            agent_output_summary=state.agent_output_summary,
            next_plan=next_plan or state.next_plan,
            validation=state.validation,
            created_at=_now(),
        )
        target = self.checkpoint_dir / state.task_id / f"{checkpoint_id}.json"
        self._atomic_json(target, asdict(checkpoint))
        state.last_checkpoint = str(target)
        state.next_plan = checkpoint.next_plan
        self._persist(state)
        self._log(state, "checkpoint_saved", checkpoint=checkpoint_id, next_plan=checkpoint.next_plan)
        return checkpoint

    def _prompt(self, state: TaskState) -> str:
        missing = [item for item in state.checklist if item not in state.completed_items]
        prior = state.previous_output[-6_000:] or "(no prior executor output)"
        validation = json.dumps(state.validation, ensure_ascii=False)[:5_000]
        instructions = "\n".join(f"- {item}" for item in state.pending_instructions)
        message_block = (
            f"NEW MESSAGE:\n{instructions}\n\n"
            if instructions
            else "HUMAN FOLLOW-UP: (none)\n\n"
        )
        evidence_schema = (
            '[{"kind":"test","command":"pytest ...","exit_code":0}, '
            '{"kind":"build","command":"...","exit_code":0}, '
            '{"kind":"file_exists|exe_exists","path":"...","ok":true}, '
            '{"kind":"service_running|mcp_connected|commit|push","ok":true}]'
        )
        return (
            "You are one execution turn inside MCP DevBridge Persistent Agent Runtime. "
            "Continue the SAME task_id and workspace; do not restart planning from scratch. "
            "A natural-language claim of completion is never enough. Work through the remaining checklist, "
            "run the relevant checks, and report concrete evidence. If credentials, permission, or a human "
            "decision is genuinely required, return status=waiting_human and explain exactly what is needed.\n\n"
            f"TASK_ID: {state.task_id}\nOBJECTIVE:\n{state.objective}\n\n"
            f"CURRENT CHECKPOINT: {state.last_checkpoint or '(initial)'}\n"
            f"WORKSPACE: {state.worktree or state.workspace}\n"
            f"ITERATION: {state.iteration}\n"
            f"FULL CHECKLIST: {json.dumps(state.checklist, ensure_ascii=False)}\n"
            f"COMPLETED ITEMS: {json.dumps(state.completed_items, ensure_ascii=False)}\n"
            f"REMAINING ITEMS: {json.dumps(missing, ensure_ascii=False)}\n"
            f"LAST VALIDATION: {validation}\n\n"
            f"PREVIOUS TURN OUTPUT TAIL:\n{prior}\n\n"
            f"{message_block}"
            "Finish as many remaining checklist IDs as actually verified. End with exactly one line:\n"
            "MCP_AGENT_RESULT: {\"status\":\"success|failed|waiting_human\","
            "\"summary\":\"...\",\"completed_items\":[\"exact checklist IDs\"],"
            "\"current_stage\":\"...\",\"next_step\":\"...\","
            "\"objective_complete\":true|false,\"evidence\":"
            f"{evidence_schema}" + "}\n"
            "Only set objective_complete=true when every checklist item and all requested release/deploy/git "
            "effects have been verified."
        )

    def _handle_failure(self, state: TaskState, reason: str, *, turn_id: str = "") -> None:
        state.retry_count += 1
        state.error = _safe_text(reason, 4_000)
        state.last_processed_turn_id = turn_id or state.last_processed_turn_id
        state.current_turn_id = ""
        state.completion_verified = False
        if state.retry_count >= state.max_retries or state.iteration >= state.max_iterations:
            state.status = "waiting_human"
            state.waiting_reason = (
                f"Persistent runtime paused after {state.retry_count} consecutive failures: {state.error}"
            )
            state.current_stage = "waiting_human"
            state.next_attempt_at = 0.0
            self._persist(state)
            self._checkpoint(state, next_plan="Wait for a human instruction, permission, credential, or fix.")
            self._log(state, "waiting_human", reason=state.waiting_reason)
            return
        delay = self.retry_policy.delay(state.retry_count)
        state.status = "queued"
        state.current_stage = "retry"
        state.next_attempt_at = _now() + delay
        self._persist(state)
        self._checkpoint(state, next_plan=f"Retry the same task after {delay:.2f}s backoff.")
        self._log(state, "retry_scheduled", reason=state.error, delay_seconds=delay)

    def _spawn_next(self, state: TaskState) -> dict[str, Any]:
        if self.turn_spawner is None:
            raise RuntimeError("AgentRuntimeLoop has no turn_spawner configured.")
        state.iteration += 1
        state.active_checklist = [item for item in state.checklist if item not in state.completed_items]
        state.current_stage = state.active_checklist[0] if state.active_checklist else "repair_validation"
        state.status = "starting"
        state.current_turn_id = ""
        state.next_attempt_at = 0.0
        prompt = self._prompt(state)
        self._persist(state)
        self._log(state, "turn_starting", active_checklist=state.active_checklist)
        try:
            turn = self.turn_spawner(state, prompt)
        except Exception as exc:  # noqa: BLE001 - persisted retry path
            self._handle_failure(state, f"turn spawn failed: {exc}")
            return self._snapshot_state(state)
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            self._handle_failure(state, "turn spawner returned no task id")
            return self._snapshot_state(state)
        state.current_turn_id = turn_id
        if turn_id not in state.turn_ids:
            state.turn_ids.append(turn_id)
        state.status = "running"
        state.executor = str(turn.get("executor") or state.executor)
        state.pending_instructions.clear()
        self._update_turn_metadata(state, turn)
        self._persist(state)
        self._log(state, "turn_started", turn_id=turn_id, executor=state.executor)
        return self._snapshot_state(state)

    @staticmethod
    def _receipt_list(receipt: dict[str, Any], key: str) -> list[Any]:
        value = receipt.get(key)
        return value if isinstance(value, list) else []

    def _update_turn_metadata(self, state: TaskState, turn: dict[str, Any]) -> None:
        state.worktree = str(turn.get("worktree") or state.worktree)
        state.branch = str(turn.get("branch") or state.branch)
        state.repo_root = str(turn.get("repo_root") or state.repo_root)
        state.base_sha = state.base_sha or str(turn.get("base_sha") or "")
        state.isolation_mode = str(turn.get("isolation_mode") or state.isolation_mode)
        tail = str(turn.get("output_tail") or "")[-_MAX_OUTPUT:]
        if tail:
            state.previous_output = tail

    def _process_completed_turn(self, state: TaskState, turn: dict[str, Any]) -> None:
        turn_id = str(turn.get("id") or state.current_turn_id)
        self._update_turn_metadata(state, turn)
        receipt_raw = turn.get("completion_receipt")
        receipt = dict(receipt_raw) if isinstance(receipt_raw, dict) else {}
        test_executor = str(turn.get("executor") or state.executor) == "test"
        if test_executor:
            receipt.setdefault("status", "success")
            receipt.setdefault("summary", _safe_text(turn.get("output_tail") or "test executor completed"))
            receipt.setdefault("completed_items", list(state.active_checklist))
            receipt.setdefault("objective_complete", True)
            receipt.setdefault("evidence", [{"kind": "test_executor", "ok": True}])

        state.completion_receipt = receipt
        state.completion_verified = bool(turn.get("completion_verified"))
        state.agent_output_summary = _safe_text(receipt.get("summary") or turn.get("output_tail"), 6_000)
        state.next_plan = _safe_text(receipt.get("next_step"), 2_000)
        state.current_stage = str(receipt.get("current_stage") or state.current_stage)[:160]
        completed = [str(item) for item in self._receipt_list(receipt, "completed_items")]
        allowed = set(state.checklist)
        state.completed_items = list(
            dict.fromkeys([*state.completed_items, *(item for item in completed if item in allowed)])
        )
        evidence = [dict(item) for item in self._receipt_list(receipt, "evidence") if isinstance(item, dict)]
        state.evidence = [*state.evidence, *evidence][-_MAX_EVIDENCE:]
        for item in evidence:
            paths = item.get("modified_files")
            if isinstance(paths, list):
                state.modified_files.extend(str(path) for path in paths)
            if str(item.get("kind") or "").casefold() in {"file_exists", "exe_exists"} and item.get("path"):
                state.modified_files.append(str(item["path"]))
        state.modified_files = list(dict.fromkeys(state.modified_files))
        state.tool_results.append(
            {
                "turn_id": turn_id,
                "state": str(turn.get("state") or "completed"),
                "exit_code": turn.get("exit_code"),
                "evidence": evidence,
                "summary": state.agent_output_summary,
            }
        )
        state.tool_results = state.tool_results[-_MAX_TOOL_RESULTS:]
        state.last_processed_turn_id = turn_id
        state.current_turn_id = ""
        self._log(state, "tool_call_result", turn_id=turn_id, evidence=evidence)

        status = str(receipt.get("status") or "").casefold()
        if status == "waiting_human" or bool(receipt.get("requires_human")):
            state.status = "waiting_human"
            state.waiting_reason = _safe_text(
                receipt.get("human_request") or receipt.get("summary") or "Executor requested human input.",
                4_000,
            )
            state.current_stage = "waiting_human"
            self._persist(state)
            self._checkpoint(state, next_plan="Resume this same task after the requested human response.")
            self._log(state, "waiting_human", reason=state.waiting_reason)
            return

        state.status = "validating"
        self._persist(state)
        result = self.validator.validate(state, turn)
        state.validation = result.as_dict()
        state.modified_files = result.modified_files
        self._log(state, "validation_result", result=state.validation)
        if result.complete:
            state.status = "completed"
            state.current_stage = "completed"
            state.retry_count = 0
            state.error = ""
            self._persist(state)
            self._checkpoint(state, next_plan="Objective and every checklist item are independently validated.")
            self._log(state, "task_completed", turns=len(state.turn_ids))
            return

        if state.iteration >= state.max_iterations:
            self._handle_failure(state, "maximum runtime iterations reached", turn_id=turn_id)
            return
        state.status = "queued"
        state.retry_count = 0
        state.current_stage = result.missing_items[0] if result.missing_items else "repair_validation"
        state.error = "; ".join(result.failures)
        state.next_plan = (
            "Continue incomplete checklist items: " + ", ".join(result.missing_items)
            if result.missing_items
            else "Repair validation failures: " + "; ".join(result.failures)
        )
        self._persist(state)
        self._checkpoint(state, next_plan=state.next_plan)
        self._log(
            state,
            "continuation_scheduled",
            missing_items=result.missing_items,
            validation_failures=result.failures,
        )

    def advance(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise ValueError(f"找不到 Runtime 任务：{task_id}")
            if task_id in self._advancing:
                return self._snapshot_state(state)
            if state.status in _TERMINAL or state.status == "waiting_human":
                return self._snapshot_state(state)
            self._advancing.add(task_id)
        try:
            with self._lock:
                state = self._tasks[task_id]
                if not state.current_turn_id:
                    if state.next_attempt_at and _now() < state.next_attempt_at:
                        return self._snapshot_state(state)
                    return self._spawn_next(state)
                turn_id = state.current_turn_id
            if self.turn_getter is None:
                raise RuntimeError("AgentRuntimeLoop has no turn_getter configured.")
            try:
                turn = self.turn_getter(turn_id)
            except Exception as exc:  # noqa: BLE001 - durable recovery path
                with self._lock:
                    self._handle_failure(state, f"cannot reload executor turn {turn_id}: {exc}", turn_id=turn_id)
                    return self._snapshot_state(state)
            with self._lock:
                self._update_turn_metadata(state, turn)
                low_state = str(turn.get("state") or "")
                if low_state in {"queued", "running"}:
                    state.status = "running"
                    self._persist(state)
                    return self._snapshot_state(state)
                if state.last_processed_turn_id == turn_id:
                    return self._snapshot_state(state)
                if low_state == "completed":
                    self._process_completed_turn(state, turn)
                elif low_state == "cancelled":
                    state.status = "cancelled"
                    state.last_processed_turn_id = turn_id
                    state.current_turn_id = ""
                    state.error = str(turn.get("error") or "")
                    self._persist(state)
                    self._checkpoint(state, next_plan="Task cancelled.")
                    self._log(state, "task_cancelled", turn_id=turn_id)
                else:
                    receipt = turn.get("completion_receipt")
                    receipt_status = (
                        str(receipt.get("status") or "").casefold()
                        if isinstance(receipt, dict)
                        else ""
                    )
                    if receipt_status == "waiting_human":
                        self._process_completed_turn(state, turn)
                    else:
                        reason = str(
                            turn.get("error") or f"executor turn ended as {low_state or 'unknown'}"
                        )
                        self._handle_failure(state, reason, turn_id=turn_id)
                return self._snapshot_state(state)
        finally:
            with self._lock:
                self._advancing.discard(task_id)

    def tick_all(self) -> None:
        with self._lock:
            task_ids = [item.task_id for item in self._tasks.values() if item.status in _RESUMABLE]
        for task_id in task_ids:
            try:
                self.advance(task_id)
            except Exception:
                continue

    def resume_incomplete(self) -> None:
        """Queue persisted running/interrupted objectives for checkpoint recovery."""
        with self._lock:
            states = [item for item in self._tasks.values() if item.status in _RESUMABLE]
            for state in states:
                if state.status == "starting" and not state.current_turn_id:
                    state.status = "queued"
                self._persist(state)
                self._log(state, "resume_queued", checkpoint=state.last_checkpoint)
        self.tick_all()

    def add_instruction(
        self,
        task_id: str,
        message: str,
        *,
        applied_to_current_turn: bool = False,
    ) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("message 不能为空。")
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise ValueError(f"找不到 Runtime 任务：{task_id}")
            if state.status == "cancelled":
                raise ValueError("任务已取消，不能恢复。")
            item = f"follow_up_{1 + sum(value.startswith('follow_up_') for value in state.checklist):03d}"
            state.checklist.append(item)
            state.pending_instructions.append(message)
            if applied_to_current_turn and state.current_turn_id:
                state.active_checklist.append(item)
            elif state.status in _TERMINAL or state.status in {"waiting_human", "interrupted"}:
                state.status = "queued"
                state.current_turn_id = ""
                state.next_attempt_at = 0.0
                state.waiting_reason = ""
                state.error = ""
                state.completion_verified = False
            state.current_stage = item
            self._persist(state)
            self._checkpoint(state, next_plan=f"Apply human follow-up checklist item {item}.")
            self._log(state, "human_instruction", checklist_item=item, message=message)
        return self.advance(task_id) if state.status == "queued" else self.snapshot(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise ValueError(f"找不到 Runtime 任务：{task_id}")
            turn_id = state.current_turn_id
        if turn_id and self.turn_canceller is not None:
            with suppress(Exception):
                self.turn_canceller(turn_id)
        with self._lock:
            state.status = "cancelled"
            state.current_turn_id = ""
            state.current_stage = "cancelled"
            self._persist(state)
            self._checkpoint(state, next_plan="Task cancelled.")
            self._log(state, "task_cancelled", turn_id=turn_id)
            return self._snapshot_state(state)

    def delete(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._task_file(task_id).unlink(missing_ok=True)

    def recent_events(self, task_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        path = self._log_file(task_id)
        if not path.is_file():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 100)) :]
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                events.append(value)
        return events

    def _snapshot_state(self, state: TaskState) -> dict[str, Any]:
        row = asdict(state)
        row["terminal"] = state.status in _TERMINAL
        row["objective_complete"] = state.status == "completed"
        row["turn_count"] = len(state.turn_ids)
        row["recent_events"] = self.recent_events(state.task_id, limit=30)
        return row

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise ValueError(f"找不到 Runtime 任务：{task_id}")
            return self._snapshot_state(state)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            states = sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)
            return [self._snapshot_state(item) for item in states]


__all__ = [
    "AgentRuntimeLoop",
    "Checkpoint",
    "CompletionValidator",
    "ObjectivePlanner",
    "RetryPolicy",
    "TaskState",
    "TaskStatus",
    "ValidationResult",
]
