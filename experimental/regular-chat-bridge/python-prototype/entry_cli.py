"""PyInstaller console entrypoint for the optional MCP DevBridge CLI."""

from __future__ import annotations

from local_dev_mcp_bridge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
