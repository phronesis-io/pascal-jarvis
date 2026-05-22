"""Settings page — agent configuration with conversational feel.

Exposes key behaviors that the user can adjust:
- Notification frequency/quiet hours
- Trust levels per domain
- Bookmark preferences
- Feed preferences
"""

import json
from pathlib import Path

from nicegui import ui

from ..db import kv_get, kv_set

JARVIS_DIR = Path(__file__).parent.parent.parent

# Default settings with descriptions
SETTINGS_SCHEMA = [
    {
        "key": "notify_max_per_hour",
        "label": "Max notifications per hour",
        "type": "number",
        "default": "3",
        "description": "Rate limit on proactive messages",
    },
    {
        "key": "quiet_hours_start",
        "label": "Quiet hours start",
        "type": "time",
        "default": "23:00",
        "description": "No proactive messages after this time",
    },
    {
        "key": "quiet_hours_end",
        "label": "Quiet hours end",
        "type": "time",
        "default": "08:00",
        "description": "Resume proactive messages after this time",
    },
    {
        "key": "resurface_count",
        "label": "Daily resurface count",
        "type": "number",
        "default": "5",
        "description": "How many bookmarks to resurface daily",
    },
    {
        "key": "resurface_serendipity",
        "label": "Serendipity ratio (%)",
        "type": "number",
        "default": "30",
        "description": "Percentage of random/unexpected items in resurface",
    },
    {
        "key": "eigenflux_auto_publish",
        "label": "Auto-publish to EigenFlux",
        "type": "bool",
        "default": "true",
        "description": "Allow agent to publish discoveries automatically",
    },
    {
        "key": "checkin_mode_ratio",
        "label": "Connection vs Wellbeing ratio",
        "type": "text",
        "default": "50:50",
        "description": "Balance between connection and wellbeing check-ins",
    },
]


@ui.page("/settings")
def settings_page():
    """Settings management page."""
    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Settings").classes("text-2xl font-bold")

        ui.label(
            "These control how the agent behaves. Changes take effect immediately."
        ).classes("text-sm text-gray-600")

        # Settings form
        inputs = {}
        for setting in SETTINGS_SCHEMA:
            key = setting["key"]
            current = kv_get(key, setting["default"])

            with ui.card().classes("w-full p-3"):
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.column().classes("gap-0"):
                        ui.label(setting["label"]).classes("font-medium text-sm")
                        ui.label(setting["description"]).classes("text-xs text-gray-500")

                    if setting["type"] == "bool":
                        inp = ui.switch(value=current.lower() == "true")
                    elif setting["type"] == "number":
                        inp = ui.number(value=float(current)).classes("w-24")
                    elif setting["type"] == "time":
                        inp = ui.input(value=current).classes("w-28")
                    else:
                        inp = ui.input(value=current).classes("w-40")
                    inputs[key] = (inp, setting)

        async def save_all():
            for key, (inp, setting) in inputs.items():
                if setting["type"] == "bool":
                    val = "true" if inp.value else "false"
                elif setting["type"] == "number":
                    val = str(int(inp.value)) if inp.value else setting["default"]
                else:
                    val = str(inp.value) if inp.value else setting["default"]
                kv_set(key, val)
            ui.notify("Settings saved", type="positive")

        ui.button("Save All", on_click=save_all).classes("mt-4")

        # System info
        ui.separator()
        ui.label("System Info").classes("text-lg font-semibold mt-4")

        config_path = JARVIS_DIR / "jarvis.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                with ui.card().classes("w-full p-3"):
                    ui.label(f"Work dir: {config.get('work_dir', 'N/A')}").classes("text-xs font-mono")
                    ui.label(f"Heartbeat model: {config.get('claude', {}).get('heartbeat_model', 'N/A')}").classes("text-xs font-mono")
                    ui.label(f"Check interval: {config.get('heartbeat', {}).get('check_interval', 'N/A')}s").classes("text-xs font-mono")
                    ui.label(f"Admin port: {config.get('admin', {}).get('port', 'N/A')}").classes("text-xs font-mono")
            except ImportError:
                ui.label("PyYAML not installed — skipping config display").classes("text-xs text-gray-400")

        # Data management
        ui.separator()
        ui.label("Data").classes("text-lg font-semibold mt-4")

        async def migrate_watchlater():
            from ..bookmark_pipeline import migrate_from_jsonl
            wl_path = JARVIS_DIR.parent / "memory" / "system" / "watchlater.jsonl"
            if not wl_path.exists():
                # Try alternative path
                wl_path = Path.home() / ".claude" / "projects" / "-Users-pascal-Desktop-jarvis-repos-pascal-jarvis" / "memory" / "system" / "watchlater.jsonl"
            count = migrate_from_jsonl(str(wl_path))
            ui.notify(f"Migrated {count} bookmarks from watchlater.jsonl", type="positive")

        ui.button("Migrate watchlater.jsonl → SQLite", on_click=migrate_watchlater)
