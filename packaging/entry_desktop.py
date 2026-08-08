"""PyInstaller entry wrapper — absolute imports so the frozen app can start.

desktop_main.py uses relative imports (``from . import ...``); freezing it
directly as the spec entry makes it a top-level ``__main__`` without a package
and the relative imports fail. This module is a plain script that imports the
package normally.
"""

import sys

from local_dev_mcp_bridge.desktop_main import main

if __name__ == "__main__":
    sys.exit(main())