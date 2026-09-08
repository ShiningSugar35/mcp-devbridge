"""Repeatable CPU/allocation benchmark for the immutable Hub catalog (no network)."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from local_dev_mcp_bridge import gateway as gm  # noqa: E402


def measure(count: int) -> dict[str, object]:
    summary = getattr(gm, "_STABLE_HUB_TOOLS_SUMMARY", None)

    def operation(index: int) -> bytes:
        payload = gm._stable_tools_list_payload(index)
        actual = summary if summary is not None else gm._tools_response_summary(payload)
        assert actual["schema_fingerprint"] == gm.HUB_TOOL_CONTRACT_FINGERPRINT
        return payload

    first = operation(0)
    gc.collect()
    wall, cpu = time.perf_counter_ns(), time.process_time_ns()
    for index in range(count):
        operation(index)
    cpu_ms = (time.process_time_ns() - cpu) / 1_000_000
    wall_ms = (time.perf_counter_ns() - wall) / 1_000_000
    gc.collect()
    tracemalloc.start()
    operation(0)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "iterations": count,
        "cached_summary": summary is not None,
        "cpu_ms": cpu_ms,
        "wall_ms": wall_ms,
        "single_peak_bytes": peak,
        "wire_bytes": len(first),
        "tool_count": len(json.loads(first)["result"]["tools"]),
        "fingerprint": gm.HUB_TOOL_CONTRACT_FINGERPRINT,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.iterations <= 100_000:
        parser.error("iterations must be between 1 and 100000")
    result = measure(args.iterations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
