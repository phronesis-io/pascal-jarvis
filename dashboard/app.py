"""NiceGUI application factory.

Creates the dashboard app with all pages and real-time updates.
Binds to 127.0.0.1 only — no auth needed for local-only access.
"""

import asyncio
import json
import time
from pathlib import Path

from nicegui import app, ui

from .db import get_db, log_list, bookmark_list, engagement_stats
from .event_bus import bus
from .watcher import FileWatcher

JARVIS_DIR = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"


def create_app():
    """Configure and return the NiceGUI app."""

    # Start file watcher
    watcher = FileWatcher(JARVIS_DIR)
    app.on_startup(watcher.start)
    app.on_shutdown(watcher.stop)

    # Serve static files
    app.add_static_files("/static", str(STATIC_DIR))

    # Import pages (they register routes via decorators)
    from .pages import home, tasks, bookmarks, settings, intentions  # noqa: F401

    # Register REST API for bot.sh integration
    from .api import register_api_routes
    register_api_routes()

    return app
