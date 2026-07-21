"""收藏 — 存下来的内容，按阅读状态流转，隔一阵再端到你面前。

Lifecycle: inbox → reading → done → archived. Search via FTS5.

REQ-43: rendering is READ-ONLY — the old page advanced surfaced_count on
every GET (each refresh/probe corrupted spaced-repetition state). The
advance is now the explicit「✓ 已看」button, and every card carries
lifecycle buttons (直连 dashboard.db, 不走 API).

Actions refresh the board in place (@ui.refreshable) instead of reloading
the whole page — a click no longer resets the tab and scroll position.
"""

import json
from pathlib import Path

from nicegui import ui

from ..db import bookmark_list, bookmark_update, bookmark_delete, bookmark_search
from ..bookmark_pipeline import capture, get_resurface_candidates, mark_surfaced
from ..uiutil import jarvis_page

# 生命周期流转: 当前状态 → 可去的状态 (按钮标签, 目标状态, 颜色)
_LIFECYCLE_MOVES = {
    "inbox":    [("▶ 开始读", "reading", "green"), ("归档", "archived", "grey")],
    "triaged":  [("▶ 开始读", "reading", "green"), ("归档", "archived", "grey")],
    "reading":  [("✓ 读完", "done", "blue"), ("归档", "archived", "grey")],
    "done":     [("归档", "archived", "grey")],
    "archived": [("↩ 回待读", "inbox", "blue")],
}

_STATUS_LABELS = {
    "inbox": "待读",
    "triaged": "已分拣",
    "reading": "在读",
    "done": "读完",
    "archived": "已归档",
}

_TABS = [("inbox", "待读"), ("reading", "在读"), ("done", "读完"), ("all", "全部")]


@ui.page("/bookmarks")
def bookmarks_page():
    """收藏管理页。"""
    with jarvis_page("/bookmarks", "收藏",
                     "存下来的内容按「待读 → 在读 → 读完」流转；旧内容会隔一阵再端上来。"):

        # 当前选中的 tab 要在局部刷新后保住，否则每点一下都弹回第一个。
        view = {"tab": "inbox"}

        # ── 搜索 ──
        search_input = ui.input(placeholder="搜收藏……回车开搜").classes("w-full")
        search_results_container = ui.column().classes("w-full")

        async def on_search():
            query = search_input.value.strip()
            search_results_container.clear()
            if not query:
                return
            results = bookmark_search(query)
            with search_results_container:
                if results:
                    ui.label(f"找到 {len(results)} 条").classes(
                        "text-sm text-gray-500")
                    for bm in results:
                        _render_bookmark(bm, on_change=lambda: board.refresh())
                else:
                    ui.label("没搜到。").classes("section-note")

        search_input.on("keydown.enter", on_search)

        @ui.refreshable
        def board():
            # ── 今日重温 — 只读挑选；只有点「✓ 已看」才推进间隔重复状态 ──
            with ui.column().classes("w-full gap-2"):
                ui.label("壹 · 重温").classes("section-kicker")
                ui.label("今日重温").classes("section-title")
                ui.label("从旧收藏里挑几条再端给你。点「✓ 已看」才算看过一次，"
                         "看过的会隔更久再出现。").classes("section-note")

                resurface = get_resurface_candidates(5)
                if resurface:
                    for bm in resurface:
                        _render_bookmark(bm, compact=True, surfaceable=True,
                                         on_change=board.refresh)
                else:
                    ui.label("暂时没有要重温的——收藏多了才有得温。").classes(
                        "section-note")

            # ── 按状态分栏 ──
            with ui.column().classes("w-full gap-2"):
                ui.label("贰 · 清单").classes("section-kicker")
                ui.label("收藏清单").classes("section-title")

                with ui.tabs(value=view["tab"],
                             on_change=lambda e: view.update(tab=e.value)) \
                        .classes("w-full") as status_tabs:
                    for name, label in _TABS:
                        ui.tab(name, label=label)

                empty_copy = {
                    "inbox": "待读是空的——存点东西进来。",
                    "reading": "现在没有在读的。",
                    "done": "还没有读完的。",
                    "all": "还没有任何收藏。",
                }
                with ui.tab_panels(status_tabs, value=view["tab"]).classes("w-full"):
                    for name, _label in _TABS:
                        with ui.tab_panel(name):
                            status = None if name == "all" else name
                            limit = 50 if name == "all" else 30
                            items = bookmark_list(status=status, limit=limit)
                            if items:
                                for bm in items:
                                    _render_bookmark(bm, on_change=board.refresh)
                            else:
                                ui.label(empty_copy[name]).classes("section-note")

        board()

        # ── 手动添加 ──
        with ui.column().classes("w-full gap-2"):
            ui.label("叁 · 添加").classes("section-kicker")
            ui.label("存一条").classes("section-title")
            with ui.card().classes("w-full p-4"):
                title_input = ui.input("标题").classes("w-full")
                url_input = ui.input("链接（可不填）").classes("w-full")

                async def add_bookmark():
                    title = title_input.value.strip()
                    if not title:
                        ui.notify("标题不能为空", type="warning")
                        return
                    capture(title=title, url=url_input.value.strip(),
                            source="dashboard")
                    ui.notify(f"已存：{title}", type="positive")
                    title_input.value = ""
                    url_input.value = ""
                    board.refresh()

                ui.button("保存", on_click=add_bookmark).classes(
                    "memorial-primary mt-2").props("no-caps")


def _render_bookmark(bm: dict, compact: bool = False, surfaceable: bool = False,
                     on_change=None):
    """Render a single bookmark card. Pure render — never writes to the DB.

    ``on_change`` is called after any user action so the enclosing
    refreshable board redraws in place (no full-page reload).
    """
    status = bm.get("status", "inbox")

    with ui.card().classes("w-full p-3"):
        with ui.row().classes("w-full items-start justify-between"):
            with ui.column().classes("gap-0 flex-1"):
                # Title (link if URL exists)
                title = bm.get("title", "（无标题）")
                url = bm.get("url", "")
                if url:
                    ui.link(title, url, new_tab=True).classes(
                        "font-medium text-sm")
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

            # 显式推进间隔重复 (原 GET 副作用 → 用户动作)
            if surfaceable:
                async def surfaced_click(bm_id=bm["id"]):
                    mark_surfaced([bm_id])
                    ui.notify("已记下看过一次，这条会隔更久再出现", type="positive")
                    if on_change:
                        on_change()

                ui.button("✓ 已看", on_click=surfaced_click).props(
                    "flat dense size=xs no-caps color=secondary")

            # Status + actions
            if not compact:
                with ui.column().classes("gap-1 items-end"):
                    ui.badge(_STATUS_LABELS.get(status, status), color={
                        "inbox": "blue", "triaged": "amber",
                        "reading": "green", "done": "gray", "archived": "gray"
                    }.get(status, "gray")).classes("text-xs")

                    # Date
                    created = bm.get("created_at", "")[:10]
                    ui.label(created).classes("text-xs text-gray-400")

                    # 生命周期流转按钮 (REQ-43: 27/27 书签卡死 inbox 的修复)
                    with ui.row().classes("gap-1"):
                        for label, target, color in _LIFECYCLE_MOVES.get(status, []):
                            async def move_click(bm_id=bm["id"], to=target):
                                bookmark_update(bm_id, status=to)
                                ui.notify(f"→ {_STATUS_LABELS.get(to, to)}",
                                          type="positive")
                                if on_change:
                                    on_change()

                            ui.button(label, on_click=move_click).props(
                                f"flat dense size=xs no-caps color={color}")
