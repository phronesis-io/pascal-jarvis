"""Flat memory system for Jarvis Harness (1M context era).

Directory layout:
  memory/
  ├── hot/               — identity, behavioral rules, healing frame
  ├── warm/              — health, cultural, investment, interests, projects
  ├── timeline/          — time-based logs (recent daily + hourly loaded)
  │   ├── hourly_log.md
  │   ├── daily_log.md
  │   ├── daily_archive.md   (not loaded — old entries)
  │   └── hourly_archive.md  (not loaded — old entries)
  └── system/            — todos, open_threads, cross_session_digest

Loading strategy (1M context — load everything):
  - hot/*          → full content (rules first for attention priority)
  - warm/*         → full content (all knowledge base files)
  - system/*.md    → full content (todos, open_threads, digest, insights)
  - timeline/hourly_log.md  → full
  - timeline/daily_log.md   → full (auto-archived after 14 days)
  - Archives       → NOT loaded (hourly_archive, daily_archive)
"""

import re
from datetime import date
from pathlib import Path

# Max chars for the entire memory payload.
# With 1M context (~4M chars), 200KB is <5% and covers all memory comfortably.
MAX_MEMORY_CHARS = 200000

# Files in timeline/ that are archives (never loaded into prompt)
_TIMELINE_SKIP = {
    "hourly_archive.md", "daily_archive.md",
    "longterm_digest.bak.md", "monthly_archive.bak.md",
}



def load_tiered_memory(memory_dir: str | Path) -> str:
    """Load all memory into a single string for system prompt injection.

    With 1M context, everything is loaded unconditionally.
    Order matters for attention: rules first, identity, knowledge, system, timeline last.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return ""

    parts: list[str] = []

    # 1. Hot files — behavioral rules, identity, healing frame (highest priority)
    hot_dir = memory_dir / "hot"
    if hot_dir.is_dir():
        # Load behavioral_rules first for attention priority
        rules = hot_dir / "behavioral_rules.md"
        if rules.exists():
            _append_file(parts, rules, "Behavioral Rules")
        for f in sorted(hot_dir.glob("*.md")):
            if f.name != "behavioral_rules.md":
                _append_file(parts, f, f"Identity: {f.stem}")

    # 2. Warm files — full knowledge base (health, interests, projects, etc.)
    warm_dir = memory_dir / "warm"
    if warm_dir.is_dir():
        for f in sorted(warm_dir.glob("*.md")):
            _append_file(parts, f, f"Knowledge: {f.stem}")

    # 3. System files — todos, open_threads, digest, insights
    sys_dir = memory_dir / "system"
    if sys_dir.is_dir():
        for f in sorted(sys_dir.glob("*.md")):
            _append_file(parts, f, f"System: {f.stem}")

    # 4. Timeline files (end of context — recency attention benefit)
    tl_dir = memory_dir / "timeline"
    if tl_dir.is_dir():
        for f in sorted(tl_dir.glob("*.md")):
            if f.name not in _TIMELINE_SKIP and f.stat().st_size > 0:
                _append_file(parts, f, _timeline_title(f.name))

    result = "\n\n".join(parts)

    # Truncate if over budget (safety net — shouldn't happen)
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
