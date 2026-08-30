from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import local_dev_mcp_bridge.flight_recorder as recorder_module
from local_dev_mcp_bridge.flight_recorder import FlightRecorder


def _rows(log_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(log_dir.glob("flight-recorder-*.jsonl*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                assert isinstance(value, dict)
                rows.append(value)
    return rows


def test_flight_recorder_redacts_sensitive_values_and_hashes_identity(
    tmp_path: Path,
) -> None:
    sensitive_value = "[REDACTED_SECRET]-" + "A" * 32
    recorder = FlightRecorder(tmp_path / "logs")

    recorder.record(
        "probe",
        authorization=f"Bearer {sensitive_value}",
        access_token=sensitive_value,
        client_secret=sensitive_value,
        workspace_id="ws_private_workspace",
        project_id="project-private",
        root=r"D:\private\repo",
        detail=(
            f"Bearer {sensitive_value} "
            f"token={sensitive_value} password={sensitive_value}"
        ),
    )

    raw = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "logs").glob("flight-recorder-*.jsonl*")
    )
    assert sensitive_value not in raw
    assert "ws_private_workspace" not in raw
    assert "project-private" not in raw
    assert r"D:\private\repo" not in raw

    row = _rows(tmp_path / "logs")[0]
    assert row["authorization"] == "[REDACTED]"
    assert row["access_token"] == "[REDACTED]"
    assert row["client_secret"] == "[REDACTED]"
    assert len(str(row["workspace_id"])) == 16
    assert len(str(row["project_id"])) == 16
    assert len(str(row["root"])) == 16
    assert "Bearer ***" in str(row["detail"])


def test_request_terminal_is_once_only_and_snapshot_is_bounded(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path / "logs", max_active_requests=2)
    first = recorder.start_request(method="POST", path="/mcp", workspace_id="ws_one")
    recorder.stage(first, "upstream_headers", status=200)

    assert recorder.finish_request(first, outcome="completed", status=200)
    assert not recorder.finish_request(first, outcome="duplicate", status=500)
    assert recorder.snapshot() == {
        "active_requests": 0,
        "oldest_request_age_ms": 0,
        "tracked_terminals": 1,
    }

    terminal_rows = [
        row
        for row in _rows(tmp_path / "logs")
        if row.get("event") == "request_terminal" and row.get("trace_id") == first
    ]
    assert len(terminal_rows) == 1
    assert terminal_rows[0]["outcome"] == "completed"


def test_active_and_terminal_maps_have_hard_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recorder_module, "_DEFAULT_MAX_TERMINALS", 2)
    recorder = FlightRecorder(tmp_path / "logs", max_active_requests=2)

    first = recorder.start_request(method="POST", path="/first")
    second = recorder.start_request(method="POST", path="/second")
    third = recorder.start_request(method="POST", path="/third")

    snapshot = recorder.snapshot()
    assert snapshot["active_requests"] == 2
    assert snapshot["tracked_terminals"] == 1
    assert not recorder.finish_request(first, outcome="late")
    assert recorder.finish_request(second, outcome="completed")
    assert recorder.finish_request(third, outcome="completed")
    assert recorder.snapshot()["tracked_terminals"] == 2


def test_disk_rotation_retention_and_oversize_fallback_are_bounded(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "flight-recorder-2000-01-01.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    old = time.time() - 20 * 86_400
    os.utime(stale, (old, old))

    recorder = FlightRecorder(log_dir, max_bytes=1_024, retention_days=2)
    recorder.record("oversize", **{f"field_{index}": "x" * 2_000 for index in range(48)})
    assert any(row.get("event") == "oversize_diagnostic_dropped" for row in _rows(log_dir))

    for index in range(20):
        recorder.record("rotation", index=index, detail="y" * 500)

    assert not stale.exists()
    paths = list(log_dir.glob("flight-recorder-*.jsonl*"))
    assert 1 <= len(paths) <= 2
    assert all(path.stat().st_size <= 1_024 for path in paths)


def test_write_failures_are_fail_open(tmp_path: Path) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    recorder = FlightRecorder(blocked)

    recorder.record("probe", detail="must not raise")
    trace_id = recorder.start_request(method="POST", path="/mcp")
    recorder.stage(trace_id, "upstream")
    assert recorder.finish_request(trace_id, outcome="completed")
    assert recorder.snapshot()["active_requests"] == 0
