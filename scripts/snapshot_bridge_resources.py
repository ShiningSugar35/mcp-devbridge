"""Measure installed bridge processes without reading command lines or credentials."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import psutil

_NAMES = {"mcpdevbridge.exe", "mcpdevbridge", "node.exe", "node", "cloudflared.exe", "cloudflared"}


def snapshot(root: Path, seconds: float) -> dict[str, object]:
    root = root.resolve()
    selected: list[tuple[psutil.Process, float]] = []
    errors: list[dict[str, object]] = []
    for process in psutil.process_iter(["name", "exe"]):
        executable = process.info.get("exe")
        if not executable or str(process.info.get("name", "")).casefold() not in _NAMES:
            continue
        try:
            if not Path(executable).resolve().is_relative_to(root):
                continue
            selected.append((process, sum(process.cpu_times()[:2])))
        except (OSError, psutil.Error) as exc:
            errors.append({"pid": process.pid, "error": type(exc).__name__})
    start = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - start
    logical_cpus = psutil.cpu_count() or 1
    rows: list[dict[str, object]] = []
    for process, before in selected:
        try:
            with process.oneshot():
                cpu_one_core = max(0.0, sum(process.cpu_times()[:2]) - before) / elapsed * 100
                rows.append(
                    {
                        "pid": process.pid,
                        "ppid": process.ppid(),
                        "name": process.name(),
                        "rss_mib": round(process.memory_info().rss / 1024**2, 3),
                        "cpu_percent_one_core": round(cpu_one_core, 4),
                        "cpu_percent_machine": round(cpu_one_core / logical_cpus, 4),
                        "threads": process.num_threads(),
                        "handles_or_fds": process.num_handles()
                        if hasattr(process, "num_handles")
                        else process.num_fds(),
                    }
                )
        except psutil.Error as exc:
            errors.append({"pid": process.pid, "error": type(exc).__name__})
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "install_root": str(root),
        "sample_seconds": round(elapsed, 4),
        "logical_cpus": logical_cpus,
        "processes": rows,
        "errors": errors,
        "scope": "Executable resides under install root; no unrelated process terminated. "
        "RSS includes shared pages; short CPU sample is not a leak or idle-load guarantee.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 0.1 <= args.seconds <= 10:
        parser.error("seconds must be between 0.1 and 10")
    result = snapshot(args.root, args.seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
