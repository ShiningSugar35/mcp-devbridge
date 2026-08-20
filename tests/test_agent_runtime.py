from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from local_dev_mcp_bridge.agent_runtime import AgentRuntimeLoop, RetryPolicy, TaskState


class FakeTurns:
    def __init__(self, factory, *, prefix: str = "turn") -> None:
        self.factory = factory
        self.prefix = prefix
        self.turns: dict[str, dict[str, Any]] = {}
        self.prompts: list[str] = []
        self.spawned_task_ids: list[str] = []

    def spawn(self, state: TaskState, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.spawned_task_ids.append(state.task_id)
        turn_id = f"{self.prefix}-{len(self.prompts)}"
        turn = {
            "id": turn_id,
            "state": "completed",
            "executor": "opencode",
            "completion_verified": True,
            "completion_receipt": self.factory(state, len(self.prompts)),
            "output_tail": f"output from {turn_id}",
            "exit_code": 0,
        }
        self.turns[turn_id] = turn
        return turn

    def get(self, turn_id: str) -> dict[str, Any]:
        return self.turns[turn_id]

    def cancel(self, turn_id: str) -> dict[str, Any]:
        self.turns[turn_id]["state"] = "cancelled"
        return self.turns[turn_id]


def _runtime(tmp_path: Path, fake: FakeTurns) -> AgentRuntimeLoop:
    return AgentRuntimeLoop(
        tmp_path / "runtime",
        turn_spawner=fake.spawn,
        turn_getter=fake.get,
        turn_canceller=fake.cancel,
        retry_policy=RetryPolicy(max_retries=3, base_delay_seconds=0),
    )


def _drive(runtime: AgentRuntimeLoop, task_id: str, limit: int = 20) -> dict[str, Any]:
    result = runtime.snapshot(task_id)
    for _ in range(limit):
        if result["status"] in {"completed", "waiting_human", "failed", "cancelled"}:
            return result
        result = runtime.advance(task_id)
    raise AssertionError(f"runtime did not settle: {result}")


def test_long_task_automatically_continues_after_first_turn(tmp_path: Path) -> None:
    def receipt(state: TaskState, iteration: int) -> dict[str, Any]:
        if iteration == 1:
            return {
                "status": "success",
                "summary": "analysis only",
                "completed_items": ["analyze_objective"],
                "objective_complete": False,
                "evidence": [],
            }
        return {
            "status": "success",
            "summary": "remaining work verified",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [],
        }

    fake = FakeTurns(receipt)
    runtime = _runtime(tmp_path, fake)
    created = runtime.create_task(
        task_id="long-task",
        objective="Analyze a complex issue and produce a verified report",
        workspace=tmp_path,
        write=False,
    )
    assert created["status"] == "running"

    final = _drive(runtime, "long-task")

    assert final["status"] == "completed"
    assert final["iteration"] == 2
    assert fake.spawned_task_ids == ["long-task", "long-task"]
    assert "PREVIOUS TURN OUTPUT TAIL" in fake.prompts[1]
    assert any(event["event"] == "continuation_scheduled" for event in final["recent_events"])


def test_validator_rejects_claim_without_test_and_build_evidence(tmp_path: Path) -> None:
    executable = tmp_path / "MCPDevBridge.exe"
    executable.write_bytes(b"release")

    def receipt(state: TaskState, iteration: int) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = [
            {"kind": "test", "exit_code": 1},
            {"kind": "build", "exit_code": 1},
        ]
        if iteration > 1:
            evidence = [
                {"kind": "test", "exit_code": 0},
                {"kind": "build", "exit_code": 0},
                {"kind": "exe_exists", "path": str(executable), "ok": True},
                {"kind": "service_running", "ok": True},
            ]
        return {
            "status": "success",
            "summary": "claimed complete",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": evidence,
        }

    fake = FakeTurns(receipt)
    runtime = _runtime(tmp_path, fake)
    runtime.create_task(
        task_id="validator-task",
        objective="测试、build 并发布桌面程序",
        workspace=tmp_path,
        write=False,
    )

    first = runtime.advance("validator-task")
    assert first["status"] == "queued"
    assert "missing successful test evidence" in first["validation"]["failures"]

    final = _drive(runtime, "validator-task")
    assert final["status"] == "completed"
    assert final["validation"]["checks"]["tests_passed"] is True
    assert final["validation"]["checks"]["build_passed"] is True


def test_restart_resumes_interrupted_turn_with_same_task_id(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    old_turns: dict[str, dict[str, Any]] = {
        "old-turn": {"id": "old-turn", "state": "running", "executor": "opencode"}
    }

    def first_spawn(_state: TaskState, _prompt: str) -> dict[str, Any]:
        return old_turns["old-turn"]

    first = AgentRuntimeLoop(
        root,
        turn_spawner=first_spawn,
        turn_getter=lambda turn_id: old_turns[turn_id],
        retry_policy=RetryPolicy(base_delay_seconds=0),
    )
    first.create_task(
        task_id="restart-task",
        objective="finish after process recovery",
        workspace=tmp_path,
        write=False,
    )

    fake = FakeTurns(
        lambda state, _iteration: {
            "status": "success",
            "summary": "resumed",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [],
        }
    )

    def resumed_get(turn_id: str) -> dict[str, Any]:
        if turn_id == "old-turn":
            return {"id": turn_id, "state": "interrupted", "error": "process exited"}
        return fake.get(turn_id)

    resumed = AgentRuntimeLoop(
        root,
        turn_spawner=fake.spawn,
        turn_getter=resumed_get,
        retry_policy=RetryPolicy(base_delay_seconds=0),
    )
    resumed.resume_incomplete()
    final = _drive(resumed, "restart-task")

    assert final["task_id"] == "restart-task"
    assert final["status"] == "completed"
    assert final["turn_ids"] == ["old-turn", "turn-1"]
    assert any(event["event"] == "restart_detected" for event in final["recent_events"])


def test_checkpoint_restores_completed_items_and_previous_output(tmp_path: Path) -> None:
    fake = FakeTurns(
        lambda state, _iteration: {
            "status": "success",
            "summary": "partial",
            "completed_items": ["analyze_objective"],
            "objective_complete": False,
            "evidence": [],
        }
    )
    runtime = _runtime(tmp_path, fake)
    runtime.create_task(
        task_id="checkpoint-task",
        objective="analyze and verify",
        workspace=tmp_path,
        write=False,
    )
    partial = runtime.advance("checkpoint-task")
    checkpoint = Path(partial["last_checkpoint"])
    assert checkpoint.is_file()
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data["agent_output_summary"] == "partial"

    resumed_fake = FakeTurns(
        lambda state, _iteration: {
            "status": "success",
            "summary": "done",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [],
        },
        prefix="resumed-turn",
    )
    resumed = AgentRuntimeLoop(
        tmp_path / "runtime",
        turn_spawner=resumed_fake.spawn,
        turn_getter=resumed_fake.get,
        retry_policy=RetryPolicy(base_delay_seconds=0),
    )
    resumed.resume_incomplete()
    final = _drive(resumed, "checkpoint-task")

    assert final["status"] == "completed"
    assert "analyze_objective" in final["completed_items"]
    assert 'COMPLETED ITEMS: ["analyze_objective"]' in resumed_fake.prompts[0]
    assert "output from turn-1" in resumed_fake.prompts[0]


def test_executor_failure_retries_with_backoff_and_recovers(tmp_path: Path) -> None:
    fake = FakeTurns(
        lambda state, _iteration: {
            "status": "success",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [],
        }
    )
    original_spawn = fake.spawn

    def spawn(state: TaskState, prompt: str) -> dict[str, Any]:
        turn = original_spawn(state, prompt)
        if len(fake.prompts) <= 2:
            turn["state"] = "failed"
            turn["completion_verified"] = False
            turn["error"] = "temporary provider failure"
        return turn

    fake.spawn = spawn  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, fake)
    runtime.create_task(
        task_id="retry-task",
        objective="retry transient model errors",
        workspace=tmp_path,
        write=False,
    )

    final = _drive(runtime, "retry-task")

    assert final["status"] == "completed"
    assert final["iteration"] == 3
    retry_events = [event for event in final["recent_events"] if event["event"] == "retry_scheduled"]
    assert len(retry_events) == 2


def test_full_release_flow_requires_all_release_evidence(tmp_path: Path) -> None:
    executable = tmp_path / "MCPDevBridge.exe"
    executable.write_bytes(b"production")

    def receipt(state: TaskState, _iteration: int) -> dict[str, Any]:
        return {
            "status": "success",
            "summary": "release verified",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [
                {"kind": "test", "command": "pytest", "exit_code": 0},
                {"kind": "build", "command": "build.ps1", "exit_code": 0},
                {"kind": "exe_exists", "path": str(executable), "ok": True},
                {"kind": "service_running", "ok": True},
                {"kind": "mcp_connected", "ok": True},
                {"kind": "commit", "hash": "abc123", "ok": True},
                {"kind": "push", "remote": "origin/master", "ok": True},
            ],
        }

    fake = FakeTurns(receipt)
    runtime = _runtime(tmp_path, fake)
    runtime.create_task(
        task_id="release-task",
        objective=(
            "修改 Persistent Agent Runtime 架构，运行 pytest 测试，build 安装包并 release，"
            "替换程序、重启 MCP，commit 后 push origin master"
        ),
        workspace=tmp_path,
        write=True,
    )
    final = _drive(runtime, "release-task")

    assert final["status"] == "completed"
    expected = {
        "modify_architecture",
        "run_tests",
        "build_artifacts",
        "replace_program",
        "restart_service",
        "validate_mcp_connection",
        "commit_changes",
        "push_changes",
    }
    assert expected.issubset(set(final["checklist"]))
    assert all(final["validation"]["checks"].values())


def test_waiting_human_resumes_same_task_instead_of_creating_new_one(tmp_path: Path) -> None:
    fake = FakeTurns(
        lambda state, _iteration: {
            "status": "success",
            "completed_items": state.checklist,
            "objective_complete": True,
            "evidence": [],
        }
    )
    original_spawn = fake.spawn

    def spawn(state: TaskState, prompt: str) -> dict[str, Any]:
        turn = original_spawn(state, prompt)
        if len(fake.prompts) <= 3:
            turn.update(state="failed", completion_verified=False, error="credential required")
        return turn

    fake.spawn = spawn  # type: ignore[method-assign]
    runtime = _runtime(tmp_path, fake)
    runtime.create_task(
        task_id="human-task",
        objective="complete after credentials arrive",
        workspace=tmp_path,
        write=False,
    )
    waiting = _drive(runtime, "human-task")
    assert waiting["status"] == "waiting_human"

    runtime.add_instruction("human-task", "credentials are now configured")
    final = _drive(runtime, "human-task")

    assert final["task_id"] == "human-task"
    assert final["status"] == "completed"
    assert final["iteration"] == 4
    assert "NEW MESSAGE" in fake.prompts[-1]
