"""Flat memory system for Jarvis Harness (1M context era).

Directory layout:
  memory/
  ├── hot/               — identity, behavioral rules, healing frame
  │   └── structured_facts.md  — load-bearing dated facts (REQ-71, top priority)
  ├── warm/              — health, cultural, investment, interests, projects
  │   └── archive/       — demoted stale warm files (REQ-73, NOT loaded)
  ├── timeline/          — time-based logs (recent daily + hourly loaded)
  │   ├── hourly_log.md
  │   ├── daily_log.md
  │   ├── daily_archive.md   (not loaded — old entries)
  │   └── hourly_archive.md  (not loaded — old entries)
  └── system/            — todos, open_threads, cross_session_digest

Loading strategy (1M context — load everything, but with per-tier budgets):
  - hot/*          → full content (rules first for attention priority)
  - warm/*         → full content (all knowledge base files)
  - system/*.md    → full content (todos, open_threads, digest, insights)
  - timeline/hourly_log.md  → full
  - timeline/daily_log.md   → full (auto-archived after 14 days)
  - Archives       → NOT loaded (hourly_archive, daily_archive, warm/archive/*)

Per-tier budgeting (REQ-73):
  The total payload is capped at MAX_MEMORY_CHARS, but rather than loading
  everything then hard-truncating at the very end (which lands inside the
  LAST-loaded tier — timeline — dropping the freshest cross-day continuity),
  each tier gets a reserved sub-budget and is truncated WITHIN itself. This
  guarantees that an over-budget warm tier can never starve timeline or the
  load-bearing structured facts.

Structured dated facts (REQ-71):
  hot/structured_facts.md holds load-bearing `key: value` lines (e.g.
  `pascal_departure: 2026-06-24`). It is ALWAYS injected with top priority as
  part of the hot reserve and is never truncated. get_fact/set_fact/all_facts
  provide a deterministic read/write API so dates stop being lost across
  sessions.
"""

import re
import sys
from pathlib import Path

from core.timeutil import now_local

# Max chars for the entire memory payload.
# With 1M context (~4M chars), 200KB is <5% and covers all memory comfortably.
MAX_MEMORY_CHARS = 200000

# Per-tier reserved sub-budgets (REQ-73). The freshest continuity tiers get
# guaranteed floors so an over-budget warm tier can't starve them. warm gets
# whatever remains of MAX_MEMORY_CHARS after the other tiers' reserves, so the
# numbers below are reserves/floors, NOT hard caps for warm.
HOT_BUDGET = 25000        # identity + rules + structured facts (never dropped)
TIMELINE_BUDGET = 15000   # freshest cross-day continuity — always survives
SYSTEM_BUDGET = 40000     # todos, open_threads, digest, perception buffers
# warm budget = MAX_MEMORY_CHARS - (HOT + TIMELINE + SYSTEM) reserves.
WARM_BUDGET = MAX_MEMORY_CHARS - HOT_BUDGET - TIMELINE_BUDGET - SYSTEM_BUDGET

# Days after which an unmodified warm/*.md is demoted to warm/archive/ (REQ-73).
WARM_STALE_DAYS = 21

# Structured-facts file (REQ-71). Lives in hot/ so it rides the hot reserve.
STRUCTURED_FACTS_NAME = "structured_facts.md"

# Files in timeline/ that are archives (never loaded into prompt)
_TIMELINE_SKIP = {
    "hourly_archive.md", "daily_archive.md",
    "longterm_digest.bak.md", "monthly_archive.bak.md",
}

# Example skeleton written by set_fact() when no structured_facts file exists.
# Deliberately contains NO real dates — just the field convention.
_STRUCTURED_FACTS_TEMPLATE = """# Structured Facts (load-bearing)

# key: value lines. These are injected with top priority and never truncated.
# Add hard deadlines / load-bearing dates here so they survive across sessions.
# Example fields (fill in real values via set_fact, do not guess):
# pascal_departure: YYYY-MM-DD
# partner_departure: YYYY-MM-DD
"""


def load_tiered_memory(memory_dir: str | Path, purpose: str = "inbound") -> str:
    """Load all memory into a single string for system prompt injection.

    With 1M context, everything is loaded — but each tier is truncated WITHIN
    its own reserved budget (REQ-73) so the freshest continuity data (timeline,
    structured facts) always survives even when warm/ is over budget.

    purpose: "inbound" (default — full view, behavior unchanged) or
    "outbound" — sensitivity gate for tasks whose output leaves Pascal's
    world (eigenflux-publish, auto-replies): system/inbox_private_*.md /
    inbox_secret_*.md perception buffers are skipped so ingested private
    content (mail, DMs) can never ride into an outward-facing context.
    (Perception PRD §3.4/§6 — sensitivity model steps 1-2.)
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return ""

    # Build each tier's sections independently, then truncate within budget.
    hot_parts = _collect_hot(memory_dir)
    warm_parts = _collect_warm(memory_dir)
    system_parts = _collect_system(memory_dir, purpose)
    timeline_parts = _collect_timeline(memory_dir)

    # Truncate within each tier to its reserved budget (most recent / highest
    # priority kept — see per-collector ordering). Hot is never truncated.
    hot_text = _join_within_budget(hot_parts, HOT_BUDGET, "hot")
    warm_text = _join_within_budget(warm_parts, WARM_BUDGET, "warm")
    system_text = _join_within_budget(system_parts, SYSTEM_BUDGET, "system")
    timeline_text = _join_within_budget(timeline_parts, TIMELINE_BUDGET, "timeline")

    blocks = [b for b in (hot_text, warm_text, system_text, timeline_text) if b]
    result = "\n\n".join(blocks)

    # Final safety net: should never trigger now that tiers are budgeted, but
    # keep a hard cap so a pathological hot tier can't blow the context. If it
    # fires, warn loudly — the per-tier budgets failed.
    if len(result) > MAX_MEMORY_CHARS:
        dropped = len(result) - MAX_MEMORY_CHARS
        print(
            f"[memory] WARNING: total payload over MAX_MEMORY_CHARS even after "
            f"per-tier budgeting — hard-truncating {dropped} chars from the tail",
            file=sys.stderr,
        )
        result = result[:MAX_MEMORY_CHARS] + "\n\n[memory truncated — over budget]"

    return result


# ── Tier collectors ──────────────────────────────────────────────────────
# Each returns a list of (title, section_text) — section_text already includes
# the "## {title}\n{content}" framing. Order within the list = priority order
# (earliest = highest priority = kept first when truncating within budget).


def _collect_hot(memory_dir: Path) -> list[str]:
    """Hot tier: structured facts FIRST (load-bearing, top priority), then
    behavioral rules, then the rest of identity. REQ-71: structured_facts is
    always present at the very front so it rides the hot reserve and is never
    truncated away."""
    parts: list[str] = []
    hot_dir = memory_dir / "hot"
    if not hot_dir.is_dir():
        return parts

    # 1. Structured facts — highest priority, always first (REQ-71).
    facts = hot_dir / STRUCTURED_FACTS_NAME
    if facts.exists():
        _append_file(parts, facts, "Structured Facts (load-bearing)")

    # 2. Behavioral rules — attention priority.
    rules = hot_dir / "behavioral_rules.md"
    if rules.exists():
        _append_file(parts, rules, "Behavioral Rules")

    # 3. Remaining identity files.
    for f in sorted(hot_dir.glob("*.md")):
        if f.name in (STRUCTURED_FACTS_NAME, "behavioral_rules.md"):
            continue
        _append_file(parts, f, f"Identity: {f.stem}")
    return parts


def _collect_warm(memory_dir: Path) -> list[str]:
    """Warm tier: full knowledge base. Skips warm/archive/ (demoted stale
    files, REQ-73). Newest-first ordering so when warm is over budget the
    freshest knowledge is kept and stale prep docs fall off first."""
    parts: list[str] = []
    warm_dir = memory_dir / "warm"
    if not warm_dir.is_dir():
        return parts
    # Only top-level *.md — archive/ subdir is deliberately skipped.
    files = [f for f in warm_dir.glob("*.md") if f.is_file()]
    # Newest mtime first → stale docs are the ones dropped when over budget.
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        _append_file(parts, f, f"Knowledge: {f.stem}")
    return parts


def _collect_system(memory_dir: Path, purpose: str) -> list[str]:
    """System tier: todos, open_threads, digest, insights, perception buffers.
    Honors the outbound sensitivity gate."""
    parts: list[str] = []
    sys_dir = memory_dir / "system"
    if not sys_dir.is_dir():
        return parts
    for f in sorted(sys_dir.glob("*.md")):
        if purpose == "outbound" and (f.name.startswith("inbox_private")
                                      or f.name.startswith("inbox_secret")):
            continue
        _append_file(parts, f, f"System: {f.stem}")
    return parts


def _collect_timeline(memory_dir: Path) -> list[str]:
    """Timeline tier (loaded last for recency attention benefit). REQ-73:
    longterm_digest (best cross-day continuity) and the recent daily/hourly
    logs are prioritized so they survive within the reserved timeline budget.
    Archives are never loaded."""
    parts: list[str] = []
    tl_dir = memory_dir / "timeline"
    if not tl_dir.is_dir():
        return parts

    # Priority order within the timeline budget: digest (cross-day continuity)
    # first so it's never the casualty, then daily summaries, then hourly, then
    # anything else. This is the fix for "truncation lands inside timeline and
    # drops the freshly-written longterm_digest".
    priority = {
        "longterm_digest.md": 0,
        "monthly_archive.md": 1,
        "daily_log.md": 2,
        "hourly_log.md": 3,
    }
    files = [
        f for f in tl_dir.glob("*.md")
        if f.name not in _TIMELINE_SKIP and f.is_file() and f.stat().st_size > 0
    ]
    files.sort(key=lambda f: (priority.get(f.name, 99), f.name))
    for f in files:
        _append_file(parts, f, _timeline_title(f.name))
    return parts


def _join_within_budget(parts: list[str], budget: int, tier: str) -> str:
    """Join a tier's sections, truncating WITHIN the tier's reserved budget.

    Sections are added in priority order (highest first). When the budget is
    exhausted, lower-priority sections are dropped and — if the budget lands
    mid-section — that section is hard-cut with a marker. Emits a stderr warning
    whenever anything is dropped so truncation is observable (REQ-73 #2).
    """
    if not parts:
        return ""

    sep = "\n\n"
    out: list[str] = []
    used = 0
    dropped_chars = 0
    truncated = False

    for section in parts:
        add = len(section) + (len(sep) if out else 0)
        if used + add <= budget:
            out.append(section)
            used += add
            continue
        # Over budget. Try to fit a partial slice of this section, then stop.
        remaining = budget - used - (len(sep) if out else 0)
        marker = f"\n\n[{tier} memory truncated — over tier budget]"
        if remaining > len(marker) + 200:
            keep = remaining - len(marker)
            out.append(section[:keep] + marker)
            dropped_chars += len(section) - keep
        else:
            dropped_chars += len(section)
        truncated = True
        # Everything after this is dropped (count it for the warning).
        idx = parts.index(section)
        for later in parts[idx + 1:]:
            dropped_chars += len(later) + len(sep)
        break

    if truncated:
        print(
            f"[memory] WARNING: {tier} tier truncated — dropped ~{dropped_chars} "
            f"chars (budget {budget})",
            file=sys.stderr,
        )

    return sep.join(out)


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
                today = now_local().strftime("%Y-%m-%d")
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


# ── REQ-73: warm staleness demotion ──────────────────────────────────────


def demote_stale_warm(memory_dir: str | Path, stale_days: int = WARM_STALE_DAYS) -> list[str]:
    """Move warm/*.md files unmodified for >= stale_days into warm/archive/.

    Safe maintenance helper (call from a maintenance path, NOT the loader):
      - Only ever MOVES files (never deletes).
      - Only touches warm/*.md (never hot/system/timeline — those are
        load-bearing by tier).
      - Skips files already in warm/archive/, the warm/_index.md, and any
        dotfiles.
      - The loader (_collect_warm) skips warm/archive/, so demoted files stop
        eating the warm budget but remain on disk for on-demand recall.

    Returns the list of filenames that were demoted.
    """
    memory_dir = Path(memory_dir)
    warm_dir = memory_dir / "warm"
    if not warm_dir.is_dir():
        return []

    import time
    cutoff = time.time() - stale_days * 86400
    archive_dir = warm_dir / "archive"
    demoted: list[str] = []

    for f in warm_dir.glob("*.md"):
        if not f.is_file():
            continue
        # Guard load-bearing / index files from demotion.
        if f.name.startswith(".") or f.name == "_index.md":
            continue
        if f.stat().st_mtime >= cutoff:
            continue  # fresh enough — keep loaded
        archive_dir.mkdir(parents=True, exist_ok=True)
        dst = archive_dir / f.name
        # Don't clobber an existing archived copy; suffix to keep both.
        if dst.exists():
            stem, suf = f.stem, f.suffix
            n = 1
            while (archive_dir / f"{stem}.{n}{suf}").exists():
                n += 1
            dst = archive_dir / f"{stem}.{n}{suf}"
        f.rename(dst)
        demoted.append(f.name)

    if demoted:
        print(
            f"[memory] demoted {len(demoted)} stale warm file(s) "
            f"(>{stale_days}d) to warm/archive/: {', '.join(demoted)}",
            file=sys.stderr,
        )
    return demoted


# ── REQ-71: structured dated-facts API ───────────────────────────────────


def _facts_path(memory_dir: str | Path) -> Path:
    return Path(memory_dir) / "hot" / STRUCTURED_FACTS_NAME


_FACT_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*:\s*(.*)$")


def all_facts(memory_dir: str | Path) -> dict[str, str]:
    """Parse hot/structured_facts.md into a {key: value} dict.

    Lines are `key: value`. Blank lines, markdown headings (#...), and comment
    lines (# ...) are ignored. Returns {} if the file is missing.
    """
    path = _facts_path(memory_dir)
    if not path.exists():
        return {}
    facts: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _FACT_LINE.match(line)
        if m:
            facts[m.group(1)] = m.group(2).strip()
    return facts


def get_fact(memory_dir: str | Path, key: str, default: str | None = None) -> str | None:
    """Return the value of a structured fact, or `default` if absent."""
    return all_facts(memory_dir).get(key, default)


def set_fact(memory_dir: str | Path, key: str, value: str) -> None:
    """Set (create or update) a structured fact in hot/structured_facts.md.

    Creates hot/ and the facts file (with an example skeleton) if missing.
    Preserves existing comments/headings; updates the matching key in place or
    appends a new `key: value` line. Deterministic round-trip with get_fact.
    """
    path = _facts_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        path.write_text(_STRUCTURED_FACTS_TEMPLATE, encoding="utf-8")

    lines = path.read_text(encoding="utf-8").splitlines()
    new_line = f"{key}: {value}"
    found = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        m = _FACT_LINE.match(stripped)
        if m and m.group(1) == key:
            lines[i] = new_line
            found = True
            break
    if not found:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
