#!/usr/bin/env python3
"""Jarvis Dashboard — entry point.

Run standalone:
    python3 -m dashboard.main

Or:
    python3 dashboard/main.py

Starts NiceGUI on 127.0.0.1:3457 (one port above admin).
"""

import sys
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dashboard.app import create_app  # noqa: E402
from dashboard.db import get_db  # noqa: E402


def main():
    """Start the dashboard server."""
    # Initialize DB on startup
    get_db()
    from core.deploy import register_runtime
    register_runtime("dashboard")

    # Create app (registers pages)
    create_app()

    # Import nicegui and run
    from nicegui import ui
    ui.run(
        host="127.0.0.1",
        port=3457,
        title="Jarvis",
        favicon=str(ROOT / "dashboard" / "static" / "app-icon.svg"),
        show=False,  # Don't auto-open browser
        reload=False,  # No hot reload in production
    )


if __name__ == "__main__":
    main()
