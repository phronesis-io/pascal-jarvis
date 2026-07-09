"""Engagement board — source-level ROI from the real engagement_log.jsonl.

REQ-54: show which proactive sources earn attention, which are merely read,
and which create ignored noise. The dashboard SQLite engagement_events table
is not the source of truth; bot/heartbeat writes engagement_log.jsonl.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import defaultdict
from pathlib import Path

from nicegui import ui

from ..uiutil import guarded_refresh_timer

JARVIS_DIR = Path(__file__).parent.parent.parent


def _epoch(row: dict) -> float:
    if row.get("epoch"):
        try:
            return float(row["epoch"])
        except (TypeError, ValueError):
            return 0
    ts = row.get("ts", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(ts, fmt))
        except (TypeError, ValueError, OverflowError):
            continue
    return 0


_read_cache: dict = {"mtime": 0.0, "size": 0, "rows": []}


def _read_rows(jarvis_dir: Path) -> list[dict]:
    path = jarvis_dir / "engagement_log.jsonl"
    try:
        st = path.stat()
    except OSError:
        return []
    if st.st_mtime == _read_cache["mtime"] and st.st_size == _read_cache["size"]:
        return _read_cache["rows"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    _read_cache.update(mtime=st.st_mtime, size=st.st_size, rows=rows)
    return rows


def source_engagement_report(jarvis_dir: Path | None = None, days: int = 7) -> dict:
    """Aggregate sent/read/replied/ignored counts by source.

    read receipts are bulk Lark events keyed by message_ids. A source is counted
    as read when any sent row for that source shares a message_id with a read
    event. Responses are already source-attributed by core.engagement.
    """
    jd = jarvis_dir or JARVIS_DIR
    cutoff = time.time() - days * 86400
    rows = [r for r in _read_rows(jd) if (_epoch(r) or 0) >= cutoff]

    stats = defaultdict(lambda: {
        "source": "",
        "sent": 0,
        "read": 0,
        "replied": 0,
        "ignored": 0,
        "late_reply": 0,
        "conversation": 0,
        "_message_ids": set(),
        "_gaps": [],
    })

    read_ids: set[str] = set()
    for row in rows:
        if row.get("type") == "read":
            for mid in row.get("message_ids") or []:
                if isinstance(mid, str) and mid:
                    read_ids.add(mid)

    for row in rows:
        if row.get("type") != "sent":
            continue
        src = row.get("source") or "unknown"
        bucket = stats[src]
        bucket["source"] = src
        bucket["sent"] += 1
        mids = {m for m in (row.get("message_ids") or []) if isinstance(m, str) and m}
        bucket["_message_ids"].update(mids)
        if mids & read_ids:
            bucket["read"] += 1

    for row in rows:
        if row.get("type") != "response":
            continue
        src = row.get("source") or "unknown"
        bucket = stats[src]
        bucket["source"] = src
        reaction = row.get("reaction")
        if reaction in ("engaged", "late_reply"):
            bucket["replied"] += 1
            if reaction == "late_reply":
                bucket["late_reply"] += 1
            try:
                bucket["_gaps"].append(float(row.get("gap_seconds", 0)))
            except (TypeError, ValueError):
                pass
        elif reaction == "ignored":
            bucket["ignored"] += 1
        elif reaction == "conversation":
            bucket["conversation"] += 1

    sources = []
    totals = {"sent": 0, "read": 0, "replied": 0, "ignored": 0}
    for src, bucket in stats.items():
        if src == "conversation":
            continue
        sent = bucket["sent"]
        if sent == 0 and bucket["replied"] == 0 and bucket["ignored"] == 0:
            continue
        read = bucket["read"]
        replied_raw = bucket["replied"]
        # Historical logs may contain duplicate credited replies for one sent
        # card (the old proximity attribution bug). Do not render impossible
        # reply rates above 100%; the raw log remains untouched.
        replied = min(replied_raw, sent) if sent else replied_raw
        ignored = bucket["ignored"]
        for key in totals:
            totals[key] += replied if key == "replied" else bucket[key]
        gaps = bucket["_gaps"]
        sources.append({
            "source": src,
            "sent": sent,
            "read": read,
            "replied": replied,
            "ignored": ignored,
            "late_reply": bucket["late_reply"],
            "reply_rate": round(replied / sent * 100, 1) if sent else 0,
            "read_rate": round(read / sent * 100, 1) if sent else 0,
            "ignored_rate": round(ignored / sent * 100, 1) if sent else 0,
            "median_gap_s": int(statistics.median(gaps)) if gaps else None,
        })
    sources.sort(key=lambda r: (r["reply_rate"], r["sent"]), reverse=True)
    return {
        "days": days,
        "totals": {
            **totals,
            "reply_rate": round(totals["replied"] / totals["sent"] * 100, 1)
            if totals["sent"] else 0,
            "read_rate": round(totals["read"] / totals["sent"] * 100, 1)
            if totals["sent"] else 0,
        },
        "sources": sources,
    }


def _fmt_gap(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"


@ui.page("/engagement")
def engagement_page():
    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.link("← Home", "/").classes("text-gray-500 text-sm")
            ui.label("Engagement").classes("text-2xl font-bold")

        ui.label(
            "真实互动漏斗：sent / read / replied / ignored，直接读取 engagement_log.jsonl。"
        ).classes("text-sm text-gray-600")

        @ui.refreshable
        def content():
            report = source_engagement_report(JARVIS_DIR, days=7)
            totals = report["totals"]
            with ui.row().classes("w-full gap-3"):
                for label, value in (
                    ("sent", totals["sent"]),
                    ("read", totals["read"]),
                    ("replied", totals["replied"]),
                    ("ignored", totals["ignored"]),
                    ("reply rate", f"{totals['reply_rate']}%"),
                ):
                    with ui.card().classes("flex-1 p-3"):
                        ui.label(str(value)).classes("text-xl font-bold")
                        ui.label(label).classes("text-xs text-gray-500")

            if not report["sources"]:
                ui.label("No engagement data in the last 7 days.").classes(
                    "text-gray-400 text-sm italic")
                return

            columns = [
                {"name": "source", "label": "Source", "field": "source", "align": "left"},
                {"name": "sent", "label": "Sent", "field": "sent", "align": "right"},
                {"name": "read", "label": "Read", "field": "read", "align": "right"},
                {"name": "replied", "label": "Replied", "field": "replied", "align": "right"},
                {"name": "ignored", "label": "Ignored", "field": "ignored", "align": "right"},
                {"name": "reply_rate", "label": "Reply %", "field": "reply_rate", "align": "right"},
                {"name": "read_rate", "label": "Read %", "field": "read_rate", "align": "right"},
                {"name": "median_gap", "label": "Median reply", "field": "median_gap", "align": "right"},
            ]
            rows = [
                {**row, "median_gap": _fmt_gap(row["median_gap_s"])}
                for row in report["sources"]
            ]
            ui.table(columns=columns, rows=rows, row_key="source").classes("w-full")

            noisy = [r for r in report["sources"] if r["sent"] >= 3 and r["reply_rate"] < 20]
            if noisy:
                ui.label("Low-reply sources").classes("text-lg font-semibold mt-2")
                for row in noisy:
                    with ui.card().classes("w-full p-3"):
                        ui.label(
                            f"{row['source']}: {row['sent']} sent, "
                            f"{row['reply_rate']}% replied, {row['ignored']} ignored"
                        ).classes("text-sm")

        content()
        guarded_refresh_timer(30, content.refresh)
