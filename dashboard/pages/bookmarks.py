"""Bookmarks page — manage saved content with AI-powered resurfacing.

Organized by status lifecycle: inbox → triaged → reading → done → archived.
Search via FTS5. Daily digest preview.
"""

import json
from pathlib import Path

from nicegui import ui

from ..db import bookmark_list, bookmark_update, bookmark_delete, bookmark_search
from ..bookmark_pipeline import capture, get_resurface_candidates


@ui.page("/bookmarks")
def bookmarks_page():
    """Bookmarks management page."""
    ui.add_head_html("""
    <style>
        .bookmark-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 0.75rem;
                         transition: all 0.15s; }
        .bookmark-card:hover { border-color: #ec4899; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .status-inbox { border-left: 3px solid #3b82f6; }
        .status-triaged { border-left: 3px solid #f59e0b; }
        .status-reading { border-left: 3px solid #10b981; }
        .status-done { border-left: 3px solid #6b7280; }
        .status-archived { border-left: 3px solid #d1d5db; }
    </style>
    """)

    with ui.column().classes("w-full max-w-4xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Bookmarks").classes("text-2xl font-bold")

        # Search bar
        search_input = ui.input(placeholder="Search bookmarks...").classes("w-full")
        search_results_container = ui.column().classes("w-full")

        async def on_search():
            query = search_input.value.strip()
            search_results_container.clear()
            if not query:
                return
            results = bookmark_search(query)
            with search_results_container:
                if results:
                    ui.label(f"{len(results)} results").classes("text-sm text-gray-500")
                    for bm in results:
                        _render_bookmark(bm)
                else:
                    ui.label("No results").classes("text-gray-400 text-sm")

        search_input.on("keydown.enter", on_search)

        # Daily Digest Preview
        ui.label("Today's Resurface").classes("text-lg font-semibold mt-4")
        ui.label("Items selected for rediscovery").classes("text-xs text-gray-500")

        resurface = get_resurface_candidates(5)
        if resurface:
            for bm in resurface:
                _render_bookmark(bm, compact=True)
        else:
            ui.label("No items to resurface yet.").classes("text-gray-400 text-sm italic")

        # Status tabs
        ui.separator()
        status_tabs = ui.tabs().classes("w-full")
        with status_tabs:
            tab_inbox = ui.tab("inbox")
            tab_reading = ui.tab("reading")
            tab_done = ui.tab("done")
            tab_all = ui.tab("all")

        panels = ui.tab_panels(status_tabs, value=tab_inbox).classes("w-full")
        with panels:
            with ui.tab_panel(tab_inbox):
                items = bookmark_list(status="inbox", limit=30)
                if items:
                    for bm in items:
                        _render_bookmark(bm)
                else:
                    ui.label("Inbox empty").classes("text-gray-400")

            with ui.tab_panel(tab_reading):
                items = bookmark_list(status="reading", limit=30)
                if items:
                    for bm in items:
                        _render_bookmark(bm)
                else:
                    ui.label("Nothing in progress").classes("text-gray-400")

            with ui.tab_panel(tab_done):
                items = bookmark_list(status="done", limit=30)
                if items:
                    for bm in items:
                        _render_bookmark(bm)
                else:
                    ui.label("Nothing completed yet").classes("text-gray-400")

            with ui.tab_panel(tab_all):
                items = bookmark_list(limit=50)
                if items:
                    for bm in items:
                        _render_bookmark(bm)
                else:
                    ui.label("No bookmarks").classes("text-gray-400")

        # Add bookmark form
        ui.separator()
        ui.label("Add Bookmark").classes("text-lg font-semibold mt-2")
        with ui.card().classes("w-full p-4"):
            title_input = ui.input("Title").classes("w-full")
            url_input = ui.input("URL (optional)").classes("w-full")

            async def add_bookmark():
                title = title_input.value.strip()
                if not title:
                    ui.notify("Title is required", type="warning")
                    return
                capture(title=title, url=url_input.value.strip(), source="dashboard")
                ui.notify(f"Saved: {title}", type="positive")
                title_input.value = ""
                url_input.value = ""

            ui.button("Save", on_click=add_bookmark).classes("mt-2")


def _render_bookmark(bm: dict, compact: bool = False):
    """Render a single bookmark card."""
    status = bm.get("status", "inbox")
    classes = f"bookmark-card status-{status} w-full"

    with ui.card().classes(classes):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-0 flex-1"):
                # Title (link if URL exists)
                title = bm.get("title", "Untitled")
                url = bm.get("url", "")
                if url:
                    ui.link(title, url, new_tab=True).classes("font-medium text-sm")
                else:
                    ui.label(title).classes("font-medium text-sm")

                if not compact:
                    # Summary
                    summary = bm.get("summary", "")
                    if summary:
                        ui.label(summary[:120]).classes("text-xs text-gray-600")

                    # Tags
                    tags_str = bm.get("tags", "[]")
                    tags = json.loads(tags_str) if isinstance(tags_str, str) else (tags_str or [])
                    if tags:
                        with ui.row().classes("gap-1 mt-1"):
                            for tag in tags[:5]:
                                ui.badge(tag, color="gray").classes("text-xs")

            # Status + actions
            if not compact:
                with ui.column().classes("gap-1 items-end"):
                    ui.badge(status, color={
                        "inbox": "blue", "triaged": "amber",
                        "reading": "green", "done": "gray", "archived": "gray"
                    }.get(status, "gray")).classes("text-xs")

                    # Date
                    created = bm.get("created_at", "")[:10]
                    ui.label(created).classes("text-xs text-gray-400")
