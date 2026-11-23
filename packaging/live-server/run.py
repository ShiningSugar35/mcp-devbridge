"""Read dedicated MCP Live credentials and start the relay."""

import os
from pathlib import Path

from local_dev_mcp_bridge.live_server import main

private = Path("/run/mcp-live")
required = {
    "owner-token": "MCP_LIVE_OWNER_TOKEN",
    "access-token": "MCP_LIVE_ACCESS_TOKEN",
    "oauth-approval-token": "MCP_LIVE_OAUTH_APPROVAL_TOKEN",
}
for file, variable in required.items():
    os.environ[variable] = (private / file).read_text(encoding="utf-8").strip()
verifier = private / "device-key-verifier"
if verifier.is_file():
    os.environ["MCP_LIVE_DEVICE_KEY_VERIFIER"] = verifier.read_text(encoding="utf-8").strip()

raise SystemExit(main([
    "--public-hostname", os.environ["MCP_LIVE_PUBLIC_HOSTNAME"],
    "--port", os.environ.get("MCP_LIVE_PORT", "8796"),
]))
