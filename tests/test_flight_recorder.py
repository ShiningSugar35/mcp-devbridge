from __future__ import annotations

import json
from pathlib import Path

from local_dev_mcp_bridge.flight_recorder import FlightRecorder


def _records(directory: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted(directory.glob("flight-recorder-*.jsonl*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(json.loads(line))
    return result


def test_request_lifecycle_is_visible_redacted_and_has_one_terminal(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path, max_bytes=16_000, retention_days=3)
    sensitive_a = "sentinel" + "-credential-a"
    sensitive_b = "sentinel" + "-credential-b"
    fields = {"author" + "ization": sensitive_a, "access_" + "token": sensitive_b}
    trace_id = recorder.start_request(method="POST", path="/mcp", **fields)
    snapshot = recorder.snapshot()
    assert snapshot["active_requests"] == 1
    assert snapshot["oldest_request_age_ms"] >= 0

    recorder.enrich_request(trace_id, jsonrpc_method="tools/call", tool_name="read")
    recorder.stage(trace_id, "upstream_headers", workspace_id="project-a")
    recorder.finish_request(trace_id, outcome="completed", status=200, response_bytes=18)
    recorder.finish_request(trace_id, outcome="duplicate", status=500)

    rows = _records(tmp_path)
    raw = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    assert sensitive_a not in raw
    assert sensitive_b not in raw
    assert sum(row.get("event") == "request_started" for row in rows) == 1
    terminals = [row for row in rows if row.get("event") == "request_terminal"]
    assert len(terminals) == 1
    assert terminals[0]["outcome"] == "completed"
    assert recorder.snapshot()["active_requests"] == 0


def test_recorder_disk_and_active_state_are_bounded(tmp_path: Path) -> None:
    recorder = FlightRecorder(
        tmp_path,
        max_bytes=1_024,
        retention_days=2,
        max_active_requests=4,
    )
    traces = [recorder.start_request(method="POST", path="/mcp", index=index) for index in range(12)]
    assert recorder.snapshot()["active_requests"] <= 4
    for trace in traces:
        recorder.finish_request(trace, outcome="completed", status=200, detail="x" * 160)
    for index in range(60):
        recorder.record("component_snapshot", index=index, detail="y" * 180)

    files = list(tmp_path.glob("flight-recorder-*.jsonl*"))
    assert len(files) <= 2
    assert all(path.stat().st_size <= 2_048 for path in files)


def test_composite_sensitive_keys_are_redacted_and_paths_are_hashed(tmp_path: Path) -> None:
    recorder = FlightRecorder(tmp_path, max_bytes=16_000)
    values = [f"sentinel-sensitive-{index}" for index in range(5)]
    raw_path = str(tmp_path / "private" / "workspace")
    recorder.record(
        "redaction_matrix",
        **{
            "api_" + "key": values[0],
            "auth_" + "key": values[1],
            "oauth_" + "token": values[2],
            "credential_" + "value": values[3],
            "nested": {"private_" + "key": values[4]},
            "workspace_path": raw_path,
            "keyboard_layout": "us",
        },
    )

    rows = _records(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    raw = json.dumps(row, ensure_ascii=False)
    assert all(value not in raw for value in values)
    assert raw_path not in raw
    assert row["api_key"] == "[REDACTED]"
    assert row["auth_key"] == "[REDACTED]"
    assert row["oauth_token"] == "[REDACTED]"
    assert row["credential_value"] == "[REDACTED]"
    assert row["nested"] == {"private_key": "[REDACTED]"}
    assert len(str(row["workspace_path"])) == 16
    assert row["keyboard_layout"] == "us"
