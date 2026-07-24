"""奏折 inbox — the durable companion to Lark's one-card interaction."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui

from ..telemetry import memorial_states as _cached_states

JARVIS_DIR = Path(__file__).parent.parent.parent


def memorial_states(jarvis_dir: str | Path) -> list[dict]:
    states = _cached_states(jarvis_dir)
    states.sort(key=lambda s: (s.get("epoch", 0), s.get("ts", "")), reverse=True)
    return states
@ui.page("/memorials")
def memorials_page():
    ui.navigate.to("/items")
