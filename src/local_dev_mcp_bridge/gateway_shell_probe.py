"""Short, sequential shell diagnostics with one shared wall-clock budget."""
from __future__ import annotations

import subprocess
import time

from .platform_support import run_platform_kwargs
from .shell import default_shell


def shell_self_test() -> str:
    """Probe the default shell once; do not repeat every installed shell version."""
    deadline = time.monotonic() + 20.0
    shell = default_shell()
    badge = "✓" if shell.executable else "✗"
    lines = [f"[{badge}] shell: {shell.name} ({shell.path})"]
    probes = [(tool, [tool, "--version"]) for tool in ("python", "git", "node", "npm")]
    if shell.kind in {"pwsh", "windows_powershell"}:
        probes.insert(0, ("shell version", [
            shell.path, "-NoProfile", "-NonInteractive", "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ]))
    probes.extend((tool, ["python", "-m", tool, "--version"]) for tool in ("pytest", "pyright"))
    for label, argv in probes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            lines.append(f"[✗] {label}: 总检测时间已用尽，未执行")
            continue
        try:
            result = subprocess.run(
                argv, capture_output=True, timeout=min(3.0, remaining),
                **run_platform_kwargs(),
            )
            output = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
            if result.returncode != 0 or not output:
                lines.append(f"[✗] {label}: 未安装或不可调用")
            else:
                lines.append(f"[✓] {label}: {output[0][:256]}")
        except subprocess.TimeoutExpired:
            lines.append(f"[✗] {label}: 检测超时")
        except OSError:
            lines.append(f"[✗] {label}: 未安装或不可调用")
    return "\n".join(lines)
