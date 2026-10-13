import hashlib
import json

import pytest

from local_dev_mcp_bridge import relay_control as rc


def controller(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    r = rc.RelayController()
    r.directory.mkdir()
    (r.directory / "relay-supervisor.ps1").write_bytes(b"fixture")
    monkeypatch.setattr(type(r), "provisioned", property(lambda self: True))
    return r


def test_unrecognized_task_rejected_before_any_remote_or_file_mutation(tmp_path, monkeypatch):
    r = controller(tmp_path, monkeypatch)
    def refuse(action):
        raise RuntimeError("unrecognized task")
    monkeypatch.setattr(r, "_task", refuse)
    monkeypatch.setattr(r, "_remote", lambda *args: pytest.fail("remote changed before task identity check"))
    with pytest.raises(RuntimeError):
        r.stop()
    assert not (r.directory / "stop.request").exists()


def test_missing_script_in_existing_deployment_is_not_absent_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    r = rc.RelayController()
    r.directory.mkdir()
    assert r.configured and not r.provisioned
    with pytest.raises(RuntimeError):
        r.stop()
    assert not (r.directory / "stop.request").exists()


def test_remote_stop_failure_keeps_forward_and_does_not_record_disabled(tmp_path, monkeypatch):
    r = controller(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_task", lambda action: None if action == "check" else pytest.fail("working forward stopped"))
    def unreachable(start):
        raise RuntimeError("unreachable")
    monkeypatch.setattr(r, "_remote", unreachable)
    with pytest.raises(RuntimeError):
        r.stop()
    assert not (r.directory / "mode.json").exists()
    assert not (r.directory / "stop.request").exists()


def test_confirmed_disabled_marker_makes_direct_restart_independent_of_ssh(tmp_path, monkeypatch):
    r = controller(tmp_path, monkeypatch)
    r._mark(True)
    monkeypatch.setattr(r, "_task", lambda action: None)
    monkeypatch.setattr(r, "_remote", lambda *args: pytest.fail("disabled proof discarded"))
    r.stop()
    assert (r.directory / "stop.request").exists()


def test_failed_remote_activation_cannot_leave_disabled_proof(tmp_path, monkeypatch):
    r = controller(tmp_path, monkeypatch)
    r._mark(True)
    monkeypatch.setattr(r, "_task", lambda action: None)
    monkeypatch.setattr(type(r), "is_running", property(lambda self: True))
    def refuse(start):
        raise RuntimeError("unit identity mismatch")
    monkeypatch.setattr(r, "_remote", refuse)
    with pytest.raises(RuntimeError):
        r.start()
    assert json.loads((r.directory / "mode.json").read_text())["remote_disabled"] is False


def test_provisioning_guard_pins_script_and_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path))
    r = rc.RelayController()
    r.directory.mkdir()
    (r.directory / "relay-supervisor.ps1").write_bytes(b"fixture")
    monkeypatch.setattr(rc, "SUPERVISOR_SHA256", hashlib.sha256(b"fixture").hexdigest())
    monkeypatch.setattr(rc.sys, "platform", "linux")
    assert not r.provisioned
    monkeypatch.setattr(rc.sys, "platform", "win32")
    assert r.provisioned
    (r.directory / "relay-supervisor.ps1").write_bytes(b"changed")
    assert not r.provisioned
    assert r.configured
    with pytest.raises(RuntimeError):
        r.stop()
