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
import time
from pathlib import Path

from core.log import log
from core.timeutil import now_local

# Max chars for the entire memory payload.
# With 1M context (~4M chars), 200KB is <5% and covers all memory comfortably.
MAX_MEMORY_CHARS = 200000

# Per-tier reserved sub-budgets (REQ-73). The freshest continuity tiers get
# guaranteed floors so an over-budget warm tier can't starve them. warm gets
# whatever remains of MAX_MEMORY_CHARS after the other tiers' reserves, so the
# numbers below are reserves/floors, NOT hard caps for warm.
# REQ-91 (2026-07-14): budgets re-audited against the measured working set.
# The old numbers were mutually inconsistent with the per-file caps: todos
# (20k) + open_threads (~12k) + roadmap (~9k) + two inbox load-caps summed to
# ~64k against a 40k budget, so the tier's tail (inbox_private_mail — ALL of
# mail-triage's output — and the issue files) was arithmetically guaranteed
# to be invisible every single cycle. hot sat at 24.5k/25k — one busy
# calendar day from silently cutting identity files. The consistency test in
# tests/test_memory.py keeps future cap edits honest against these budgets.
HOT_BUDGET = 30000        # identity + rules + structured facts (never dropped)
TIMELINE_BUDGET = 15000   # freshest cross-day continuity — always survives
# 2026-07-21 记忆瘦身 PRD R6: 56k was exactly the measured working set (56.6k
# assembled) — the tier lived one paragraph from the knife and shaved the tail
# of inbox_private_mail every cycle. 60k restores breathing room.
SYSTEM_BUDGET = 60000     # todos, open_threads, digest, perception buffers
# warm budget = MAX_MEMORY_CHARS - (HOT + TIMELINE + SYSTEM) reserves.
WARM_BUDGET = MAX_MEMORY_CHARS - HOT_BUDGET - TIMELINE_BUDGET - SYSTEM_BUDGET

# Days after which an unmodified warm/*.md is demoted to warm/archive/ (REQ-73).
WARM_STALE_DAYS = 21

# 2026-07-21 记忆瘦身 PRD R3/R5: standing behavioral guidance is timeless —
# feedback_*/user_* files are rarely edited, so BY mtime they always look
# stalest and were the first casualties of both the newest-first drop order
# and any mtime-based demotion, while fat fresh prep docs survived whole.
# These prefixes are (a) loaded first in _collect_warm regardless of mtime,
# (b) never demoted by demote_stale_warm.
PROTECTED_WARM_PREFIXES = ("feedback_", "user_")

# 2026-07-21 记忆瘦身 PRD R4: per-file cap for warm sections (chars,
# head-keep — knowledge docs front-load their summary/index; contrast the
# tail-keep inbox buffers). Without this a single 22k-char roadmap ate 19%
# of the squeezed warm room while 19 small guidance files were dropped.
# 11000 (not 12000): measured against the live corpus, 12k left the total
# assembled payload ~1.5k over the global cap — a permanent sliver of
# truncation and an hourly warn. 11k puts steady state under with headroom.
WARM_FILE_CAP = 11000

# Structured-facts file (REQ-71). Lives in hot/ so it rides the hot reserve.
STRUCTURED_FACTS_NAME = "structured_facts.md"

# Files in timeline/ that are archives (never loaded into prompt)
_TIMELINE_SKIP = {
    "hourly_archive.md", "daily_archive.md",
    "longterm_digest.bak.md", "monthly_archive.bak.md",
}

# Append-only system files (new entries at the TAIL): when a hard cut lands
# mid-section, keep the tail, not the head. todos.md grows via dated
# auto-update appends since April — the head-keep cut had the model reading
# April/May todos while the same-day entries were exactly what got dropped
# (2026-07-07 memory audit). Curated files (open_threads) stay head-keep, so
# this is per-file, not tier-wide. Keys = section header lines.
_TAIL_KEEP_SECTIONS = {"## System: todos"}

# Load-time caps (chars, tail-keep) for the bulky perception inbox buffers.
# INBOX_MAX_LINES=500 lets each buffer reach ~40KB — the two of them alone
# exceed SYSTEM_BUDGET, so they were evicting cross_session_digest /
# pending_updates on every call (2026-07-07 memory audit). Capping at load
# keeps the newest signals visible without letting raw buffers starve the
# load-bearing system files; the full files stay on disk for on-demand Read.
# REQ-92 (2026-07-14): this is also the single source of truth for the ON-DISK
# retention of these buffers — perception._trim_inbox imports it, so what's
# kept on disk ≈ what the loader injects (the old 500-line disk retention kept
# ~35k chars of which only the tail 12k was ever loaded — pure dark matter).
_SYSTEM_FILE_CAPS = {
    "inbox_ops.md": 8000,
    "inbox_private_mail.md": 8000,
}

# Per-tier timestamp of the last structured truncation warn — heartbeat calls
# the loader every cycle, and 400+ identical warns/day buried the signal (the
# multi-day truncation of 2026-07 stayed invisible). The original once-per-
# PROCESS set re-suppressed the very thing it was built to surface: a long-
# lived heartbeat process warned once and then stayed silent for a NEW
# truncation episode days later, invisible to selfmon's window-based counting
# (2026-07-08 red-team fix). Re-warn when >1h has passed for that tier —
# per-call CLI processes still warn at most once per run.
_TRUNCATION_WARN_INTERVAL_S = 3600
_TRUNCATION_WARNED_AT: dict[str, float] = {}

# Example skeleton written by set_fact() when no structured_facts file exists.
# Deliberately contains NO real dates — just the field convention.
_STRUCTURED_FACTS_TEMPLATE = """# Structured Facts (load-bearing)

# key: value lines. These are injected with top priority and never truncated.
# Add hard deadlines / load-bearing dates here so they survive across sessions.
# Example fields (fill in real values via set_fact, do not guess):
# pascal_departure: YYYY-MM-DD
# partner_departure: YYYY-MM-DD
"""


def load_tiered_memory(memory_dir: str | Path, purpose: str = "inbound",
                       max_chars: int | None = None) -> str:
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

    max_chars: override the global memory budget. Used when the backup LLM
    relay has a smaller context window than the primary (1M) channel.
    Sub-tier budgets scale proportionally.
    """
    memory_dir = Path(memory_dir)
    if not memory_dir.is_dir():
        return ""

    if max_chars is not None and int(max_chars) <= 0:
        raise ValueError("max_chars must be positive")
    budget = int(max_chars) if max_chars is not None else MAX_MEMORY_CHARS
    if budget < MAX_MEMORY_CHARS:
        ratio = max_chars / MAX_MEMORY_CHARS
        hot_budget = int(HOT_BUDGET * ratio)
        system_budget = int(SYSTEM_BUDGET * ratio)
        timeline_budget = int(TIMELINE_BUDGET * ratio)
    else:
        hot_budget = HOT_BUDGET
        system_budget = SYSTEM_BUDGET
        timeline_budget = TIMELINE_BUDGET

    # Build each tier's sections independently (priority-ordered within tier).
    hot_parts = _collect_hot(memory_dir)
    warm_parts = _collect_warm(memory_dir)
    system_parts = _collect_system(memory_dir, purpose)
    timeline_parts = _collect_timeline(memory_dir)

    sep = "\n\n"
    full = {
        "hot": sep.join(hot_parts),
        "warm": sep.join(warm_parts),
        "system": sep.join(system_parts),
        "timeline": sep.join(timeline_parts),
    }
    total = sum(len(v) for v in full.values()) + sep.count("") * 3

    # COMMON CASE — everything fits under the global cap → load it ALL, no
    # truncation (red-team fix: per-tier budgets were dropping load-bearing
    # system files — open_threads/todos/pending_updates — even though the
    # total was UNDER MAX_MEMORY_CHARS, with 15KB of headroom unused. Per-tier
    # reserves are FLOORS for the over-budget case, never caps that throw away
    # headroom).
    if total <= budget:
        blocks = [full[t] for t in ("hot", "warm", "system", "timeline") if full[t]]
        return sep.join(blocks)

    # OVER BUDGET — apply per-tier reserves. hot + system + timeline get their
    # reserves (load-bearing); warm absorbs the squeeze with the remainder. If
    # a reserved tier is under its reserve, the slack flows to warm.
    hot_text = _join_within_budget(hot_parts, hot_budget, "hot")
    system_text = _join_within_budget(system_parts, system_budget, "system")
    timeline_text = _join_within_budget(timeline_parts, timeline_budget, "timeline")
    used = len(hot_text) + len(system_text) + len(timeline_text)
    warm_room = max(0, budget - used - 3 * len(sep))
    warm_text = _join_within_budget(warm_parts, warm_room, "warm")

    blocks = [b for b in (hot_text, warm_text, system_text, timeline_text) if b]
    result = sep.join(blocks)

    # Final hard safety net (should never fire now).
    if len(result) > budget:
        marker = "\n\n[memory truncated - over budget]"
        keep = max(0, budget - len(marker))
        result = result[:keep] + marker[:budget - keep]
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
    files, REQ-73).

    Ordering (2026-07-21 记忆瘦身 PRD R3): protected guidance band first
    (PROTECTED_WARM_PREFIXES — timeless behavioral rules whose mtime never
    reflects importance), then the rest newest-first so stale prep docs fall
    off first when over budget. Every section is capped at WARM_FILE_CAP
    (head-keep, R4) so one fat doc can't starve the band below it."""
    parts: list[str] = []
    warm_dir = memory_dir / "warm"
    if not warm_dir.is_dir():
        return parts
    # Only top-level *.md — archive/ subdir is deliberately skipped.
    files = [f for f in warm_dir.glob("*.md") if f.is_file()]
    # Guard stat() (red-team fix): a file vanishing between glob and sort
    # (demote_stale_warm rename / external move) raised FileNotFoundError out
    # of load_tiered_memory, aborting the whole prompt build.
    def _mtime(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0
    protected = sorted((f for f in files
                        if f.name.startswith(PROTECTED_WARM_PREFIXES)),
                       key=lambda f: f.name)
    rest = sorted((f for f in files
                   if not f.name.startswith(PROTECTED_WARM_PREFIXES)),
                  key=_mtime, reverse=True)
    for f in protected + rest:
        _append_file(parts, f, f"Knowledge: {f.stem}",
                     cap=WARM_FILE_CAP, keep="head")
    return parts


def _collect_system(memory_dir: Path, purpose: str) -> list[str]:
    """System tier: todos, open_threads, digest, insights, perception buffers.
    Honors the outbound sensitivity gate.

    Priority-ordered (red-team fix): the load-bearing operational files
    (open_threads — drives heartbeat proactive follow-up per CLAUDE.md, todos,
    pending_updates, the digest) come FIRST so that if the tier is ever
    truncated the casualties are the bulky perception buffers (inbox_ops /
    inbox_private_mail), never Pascal's todos/threads. Previously plain
    alphabetical, so inbox_ops (21KB) ate the budget and dropped open_threads."""
    parts: list[str] = []
    sys_dir = memory_dir / "system"
    if not sys_dir.is_dir():
        return parts
    priority = {
        "open_threads.md": 0,
        "todos.md": 1,
        "pending_updates.md": 2,
        "cross_session_digest.md": 3,
        "engagement_insights.md": 4,
        "engineering_roadmap.md": 5,
    }
    files = [f for f in sys_dir.glob("*.md") if f.is_file()]
    files.sort(key=lambda f: (priority.get(f.name, 50), f.name))
    for f in files:
        if purpose == "outbound" and (f.name.startswith("inbox_private")
                                      or f.name.startswith("inbox_secret")):
            continue
        _append_file(parts, f, f"System: {f.stem}",
                     cap=_SYSTEM_FILE_CAPS.get(f.name))
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
    def _nonempty(f):
        try:
            return f.stat().st_size > 0
        except OSError:
            return False
    files = [
        f for f in tl_dir.glob("*.md")
        if f.name not in _TIMELINE_SKIP and f.is_file() and _nonempty(f)
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
        dropped_sections: list[str] = []
        if remaining > len(marker) + 200:
            keep = remaining - len(marker)
            header, _, body = section.partition("\n")
            if header in _TAIL_KEEP_SECTIONS and keep > len(header) + 1:
                # Append-only file: keep the header + newest TAIL — the head
                # is months-old history (see _TAIL_KEEP_SECTIONS). The
                # omission note sits ABOVE the kept tail (right after the
                # header) and names the OLDEST entries as the casualty — the
                # old bottom marker read as "newest entries were cut", the
                # exact confusion tail-keep was built to remove (2026-07-08
                # red-team fix).
                omitted = max(0, len(section) - remaining)
                fname = (header.rpartition(":")[2].strip() or "todos") + ".md"
                note = (f"\n[oldest ~{omitted} chars omitted — tail kept; "
                        f"full file on disk: {fname}]\n")
                tail = body[len(body) - max(0, remaining - len(header) - len(note)):]
                # Snap forward to the next entry boundary so the tail never
                # opens mid-entry (best-effort — raw slice when none found).
                snap = tail.find("\n<!-- auto-update")
                if snap != -1:
                    tail = tail[snap + 1:]
                out.append(header + note + tail)
                dropped_chars += max(0, len(section) - len(header) - len(tail))
            else:
                out.append(section[:keep] + marker)
                dropped_chars += len(section) - keep
            # Name the partially-cut section too — with a single big file
            # (the todos case) dropped_sections was [] exactly when the warn
            # mattered most (2026-07-08 red-team fix).
            dropped_sections.append(f"{header} (partial)")
        else:
            dropped_chars += len(section)
            dropped_sections.append(section.split("\n", 1)[0])
        truncated = True
        # Everything after this is dropped (count it for the warning).
        idx = parts.index(section)
        dropped_sections += [p.split("\n", 1)[0] for p in parts[idx + 1:]]
        for later in parts[idx + 1:]:
            dropped_chars += len(later) + len(sep)
        break

    if truncated:
        # Bare stderr line kept for backward compat (tests + historical grep
        # of jarvis.log count this exact string).
        print(
            f"[memory] WARNING: {tier} tier truncated — dropped ~{dropped_chars} "
            f"chars (budget {budget})",
            file=sys.stderr,
        )
        # Structured leveled warn so selfmon/alerting can see it — the bare
        # print bypassed leveled logging and a multi-day truncation stayed
        # invisible (2026-07-07 memory audit). Rate-limited per tier (see
        # _TRUNCATION_WARNED_AT) so heartbeat doesn't emit 400+/day, but a
        # persistent or NEW episode keeps re-surfacing hourly.
        now = time.time()
        if now - _TRUNCATION_WARNED_AT.get(tier, 0) >= _TRUNCATION_WARN_INTERVAL_S:
            _TRUNCATION_WARNED_AT[tier] = now
            # expected=True on warm: warm absorbing the global squeeze is the
            # loader's DESIGN, not a failure — selfmon's silent-failure scan
            # skips expected entries, so only hot/system/timeline truncation
            # (always a real problem) is counted (REQ-94).
            log("memory", "tier_truncated", level="warn", tier=tier,
                dropped_chars=dropped_chars, budget=budget,
                dropped_sections=dropped_sections,
                expected=(tier == "warm"))

    return sep.join(out)


# REQ-100 (2026-07-14 group chat): the ONLY memory a group-chat session gets.
# Group sessions must never load the tiered memory — hot/warm/system/timeline
# all carry the owner's private life (health, schedule, contacts, mail
# summaries) and any group member can drive the session. Opt-in by curation:
# hot/group_context.md holds what groups MAY know (public professional
# context); absent file = a minimal generic line.
_GROUP_CONTEXT_NAME = "group_context.md"
_GROUP_CONTEXT_FALLBACK = (
    "## Group Context\n"
    "(未配置 group_context.md — 你只知道自己是主人的 AI 助手，"
    "没有关于主人的其他可分享信息。)"
)


def load_group_context(memory_dir) -> str:
    """Curated group-visible context — NEVER the tiered memory."""
    path = Path(memory_dir) / "hot" / _GROUP_CONTEXT_NAME
    try:
        content = path.read_text(encoding="utf-8").strip()
        if content:
            return f"## Group Context (可对群成员使用的上下文)\n{content}"
    except OSError:
        pass
    return _GROUP_CONTEXT_FALLBACK


def _append_file(parts: list[str], path: Path, title: str, cap: int | None = None,
                 keep: str = "tail"):
    """Read a file and append as a titled section.

    For calendar_today.md: if the synced date doesn't match today,
    prepend a visible warning so Claude knows the data is stale.

    cap: optional per-file char cap. keep="tail" (default) suits append-only
    buffers whose newest entries sit at the bottom (inbox files, see
    _SYSTEM_FILE_CAPS); keep="head" suits curated knowledge docs that
    front-load their summary (warm tier, WARM_FILE_CAP).
    """
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return
        if cap is not None and len(content) > cap:
            if keep == "head":
                head = content[:cap]
                # Snap back to the last line boundary so the kept view never
                # ends mid-sentence — but only when the boundary is NEAR the
                # cap. A short first line followed by one unbroken 12k blob
                # would otherwise collapse the whole section to 4 chars
                # (red-team finding: snap needs a floor; raw slice instead).
                snap = head.rfind("\n")
                if snap >= cap - 200:
                    head = head[:snap]
                content = head + (
                    f"\n[capped — newest/rest {len(content) - len(head)} chars "
                    f"omitted; full file on disk: {path.name}]"
                )
            else:
                tail = content[-cap:]
                # Snap forward to the next entry boundary so the injected view
                # never opens mid-entry (capped files are `### `-delimited
                # perception buffers; raw slice when no boundary is found).
                snap = tail.find("\n### ")
                if snap != -1:
                    tail = tail[snap + 1:]
                content = (
                    f"[capped — oldest {len(content) - len(tail)} chars omitted; "
                    f"full file on disk: {path.name}]\n" + tail
                )
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


# 记忆瘦身 PRD R5（红队修正版）：mtime 判据的豁免面。
# — 前缀：feedback_/user_（永恒准则，见 PROTECTED_WARM_PREFIXES）
# — frontmatter type：文件自我声明的保护类。user/feedback 与前缀同义；
#   question/project 归 thinking-review 任务做"还活着吗"的人工裁决
#   （HEARTBEAT.md thinking-review），机器 demote 不得抢跑。
# — 具名：memory-consolidate 的 prompt 硬编码 warm/projects.md 为 UPDATE
#   目标；归档它会让每日项目事实静默落空（红队 finding 3）。
_DEMOTION_EXEMPT_TYPES = re.compile(
    r"^type:\s*(?:user|feedback|question|project)\s*$",
    re.MULTILINE | re.IGNORECASE)
_DEMOTION_EXEMPT_NAMES = {"projects.md"}


def _warm_demotion_exempt(f: Path) -> bool:
    if f.name in _DEMOTION_EXEMPT_NAMES:
        return True
    if f.name.startswith(PROTECTED_WARM_PREFIXES):
        return True
    try:
        head = f.read_text(encoding="utf-8")[:2000]
    except OSError:
        return True  # unreadable — never archive what we can't inspect
    if not head.startswith("---"):
        return False
    fm_end = head.find("\n---", 3)
    frontmatter = head[:fm_end] if fm_end != -1 else head
    return bool(_DEMOTION_EXEMPT_TYPES.search(frontmatter))


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
        if _warm_demotion_exempt(f):
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
    # Sanitize (red-team fix): a newline/colon in value would inject a phantom
    # fact line that all_facts parses and that re-setting the key can't remove.
    key = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(key)).strip("_") or "key"
    value = re.sub(r"\s+", " ", str(value)).strip()

    path = _facts_path(memory_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic, locked read-modify-write (red-team fix): the bot is multi-process
    # (heartbeat + handle_message + tasks share memory_dir); an unlocked
    # truncate-then-write could lose a concurrent set or be read half-written.
    import fcntl, os as _os
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)
        except OSError:
            pass
        if not path.exists():
            path.write_text(_STRUCTURED_FACTS_TEMPLATE, encoding="utf-8")
        lines = path.read_text(encoding="utf-8").splitlines()
        new_line = f"{key}: {value}"
        found = False
        kept = []
        for raw in lines:
            stripped = raw.strip()
            m = _FACT_LINE.match(stripped) if not stripped.startswith("#") else None
            if m and m.group(1) == key:
                if not found:
                    kept.append(new_line)   # replace first occurrence
                    found = True
                # drop any further duplicate lines for this key (consistency)
                continue
            kept.append(raw)
        if not found:
            if kept and kept[-1].strip() != "":
                kept.append("")
            kept.append(new_line)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
        _os.replace(tmp, path)
