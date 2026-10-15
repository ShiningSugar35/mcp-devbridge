"""Native packaging probes may precede full tests; release archives may not."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "macos_builder", Path(__file__).resolve().parents[1] / "scripts" / "build_macos.py"
)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


@pytest.fixture
def build_case(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="0.9.0"\n', encoding="utf-8")
    calls = []
    options = {"fail_tests": False}
    monkeypatch.setattr(BUILDER, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "argv", ["build_macos.py", "--version", "0.9.0"])
    monkeypatch.setattr(BUILDER.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(BUILDER.shutil, "which", lambda _: "npm")
    monkeypatch.setattr(BUILDER, "prepare_runtimes", lambda *args: tmp_path / "runtime")

    def output(args, **kwargs):
        return "a" * 40 if args[1] == "rev-parse" else ""

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        if "PyInstaller" in args:
            output_dir = Path(args[args.index("--distpath") + 1])
            executable = output_dir / "MCPDevBridge.app" / "Contents" / "MacOS" / "MCPDevBridge"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"simulated package")
        if "--macos-self-test" in args:
            env = kwargs["env"]
            assert env["MCPDEVBRIDGE_ISOLATED_SELFTEST"] == "1"
            report = Path(env["MCPDEVBRIDGE_SELFTEST_REPORT"])
            report.write_text(json.dumps({"architecture": "arm64", "mcp": "passed", "qt": "passed", "private_runtimes": "passed", "read_only_call": "passed", "tool_count": 41}), encoding="utf-8")
        if "pytest" in args and options["fail_tests"]:
            raise subprocess.CalledProcessError(1, args)
        if args[0] == "/usr/bin/ditto":
            Path(args[-1]).write_bytes(b"simulated archive")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(BUILDER.subprocess, "check_output", output)
    monkeypatch.setattr(BUILDER.subprocess, "run", execute)
    return tmp_path, calls, options


def test_native_probe_and_all_gates_precede_release_archive(build_case):
    root, calls, _options = build_case
    assert BUILDER.main() == 0
    commands = [command for command, _kwargs in calls]
    smoke = next(i for i, command in enumerate(commands) if "--macos-self-test" in command)
    tests = next(i for i, command in enumerate(commands) if "pytest" in command)
    archive = next(i for i, command in enumerate(commands) if command[0] == "/usr/bin/ditto")
    assert smoke < tests < archive
    for gate in ("pytest", "ruff", "pyright"):
        assert any(gate in command for command in commands[:archive])
    assert any(command == ["npm", "run", "smoke"] for command in commands[:archive])
    report = json.loads((root / ".ai-bridge" / "macos-build" / "native-check-arm64.json").read_text(encoding="utf-8"))
    assert report["release_eligible"] is False
    assert report["scope"] == "native_frozen_probe"
    assert report["source_commit"] == "a" * 40


def test_failed_full_gate_keeps_probe_receipt_but_never_emits_release(build_case):
    root, calls, options = build_case
    options["fail_tests"] = True
    with pytest.raises(subprocess.CalledProcessError):
        BUILDER.main()
    assert (root / ".ai-bridge" / "macos-build" / "native-check-arm64.json").is_file()
    assert not any(command[0] == "/usr/bin/ditto" for command, _kwargs in calls)
    assert not (root / "release").exists()
