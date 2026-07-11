"""NiceGUI application factory.

Creates the dashboard app with all pages and real-time updates.
Binds to 127.0.0.1 only — no auth needed for local-only access.

Refresh model: pages poll with ui.timer (the single idiom). The old
event_bus/watcher pair was dead code — zero subscribers, and thread-side
emits were dropped by design — so it was deleted (REQ-46).
"""

from pathlib import Path

from nicegui import app

JARVIS_DIR = Path(__file__).parent.parent
STATIC_DIR = Path(__file__).parent / "static"


def create_app():
    """Configure and return the NiceGUI app."""

    # Serve static files
    app.add_static_files("/static", str(STATIC_DIR))

    # Import pages (they register routes via decorators). thinking and
    # agent_calendar were previously never imported — their routes 404'd in
    # production while the home page linked to them (REQ-43).
    from .pages import (  # noqa: F401
        home, tasks, bookmarks, settings, intentions, thinking, agent_calendar,
        engagement, ops, usage, memorials,
    )

    # Register REST API for bot.sh integration
    from .api import register_api_routes
    register_api_routes()

    return app
