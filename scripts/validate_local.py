"""Run one validation module with project-local temporary state and a terminal receipt."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("module", choices=("pytest", "ruff", "pyright"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.label):
        parser.error("label must be a short filename component")
    root = Path(__file__).resolve().parents[1]
    output = root / ".ai-bridge" / "v091-validation" / args.label
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / "tmp"
    temporary.mkdir()
    env = dict(os.environ)
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = str(temporary)
    env.update({
        "LOCALDEV_MCP_CONFIG_DIR": str(output / "config"),
        "RUFF_CACHE_DIR": str(output / "ruff-cache"),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1", "QT_QPA_PLATFORM": "offscreen",
        "GIT_CEILING_DIRECTORIES": str(root),
    })
    command = [sys.executable, "-B", "-m", args.module, *args.arguments]
    if args.module == "pytest":
        command += ["--basetemp=" + str(temporary / "pytest"), "-o", "cache_dir=" + str(output / "pytest-cache")]
    started = time.time()
    log = output / "output.log"
    with log.open("wb") as stream:
        result = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    receipt = {"module": args.module, "label": args.label, "exit_code": result.returncode,
               "duration_seconds": round(time.time() - started, 3), "log": str(log.relative_to(root))}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(log.read_text(encoding="utf-8", errors="replace")[-24000:])
    print(json.dumps(receipt))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
