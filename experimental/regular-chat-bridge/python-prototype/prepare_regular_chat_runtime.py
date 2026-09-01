"""Build a production-only Regular Chat Controller runtime for packaging."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party" / "regular-chat-controller"
TARGET = ROOT / "build" / "regular-chat-controller-runtime"


def main() -> int:
    dist = SOURCE / "dist"
    if not (dist / "src" / "stdioMain.js").is_file():
        raise RuntimeError("Regular Chat Controller dist missing; run npm run build first")
    if not (SOURCE / "package-lock.json").is_file():
        raise RuntimeError("Regular Chat Controller package-lock.json missing")

    shutil.rmtree(TARGET, ignore_errors=True)
    TARGET.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist, TARGET / "dist")
    for name in ("package.json", "package-lock.json", "README.md"):
        source = SOURCE / name
        if source.is_file():
            shutil.copy2(source, TARGET / name)

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to prepare the Regular Chat production runtime")
    completed = subprocess.run(
        [npm, "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=TARGET,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Regular Chat production npm ci failed: exit={completed.returncode}")

    node_modules = TARGET / "node_modules"
    if not (node_modules / "playwright" / "cli.js").is_file():
        raise RuntimeError("Regular Chat production runtime missing playwright/cli.js")
    if not (node_modules / "playwright-core").is_dir():
        raise RuntimeError("Regular Chat production runtime missing playwright-core")

    total_bytes = sum(item.stat().st_size for item in TARGET.rglob("*") if item.is_file())
    print(f"Regular Chat runtime: {TARGET}")
    print(f"Regular Chat runtime bytes: {total_bytes}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"prepare_regular_chat_runtime failed: {exc}", file=sys.stderr)
        raise
