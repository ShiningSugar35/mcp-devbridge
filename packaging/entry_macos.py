"""Native .app entry: establish the profile before desktop imports constants."""
from local_dev_mcp_bridge.macos_app import main

if __name__ == "__main__":
    raise SystemExit(main())
