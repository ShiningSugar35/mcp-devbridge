"""Reject macOS crypto wheels that can collide with Python's bundled OpenSSL.

Run before PyInstaller. This is a build-only check, never a production TLS hook.
The Intel source build must use OPENSSL_STATIC=1; official macOS wheels already
bundle OpenSSL statically. Keep the normal frozen-application smoke afterwards.
"""
from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
from pathlib import Path

_LIBRARY = re.compile(r"lib(?:ssl|crypto)(?:\.[0-9]+)*\.dylib\Z")


def openssl_dependencies(output: str) -> list[str]:
    """Parse otool -L dependency rows, including paths containing spaces."""
    rows = output.splitlines()
    if not rows or not rows[0].rstrip().endswith(":"):
        raise ValueError("Expected an otool dependency table")
    dependencies: list[str] = []
    seen = False
    for row in rows[1:]:
        path, separator, _versions = row.strip().partition(" (compatibility version ")
        if not separator:
            if row.strip():
                raise ValueError("Unrecognized otool dependency row")
            continue
        seen = True
        if _LIBRARY.fullmatch(Path(path).name):
            dependencies.append(path)
    if not seen:
        raise ValueError("Empty otool dependency table")
    return dependencies


def main() -> int:
    if sys.platform != "darwin":
        raise RuntimeError("Run the binary dependency check on a native macOS host")
    import ssl

    import cryptography
    from cryptography.hazmat.bindings import _rust
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    extension = Path(_rust.__file__).resolve()
    result = subprocess.run(
        ["/usr/bin/otool", "-L", str(extension)],
        check=True, capture_output=True, text=True, timeout=10,
    )
    if len(result.stdout) > 65536:
        raise RuntimeError("Unexpectedly large native dependency table")
    libraries = openssl_dependencies(result.stdout)
    if libraries:
        raise RuntimeError(
            "cryptography dynamically links OpenSSL; rebuild the locked package "
            "with OPENSSL_STATIC=1 and a fresh build cache before freezing"
        )
    # Fixed test material only, wholly in memory. No user credentials or files.
    cipher = AESGCM(bytes(range(32)))
    nonce, message, context = bytes(range(12)), b"native-build-check", b"MCPDevBridge"
    encrypted = cipher.encrypt(nonce, message, context)
    if cipher.decrypt(nonce, encrypted, context) != message:
        raise RuntimeError("Native cryptography round-trip failed")
    print(json.dumps({
        "check": "macos_crypto_linkage", "architecture": platform.machine(),
        "cryptography": cryptography.__version__, "python_ssl": ssl.OPENSSL_VERSION,
        "dynamic_openssl_dependencies": 0, "aesgcm_round_trip": "passed",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
