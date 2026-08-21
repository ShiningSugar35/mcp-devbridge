"""Cross-platform protected secret storage.

Windows uses Credential Manager first and a per-user DPAPI encrypted fallback.
Linux/SteamOS uses the freedesktop Secret Service through ``secret-tool`` when
available and otherwise falls back to an AES-GCM encrypted file whose random
master key is readable only by the current Unix account (0600).

No bearer token, OAuth secret or tunnel credential is ever written plaintext to
``projects.json`` / ``config.json``.
"""

from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import APP_IDENT, constants
from .platform_support import IS_LINUX, IS_WINDOWS, run_platform_kwargs

if IS_WINDOWS:
    import ctypes
    import ctypes.wintypes as wt

    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data: bytes) -> bytes:
    if not IS_WINDOWS:
        raise RuntimeError("DPAPI is only available on Windows")
    buf_in = _DataBlob(  # type: ignore[name-defined]
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),  # type: ignore[name-defined]
    )
    buf_out = _DataBlob()  # type: ignore[name-defined]
    if not ctypes.windll.crypt32.CryptProtectData(  # type: ignore[name-defined,union-attr]
        ctypes.byref(buf_in), None, None, None, None, 0x01, ctypes.byref(buf_out)  # type: ignore[name-defined]
    ):
        raise RuntimeError("CryptProtectData failed")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)  # type: ignore[name-defined]
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)  # type: ignore[name-defined,union-attr]


def _dpapi_unprotect(data: bytes) -> bytes:
    if not IS_WINDOWS:
        raise RuntimeError("DPAPI is only available on Windows")
    buf_in = _DataBlob(  # type: ignore[name-defined]
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),  # type: ignore[name-defined]
    )
    buf_out = _DataBlob()  # type: ignore[name-defined]
    if not ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[name-defined,union-attr]
        ctypes.byref(buf_in), None, None, None, None, 0x01, ctypes.byref(buf_out)  # type: ignore[name-defined]
    ):
        raise RuntimeError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)  # type: ignore[name-defined]
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)  # type: ignore[name-defined,union-attr]


def _fallback_file() -> Path:
    constants.ensure_dirs()
    name = "secrets.dpapi.json" if IS_WINDOWS else "secrets.aesgcm"
    return constants.config_dir() / name


def _linux_key_file() -> Path:
    constants.ensure_dirs()
    return constants.config_dir() / "secrets.key"


def _chmod_user_only(path: Path) -> None:
    if IS_WINDOWS:
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _linux_master_key() -> bytes:
    path = _linux_key_file()
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) == 32:
            _chmod_user_only(path)
            return raw
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    # O_EXCL + mode 0600 prevents a permissive umask from exposing a new key.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raw = path.read_bytes()
        if len(raw) != 32:
            raise RuntimeError("Linux secret master key is corrupted") from None
        _chmod_user_only(path)
        return raw
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    _chmod_user_only(path)
    return key


def _linux_protect(data: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(_linux_master_key()).encrypt(nonce, data, APP_IDENT.encode("utf-8"))
    return b"MCPDB1" + nonce + encrypted


def _linux_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"MCPDB1") or len(data) < 6 + 12 + 16:
        raise ValueError("invalid Linux secret file")
    nonce = data[6:18]
    encrypted = data[18:]
    return AESGCM(_linux_master_key()).decrypt(
        nonce, encrypted, APP_IDENT.encode("utf-8")
    )


def _read_fallback() -> dict[str, str]:
    path = _fallback_file()
    if not path.is_file():
        return {}
    try:
        raw = _dpapi_unprotect(path.read_bytes()) if IS_WINDOWS else _linux_unprotect(path.read_bytes())
        payload = json.loads(raw.decode("utf-8"))
        return {str(k): str(v) for k, v in dict(payload).items()}
    except Exception:
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    constants.ensure_dirs()
    path = _fallback_file()
    tmp = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    protected = _dpapi_protect(raw) if IS_WINDOWS else _linux_protect(raw)
    tmp.write_bytes(protected)
    _chmod_user_only(tmp)
    tmp.replace(path)
    _chmod_user_only(path)


def _secret_tool_path() -> str:
    return shutil.which("secret-tool") or "" if IS_LINUX else ""


def _linux_native_set(key: str, value: str) -> bool:
    tool = _secret_tool_path()
    if not tool:
        return False
    try:
        result = subprocess.run(
            [tool, "store", f"--label=MCP DevBridge: {key}", "service", APP_IDENT, "key", key],
            input=value.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _linux_native_get(key: str) -> str | None:
    tool = _secret_tool_path()
    if not tool:
        return None
    try:
        result = subprocess.run(
            [tool, "lookup", "service", APP_IDENT, "key", key],
            capture_output=True,
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        if result.returncode == 0:
            value = result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
            return value or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _linux_native_delete(key: str) -> bool:
    tool = _secret_tool_path()
    if not tool:
        return False
    try:
        result = subprocess.run(
            [tool, "clear", "service", APP_IDENT, "key", key],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            **run_platform_kwargs(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class SecretsStore:
    """Native protected store with an encrypted per-user fallback."""

    def __init__(self, use_credential_manager: bool = True) -> None:
        # Keep the historical argument name for API/test compatibility.  On Linux
        # it means “try the desktop's native Secret Service first”.
        self._use_native = use_credential_manager

    def set(self, key: str, value: str) -> None:
        if self._use_native and IS_WINDOWS:
            try:
                win32cred = importlib.import_module("win32cred")

                win32cred.CredWrite(
                    {
                        "Type": win32cred.CRED_TYPE_GENERIC,
                        "TargetName": key,
                        "UserName": constants.ACCESS_TOKEN_USERNAME,
                        "CredentialBlob": value.encode("utf-16-le"),
                        "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                    }
                )
                return
            except Exception:
                self._use_native = False
        elif self._use_native and IS_LINUX and _linux_native_set(key, value):
            return
        data = _read_fallback()
        data[key] = value
        _write_fallback(data)

    def get(self, key: str) -> str | None:
        if self._use_native and IS_WINDOWS:
            try:
                win32cred = importlib.import_module("win32cred")

                cred = win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC)
                blob = cred["CredentialBlob"]
                value = (
                    blob.decode("utf-16-le", errors="replace")
                    if isinstance(blob, bytes)
                    else str(blob)
                )
                if value:
                    return value
            except Exception:
                pass
        elif self._use_native and IS_LINUX:
            value = _linux_native_get(key)
            if value:
                return value
        return _read_fallback().get(key) or None

    def delete(self, key: str) -> None:
        if self._use_native and IS_WINDOWS:
            try:
                win32cred = importlib.import_module("win32cred")

                win32cred.CredDelete(key, win32cred.CRED_TYPE_GENERIC)
            except Exception:
                pass
        elif self._use_native and IS_LINUX:
            _linux_native_delete(key)
        data = _read_fallback()
        if key in data:
            del data[key]
            _write_fallback(data)


def get_store() -> SecretsStore:
    return SecretsStore()


def generate_token(bits: int = 256) -> str:
    raw = secrets.token_bytes(bits // 8)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


__all__ = ["SecretsStore", "get_store", "generate_token"]
