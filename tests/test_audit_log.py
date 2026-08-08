"""Phase 6: audit log query API + retention behavior (UI is smoke-tested)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from local_dev_mcp_bridge.audit import (
    AuditLogger,
    AuditQuery,
    available_tool_names,
    query_logs,
)


def _write_log(
    directory: Path,
    day: str,
    *,
    tools: list[str] | None = None,
    successes: list[bool] | None = None,
) -> None:
    tools = tools or ["file_read"]
    successes = successes or []
    path = directory / f"mcp-{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        for i, tool in enumerate(tools):
            ok = successes[i] if i < len(successes) else True
            record = {
                "timestamp": f"{day}T10:0{i}:00.000",
                "request_id": f"rid{i}",
                "client_name": "cli",
                "tool_name": tool,
                "parameter_summary": {"path": "/tmp/x.txt"},
                "workspace": "W",
                "permission_mode": "workspace",
                "duration_ms": 12,
                "success": ok,
                "exit_code": None,
                "error_type": None,
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestQueryLogs:
    def test_day_filter(self, tmp_path: Path) -> None:
        _write_log(tmp_path, "2026-08-06", tools=["file_read"])
        _write_log(tmp_path, "2026-08-08", tools=["git_status"])
        hits = query_logs(AuditQuery(day="2026-08-08"), directory=tmp_path)
        assert [r["tool_name"] for r in hits] == ["git_status"]

    def test_tool_and_success_filters(self, tmp_path: Path) -> None:
        _write_log(tmp_path, "2026-08-08", tools=["file_read", "git_status"], successes=[True, False])
        ok = query_logs(AuditQuery(tool_name="file_read", success=True), directory=tmp_path)
        assert len(ok) == 1 and ok[0]["tool_name"] == "file_read"
        bad = query_logs(AuditQuery(success=False), directory=tmp_path)
        assert len(bad) == 1 and bad[0]["tool_name"] == "git_status"

    def test_newest_first_and_limit(self, tmp_path: Path) -> None:
        _write_log(tmp_path, "2026-08-08", tools=["a", "b", "c"])
        records = query_logs(AuditQuery(limit=2), directory=tmp_path)
        assert len(records) == 2
        assert records[0]["tool_name"] == "c"

    def test_garbage_lines_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "mcp-2026-08-08.jsonl").write_text(
            "{not json\n{}\n[1]\n", encoding="utf-8"
        )
        assert query_logs(AuditQuery(day="2026-08-08"), directory=tmp_path) == []

    def test_available_tool_names(self, tmp_path: Path) -> None:
        _write_log(tmp_path, "2026-08-08", tools=["git_status", "file_read"])
        assert available_tool_names(tmp_path) == ["file_read", "git_status"]


class TestRetention:
    def test_old_jsonl_removed(self, tmp_path: Path) -> None:
        logger = AuditLogger(directory=tmp_path, retention_days=1)
        old = tmp_path / "mcp-2020-01-01.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        old_time = 1_500_000_000  # 2017
        os.utime(old, (old_time, old_time))
        logger._rotate_if_needed()
        assert not old.exists()

    def test_rotation_keeps_latest_two_when_over_cap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import local_dev_mcp_bridge.audit as audit_module

        monkeypatch.setattr(audit_module, "MAX_JSONL_BYTES", 200)
        logger = AuditLogger(directory=tmp_path, retention_days=365)
        for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
            (tmp_path / f"mcp-{day}.jsonl").write_text("x" * 120, encoding="utf-8")
        logger._rotate_if_needed()
        remaining = sorted(p.name for p in tmp_path.glob("mcp-*.jsonl"))
        assert remaining == ["mcp-2026-08-02.jsonl", "mcp-2026-08-03.jsonl"]

    def test_daily_write_touches_file(self, tmp_path: Path) -> None:
        logger = AuditLogger(directory=tmp_path, retention_days=14)
        logger.log_tool_call(
            request_id="r1",
            client_name="gui",
            tool_name="file_read",
            parameters={"path": "/tmp/a.txt"},
            workspace="W",
            permission_mode="workspace",
            duration_ms=5,
            success=True,
        )
        files = list(tmp_path.glob("mcp-*.jsonl"))
        assert len(files) == 1
        assert "file_read" in files[0].read_text(encoding="utf-8")

    def test_logger_redacts_content_params(self, tmp_path: Path) -> None:
        logger = AuditLogger(directory=tmp_path, retention_days=14)
        logger.log_tool_call(
            request_id="r",
            tool_name="file_write",
            parameters={"path": "/x", "content": "TOPSECRET", "api_key": "k-123"},
            client_name="gui",
            workspace="W",
            permission_mode="workspace",
            duration_ms=1,
            success=True,
        )
        data = next(tmp_path.glob("mcp-*.jsonl")).read_text(encoding="utf-8")
        assert "TOPSECRET" not in data
        assert "k-123" not in data
        assert "[redacted]" in data