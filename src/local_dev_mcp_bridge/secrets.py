"""Secret storage backed by Windows Credential Manager with DPAPI fallback.

* MCP public-access bearer token (rotatable on demand, single active value).
* Cloudflare Named Tunnel token (optional; never persisted in plaintext).

DPAPI encryption is user-bound, so the fallback file is undecipherable to
other accounts.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import secrets
from pathlib import Path

from . import constants


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(data: bytes) -> bytes:
    buf_in = _DataBlob(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
    )
    buf_out = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(buf_in), None, None, None, None, 0x01, ctypes.byref(buf_out)
    ):
        raise RuntimeError("CryptProtectData failed")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    buf_in = _DataBlob(
        len(data),
        ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)),
    )
    buf_out = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(buf_in), None, None, None, None, 0x01, ctypes.byref(buf_out)
    ):
        raise RuntimeError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(buf_out.pbData, buf_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buf_out.pbData)


def _fallback_file() -> Path:
    constants.ensure_dirs()
    return constants.config_dir() / "secrets.dpapi.json"


def _read_fallback() -> dict[str, str]:
    path = _fallback_file()
    if not path.is_file():
        return {}
    try:
        raw = _dpapi_unprotect(path.read_bytes())
        return dict(json.loads(raw.decode("utf-8")))
    except Exception:
        return {}


def _write_fallback(data: dict[str, str]) -> None:
    constants.ensure_dirs()
    path = _fallback_file()
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(_dpapi_protect(json.dumps(data).encode("utf-8")))
    tmp.replace(path)


class SecretsStore:
    """Windows Credential Manager with DPAPI-encrypted file fallback."""

    def __init__(self, use_credential_manager: bool = True) -> None:
        self._use_cm = use_credential_manager

    def set(self, key: str, value: str) -> None:
        if self._use_cm:
            try:
                import win32cred

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
                self._use_cm = False
        data = _read_fallback()
        data[key] = value
        _write_fallback(data)

    def get(self, key: str) -> str | None:
        if self._use_cm:
            try:
                import win32cred

                cred = win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC)
                blob = cred["CredentialBlob"]
                value = blob.decode("utf-16-le", errors="replace") if isinstance(blob, bytes) else str(blob)
                if value:
                    return value
            except Exception:
                pass
        return _read_fallback().get(key) or None

    def delete(self, key: str) -> None:
        if self._use_cm:
            try:
                import win32cred

                win32cred.CredDelete(key, win32cred.CRED_TYPE_GENERIC)
            except Exception:
                pass
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