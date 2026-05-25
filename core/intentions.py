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
    _init()
    db = _get_db()
    row = db.execute("SELECT status FROM intentions WHERE id = ?", (intent_id,)).fetchone()
    if not row or row[0] not in ("pending", "triggered"):
        return False
    db.execute(
        "UPDATE intentions SET status = 'cancelled', last_error = ? WHERE id = ?",
        (reason, intent_id),
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
                    target_dt = datetime.fromisoformat(target)
                    triggered = now >= target_dt
                except (ValueError, TypeError):
                    pass  # Skip intents with malformed datetime

        elif trigger_type == "cron":
            from dashboard.scheduler import cron_matches
            expr = trigger_config.get("expression", "")
            triggered = cron_matches(expr, now)
            # Prevent re-triggering within same minute
            if triggered and intent.get("executed_at"):
                last = datetime.fromisoformat(intent["executed_at"])
                if (now - last).total_seconds() < 60:
                    triggered = False

        elif trigger_type == "interval":
            seconds = trigger_config.get("seconds", 600)
            created = datetime.fromisoformat(intent["created_at"])
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


def reset_stale_triggered(stale_minutes: int = 10) -> int:
    """Recover intents stuck in 'triggered' state.

    If a heartbeat cycle crashes between mark_triggered and mark_executed
    (Claude timeout, JSON parse failure, post-script crash, etc.) the intent
    stays 'triggered' forever and never re-fires. This bumps them back to
    'pending' after `stale_minutes` so they get another chance.

    Returns count of reset intents.
    """
    _init()
    db = _get_db()
    cutoff = (now_local() - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    cur = db.execute(
        "UPDATE intentions SET status = 'pending', last_error = ? "
        "WHERE status = 'triggered' AND triggered_at IS NOT NULL AND triggered_at < ?",
        (f"auto-reset after {stale_minutes}m stuck in triggered", cutoff),
    )
    db.commit()
    return cur.rowcount


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

        event_dt = datetime.fromisoformat(f"{current_date}T{start_time}:00")

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
