"""Multi-device registry and pairing support for MCP DevBridge Hub.

A Hub never connects to arbitrary LAN addresses. Remote DevBridge instances
register an already-public MCP endpoint (Named/Quick/ngrok) using a one-time
pairing code, then refresh it with authenticated heartbeats. Remote Bearer and
heartbeat secrets live in SecretsStore; devices.json contains only non-secret metadata.
"""

from __future__ import annotations

import hmac
import secrets as pysecrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from .config_store import delete_device, load_devices, upsert_device
from .models import DeviceConfig
from .secrets import SecretsStore, generate_token

PAIR_CODE_TTL_SECONDS = 10 * 60
REMOTE_ONLINE_TTL_SECONDS = 45
DEVICE_BEARER_PREFIX = "LocalDevMCPBridge/DeviceBearer:"
DEVICE_HEARTBEAT_PREFIX = "LocalDevMCPBridge/DeviceHeartbeat:"
HUB_PEER_SECRET_KEY = "LocalDevMCPBridge/HubPeerSecret"


class SecretStoreLike(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


@dataclass(frozen=True)
class DeviceView:
    id: str
    name: str
    endpoint_url: str
    online: bool
    local: bool
    last_seen: float


@dataclass(frozen=True)
class RemoteTarget:
    device_id: str
    name: str
    base_url: str
    bearer: str


def normalize_mcp_url(value: str) -> str:
    """Return a canonical MCP URL (https://host[/prefix]/mcp)."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("MCP 地址不能为空。")
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower()
    local_http = parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not local_http:
        raise ValueError("远程设备 MCP 地址必须使用 https://。")
    if not parsed.netloc:
        raise ValueError("MCP 地址格式不正确。")
    path = parsed.path.rstrip("/")
    if not path.endswith("/mcp"):
        path = f"{path}/mcp" if path else "/mcp"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def mcp_base_url(value: str) -> str:
    normalized = normalize_mcp_url(value)
    parsed = urlsplit(normalized)
    path = parsed.path[:-4].rstrip("/") if parsed.path.endswith("/mcp") else parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


class DeviceRegistry:
    """Thread-safe remote-device catalog plus one-time pairing codes."""

    def __init__(
        self,
        *,
        local_device_id: str,
        local_device_name: str,
        store: SecretStoreLike | None = None,
        online_ttl_seconds: float = REMOTE_ONLINE_TTL_SECONDS,
    ) -> None:
        self.local_device_id = local_device_id.strip()
        self.local_device_name = local_device_name.strip() or "本机"
        self.store = store or SecretsStore()
        self.online_ttl = max(5.0, float(online_ttl_seconds))
        self._pair_codes: dict[str, float] = {}
        self._lock = threading.RLock()
        self._paired_ids = {
            device.id
            for device in load_devices()
            if self.store.get(self._bearer_key(device.id))
            and self.store.get(self._heartbeat_key(device.id))
        }

    @staticmethod
    def _bearer_key(device_id: str) -> str:
        return f"{DEVICE_BEARER_PREFIX}{device_id}"

    @staticmethod
    def _heartbeat_key(device_id: str) -> str:
        return f"{DEVICE_HEARTBEAT_PREFIX}{device_id}"

    def set_local_identity(self, device_id: str, name: str) -> None:
        with self._lock:
            self.local_device_id = device_id.strip()
            self.local_device_name = name.strip() or "本机"

    def generate_pair_code(self, ttl_seconds: int = PAIR_CODE_TTL_SECONDS) -> tuple[str, float]:
        code = "".join(pysecrets.choice("0123456789") for _ in range(6))
        expires = time.time() + max(60, int(ttl_seconds))
        with self._lock:
            now = time.time()
            self._pair_codes = {k: v for k, v in self._pair_codes.items() if v > now}
            self._pair_codes[code] = expires
        return code, expires

    def _consume_pair_code(self, code: str) -> None:
        with self._lock:
            expires = self._pair_codes.pop(code.strip(), 0.0)
        if expires <= time.time():
            raise ValueError("配对码无效或已过期，请在 Hub 电脑重新生成。")

    def register_remote(
        self, *, pair_code: str, device_id: str, name: str, endpoint_url: str, bearer: str
    ) -> str:
        self._consume_pair_code(pair_code)
        device_id = device_id.strip()
        if not device_id:
            raise ValueError("远程设备缺少 device_id。")
        if device_id == self.local_device_id:
            raise ValueError("不能把本机重复注册为远程设备。")
        bearer = bearer.strip()
        if len(bearer) < 24:
            raise ValueError("远程设备访问令牌无效。")
        endpoint = normalize_mcp_url(endpoint_url)
        now = time.time()
        device = DeviceConfig(
            id=device_id,
            name=name.strip() or f"设备-{device_id[:6]}",
            endpoint_url=endpoint,
            last_seen=now,
            paired_at=now,
            enabled=True,
        )
        with self._lock:
            upsert_device(device)
            self.store.set(self._bearer_key(device_id), bearer)
            peer_secret = generate_token(256)
            self.store.set(self._heartbeat_key(device_id), peer_secret)
            self._paired_ids.add(device_id)
        return peer_secret

    def heartbeat(
        self,
        *,
        device_id: str,
        peer_secret: str,
        endpoint_url: str,
        name: str = "",
        bearer: str = "",
    ) -> DeviceConfig:
        device_id = device_id.strip()
        expected = self.store.get(self._heartbeat_key(device_id)) or ""
        if not expected or not hmac.compare_digest(expected, peer_secret.strip()):
            raise ValueError("设备心跳凭据无效，请重新配对。")
        device = next((d for d in load_devices() if d.id == device_id), None)
        if device is None:
            raise ValueError("Hub 中找不到这台设备，请重新配对。")
        device.endpoint_url = normalize_mcp_url(endpoint_url)
        device.last_seen = time.time()
        if name.strip():
            device.name = name.strip()
        device.enabled = True
        with self._lock:
            upsert_device(device)
            if bearer.strip():
                self.store.set(self._bearer_key(device_id), bearer.strip())
            self._paired_ids.add(device_id)
        return device

    def remove(self, device_id: str) -> None:
        if not device_id or device_id == self.local_device_id:
            return
        with self._lock:
            delete_device(device_id)
            self.store.delete(self._bearer_key(device_id))
            self.store.delete(self._heartbeat_key(device_id))
            self._paired_ids.discard(device_id)

    def remote_devices(self) -> list[DeviceConfig]:
        return load_devices()

    def _is_remote_online(self, device: DeviceConfig, now: float | None = None) -> bool:
        now = now or time.time()
        return bool(
            device.enabled
            and device.endpoint_url
            and device.last_seen > 0
            and now - device.last_seen <= self.online_ttl
            and device.id in self._paired_ids
        )

    def views(self, *, local_online: bool) -> list[DeviceView]:
        now = time.time()
        rows = [
            DeviceView(
                id=self.local_device_id,
                name=self.local_device_name,
                endpoint_url="",
                online=bool(local_online),
                local=True,
                last_seen=now if local_online else 0.0,
            )
        ]
        for device in load_devices():
            rows.append(
                DeviceView(
                    id=device.id,
                    name=device.name or f"设备-{device.id[:6]}",
                    endpoint_url=device.endpoint_url,
                    online=self._is_remote_online(device, now),
                    local=False,
                    last_seen=device.last_seen,
                )
            )
        return rows

    def online_ids(self, *, local_online: bool) -> list[str]:
        return [item.id for item in self.views(local_online=local_online) if item.online]

    def resolve_remote(self, device_id: str) -> RemoteTarget | None:
        if not device_id or device_id == self.local_device_id:
            return None
        device = next((d for d in load_devices() if d.id == device_id), None)
        if device is None or not self._is_remote_online(device):
            return None
        bearer = self.store.get(self._bearer_key(device_id)) or ""
        if not bearer:
            return None
        return RemoteTarget(
            device_id=device.id,
            name=device.name or f"设备-{device.id[:6]}",
            base_url=mcp_base_url(device.endpoint_url),
            bearer=bearer,
        )

    def get_peer_secret(self, device_id: str) -> str | None:
        return self.store.get(self._heartbeat_key(device_id))


__all__ = [
    "DeviceRegistry",
    "DeviceView",
    "RemoteTarget",
    "normalize_mcp_url",
    "mcp_base_url",
    "PAIR_CODE_TTL_SECONDS",
    "REMOTE_ONLINE_TTL_SECONDS",
    "HUB_PEER_SECRET_KEY",
    "SecretStoreLike",
]
