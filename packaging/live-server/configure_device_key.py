"""Create a salted verifier without echoing or persisting the device key."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    value = getpass.getpass("Device key: ").strip()
    if not 1 <= len(value) <= 256 or not value.isascii() or not value.isprintable() or " " in value:
        parser.error("Enter a non-empty printable ASCII key without spaces.")
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(value.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    with NamedTemporaryFile("w", dir=args.output.parent, encoding="ascii", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(f"scrypt-v1:{salt.hex()}:{key.hex()}\n")
    try:
        os.chmod(temporary, 0o600)
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
