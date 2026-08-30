"""Self-test (selftest.py) against a real backend subprocess."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from local_dev_mcp_bridge.models import RuntimeConfig
from local_dev_mcp_bridge.selftest import run_selftest


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def backend_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("LOCALDEV_MCP_NO_CREDENTIAL_MANAGER", "1")
    workspace = tmp_path / "工作区"
    workspace.mkdir()
    rc = RuntimeConfig(workspace=str(workspace), log_dir=str(tmp_path / "logs"))
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(rc.model_dump(), ensure_ascii=False), encoding="utf-8")

    port = _free_port()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_dev_mcp_bridge.server_main", "--config", str(config_path), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            assert proc.stdout is not None
            raise RuntimeError(f"backend exited early:\n{proc.stdout.read()}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.2)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def test_selftest_legacy_backend_fails_closed_without_current_canary(backend_url: str) -> None:
    result = run_selftest(backend_url)
    assert not result.ok
    step_names = [s["step"] for s in result.steps]
    assert "initialize" in step_names
    assert "streamable_http" in step_names
    assert "list_tools" in step_names
    assert "read_only_tool" in step_names
    assert "get_workspace_info" not in step_names
    assert "get_capabilities" not in step_names
    assert "list_directory" not in step_names
    assert "只读" in result.error


def test_selftest_wrong_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    result = run_selftest("http://127.0.0.1:1/mcp", timeout=10)
    assert not result.ok
    assert result.error != ""