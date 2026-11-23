"""Verification for the fixed device-only key of an MCP Live relay."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections import deque

_FAILURE_LIMIT = 8
_FAILURE_WINDOW = 20


class DeviceKey:
    def __init__(self, verifier: str) -> None:
        fields = verifier.split(":")
        if len(fields) != 3 or fields[0] != "scrypt-v1":
            raise ValueError("设备密钥验证配置无效。")
        if not re.fullmatch(r"[0-9a-f]{32}", fields[1]) or not re.fullmatch(
            r"[0-9a-f]{64}", fields[2]
        ):
            raise ValueError("设备密钥验证配置无效。")
        self._salt = bytes.fromhex(fields[1])
        self._digest = bytes.fromhex(fields[2])
        self._failures: deque[float] = deque()

    def matches(self, candidate: str) -> bool:
        now = time.monotonic()
        while self._failures and self._failures[0] <= now - _FAILURE_WINDOW:
            self._failures.popleft()
        if len(self._failures) >= _FAILURE_LIMIT or not 1 <= len(candidate) <= 256:
            return False

        attempt = hashlib.scrypt(
            candidate.encode(), salt=self._salt, n=2**14, r=8, p=1, dklen=32
        )
        if hmac.compare_digest(attempt, self._digest):
            self._failures.clear()
            return True
        self._failures.append(now)
        return False
