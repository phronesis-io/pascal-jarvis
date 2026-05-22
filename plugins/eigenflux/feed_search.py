"""Search EigenFlux feed history against the official CLI cache.

The EigenFlux CLI persists each pulled feed page as JSON under:
    ~/.eigenflux/servers/<server>/data/broadcasts/<YYYYMMDD>/feeds-*.json

The CLI keeps an 8-day window. To preserve searches further back, this
module also reads the legacy `eigenflux/feed_store.jsonl` file when present
(written by the old Python client, frozen after the CLI migration).

Public API: `search_feed_history(query, limit=5, server="eigenflux") -> list[dict]`

Each result dict carries:
    item_id, summary, suggestion, url, domains, keywords,
    broadcast_type, source, source_type, updated_at, fetched_at

`source` is "cli-cache" or "legacy-jsonl" so callers can show provenance.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

CLI_HOME = Path(os.environ.get("EIGENFLUX_HOME", str(Path.home() / ".eigenflux")))
LEGACY_JSONL = Path(__file__).resolve().parent.parent.parent / "eigenflux" / "feed_store.jsonl"

# Fields included in the substring match. Keep this list small — searching
# the full JSON dump (old client's approach) matches noise like agent IDs.
SEARCHABLE_FIELDS = ("summary", "suggestion", "url")
SEARCHABLE_LIST_FIELDS = ("domains", "keywords")


def _ts_to_iso(value) -> str:
    """Normalize CLI epoch-ms (int) or legacy ISO (str) to an ISO-8601 string.

    Returns "" for missing / unparseable values so callers can rely on a
    sortable, sliceable string everywhere downstream.
    """
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        # CLI cache stores milliseconds since epoch (1778904666401 ≈ 2026-05-15)
        try:
            secs = value / 1000 if value > 10**12 else value
            return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    if isinstance(value, str):
        return value
    return ""


def _normalize(item: dict, source: str) -> dict:
    """Pick a stable subset of fields. Tolerates schema drift between sources."""
    return {
        "item_id": str(item.get("item_id", "")),
        "summary": item.get("summary", "") or "",
        "suggestion": item.get("suggestion", "") or "",
        "url": item.get("url", "") or "",
        "domains": item.get("domains") or [],
        "keywords": item.get("keywords") or [],
        "broadcast_type": item.get("broadcast_type", "") or "",
        "source_type": item.get("source_type", "") or "",
        # CLI cache emits int epoch ms for updated_at; legacy emits ISO string
        # for fetched_at. Normalize both to ISO so sort/slice is type-safe.
        "updated_at": _ts_to_iso(item.get("updated_at")),
        "fetched_at": _ts_to_iso(item.get("fetched_at")),
        "source": source,
    }


def _matches(item: dict, query_lower: str) -> bool:
    """Case-insensitive substring match against a curated set of fields."""
    for field in SEARCHABLE_FIELDS:
        val = item.get(field) or ""
        if isinstance(val, str) and query_lower in val.lower():
            return True
    for field in SEARCHABLE_LIST_FIELDS:
        for elem in item.get(field) or []:
            if isinstance(elem, str) and query_lower in elem.lower():
                return True
    return False


def _iter_cli_items(server: str):
    """Yield items from CLI cache. Silently skips missing/corrupt files."""
    broadcasts_dir = CLI_HOME / "servers" / server / "data" / "broadcasts"
    if not broadcasts_dir.exists():
        return
    # newest day first so dedup by item_id keeps the latest entry
    for day_dir in sorted(broadcasts_dir.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        for feed_file in sorted(day_dir.glob("feeds-*.json"), reverse=True):
            try:
                data = json.loads(feed_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for it in data.get("items", []):
                if isinstance(it, dict):
                    yield it


def _iter_legacy_items():
    """Yield items from the frozen legacy feed_store.jsonl (best-effort)."""
    if not LEGACY_JSONL.exists():
        return
    try:
        with LEGACY_JSONL.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def search_feed_history(query: str, limit: int = 5,
                        server: str = "eigenflux") -> list[dict]:
    """Search both CLI cache (recent ~8 days) and legacy jsonl (archive).

    Dedupes by item_id, preferring CLI cache (newer schema). Returns up to
    `limit` results sorted by updated_at descending.
    """
    if not query:
        return []
    q = query.lower()
    by_id: dict[str, dict] = {}

    for raw in _iter_cli_items(server):
        if _matches(raw, q):
            item = _normalize(raw, "cli-cache")
            iid = item["item_id"]
            if iid and iid not in by_id:
                by_id[iid] = item

    for raw in _iter_legacy_items():
        if _matches(raw, q):
            item = _normalize(raw, "legacy-jsonl")
            iid = item["item_id"]
            if iid and iid not in by_id:
                by_id[iid] = item

    results = sorted(
        by_id.values(),
        key=lambda x: x.get("updated_at") or x.get("fetched_at") or "",
        reverse=True,
    )
    return results[:limit]


def format_results(results: list[dict]) -> str:
    """Human-readable rendering for Lark output."""
    if not results:
        return "没找到相关内容"
    lines: list[str] = []
    for item in results:
        ts = (item.get("updated_at") or item.get("fetched_at") or "")[:16]
        # Prefer suggestion (more actionable); fall back to summary head.
        headline = item.get("suggestion") or item.get("summary") or ""
        headline = headline.strip().replace("\n", " ")
        if len(headline) > 100:
            headline = headline[:100] + "…"
        prefix = f"• [{ts}] " if ts else "• "
        lines.append(f"{prefix}{headline}")
        if item.get("url"):
            lines.append(f"  {item['url']}")
        lines.append("")
    return "\n".join(lines).rstrip()


if __name__ == "__main__":
    import sys
    query = os.environ.get("JV_QUERY") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not query:
        print("usage: JV_QUERY=<term> python -m plugins.eigenflux.feed_search",
              file=sys.stderr)
        sys.exit(1)
    print(format_results(search_feed_history(query, limit=5)))
