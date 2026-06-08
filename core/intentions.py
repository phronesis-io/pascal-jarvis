"""Intent manager — future-directed actions with unique context.

An Intent is NOT a reminder. It's: "at time T, wake up with context C and
execute action A." Each intent has its own prompt/context, so the agent
at that moment knows exactly what to think about.

Lifecycle: create → pending → triggered → executed | expired | cancelled

Architecture:
  - Stored in SQLite `intentions` table (via dashboard.db)
  - Checked every heartbeat cycle by intention-check task
  - Can be created by: agent (ACTION:intent_create), calendar bridge, seed script
  - Supports: one-shot (date), recurring (cron), relative (interval), chains
"""

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# DB helpers — thin wrappers over dashboard.db
# ---------------------------------------------------------------------------

def _get_db():
    import sys
    sys.path.insert(0, str(ROOT))
    from dashboard.db import get_db
    return get_db()


def _ensure_table():
    """Create intentions table if not exists (called on first use)."""
    db = _get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS intentions (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'agent',
            status TEXT NOT NULL DEFAULT 'pending',
            trigger_type TEXT NOT NULL DEFAULT 'date',
            trigger_config TEXT NOT NULL DEFAULT '{}',
            prompt TEXT NOT NULL DEFAULT '',
            context TEXT NOT NULL DEFAULT '{}',
            action_type TEXT NOT NULL DEFAULT 'prompt',
            action_config TEXT NOT NULL DEFAULT '{}',
            conditions TEXT DEFAULT '[]',
            priority INTEGER DEFAULT 5,
            chain_next TEXT,
            purpose TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            triggered_at TEXT,
            executed_at TEXT,
            expires_at TEXT,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_intentions_status ON intentions(status);
        CREATE INDEX IF NOT EXISTS idx_intentions_trigger ON intentions(trigger_type);
    """)
    db.commit()


_table_ready = False

def _init():
    global _table_ready
    if not _table_ready:
        try:
            _ensure_table()
            _table_ready = True
        except Exception:
            # Reset flag so next call retries (handles DB reconnection)
            _table_ready = False
            raise


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_intent(
    name: str,
    trigger_type: str,  # "date" | "cron" | "interval" | "event"
    trigger_config: dict,
    prompt: str = "",
    context: dict | None = None,
    action_type: str = "prompt",  # "prompt" | "notify" | "lark_card" | "script"
    action_config: dict | None = None,
    conditions: list | None = None,
    priority: int = 5,
    chain_next: str | None = None,
    purpose: str = "",
    tags: list[str] | None = None,
    source: str = "agent",
    expires_at: str | None = None,
    intent_id: str | None = None,
) -> str:
    """Create a new intent. Returns intent ID."""
    _init()
    db = _get_db()
    iid = intent_id or f"int_{uuid.uuid4().hex[:10]}"
    now = now_local_str("%Y-%m-%dT%H:%M:%S")

    db.execute(
        """INSERT OR REPLACE INTO intentions
           (id, name, source, status, trigger_type, trigger_config,
            prompt, context, action_type, action_config, conditions,
            priority, chain_next, purpose, tags, created_at, expires_at)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (iid, name, source, trigger_type,
         json.dumps(trigger_config, ensure_ascii=False), prompt,
         json.dumps(context or {}, ensure_ascii=False), action_type,
         json.dumps(action_config or {}, ensure_ascii=False),
         json.dumps(conditions or [], ensure_ascii=False),
         priority, chain_next, purpose,
         json.dumps(tags or [], ensure_ascii=False), now, expires_at),
    )
    db.commit()
    return iid


def list_intents(
    status: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List intents, optionally filtered."""
    _init()
    db = _get_db()
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if source:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"SELECT * FROM intentions {where} ORDER BY priority, created_at LIMIT ?",
        params + [limit],
    ).fetchall()
    results = [dict(r) for r in rows]
    if tags:
        tag_set = set(tags)
        results = [r for r in results
                    if tag_set & set(json.loads(r.get("tags", "[]")))]
    return results


def get_intent(intent_id: str) -> dict | None:
    _init()
    db = _get_db()
    row = db.execute("SELECT * FROM intentions WHERE id = ?", (intent_id,)).fetchone()
    return dict(row) if row else None


def cancel_intent(intent_id: str, reason: str = "") -> bool:
    """Retire an intent from ANY state (pending/triggered/executed/expired).

    Previously this only accepted pending/triggered, so 'executed' residue —
    e.g. junk one-shot intents that already fired (the 2026-06-08 test-intent
    storm) — could not be cleared through the agent and needed a raw DB DELETE.
    The agent now has one reliable verb to make any intent go away. Cancelling
    an already-cancelled intent returns True idempotently. Returns False only
    when the intent does not exist.

    Note: cancel marks status='cancelled' but keeps the row (audit trail). To
    physically remove rows use delete_intent / the `purge` CLI.
    """
    _init()
    db = _get_db()
    row = db.execute("SELECT status FROM intentions WHERE id = ?", (intent_id,)).fetchone()
    if not row:
        return False
    if row[0] == "cancelled":
        return True
    db.execute(
        "UPDATE intentions SET status = 'cancelled', last_error = ? WHERE id = ?",
        (reason or f"cancelled (was {row[0]})", intent_id),
    )
    db.commit()
    return True


def update_intent(intent_id: str, **kwargs) -> bool:
    """Update mutable fields of an intent."""
    _init()
    allowed = {"name", "prompt", "context", "action_config", "conditions",
               "priority", "purpose", "tags", "trigger_config", "expires_at", "status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    for k in ("context", "action_config", "conditions", "tags", "trigger_config"):
        if k in updates and not isinstance(updates[k], str):
            updates[k] = json.dumps(updates[k], ensure_ascii=False)
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [intent_id]
    db = _get_db()
    db.execute(f"UPDATE intentions SET {set_clause} WHERE id = ?", values)
    db.commit()
    return True


def delete_intent(intent_id: str) -> bool:
    _init()
    db = _get_db()
    db.execute("DELETE FROM intentions WHERE id = ?", (intent_id,))
    db.commit()
    return True


def cleanup_expired():
    """Mark expired intents."""
    _init()
    db = _get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE intentions SET status = 'expired' WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < ?",
        (now,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Trigger evaluation
# ---------------------------------------------------------------------------

def _coerce(dt: datetime) -> datetime:
    """Make `dt` comparable to now_local() regardless of tz-awareness.

    Stored timestamps (created_at, trigger datetime) are local-time strings
    written WITHOUT an offset, so datetime.fromisoformat() returns a *naive*
    datetime — while now_local() is tz-aware. Comparing the two raises
    TypeError, which previously crashed the entire due-check whenever an
    interval intent was pending, and silently skipped naive-target date
    intents via the surrounding try/except. This aligns `dt` to now_local()'s
    awareness (works in both directions, so it's safe if now_local ever
    becomes naive).
    """
    ref = now_local()
    if ref.tzinfo is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=ref.tzinfo)
    if ref.tzinfo is None and dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def get_due_intents() -> list[dict]:
    """Find all pending intents whose trigger condition is met NOW."""
    _init()
    cleanup_expired()
    db = _get_db()
    pending = db.execute(
        "SELECT * FROM intentions WHERE status = 'pending' ORDER BY priority"
    ).fetchall()

    now = now_local()
    due = []

    for row in pending:
        intent = dict(row)
        trigger_type = intent["trigger_type"]
        try:
            trigger_config = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
        except (json.JSONDecodeError, TypeError):
            trigger_config = {}
        try:
            conditions = json.loads(intent["conditions"]) if isinstance(intent["conditions"], str) else (intent["conditions"] or [])
        except (json.JSONDecodeError, TypeError):
            conditions = []

        triggered = False

        if trigger_type == "date":
            target = trigger_config.get("datetime", "")
            if target:
                try:
                    target_dt = _coerce(datetime.fromisoformat(target))
                    triggered = now >= target_dt
                except (ValueError, TypeError):
                    pass  # Skip intents with malformed datetime

        elif trigger_type == "cron":
            from dashboard.scheduler import cron_matches
            expr = trigger_config.get("expression", "")
            triggered = cron_matches(expr, now)
            # Prevent re-triggering within same minute
            if triggered and intent.get("executed_at"):
                last = _coerce(datetime.fromisoformat(intent["executed_at"]))
                if (now - last).total_seconds() < 60:
                    triggered = False

        elif trigger_type == "interval":
            seconds = trigger_config.get("seconds", 600)
            created = _coerce(datetime.fromisoformat(intent["created_at"]))
            triggered = (now - created).total_seconds() >= seconds

        elif trigger_type == "event":
            # Event-based triggers are handled separately via event bus
            pass

        # Check conditions (reuse scheduler's condition engine)
        if triggered and conditions:
            from dashboard.scheduler import check_conditions
            if not check_conditions(conditions, intent):
                triggered = False

        if triggered:
            due.append(intent)

    return due


def mark_triggered(intent_id: str):
    """Mark an intent as triggered (being processed)."""
    _init()
    db = _get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE intentions SET status = 'triggered', triggered_at = ? WHERE id = ?",
        (now, intent_id),
    )
    db.commit()


def _is_overdue_oneshot(intent: dict, ref: datetime | None = None) -> bool:
    """True if `intent` is a one-shot `date` intent whose trigger time is past.

    Such an intent can never usefully fire again: get_due_intents marks any
    past-dated `date` intent as due, so resetting it to 'pending' makes it
    re-fire immediately. Used by reset_stale_triggered to break the resurrection
    loop (see that function's docstring).
    """
    if intent.get("trigger_type") != "date":
        return False
    try:
        cfg = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
    except (json.JSONDecodeError, TypeError):
        return False
    target = (cfg or {}).get("datetime", "")
    if not target:
        return False
    try:
        target_dt = _coerce(datetime.fromisoformat(target))
    except (ValueError, TypeError):
        return False
    return target_dt < (ref or now_local())


def reset_stale_triggered(stale_minutes: int = 10) -> int:
    """Recover intents stuck in 'triggered' state.

    If a heartbeat cycle crashes between mark_triggered and mark_executed
    (Claude timeout, JSON parse failure, post-script crash, etc.) the intent
    stays 'triggered' forever and never re-fires. This bumps recoverable ones
    back to 'pending' after `stale_minutes` so they get another chance.

    EXCEPTION — the resurrection loop: a one-shot `date` intent whose trigger
    time is already in the past must NOT be reset to 'pending'. get_due_intents
    treats any past-dated `date` intent as due, so it would re-fire instantly,
    and if execution keeps failing it re-sticks in 'triggered' → it reappears
    every single cycle forever. This is exactly the 2026-06-08 storm: junk
    intents dated 2026-01-01 resurrected on every heartbeat. Those are marked
    'expired' instead (with a last_error breadcrumb), terminating the loop.

    Returns the count of intents reset to 'pending' (expired ones are NOT
    counted — they were retired, not recovered).
    """
    _init()
    db = _get_db()
    cutoff = (now_local() - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    stuck = db.execute(
        "SELECT * FROM intentions "
        "WHERE status = 'triggered' AND triggered_at IS NOT NULL AND triggered_at < ?",
        (cutoff,),
    ).fetchall()

    now = now_local()
    reset = 0
    for row in stuck:
        intent = dict(row)
        if _is_overdue_oneshot(intent, now):
            db.execute(
                "UPDATE intentions SET status = 'expired', last_error = ? WHERE id = ?",
                ("auto-expired: overdue one-shot stuck in triggered "
                 "(would resurrection-loop if reset to pending)", intent["id"]),
            )
        else:
            db.execute(
                "UPDATE intentions SET status = 'pending', last_error = ? WHERE id = ?",
                (f"auto-reset after {stale_minutes}m stuck in triggered", intent["id"]),
            )
            reset += 1
    db.commit()
    return reset


def mark_executed(intent_id: str, result: str = ""):
    """Mark an intent as executed. Handle recurring (cron) reset and chains."""
    _init()
    db = _get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")

    intent = get_intent(intent_id)
    if not intent:
        return

    if intent["trigger_type"] == "cron":
        # Recurring: reset to pending for next trigger
        db.execute(
            "UPDATE intentions SET status = 'pending', executed_at = ?, last_error = ? WHERE id = ?",
            (now, result, intent_id),
        )
    else:
        # One-shot: mark executed
        db.execute(
            "UPDATE intentions SET status = 'executed', executed_at = ?, last_error = ? WHERE id = ?",
            (now, result, intent_id),
        )
    db.commit()

    # Handle chain
    if intent.get("chain_next"):
        chain_data = json.loads(intent["chain_next"]) if isinstance(intent["chain_next"], str) else intent["chain_next"]
        if isinstance(chain_data, dict) and chain_data.get("name"):
            create_intent(**chain_data)


def mark_failed(intent_id: str, error: str):
    _init()
    db = _get_db()
    db.execute(
        "UPDATE intentions SET last_error = ? WHERE id = ?",
        (error, intent_id),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Calendar bridge — auto-generate prep intents from calendar events
# ---------------------------------------------------------------------------

def generate_calendar_intents(calendar_md: str) -> list[str]:
    """Parse calendar markdown and create prep intents for upcoming events.

    Creates intents like:
    - 30min before meeting: "prepare context for meeting X"
    - Morning of event day: "today you have X, remember to bring Y"
    - 2 days before activity: "prepare for activity X"

    Returns list of created intent IDs.
    """
    import re
    _init()
    db = _get_db()
    created = []

    # Parse events from calendar markdown
    # Format: "  HH:MM-HH:MM  Title  (optional details)"
    lines = calendar_md.strip().splitlines()
    current_date = None
    date_label = ""

    for line in lines:
        # Detect date headers
        date_match = re.match(r'^(Today|Tomorrow|Day \d+)\s+\((\d{4}-\d{2}-\d{2})', line)
        if date_match:
            date_label = date_match.group(1)
            current_date = date_match.group(2)
            continue

        if not current_date:
            continue

        # Parse event lines
        event_match = re.match(r'^\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s+(.+?)(?:\s+\((.+)\))?\s*$', line)
        if not event_match:
            continue

        start_time = event_match.group(1)
        end_time = event_match.group(2)
        title = event_match.group(3).strip()
        details = event_match.group(4) or ""

        event_dt = _coerce(datetime.fromisoformat(f"{current_date}T{start_time}:00"))

        # Skip past events
        if event_dt < now_local():
            continue

        # Create prep intent: 30min before for meetings, 2 days before for activities
        intent_tag = f"cal:{current_date}:{start_time}:{title[:20]}"

        # Check if already created. Use JSON parse (LIKE pre-filter is unreliable
        # because json.dumps may store Chinese as \uXXXX while intent_tag is raw UTF-8).
        existing = db.execute(
            "SELECT tags FROM intentions WHERE status = 'pending' AND source = 'calendar'"
        ).fetchall()
        already_exists = False
        for row in existing:
            try:
                row_tags = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if intent_tag in row_tags:
                    already_exists = True
                    break
            except (json.JSONDecodeError, TypeError):
                pass
        if already_exists:
            continue

        # Determine prep time based on event type
        hours_until = (event_dt - now_local()).total_seconds() / 3600

        if hours_until > 48:
            # Far future — fire at 09:00 the day before the event
            prep_dt = (event_dt - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            prep_prompt = f"明天 {start_time} 有 {title}。帮 Pascal 做准备：回忆相关上下文、准备需要带的东西、确认地点和时间。"
        elif hours_until > 2:
            # Same day or tomorrow — 30min before
            prep_dt = event_dt - timedelta(minutes=30)
            prep_prompt = f"{title} 在 {start_time} 开始（还有 30 分钟）。快速回顾：这个会/活动的目的是什么？有什么需要提前准备的？"
        else:
            # Too close, skip
            continue

        if prep_dt < now_local():
            continue

        iid = create_intent(
            name=f"Prep: {title}",
            trigger_type="date",
            trigger_config={"datetime": prep_dt.isoformat()},
            prompt=prep_prompt,
            context={"event_title": title, "event_time": f"{start_time}-{end_time}",
                      "event_date": current_date, "event_details": details},
            action_type="notify",
            action_config={"type": "prep_reminder"},
            purpose=f"为 {title} 做心理和实际准备",
            tags=[intent_tag, "calendar-prep"],
            source="calendar",
            expires_at=event_dt.isoformat(),
        )
        created.append(iid)

    return created


# ---------------------------------------------------------------------------
# Rendering for heartbeat
# ---------------------------------------------------------------------------

def format_due_intents_for_claude(intents: list[dict]) -> str:
    """Format due intents as a prompt for Claude to process."""
    if not intents:
        return ""

    parts = ["[INTENTION EXECUTION]",
             "The following intents are due. For each, execute the prompt with the given context.",
             "Return JSON: {\"intents\": {\"<intent_id>\": {\"response\": \"<text>\", \"action\": \"notify|silent|chain\"}}}",
             ""]

    for intent in intents:
        ctx = json.loads(intent["context"]) if isinstance(intent["context"], str) else intent["context"]
        parts.append(f"--- Intent: {intent['id']} ({intent['name']}) ---")
        parts.append(f"Purpose: {intent['purpose']}")
        parts.append(f"Prompt: {intent['prompt']}")
        if ctx:
            parts.append(f"Context: {json.dumps(ctx, ensure_ascii=False)}")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Always-on snapshot — keeps ALL active intents in context every cycle
# ---------------------------------------------------------------------------

def _intent_when(it: dict) -> str:
    """Human-readable trigger time for an intent row."""
    try:
        cfg = json.loads(it["trigger_config"]) if isinstance(it["trigger_config"], str) else it["trigger_config"]
    except Exception:
        cfg = {}
    t = it.get("trigger_type")
    if t == "date":
        return cfg.get("datetime", "?")
    if t == "cron":
        return f"cron {cfg.get('expression', '?')}"
    if t == "interval":
        return f"every {cfg.get('seconds', '?')}s"
    return str(t)


def snapshot_active_intents(memory_dir: str | Path) -> int:
    """Write all active (pending + triggered) intents to hot/active_intents.md.

    This makes the FULL set of future commitments present in every reasoning
    cycle — both the main conversation (build_system_prompt) and the heartbeat
    (claude_call) read hot/*.md via load_tiered_memory. Without this, intents
    are only visible at the moment they fire (intention-check), so the agent
    reasons without awareness of what it has already committed to.

    Returns the number of intents written.
    """
    _init()
    intents = list_intents(status="pending", limit=200)
    intents += list_intents(status="triggered", limit=50)

    dated = sorted(
        [i for i in intents if i.get("trigger_type") == "date"],
        key=_intent_when,
    )
    recurring = [i for i in intents if i.get("trigger_type") != "date"]

    def _line(i: dict) -> str:
        label = i.get("purpose") or (i.get("prompt", "")[:60])
        return f"- **{_intent_when(i)}** · {i['name']} — {label}  `[{i['id']}]`"

    lines = [
        f"# 活跃 Intents（共 {len(intents)} 条 · 自动刷新）",
        "",
        "> **这不是待办清单，是给 Pascal 的成长加顺风。** 指针 = 长得更快，不是做完更多。",
        "> 不追「做没做」；没完成 ≠ 失败 = 探索信号（悄悄喂疗愈观察，绝不算账）。只有极少数真有后果的硬约束（高铁票类）才硬提醒。",
        "> 我是这些 intent 的 **owner**：主动复盘、明显漂移（日期对不上日历/stale/重复）自己修，只有真正不确定且影响结果的才问 Pascal。",
        "",
        "## 定时（一次性，按时间排序）",
    ]
    lines += [_line(i) for i in dated] or ["- （无）"]
    lines += ["", "## 循环 / 周期"]
    lines += [_line(i) for i in recurring] or ["- （无）"]
    lines.append("")

    out = Path(memory_dir) / "hot" / "active_intents.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return len(intents)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def intent_stats() -> dict:
    """Get intention statistics."""
    _init()
    db = _get_db()
    stats = {}
    for status in ("pending", "triggered", "executed", "expired", "cancelled"):
        count = db.execute(
            "SELECT COUNT(*) FROM intentions WHERE status = ?", (status,)
        ).fetchone()[0]
        stats[status] = count
    return stats


# ---------------------------------------------------------------------------
# CLI — synchronous, verifiable intent ops for in-turn Bash tool calls.
#
# The whole point: the agent runs these with Bash DURING its turn, reads the
# printed result, and only then reports "done" — instead of firing a
# [ACTION:intent_*] marker it can never observe. Run from JARVIS_DIR:
#
#   python3 -m core.intentions list [status]
#   python3 -m core.intentions due
#   python3 -m core.intentions get <id>
#   python3 -m core.intentions cancel <id> [reason...]
#   python3 -m core.intentions delete <id>
#   python3 -m core.intentions stats
#   python3 -m core.intentions reset-stale [stale_minutes]
#   python3 -m core.intentions purge <executed|expired|cancelled>
# ---------------------------------------------------------------------------

_TERMINAL_STATES = ("executed", "expired", "cancelled")


def _cli_fmt(it: dict) -> str:
    return f"{it['id']}  [{it['status']:9}] {_intent_when(it):24}  {it['name']}"


def _cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "list"
    rest = argv[1:]

    if cmd == "list":
        status = rest[0] if rest else None
        rows = list_intents(status=status, limit=500)
        if status is None:  # default view = pending + triggered (the active set)
            rows = [r for r in rows if r["status"] in ("pending", "triggered")]
        print(f"{len(rows)} intent(s)" + (f" status={status}" if status else " (active)"))
        for r in rows:
            print(_cli_fmt(r))
        return 0

    if cmd == "due":
        rows = get_due_intents()
        print(f"{len(rows)} due now")
        for r in rows:
            print(_cli_fmt(r))
        return 0

    if cmd == "get":
        if not rest:
            print("usage: get <id>", file=sys.stderr); return 2
        it = get_intent(rest[0])
        if not it:
            print(f"not found: {rest[0]}"); return 1
        print(json.dumps(it, ensure_ascii=False, indent=2))
        return 0

    if cmd == "cancel":
        if not rest:
            print("usage: cancel <id> [reason...]", file=sys.stderr); return 2
        ok = cancel_intent(rest[0], " ".join(rest[1:]))
        print(f"cancelled {rest[0]}" if ok else f"not found: {rest[0]}")
        return 0 if ok else 1

    if cmd == "delete":
        if not rest:
            print("usage: delete <id>", file=sys.stderr); return 2
        delete_intent(rest[0])
        print(f"deleted {rest[0]} (gone={get_intent(rest[0]) is None})")
        return 0

    if cmd == "stats":
        print(json.dumps(intent_stats(), ensure_ascii=False))
        return 0

    if cmd == "reset-stale":
        mins = int(rest[0]) if rest and rest[0].isdigit() else 10
        n = reset_stale_triggered(stale_minutes=mins)
        print(f"reset {n} stale intent(s) to pending (overdue one-shots expired)")
        return 0

    if cmd == "purge":
        status = rest[0] if rest else ""
        if status not in _TERMINAL_STATES:
            print(f"purge needs a terminal status {_TERMINAL_STATES}; got {status!r} "
                  "(refuses to delete pending/triggered)", file=sys.stderr)
            return 2
        rows = list_intents(status=status, limit=10000)
        for r in rows:
            delete_intent(r["id"])
        print(f"purged {len(rows)} intent(s) with status={status}")
        return 0

    print(f"unknown command: {cmd}\n"
          "commands: list|due|get|cancel|delete|stats|reset-stale|purge", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(_cli(sys.argv[1:]))
