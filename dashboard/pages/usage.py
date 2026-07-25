"""用量 — 本机 Claude Code 使用量面板。

Read-only, numbers-only view over the local transcripts under
~/.claude/projects (see core.usage_stats). Sessions, token consumption
(by model / day / project), and an activity heatmap. No message text is
read or shown; nothing is uploaded.
"""

from __future__ import annotations

import html

from nicegui import run, ui

from core.usage_stats import load_aggregate, fmt_tokens

from ..uiutil import guarded_refresh_timer, jarvis_page

_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

# 图表配色引用 CSS 变量（style.css :root）——内联 style 里 var() 照常生效，
# 且跟着 prefers-color-scheme 一起翻转，暗色下不会瞎。
_JADE = "var(--jade)"          # 条形/热力
_MIST_SOFT = "var(--mist-soft)"  # 空底
_INK = "var(--ink)"            # 主文字
_INK_SOFT = "var(--ink-soft)"  # 次要文字


def _tile(
    value: str,
    label: str,
    sub: str = "",
    *,
    value_class: str = "",
) -> None:
    with ui.element("div").classes("metric-cell flex-1 min-w-[130px]"):
        ui.label(value).classes(
            "metric-value" + (f" {value_class}" if value_class else "")
        )
        ui.label(label).classes("metric-label")
        if sub:
            ui.label(sub).classes("text-xs text-gray-400")


def _daily_bars_html(by_day: list[dict], n: int = 30) -> str:
    rows = by_day[-n:]
    if not rows:
        return f"<div style='color:{_INK_SOFT};font-size:13px'>暂无数据</div>"
    max_out = max((r["out"] for r in rows), default=0) or 1
    parts = ["<div style='display:flex;flex-direction:column;gap:2px'>"]
    for r in rows:
        pct = max(1, round(100 * r["out"] / max_out))
        label = f"{r['day'][5:]}"  # MM-DD
        title = (
            f"{r['day']} · {r['turns']} 轮 · "
            f"输出 {fmt_tokens(r['out'])} · 输入 {fmt_tokens(r['in'])} · "
            f"缓存读 {fmt_tokens(r['cache_read'])}"
        )
        parts.append(
            "<div style='display:flex;align-items:center;gap:6px' "
            f"title='{html.escape(title)}'>"
            f"<span style='width:44px;font-size:11px;color:{_INK_SOFT};text-align:right'>{label}</span>"
            f"<div style='flex:1;background:{_MIST_SOFT};border-radius:4px;height:14px'>"
            f"<div style='width:{pct}%;background:{_JADE};height:14px;border-radius:4px'></div>"
            "</div>"
            f"<span style='width:56px;font-size:11px;color:{_INK}'>{fmt_tokens(r['out'])}</span>"
            "</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _heatmap_html(heat: list[list[int]], heat_max: int) -> str:
    if not heat_max:
        return f"<div style='color:{_INK_SOFT};font-size:13px'>暂无数据</div>"
    cell = "width:14px;height:14px;border-radius:2px;"
    parts = [
        "<div class='usage-heatmap' "
        "style='display:flex;flex-direction:column;gap:2px'>"
    ]
    # Hour header (every 3h)
    header = ["<div style='display:flex;gap:2px;margin-left:22px'>"]
    for h in range(24):
        txt = str(h) if h % 3 == 0 else ""
        header.append(
            f"<div style='width:14px;font-size:9px;color:{_INK_SOFT};text-align:center'>{txt}</div>"
        )
    header.append("</div>")
    parts.append("".join(header))
    for w in range(7):
        row = [
            "<div style='display:flex;gap:2px;align-items:center'>",
            f"<span style='width:20px;font-size:10px;color:{_INK_SOFT}'>{_WEEKDAYS[w]}</span>",
        ]
        for h in range(24):
            c = heat[w][h]
            if c:
                alpha = 0.15 + 0.85 * (c / heat_max)
                # color-mix 让透明度阶梯跟着暗色下的 --jade 一起换色。
                bg = (f"color-mix(in srgb, var(--jade) {alpha * 100:.0f}%, "
                      "transparent)")
            else:
                bg = _MIST_SOFT
            title = f"周{_WEEKDAYS[w]} {h:02d}:00 · {c} 轮"
            row.append(
                f"<div style='{cell}background:{bg}' title='{html.escape(title)}'></div>"
            )
        row.append("</div>")
        parts.append("".join(row))
    parts.append("</div>")
    return "".join(parts)


# Cold aggregate rebuild measured at ~4-5s > NiceGUI's default 3s page
# response_timeout — without the override the first cold hit gets cancelled
# mid-build and renders a dead page.
@ui.page("/usage", response_timeout=30)
async def usage_page():
    with jarvis_page("/usage", "用量",
                     "本机 Claude Code 用量：只读本地对话记录、只提数字"
                     "（会话/token/活跃时段），不读正文、不上传。含子代理的消耗。"):

        # content() renders ONLY from this holder — load_aggregate()'s memo
        # short-circuit re-computes a (file-count, newest-mtime) signature on
        # every call, and on this machine transcripts change many times per
        # second, so calling it synchronously here would re-parse on the event
        # loop most cycles (stalling every page + the bot REST API). All
        # recomputation stays on the io_bound thread below.
        holder: dict = {}

        @ui.refreshable
        def content():
            agg = holder["agg"]
            t = agg["tokens"]

            with ui.row().classes("w-full gap-3 flex-wrap"):
                _tile(f"{agg['sessions']:,}", "会话数（主）")
                _tile(
                    f"{agg['assistant_turns']:,}", "响应轮次",
                    f"含子代理 {agg['subagent_turns']:,}",
                )
                _tile(fmt_tokens(t["in"]), "输入 token")
                _tile(fmt_tokens(t["out"]), "输出 token")
                _tile(fmt_tokens(t["cache_read"]), "缓存读取 token")
                _tile(f"{agg['active_days']:,}", "活跃天数")

            with ui.row().classes("w-full gap-3 flex-wrap"):
                _tile(fmt_tokens(t["cache_creation"]), "缓存写入 token")
                _tile(fmt_tokens(agg["subagent_tokens"]), "子代理 token")
                _tile(f"{agg['files']:,}", "对话记录文件数")
                span = ""
                if agg["first_ts"]:
                    span = (
                        f"{agg['first_ts'][:10]}\n→ {agg['last_ts'][:10]}"
                    )
                _tile(span or "—", "覆盖区间", value_class="is-range")

            with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                with ui.card().classes("usage-chart-card"):
                    ui.label("每日输出 token（近 30 天）").classes(
                        "text-sm font-semibold mb-2")
                    ui.html(_daily_bars_html(agg["by_day"], 30))
                with ui.card().classes("usage-chart-card"):
                    ui.label("活跃热力图（小时 × 星期，本地时间）").classes(
                        "text-sm font-semibold mb-2")
                    ui.html(_heatmap_html(
                        agg["heat"], agg["heat_max"]
                    )).classes("usage-heatmap-scroll")

            with ui.column().classes("w-full gap-2"):
                ui.label("按模型").classes("text-sm font-semibold")
                mcols = [
                    {"name": "model", "label": "模型", "field": "model", "align": "left"},
                    {"name": "in", "label": "输入", "field": "in_f", "align": "right"},
                    {"name": "out", "label": "输出", "field": "out_f", "align": "right"},
                    {"name": "cw", "label": "缓存写", "field": "cw_f", "align": "right"},
                    {"name": "cr", "label": "缓存读", "field": "cr_f", "align": "right"},
                ]
                mrows = [
                    {
                        "model": r["model"],
                        "in_f": fmt_tokens(r["in"]),
                        "out_f": fmt_tokens(r["out"]),
                        "cw_f": fmt_tokens(r["cache_creation"]),
                        "cr_f": fmt_tokens(r["cache_read"]),
                    }
                    for r in agg["by_model"]
                ]
                with ui.element("div").classes("table-scroll"):
                    ui.table(columns=mcols, rows=mrows, row_key="model").classes(
                        "jarvis-table")

            with ui.column().classes("w-full gap-2"):
                ui.label("按项目").classes("text-sm font-semibold")
                pcols = [
                    {"name": "project", "label": "项目", "field": "project", "align": "left"},
                    {"name": "sessions", "label": "会话", "field": "sessions", "align": "right"},
                    {"name": "turns", "label": "轮次", "field": "turns", "align": "right"},
                    {"name": "in", "label": "输入", "field": "in_f", "align": "right"},
                    {"name": "out", "label": "输出", "field": "out_f", "align": "right"},
                    {"name": "cr", "label": "缓存读", "field": "cr_f", "align": "right"},
                ]
                prows = [
                    {
                        "project": r["project"],
                        "sessions": r["sessions"],
                        "turns": r["turns"],
                        "in_f": fmt_tokens(r["in"]),
                        "out_f": fmt_tokens(r["out"]),
                        "cr_f": fmt_tokens(r["cache_read"]),
                    }
                    for r in agg["by_project"]
                ]
                with ui.element("div").classes("table-scroll"):
                    ui.table(columns=pcols, rows=prows, row_key="project").classes(
                        "jarvis-table")

        holder["agg"] = await run.io_bound(load_aggregate)
        content()

        async def _rewarm_and_refresh():
            holder["agg"] = await run.io_bound(load_aggregate)
            content.refresh()

        guarded_refresh_timer(60, _rewarm_and_refresh)
