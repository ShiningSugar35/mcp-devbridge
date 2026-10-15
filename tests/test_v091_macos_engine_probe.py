"""Packaged-engine probe must use its own files, process, port and workspace."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_dev_mcp_bridge import macos_package_probe as probe


@pytest.fixture
def layout(tmp_path):
    bundle = tmp_path / "MCPDevBridge.app"
    node = bundle / "Contents" / "Frameworks" / "runtime" / "node"
    dist = bundle / "Contents" / "Resources" / "third_party" / "codexpro" / "dist"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"fixture")
    dist.mkdir(parents=True)
    (dist / "http.js").write_text("fixture", encoding="utf-8")
    (dist.parent / "package.json").write_text("{}", encoding="utf-8")
    (dist.parent / "node_modules").mkdir()
    profile = tmp_path / "isolated-profile"
    return bundle, node, dist, profile


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(instances=[], fail_start=False, fail_probe=False)

    class Engine:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_running = False
            self.stopped = False
            state.instances.append(self)

        def start(self, root, credential, **kwargs):
            self.root = Path(root)
            self.settings = kwargs
            assert len(credential) >= 24
            self.is_running = True
            if state.fail_start:
                raise RuntimeError("private-error-fixture")

        def stop(self, timeout_seconds=5):
            self.is_running = False
            self.stopped = True

    async def check(_url, _credential):
        if state.fail_probe:
            raise RuntimeError("private-error-fixture")
        return {"mcp": "passed", "tool_count": 41, "read_only_call": "passed"}

    monkeypatch.setattr(probe, "CodexProManager", Engine)
    monkeypatch.setattr(probe, "_wait_for_listener", lambda _engine: None)
    monkeypatch.setattr(probe, "_check_protocol", check)
    return state


def test_success_checks_packaged_engine_and_removes_only_owned_workspace(layout, harness):
    bundle, node, dist, profile = layout
    profile.mkdir()
    retained = profile / "keep.txt"
    retained.write_text("retain", encoding="utf-8")
    result = probe.run_bundled_engine_probe(bundle, node, dist, profile)
    engine = harness.instances[0]
    assert result == {"mcp": "passed", "tool_count": 41, "read_only_call": "passed"}
    assert engine.settings["permission_mode"] == "read_only"
    assert engine.kwargs["dist_dir"] == dist.resolve()
    assert engine.kwargs["node_exe"] == str(node.resolve())
    assert 1024 < engine.kwargs["port"] < 65536
    assert engine.stopped and not engine.root.exists()
    assert retained.read_text(encoding="utf-8") == "retain"


@pytest.mark.parametrize("part", ["node", "http.js", "package.json", "node_modules"])
def test_missing_package_input_never_falls_back_to_system(layout, harness, part):
    bundle, node, dist, profile = layout
    target = {"node": node, "http.js": dist / "http.js", "package.json": dist.parent / "package.json", "node_modules": dist.parent / "node_modules"}[part]
    target.rmdir() if target.is_dir() else target.unlink()
    with pytest.raises(RuntimeError, match="package input"):
        probe.run_bundled_engine_probe(bundle, node, dist, profile)
    assert not harness.instances


def test_external_node_is_not_accepted_as_packaged_runtime(layout, harness, tmp_path):
    bundle, _node, dist, profile = layout
    external = tmp_path / "node"
    external.write_bytes(b"fixture")
    with pytest.raises(RuntimeError, match="outside"):
        probe.run_bundled_engine_probe(bundle, external, dist, profile)
    assert not harness.instances


@pytest.mark.parametrize("kind", ["fail_start", "fail_probe"])
def test_probe_failure_stops_owned_process_and_does_not_echo_secrets(layout, harness, kind):
    setattr(harness, kind, True)
    with pytest.raises(RuntimeError) as caught:
        probe.run_bundled_engine_probe(*layout)
    assert "private-error-fixture" not in str(caught.value)
    assert harness.instances[0].stopped
    assert not harness.instances[0].root.exists()


def test_profile_cannot_write_into_the_app(layout, harness):
    bundle, node, dist, _profile = layout
    with pytest.raises(RuntimeError, match="profile"):
        probe.run_bundled_engine_probe(bundle, node, dist, bundle / "profile")
    assert not harness.instances


@pytest.mark.parametrize("url", [
    "https://example.org/mcp", "http://127.0.0.1:80@example.org/mcp",
    "http://127.0.0.1:8123/mcp?x=1", "http://127.0.0.1/mcp",
    "http://127.0.0.1:8123/mcp#fragment", "http://127.0.0.1:0/mcp",
])
def test_unsafe_probe_url_is_rejected_before_http_client(url, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Network client must not be constructed")
    monkeypatch.setattr(probe.httpx, "AsyncClient", unexpected)
    with pytest.raises(ValueError):
        asyncio.run(probe._check_protocol(url, "fixture-only"))


def test_constructor_failure_also_removes_probe_workspace(layout, monkeypatch):
    def unexpected(**kwargs):
        raise RuntimeError("private-error-fixture")
    monkeypatch.setattr(probe, "CodexProManager", unexpected)
    with pytest.raises(RuntimeError) as caught:
        probe.run_bundled_engine_probe(*layout)
    assert "private-error-fixture" not in str(caught.value)
    assert list(layout[3].iterdir()) == []


def test_protocol_rejects_empty_tools_and_error_canary(monkeypatch):
    # This validates the result classifier without a network server or user data.
    for tools, result in [([], SimpleNamespace(is_error=False, content=[])),
                          ([SimpleNamespace(name="server_config")], SimpleNamespace(is_error=True, content=[SimpleNamespace(text="bad")]))]:
        with pytest.raises(RuntimeError):
            probe._checked_result(tools, result)
    result = probe._checked_result([SimpleNamespace(name="server_config")], SimpleNamespace(is_error=False, content=[SimpleNamespace(text="ok")]))
    assert result["read_only_call"] == "passed"
    assert "ok" not in json.dumps(result)


def test_native_self_test_requires_frozen_and_explicit_isolation(monkeypatch):
    from local_dev_mcp_bridge import macos_app
    monkeypatch.setattr(macos_app.sys, "platform", "darwin")
    monkeypatch.setattr(macos_app.sys, "frozen", False, raising=False)
    with pytest.raises(RuntimeError, match="frozen"):
        macos_app.self_test()
    monkeypatch.setattr(macos_app.sys, "frozen", True)
    monkeypatch.delenv("MCPDEVBRIDGE_ISOLATED_SELFTEST", raising=False)
    with pytest.raises(RuntimeError, match="isolated"):
        macos_app.self_test()


def test_native_self_test_wires_packaged_engine_and_writes_bounded_receipt(layout, monkeypatch):
    import shutil
    import subprocess
    import sys

    from local_dev_mcp_bridge import engines, macos_app

    bundle, node, dist, profile = layout
    report = profile.parent / "native-smoke.json"
    tunnel = node.parent / "cloudflared"
    tunnel.write_bytes(b"fixture")
    calls = []

    class Application:
        @staticmethod
        def instance():
            return None
        def __init__(self, _args):
            pass
        def processEvents(self):
            pass

    class Window:
        def setWindowTitle(self, _title):
            pass
        def show(self):
            pass
        def close(self):
            pass

    def checked(*args):
        calls.append(args)
        return {"mcp": "passed", "read_only_call": "passed", "tool_count": 41}

    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", SimpleNamespace(QApplication=Application, QWidget=Window))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle / "Contents" / "Resources"), raising=False)
    monkeypatch.setenv("MCPDEVBRIDGE_ISOLATED_SELFTEST", "1")
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(profile))
    monkeypatch.setenv("MCPDEVBRIDGE_SELFTEST_REPORT", str(report))
    monkeypatch.setattr(macos_app, "bundle_for_executable", lambda _: bundle)
    monkeypatch.setattr(macos_app, "architecture", lambda: "arm64")
    monkeypatch.setattr(engines, "find_node", lambda: str(node))
    monkeypatch.setattr(shutil, "which", lambda _: str(tunnel))
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "arm64")
    monkeypatch.setattr(probe, "run_bundled_engine_probe", checked)
    assert macos_app.self_test() == 0
    assert calls == [(bundle, node, dist, profile.resolve())]
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["mcp"] == "passed" and result["architecture"] == "arm64"
    assert "credential" not in result
