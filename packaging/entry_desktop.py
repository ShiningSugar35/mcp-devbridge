"""PyInstaller entry wrapper — absolute imports so the frozen app can start."""

import sys

if "--elevated-broker" in sys.argv or "--register-elevated-broker-task" in sys.argv:
    from local_dev_mcp_bridge.elevation import broker_main
    from local_dev_mcp_bridge.elevation import main as elevation_main

    if __name__ == "__main__":
        sys.exit(broker_main() if "--elevated-broker" in sys.argv else elevation_main())
else:
    from local_dev_mcp_bridge.desktop_main import main

    if __name__ == "__main__":
        sys.exit(main())
