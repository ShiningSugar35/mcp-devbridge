"""Long-running managed process registry (backend-side)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import MAX_PROCESS_LOG_BYTES, PROCESS_LOG_DIR
from .platform_support import popen_platform_kwargs
from .shell import kill_process_tree

MAX_POLL_DEFAULT = 4000


@dataclass
class ManagedProcess:
    process_id: str
    pid: int
    label: str
    cwd: str
    started_at: str
    log_file: str
    status: str
    executable: str
    args: list[str] = field(default_factory=list)
    poll_offset: int = 0


class ProcessRegistry:
    """In-memory registry of processes started through MCP tools."""

    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = Path(log_dir or PROCESS_LOG_DIR)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, ManagedProcess] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def start(
        self,
        executable: str,
        args: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        label: str = "",
    ) -> ManagedProcess:
        environment = dict(os.environ)
        if env:
            environment.update(env)
        proc = subprocess.Popen(
            [executable, *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=environment,
            **popen_platform_kwargs(new_session=True),
        )
        process_id = uuid.uuid4().hex[:12]
        log_file = self.log_dir / f"{process_id}.log"
        record = ManagedProcess(
            process_id=process_id,
            pid=proc.pid,
            label=label or Path(executable).name,
            cwd=str(cwd),
            started_at=datetime.now().isoformat(timespec="seconds"),
            log_file=str(log_file),
            status="running",
            executable=executable,
            args=list(args),
        )
        with self._lock:
            self._processes[process_id] = record
            self._procs[process_id] = proc
        threading.Thread(target=self._tail, args=(process_id,), daemon=True).start()
        return record

    def _tail(self, process_id: str) -> None:
        """Drain stdout into a capped log file."""
        proc = self._procs.get(process_id)
        if proc is None or proc.stdout is None:
            return
        try:
            with self.log_dir.joinpath(f"{process_id}.log").open("ab") as handle:
                while True:
                    chunk = proc.stdout.readline()
                    if not chunk:
                        break
                    if handle.tell() > MAX_PROCESS_LOG_BYTES:
                        continue
                    handle.write(chunk)
                    handle.flush()
        except Exception:
            pass
        finally:
            self._mark_finished(process_id)

    def _mark_finished(self, process_id: str) -> None:
        with self._lock:
            record = self._processes.get(process_id)
            if record and record.status == "running":
                record.status = "exited"

    def get(self, process_id: str) -> ManagedProcess | None:
        with self._lock:
            return self._processes.get(process_id)

    def poll(self, process_id: str, max_chars: int = MAX_POLL_DEFAULT) -> dict[str, Any] | None:
        record = self.get(process_id)
        if record is None:
            return None
        path = Path(record.log_file)
        try:
            with path.open("rb") as handle:
                if record.poll_offset:
                    handle.seek(record.poll_offset)
                chunk = handle.read()
                record.poll_offset = handle.tell()
            if max_chars > 0 and len(chunk) > max_chars:
                truncated = True
                chunk = chunk[-max_chars:]
            else:
                truncated = False
            text = chunk.decode("utf-8", errors="replace")
        except OSError:
            text, truncated = "", False
        alive = self.is_alive(process_id)
        if not alive and record.status == "running":
            record.status = "exited"
        return {
            "process_id": process_id,
            "pid": record.pid,
            "status": record.status,
            "running": alive,
            "new_output": text,
            "output_truncated": truncated,
        }

    def is_alive(self, process_id: str) -> bool:
        proc = self._procs.get(process_id)
        if proc is None:
            return False
        return proc.poll() is None

    def stop(self, process_id: str, force: bool = False) -> dict[str, Any]:
        record = self.get(process_id)
        if record is None:
            return {"process_id": process_id, "stopped": False, "reason": "not_found"}
        proc = self._procs.get(process_id)
        stopped = False
        if proc is not None and proc.poll() is None:
            if force:
                stopped = kill_process_tree(proc.pid)
            else:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                    stopped = True
                except subprocess.TimeoutExpired:
                    stopped = kill_process_tree(proc.pid)
            record.status = "stopped" if stopped else "stopping"
        elif proc is not None:
            stopped = True
            record.status = "exited"
        return {
            "process_id": process_id,
            "pid": record.pid,
            "stopped": stopped or record.status in ("stopped", "exited"),
            "status": record.status,
        }

    def stop_all(self, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._processes.keys())
        results = [self.stop(process_id, force=force) for process_id in ids]
        return results

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [record for record in self._processes.values()]
        result = []
        for record in records:
            proc = self._procs.get(record.process_id)
            alive = proc is not None and proc.poll() is None
            if not alive and record.status == "running":
                record.status = "exited"
            result.append(
                {
                    "process_id": record.process_id,
                    "pid": record.pid,
                    "label": record.label,
                    "cwd": record.cwd,
                    "started_at": record.started_at,
                    "status": record.status,
                    "running": alive,
                    "log_file": record.log_file,
                }
            )
        return result


__all__ = ["ProcessRegistry", "ManagedProcess"]