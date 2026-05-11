"""Tiered memory system for Jarvis Harness.

Directory layout:
  memory/
  ├── _index.md          — compact index of all files (always loaded)
  ├── hot/               — always loaded in full (~6K chars budget)
  ├── warm/              — only name+description loaded; Claude reads on demand
  ├── timeline/          — time-based logs (recent portions loaded)
  │   ├── hourly_log.md
  │   ├── daily_log.md
  │   ├── longterm_digest.md
  │   └── monthly_archive.md
  └── system/            — operational files (todos loaded, rest summarized)

Loading strategy:
  - hot/*          → full content
  - _index.md      → full content (tells Claude what's in warm/)
  - system/todos.md → full content
  - timeline/hourly_log.md    → full (today only)
  - timeline/daily_log.md     → full (kept short by daily compression)
  - timeline/longterm_digest.md → full
  - timeline/monthly_archive.md → full
  - warm/*         → NOT loaded (Claude uses Read tool when needed)
  - system/others  → NOT loaded (referenced in index)
"""

import re
from datetime import date
from pathlib import Path

# Max chars for the entire memory payload to prevent prompt bloat
MAX_MEMORY_CHARS = 20000

# Files in timeline/ that get loaded (archives are excluded)
_TIMELINE_LOAD = {
    "hourly_log.md", "daily_log.md", "longterm_digest.md", "monthly_archive.md",
}

# Files in timeline/ that are archives (never loaded into prompt)
_TIMELINE_SKIP = {
    "hourly_archive.md", "daily_archive.md",
    "longterm_digest.bak.md", "monthly_archive.bak.md",
}

# System files that get loaded in full
_SYSTEM_LOAD = {"todos.md", "cross_session_digest.md", "engagement_insights.md"}


def load_tiered_memory(memory_dir: str | Path) -> str:
    """Load memory tiers into a single string for system prompt injection.

    Returns a string with hot files, index, active system files, and timeline.
    Warm files are NOT included — Claude reads them on demand via Read tool.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return ""

    parts: list[str] = []

    # 1. Hot files — always loaded in full
    hot_dir = memory_dir / "hot"
    if hot_dir.is_dir():
        for f in sorted(hot_dir.glob("*.md")):
            _append_file(parts, f, f"Hot: {f.stem}")

    # 2. Index — tells Claude what's available in warm/
    index_file = memory_dir / "_index.md"
    if index_file.exists():
        _append_file(parts, index_file, "Memory Index")

    # 3. System files (only todos loaded in full)
    sys_dir = memory_dir / "system"
    if sys_dir.is_dir():
        for f in sorted(sys_dir.glob("*.md")):
            if f.name in _SYSTEM_LOAD:
                _append_file(parts, f, f"System: {f.stem}")

    # 4. Timeline files
    tl_dir = memory_dir / "timeline"
    if tl_dir.is_dir():
        # Load in specific order for readability
        for name in ["monthly_archive.md", "longterm_digest.md",
                     "daily_log.md", "hourly_log.md"]:
            f = tl_dir / name
            if f.exists() and f.stat().st_size > 0:
                _append_file(parts, f, _timeline_title(name))

    result = "\n\n".join(parts)

    # Truncate if over budget (shouldn't happen with well-maintained files)
    if len(result) > MAX_MEMORY_CHARS:
        result = result[:MAX_MEMORY_CHARS] + "\n\n[memory truncated — over budget]"

    return result


def _append_file(parts: list[str], path: Path, title: str):
    """Read a file and append as a titled section.

    For calendar_today.md: if the synced date doesn't match today,
    prepend a visible warning so Claude knows the data is stale.
    """
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return
        # Stale calendar detection
        if path.name == "calendar_today.md":
            m = re.search(r"synced (\d{4}-\d{2}-\d{2})", content)
            if m:
                synced = m.group(1)
                today = date.today().isoformat()
                if synced != today:
                    content = (
                        f"⚠️ WARNING: This calendar was last synced on {synced}, "
                        f"but today is {today}. DO NOT trust event times below — "
                        f"they are from a PREVIOUS DAY. Wait for next calendar-sync.\n\n"
                        + content
                    )
        parts.append(f"## {title}\n{content}")
    except OSError:
        pass


def _timeline_title(name: str) -> str:
    """Human-readable title for timeline files."""
    return {
        "hourly_log.md": "Today's Hourly Log",
        "daily_log.md": "Recent Daily Summaries",
        "longterm_digest.md": "Weekly Digest",
        "monthly_archive.md": "Monthly Archive",
    }.get(name, name)
