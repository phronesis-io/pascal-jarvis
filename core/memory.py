"""Tiered memory system for Jarvis Harness.

Memory is organized in layers from fine-grained to coarse:
  Tier 1: Permanent files (user_profile, interaction_principles, etc.)
  Tier 2a: Monthly archive (compressed months)
  Tier 2b: Weekly digest (compressed weeks)
  Tier 3: Daily summaries
  Tier 4: Hourly log (today)
"""

from pathlib import Path

# Files that are working logs or archives — not loaded as Tier 1
_SKIP_TIER1 = {
    "MEMORY.md",
    "hourly_log.md", "daily_log.md", "longterm_digest.md", "monthly_archive.md",
    "hourly_archive.md", "daily_archive.md",
    "longterm_digest.bak.md", "monthly_archive.bak.md",
    # Check-in log is context for the checkin pre-script, not permanent memory
    "checkin_log.md", "checkin_log.jsonl",
}


def load_tiered_memory(memory_dir: str | Path) -> str:
    """Load all memory tiers into a single string for system prompt injection."""
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return ""

    parts = []

    # Tier 1: Permanent memory files (.md only; .jsonl logs are skipped)
    for f in sorted(memory_dir.glob("*.md")):
        if f.name in _SKIP_TIER1:
            continue
        try:
            parts.append(f"## {f.stem}\n{f.read_text(encoding='utf-8').strip()}")
        except OSError:
            # Unreadable file should not poison the whole memory load
            continue

    # Tier 2a: Monthly archive
    _append_if_exists(parts, memory_dir / "monthly_archive.md", "Monthly Archive")

    # Tier 2b: Weekly digest
    _append_if_exists(parts, memory_dir / "longterm_digest.md", "Weekly Digest")

    # Tier 3: Daily summaries
    _append_if_exists(parts, memory_dir / "daily_log.md", "Recent Daily Summaries")

    # Tier 4: Today's hourly log
    _append_if_exists(parts, memory_dir / "hourly_log.md", "Today's Hourly Log")

    return "\n\n".join(parts)


def _append_if_exists(parts: list, path: Path, title: str):
    if path.exists() and path.stat().st_size > 0:
        parts.append(f"## {title}\n{path.read_text(encoding='utf-8').strip()}")
