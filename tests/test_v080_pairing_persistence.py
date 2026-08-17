from __future__ import annotations

from pathlib import Path

import pytest

from local_dev_mcp_bridge.device_hub import PAIR_RECEIPT_TTL_SECONDS, DeviceRegistry


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def test_pair_receipt_is_valid_for_1800_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore()
    now = [1_000_000.0]
    monkeypatch.setattr("local_dev_mcp_bridge.device_hub.time.time", lambda: now[0])
    monkeypatch.setattr("local_dev_mcp_bridge.device_hub.load_devices", lambda: [])
    monkeypatch.setattr("local_dev_mcp_bridge.device_hub.upsert_device", lambda _device: None)
    registry = DeviceRegistry(local_device_id="main-pc", local_device_name="Main", store=store)
    code, _expires = registry.generate_pair_code()
    kwargs = {
        "pair_code": code,
        "device_id": "friend-pc",
        "name": "Friend",
        "endpoint_url": "https://friend.example/mcp",
        "bearer": "b" * 32,
    }
    first = registry.register_remote(**kwargs)
    now[0] += PAIR_RECEIPT_TTL_SECONDS - 1
    assert registry.register_remote(**kwargs) == first
    now[0] += 2
    with pytest.raises(ValueError, match="配对码无效或已过期"):
        registry.register_remote(**kwargs)


def test_pairing_credentials_survive_registry_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOCALDEV_MCP_CONFIG_DIR", str(tmp_path / "cfg"))
    store = MemoryStore()
    registry = DeviceRegistry(
        local_device_id="hub-pc",
        local_device_name="Hub",
        store=store,
        online_ttl_seconds=999,
    )
    code, _expires = registry.generate_pair_code()
    peer = registry.register_remote(
        pair_code=code,
        device_id="friend-pc",
        name="Friend",
        endpoint_url="https://friend.example/mcp",
        bearer="b" * 32,
    )

    restarted = DeviceRegistry(
        local_device_id="hub-pc",
        local_device_name="Hub",
        store=store,
        online_ttl_seconds=999,
    )
    assert restarted.get_peer_secret("friend-pc") == peer
    assert restarted.resolve_remote("friend-pc") is not None
    restarted.heartbeat(
        device_id="friend-pc",
        peer_secret=peer,
        endpoint_url="https://friend-after-restart.example/mcp",
    )
    target = restarted.resolve_remote("friend-pc")
    assert target is not None
    assert target.base_url == "https://friend-after-restart.example"
