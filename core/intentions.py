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
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Closure model — Input/Decision/Output + a category-driven closure policy.
#
# An intent is no longer fire-and-forget. After its MOMENT fires, a FOLLOW-UP
# may be spawned to capture the OUTPUT ("did you do it?") into closure_result.
# The TWO orthogonal knobs (the design's core insight):
#   CAPTURE  — closure_status/closure_result are ALWAYS maintained (the record).
#   SURFACE  — whether we actively re-ask Pascal is gated by `category`.
# Healing/autonomous CAPTURE but never SURFACE (no card, no re-ask) — that is
# how "追问做了吗+记录" coexists with "不追做没做/永不催" (closure != nagging).
#
# category 1:1 maps behavioral_rules.md §5 (no separate feedback_intent_review
# file — that section is the authoritative source).
#   followup:   None | ("rel_hours", N) | ("next_day_at", HOUR) — when the
#               one-shot-date follow-up fires relative to the moment.
#   may_notify: True  → follow-up is a `notify` card; False → silent `prompt`.
#   re_surface: True  → the nightly review (get_closure_due) may re-ask;
#               False → asked at most once, never re-surfaced (healing safety).
#   decay_budget: hard cap on proactive touches.
# ---------------------------------------------------------------------------
CLOSURE_POLICY = {
    "hard":       {"followup": ("rel_hours", 2),   "may_notify": True,  "re_surface": True,  "decay_budget": 99},
    "context":    {"followup": None,                "may_notify": False, "re_surface": False, "decay_budget": 0},
    "healing":    {"followup": ("next_day_at", 11), "may_notify": False, "re_surface": False, "decay_budget": 1},
    "external":   {"followup": ("next_day_at", 11), "may_notify": True,  "re_surface": True,  "decay_budget": 5},
    "autonomous": {"followup": ("next_day_at", 11), "may_notify": False, "re_surface": False, "decay_budget": 1},
    "none":       {"followup": None,                "may_notify": False, "re_surface": False, "decay_budget": 0},
}

_VALID_ACTION_TYPES = ("prompt", "notify", "lark_card", "script")
_CLOSURE_TERMINAL = ("done", "recorded", "na")

# Social / 外联 calendar events get an extra post-event closure intent (the
# others are prep-only). Deliberately narrow — bare 约/见 over-match logistics.
_SOCIAL_RE = re.compile(r"饭|聚|咖啡|午餐|晚餐|见面|面试|lunch|dinner|coffee|meet")


def snap_to_golden(dt: datetime) -> datetime:
    """Clamp a may_notify follow-up time into the 11:00-18:00 golden window.

    behavioral_rules §3: dead zones 20:00-23:00 and 05:00, golden 11:00-18:00.
    A closure follow-up that wants to send a card must NEVER fire in a dead zone
    (that turns "做了吗" into a nag at a bad hour). In golden → unchanged; before
    11:00 → that day 11:00 (still after an early moment); 18:00+ → next day 11:00.
    Silent (prompt) follow-ups skip this — they never card, so timing is moot.
    """
    if 11 <= dt.hour < 18:
        return dt
    if dt.hour < 11:
        return dt.replace(hour=11, minute=0, second=0, microsecond=0)
    return (dt + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)

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
    _migrate()


# Additive closure columns. CREATE TABLE IF NOT EXISTS above will NOT add these
# to an already-existing table, so an idempotent ALTER migration is required.
# Single source of truth for the column set (do NOT duplicate into CREATE TABLE
# — that would drift). NOT NULL DEFAULT <const> back-fills old rows safely; the
# two FK columns are nullable (NULL on legacy rows → every reader must guard).
_NEW_COLS = [
    ("category",            "TEXT NOT NULL DEFAULT 'none'"),
    ("input_ctx",           "TEXT NOT NULL DEFAULT ''"),
    ("decision",            "TEXT NOT NULL DEFAULT ''"),
    ("closure_question",    "TEXT NOT NULL DEFAULT ''"),
    ("closure_status",      "TEXT NOT NULL DEFAULT 'none'"),
    ("closure_result",      "TEXT NOT NULL DEFAULT ''"),
    ("closure_touches",     "INTEGER NOT NULL DEFAULT 0"),
    ("closure_followup_id", "TEXT"),
    ("parent_intent_id",    "TEXT"),
]


def _migrate():
    """Idempotent ALTER migration for the closure columns. Safe to call on every
    init: it only adds columns that are missing. A lock-contention OperationalError
    (busy dashboard connection) is logged, not raised — must never crash heartbeat.
    """
    db = _get_db()
    have = {r[1] for r in db.execute("PRAGMA table_info(intentions)").fetchall()}
    for col, ddl in _NEW_COLS:
        if col not in have:
            try:
                db.execute(f"ALTER TABLE intentions ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                print(f"[intentions._migrate] skip {col}: {e}", file=sys.stderr)
    db.execute("CREATE INDEX IF NOT EXISTS idx_intentions_closure ON intentions(closure_status)")
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
    # ── closure model (all trailing + defaulted → existing call sites unaffected) ──
    category: str = "none",
    input_ctx: str = "",
    decision: str = "",
    closure_question: str = "",
    closure_status: str = "none",
    closure_result: str = "",
    closure_touches: int = 0,
    closure_followup_id: str | None = None,
    parent_intent_id: str | None = None,
) -> str:
    """Create a new intent. Returns intent ID.

    Validates action_type/category (rejecting the action_type=cron dirty-data
    class). When this is a closure-bearing one-shot MOMENT (closure_question set,
    a chasing category, trigger_type date, and NOT itself a follow-up), the
    actual follow-up is spawned later by mark_executed — never here — so a
    cancelled/expired parent never leaves a dangling follow-up.
    """
    _init()
    if action_type not in _VALID_ACTION_TYPES:
        raise ValueError(f"invalid action_type: {action_type!r} (must be one of {_VALID_ACTION_TYPES})")
    if category not in CLOSURE_POLICY:
        raise ValueError(f"invalid category: {category!r} (must be one of {tuple(CLOSURE_POLICY)})")
    db = _get_db()
    iid = intent_id or f"int_{uuid.uuid4().hex[:10]}"
    now = now_local_str("%Y-%m-%dT%H:%M:%S")

    db.execute(
        """INSERT OR REPLACE INTO intentions
           (id, name, source, status, trigger_type, trigger_config,
            prompt, context, action_type, action_config, conditions,
            priority, chain_next, purpose, tags, created_at, expires_at,
            category, input_ctx, decision, closure_question, closure_status,
            closure_result, closure_touches, closure_followup_id, parent_intent_id)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (iid, name, source, trigger_type,
         json.dumps(trigger_config, ensure_ascii=False), prompt,
         json.dumps(context or {}, ensure_ascii=False), action_type,
         json.dumps(action_config or {}, ensure_ascii=False),
         json.dumps(conditions or [], ensure_ascii=False),
         priority, chain_next, purpose,
         json.dumps(tags or [], ensure_ascii=False), now, expires_at,
         category, input_ctx, decision, closure_question, closure_status,
         closure_result, closure_touches, closure_followup_id, parent_intent_id),
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

    Closure cleanup: cancelling a parent that is 'awaiting' closure also retires
    its closure axis to 'na' and cancels the still-pending follow-up — so a
    cancelled intent never leaves a dangling 'awaiting' that the nightly review /
    snapshot keep surfacing.
    """
    _init()
    db = _get_db()
    row = db.execute(
        "SELECT status, closure_status, closure_followup_id FROM intentions WHERE id = ?",
        (intent_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] == "cancelled":
        return True
    db.execute(
        "UPDATE intentions SET status = 'cancelled', last_error = ? WHERE id = ?",
        (reason or f"cancelled (was {row['status']})", intent_id),
    )
    # Retire a dangling closure loop, and the still-pending follow-up with it.
    if row["closure_status"] == "awaiting":
        db.execute("UPDATE intentions SET closure_status = 'na' WHERE id = ?", (intent_id,))
        fu_id = row["closure_followup_id"]
        if fu_id:
            db.execute(
                "UPDATE intentions SET status = 'cancelled', last_error = ? "
                "WHERE id = ? AND status = 'pending'",
                ("parent cancelled", fu_id),
            )
    db.commit()
    return True


def update_intent(intent_id: str, **kwargs) -> bool:
    """Update mutable fields of an intent."""
    _init()
    allowed = {"name", "prompt", "context", "action_config", "conditions",
               "priority", "purpose", "tags", "trigger_config", "expires_at", "status",
               # closure columns (all scalar, NOT json-encoded)
               "category", "input_ctx", "decision", "closure_question",
               "closure_status", "closure_result", "closure_touches",
               "closure_followup_id", "parent_intent_id"}
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
            # Retire any dangling closure loop so the nightly review stops chasing it.
            if intent.get("closure_status") == "awaiting":
                db.execute("UPDATE intentions SET closure_status = 'na' WHERE id = ?", (intent["id"],))
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

    # ── Closure follow-up spawn (ONE-SHOT date intents only) ──────────────
    # CRITICAL: this lives in the one-shot path only. cron/interval MUST NOT
    # spawn a follow-up per fire (that builds the "left-glute did-you-do-it"
    # nag-mountain the healing frame forbids); recurring closure is handled by
    # the moment prompt + the nightly review's get_closure_due, not per-fire.
    # Guards: has a closure_question, not already awaiting/closed (防重派生),
    # and is itself a MOMENT (not a follow-up). Wrapped so a spawn failure
    # never crashes the post-script (the parent is already committed executed).
    if (intent["trigger_type"] == "date"
            and intent.get("closure_question")
            and intent.get("closure_status", "none") == "none"
            and not intent.get("parent_intent_id")):
        try:
            _spawn_closure_followup(intent)
        except Exception as e:
            print(f"[intentions] closure follow-up spawn failed for {intent_id}: {e}",
                  file=sys.stderr)

    # Handle explicit chain (legacy mechanism, unchanged — 0 live rows use it)
    if intent.get("chain_next"):
        chain_data = json.loads(intent["chain_next"]) if isinstance(intent["chain_next"], str) else intent["chain_next"]
        if isinstance(chain_data, dict) and chain_data.get("name"):
            try:
                create_intent(**chain_data)
            except Exception as e:
                print(f"[intentions] chain_next spawn failed for {intent_id}: {e}",
                      file=sys.stderr)


def _spawn_closure_followup(parent: dict) -> str | None:
    """Spawn the FOLLOW-UP intent that captures a one-shot's OUTPUT.

    The follow-up is a normal `date` intent firing after the moment; when it
    fires Claude asks (notify) or silently listens (prompt) per category, and
    records the answer via record_closure. Returns the follow-up id (or None if
    the category has no follow-up policy). Deterministic id ('<parent>__fu') +
    INSERT OR REPLACE makes a re-spawn overwrite rather than duplicate (belt &
    suspenders alongside the closure_status='none' guard in mark_executed).
    """
    cat = parent.get("category", "none")
    pol = CLOSURE_POLICY.get(cat, CLOSURE_POLICY["none"])
    if not pol["followup"]:
        return None

    try:
        cfg = json.loads(parent["trigger_config"]) if isinstance(parent["trigger_config"], str) else parent["trigger_config"]
        moment_dt = _coerce(datetime.fromisoformat((cfg or {}).get("datetime", "")))
    except (ValueError, TypeError, KeyError):
        moment_dt = now_local()

    kind, val = pol["followup"]
    if kind == "rel_hours":
        fu_dt = moment_dt + timedelta(hours=val)
    else:  # next_day_at
        fu_dt = (moment_dt + timedelta(days=1)).replace(hour=val, minute=0, second=0, microsecond=0)
    if pol["may_notify"]:
        fu_dt = snap_to_golden(fu_dt)

    action = "notify" if pol["may_notify"] else "prompt"
    cq = parent.get("closure_question", "")
    pid = parent["id"]
    if pol["may_notify"]:
        fu_prompt = (
            f"闭环跟进：{parent['name']}。直接问 Pascal：{cq}\n"
            f"他答了就在信封里带 closure 字段记录："
            f'{{"closure":{{"parent":"{pid}","outcome":"done","result":"<他的一句话答复>"}}}}。'
            f"还没答就发这条 notify 卡片问他；什么都没有就 action: silent。"
        )
    else:
        fu_prompt = (
            f"闭环跟进（疗愈/自主类，永不主动催、永不发卡）：{parent['name']}。闭环问题：{cq}\n"
            f"绝不主动问 Pascal。仅当他已在对话/记忆里自然提到答案时，"
            f'在信封里带 closure 记录：{{"closure":{{"parent":"{pid}","outcome":"done","result":"<一句>"}}}}，'
            f"并 action: silent。否则 action: silent（不产出任何用户可见内容）。"
        )

    fu_id = create_intent(
        name=f"闭环: {parent['name']}",
        trigger_type="date",
        trigger_config={"datetime": fu_dt.isoformat()},
        prompt=fu_prompt,
        action_type=action,
        priority=int(parent.get("priority", 5) or 5),
        source="closure",
        category=cat,
        closure_question=cq,
        parent_intent_id=pid,            # marks this as a follow-up → never re-spawns
        intent_id=f"{pid}__fu",          # deterministic → re-spawn overwrites
        tags=["closure-followup"],
    )
    db = _get_db()
    db.execute(
        "UPDATE intentions SET closure_status = 'awaiting', closure_followup_id = ? WHERE id = ?",
        (fu_id, pid),
    )
    db.commit()
    return fu_id


def record_closure(parent_id: str, outcome: str = "done", result: str = "") -> bool:
    """Record a closure OUTPUT on an awaiting parent. The single write path.

    Hardens its own boundary (does not trust callers): str().strip() the id,
    whitelist the outcome (a polluted value can never corrupt the orthogonal
    closure_status axis), idempotent no-op on unknown/already-terminal rows, and
    NULL-guards the follow-up before cancelling it (no double-ask). Returns
    False on no-op so callers can tell whether a write happened.
    """
    _init()
    parent_id = str(parent_id).strip()
    outcome = outcome if outcome in _CLOSURE_TERMINAL else "done"
    p = get_intent(parent_id)
    if not p or p.get("closure_status") in _CLOSURE_TERMINAL:
        return False
    db = _get_db()
    db.execute(
        "UPDATE intentions SET closure_status = ?, closure_result = ?, "
        "closure_touches = closure_touches + 1 WHERE id = ?",
        (outcome, str(result), parent_id),
    )
    fu_id = p.get("closure_followup_id")
    if fu_id:
        fu = get_intent(fu_id)
        if fu and fu.get("status") == "pending":
            db.execute(
                "UPDATE intentions SET status = 'cancelled', last_error = ? WHERE id = ?",
                ("parent closure recorded (no double-ask)", fu_id),
            )
    db.commit()
    return True


def get_closure_due() -> list[dict]:
    """Awaiting parents the NIGHTLY review may re-ask. Healing-safe by design.

    Returns ONLY re_surface categories (hard/external) — healing/autonomous are
    structurally excluded, so they are asked at most once (their follow-up) and
    NEVER re-surfaced. Further filters: the follow-up is already terminal (it
    fired/expired/cancelled, or is NULL — so we are not double-asking while it is
    still pending) and proactive touches are under the category budget.
    """
    _init()
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM intentions WHERE closure_status = 'awaiting'"
    ).fetchall()
    due = []
    for row in rows:
        it = dict(row)
        if it.get("status") in ("cancelled", "expired"):
            continue  # a cancelled/expired moment has no live closure to chase
        pol = CLOSURE_POLICY.get(it.get("category", "none"), CLOSURE_POLICY["none"])
        if not pol["re_surface"]:
            continue
        if (it.get("closure_touches") or 0) >= pol["decay_budget"]:
            continue
        fu_id = it.get("closure_followup_id")
        if fu_id:
            fu = get_intent(fu_id)
            if fu and fu.get("status") in ("pending", "triggered"):
                continue  # follow-up still active — let it ask first, don't pile on
        due.append(it)
    return due


def awaiting_closures(categories: tuple = ("hard", "external")) -> list[dict]:
    """Open closure loops to SURFACE (snapshot wall + dashboard). Excludes
    healing/autonomous by default so health/learning follow-through is never
    displayed as an undone ledger. Queried directly (not from pending+triggered)
    because a fired one-shot moment is status='executed' yet still awaiting.
    """
    _init()
    db = _get_db()
    placeholders = ",".join("?" for _ in categories)
    rows = db.execute(
        f"SELECT * FROM intentions WHERE closure_status = 'awaiting' "
        f"AND category IN ({placeholders}) AND status != 'cancelled' "
        f"AND (parent_intent_id IS NULL OR parent_intent_id = '') "
        f"ORDER BY priority, created_at",
        tuple(categories),
    ).fetchall()
    return [dict(r) for r in rows]


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

        intent_tag = f"cal:{current_date}:{start_time}:{title[:20]}"
        close_tag = f"cal-close:{current_date}:{start_time}:{title[:20]}"

        # All existing calendar-intent tags (dedup BOTH prep and close in one pass).
        # JSON parse, not LIKE: json.dumps may store Chinese as \uXXXX.
        existing_tags = set()
        for row in db.execute(
            "SELECT tags FROM intentions WHERE source = 'calendar' "
            "AND status IN ('pending', 'triggered')"
        ).fetchall():
            try:
                existing_tags.update(json.loads(row[0]) if isinstance(row[0], str) else (row[0] or []))
            except (json.JSONDecodeError, TypeError):
                pass

        # ── Prep intent (category='context' — legitimately closure-free; a prep
        #    correctly classified is NOT a 美化版日历提醒) ──
        hours_until = (event_dt - now_local()).total_seconds() / 3600
        prep_dt, prep_prompt = None, ""
        if hours_until > 48:
            prep_dt = (event_dt - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            prep_prompt = f"明天 {start_time} 有 {title}。帮 Pascal 做准备：回忆相关上下文、准备需要带的东西、确认地点和时间。"
        elif hours_until > 2:
            prep_dt = event_dt - timedelta(minutes=30)
            prep_prompt = f"{title} 在 {start_time} 开始（还有 30 分钟）。快速回顾：这个会/活动的目的是什么？有什么需要提前准备的？"

        if prep_dt and prep_dt >= now_local() and intent_tag not in existing_tags:
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
                category="context",
                expires_at=event_dt.isoformat(),
            )
            created.append(iid)

        # ── Post-event closure for social / 外联 events (category='external') ──
        # Logistics-only events (康复课/workshop) stay prep-only. The closure asks,
        # AFTER the event, whether there is anything to follow up — only cards on
        # a real lead (external policy), never nags.
        if _SOCIAL_RE.search(title) and close_tag not in existing_tags:
            try:
                event_end_dt = _coerce(datetime.fromisoformat(f"{current_date}T{end_time}:00"))
            except (ValueError, TypeError):
                event_end_dt = event_dt
            close_dt = snap_to_golden(event_end_dt + timedelta(minutes=90))
            if close_dt >= now_local():
                cid = create_intent(
                    name=f"{title} 后闭环",
                    trigger_type="date",
                    trigger_config={"datetime": close_dt.isoformat()},
                    prompt=(f"{title} 应该结束了。若 Pascal 提到，记录：聊了/做了什么值得跟进的？"
                            f"有具体线索才发卡问，没有就静默。"),
                    context={"event_title": title, "event_date": current_date},
                    action_type="notify",
                    category="external",
                    closure_question=f"{title} 之后——有什么值得跟进的吗？",
                    purpose=f"{title} 的会后/饭后闭环",
                    tags=[close_tag, "calendar-close"],
                    source="calendar",
                )
                created.append(cid)

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
             "INPUT = prep material to surface now. DECISION = the yes/no or A/B judgment to put to Pascal.",
             "If a closure sub-object is present, this is a FOLLOW-UP capturing a result — follow its prompt's",
             "rules (healing/autonomous: never proactively ask; only record if Pascal already volunteered).",
             "Return JSON: {\"intents\": {\"<intent_id>\": {\"response\": \"<text>\", \"action\": \"notify|silent|chain\","
             " \"closure\": {\"parent\": \"<parent_id>\", \"outcome\": \"done|recorded|na\", \"result\": \"<one line>\"}}}}",
             "(omit \"closure\" unless you are recording a result.)",
             ""]

    for intent in intents:
        ctx = json.loads(intent["context"]) if isinstance(intent["context"], str) else intent["context"]
        parts.append(f"--- Intent: {intent['id']} ({intent['name']}) [category={intent.get('category', 'none')}] ---")
        parts.append(f"Purpose: {intent['purpose']}")
        parts.append(f"Prompt: {intent['prompt']}")
        if intent.get("input_ctx"):
            parts.append(f"INPUT: {intent['input_ctx']}")
        if intent.get("decision"):
            parts.append(f"DECISION: {intent['decision']}")
        if intent.get("closure_question"):
            parts.append(f"CLOSURE: {intent['closure_question']}")
        if intent.get("parent_intent_id"):
            parts.append(f"(follow-up of {intent['parent_intent_id']} — record via closure field)")
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

    # Follow-up rows are internal closure mechanism, not user-facing commitments.
    # Excluding them keeps the wall a list of real commitments AND prevents a
    # silent healing follow-up's embedded closure question from leaking onto it.
    commitments = [i for i in intents if not i.get("parent_intent_id")]
    dated = sorted(
        [i for i in commitments if i.get("trigger_type") == "date"],
        key=_intent_when,
    )
    recurring = [i for i in commitments if i.get("trigger_type") != "date"]

    def _line(i: dict) -> str:
        label = i.get("purpose") or (i.get("prompt", "")[:60])
        return f"- **{_intent_when(i)}** · {i['name']} — {label}  `[{i['id']}]`"

    # 待闭环 = open hard/external loops awaiting a result (healing/autonomous
    # excluded — never a visible "你没做" ledger; closure != nagging).
    awaiting = awaiting_closures()

    lines = [
        f"# 活跃 Intents（共 {len(intents)} 条 · 自动刷新）",
        "",
        "> **这不是待办清单，是给 Pascal 的成长加顺风。** 指针 = 长得更快，不是做完更多。",
        "> 记录结果、不施压：做了就记一句，没做就悄悄记成探索信号，绝不算账 / 不打分 / 不催"
        "（①硬约束、④对外跟进 除外，那两类我替你追）。没完成 ≠ 失败 = 探索信号。",
        "> 我是这些 intent 的 **owner**：明显漂移（日期对不上日历/stale/重复）自己修；"
        "唯独「他到底执行没」绝不假设、有影响才问。",
        "> 闭环记录用 `python3 -m core.actions do intent_close id=<父id> outcome=done result=<一句>`——"
        "**仅当 Pascal 主动提到或记忆里出现答案时**才记；**绝不**主动问「你康复/读书做了吗」。",
        "",
        "## 定时（一次性，按时间排序）",
    ]
    lines += [_line(i) for i in dated] or ["- （无）"]
    lines += ["", "## 循环 / 周期"]
    lines += [_line(i) for i in recurring] or ["- （无）"]
    lines += ["", "## 待闭环（已触发，等结果 — 不催；仅 ①硬约束 / ④对外 上墙）"]
    lines += [f"- {i['name']} — 闭环问题：{i.get('closure_question') or i.get('purpose', '')}  `[{i['id']}]`"
              for i in awaiting] or ["- （无）"]
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

    if cmd == "awaiting":
        # Parents the nightly review may re-ask (hard/external; healing/autonomous
        # are excluded by policy — asked at most once, never re-surfaced).
        rows = get_closure_due()
        print(f"{len(rows)} awaiting closure (re-askable)")
        for r in rows:
            print(f"{r['id']}  [{r.get('category','?'):10}] {r['name']}\n"
                  f"    闭环问题: {r.get('closure_question','')}")
        return 0

    if cmd == "close":
        # close <id> <outcome> [result...]  — result keeps spaces (joined rest).
        if len(rest) < 1:
            print("usage: close <id> [done|recorded|na] [result...]", file=sys.stderr); return 2
        cid = rest[0]
        outcome = rest[1] if len(rest) > 1 and rest[1] in _CLOSURE_TERMINAL else "done"
        result_start = 2 if (len(rest) > 1 and rest[1] in _CLOSURE_TERMINAL) else 1
        result = " ".join(rest[result_start:])
        ok = record_closure(cid, outcome=outcome, result=result)
        print(f"closure recorded on {cid} (outcome={outcome})" if ok
              else f"no-op: {cid} not found or already closed")
        return 0 if ok else 1

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
          "commands: list|due|awaiting|get|cancel|close|delete|stats|reset-stale|purge", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(_cli(sys.argv[1:]))
