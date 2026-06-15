"""Intent manager — future-directed actions with unique context.

An Intent is NOT a reminder. It's: "at time T, wake up with context C and
execute action A." Each intent has its own prompt/context, so the agent
at that moment knows exactly what to think about.

Lifecycle (execution axis, REQ-30/31): create → pending → triggered(+attempt)
  → executed | (retry → pending, ≤3 attempts within 2h grace) | expired(+breach
  notification) | cancelled. Every transition is owned by deterministic code —
  the LLM only authors content; absence of its envelope is itself a
  deterministic signal via the inflight manifest (data/.intention_inflight.json).

Architecture:
  - Stored in SQLite `intentions` table (via dashboard.db)
  - Checked every heartbeat cycle by intention-check task
  - Can be created by: agent (ACTION:intent_create), calendar bridge, seed script
  - Supports: one-shot (date), recurring (cron with next_fire_at catch-up),
    relative (interval), chains
  - Every transition emits an intent_* event to sched_events.jsonl (REQ-35)
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

# Sidecar state files (repo-root data/, all writers use atomic tmp+rename)
INFLIGHT_FILE = ROOT / "data" / ".intention_inflight.json"
BREACH_QUEUE = ROOT / "data" / ".intent_breach_queue.jsonl"

# Bounded-retry policy for one-shot date intents stuck in 'triggered'
# (REQ-31). One failed cycle no longer means permanent silent death: retry up
# to MAX_ATTEMPTS within RETRY_GRACE of the trigger time, then expire WITH a
# user-visible breach notification. Intents already ancient at first sweep
# (> STORM_AGE past trigger — the 2026-06-08 resurrection-storm class) are
# expired silently, exactly as before.
MAX_ATTEMPTS = 3
RETRY_GRACE = timedelta(hours=2)
STORM_AGE = timedelta(hours=24)

# Cron catch-up (REQ-32): a missed minute fires on the next check via
# next_fire_at, but never more than CRON_STALENESS late — a laptop asleep all
# evening must not fire 21:00 content at 03:00; the occurrence is skipped
# (with an intent_occurrence_skipped event) and next_fire_at recomputed.
CRON_STALENESS = timedelta(hours=6)

# Closure follow-up staleness (REQ-60): a "后来怎么样" ask more than this past
# its target is no longer worth surfacing — Pascal got the 小明 dinner
# closure 2 days late. Expire instead of nag.
CLOSURE_STALE_DAYS = timedelta(days=2)


def _emit_intent(event: str, intent_id: str, **fields) -> None:
    """Emit an intent-lifecycle event to sched_events.jsonl. Never raises."""
    try:
        from core.sched_events import emit as sched_emit
        sched_emit(ROOT, event, task=intent_id, **fields)
    except Exception:
        pass

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
#   awaiting_ttl_days: how long an 'awaiting' closure may live with no live
#               follow-up before the sweeper retires it to 'na' (REQ-33 —
#               'awaiting' is now a guaranteed-terminating state; the
#               int_7a545cc10d zombie class cannot recur).
CLOSURE_POLICY = {
    "hard":       {"followup": ("rel_hours", 2),   "may_notify": True,  "re_surface": True,  "decay_budget": 99, "awaiting_ttl_days": 7},
    "context":    {"followup": None,                "may_notify": False, "re_surface": False, "decay_budget": 0,  "awaiting_ttl_days": 3},
    "healing":    {"followup": ("next_day_at", 11), "may_notify": False, "re_surface": False, "decay_budget": 1,  "awaiting_ttl_days": 3},
    "external":   {"followup": ("next_day_at", 11), "may_notify": True,  "re_surface": True,  "decay_budget": 5,  "awaiting_ttl_days": 14},
    "autonomous": {"followup": ("next_day_at", 11), "may_notify": False, "re_surface": False, "decay_budget": 1,  "awaiting_ttl_days": 3},
    "none":       {"followup": None,                "may_notify": False, "re_surface": False, "decay_budget": 0,  "awaiting_ttl_days": 3},
}

_VALID_ACTION_TYPES = ("prompt", "notify", "lark_card", "script")
_CLOSURE_TERMINAL = ("done", "recorded", "na")

# Social / 外联 calendar events get an extra post-event closure intent (the
# others are prep-only). Deliberately narrow — bare 约/见 over-match logistics.
_SOCIAL_RE = re.compile(r"饭|聚|咖啡|午餐|晚餐|见面|面试|lunch|dinner|coffee|meet")

# REQ-70 carry/bring detection. A "要带的东西" reminder (伞/球拍/要还的东西…)
# must fire in the MORNING before Pascal leaves home, not at the event's own
# prep time — the 6/13 root cause: 复动肌骨 12:30 康复课的伞被锚到午饭点，
# 他早上 9 点出门时根本没收到。匹配事件标题/详情里暗示需要随身携带的线索。
_CARRY_RE = re.compile(r"伞|球拍|拍子|带[上好]?|要还|还的|归还|护照|证件|材料|文件|"
                       r"复动肌骨|康复|羽毛球|网球|游泳|装备|umbrella|racket|return|bring|gear")

# REQ-70 first-leave anchor. A carry checklist fires this many minutes before
# the day's earliest out-of-home event, but never earlier than the morning
# floor nor later than the morning ceiling — so it lands while Pascal is still
# home getting ready, regardless of whether the first event is at 09:00 or
# 14:30.
CARRY_LEAD = timedelta(minutes=75)        # ~60-90min before first leave
CARRY_MORNING_FLOOR = 7                    # never card before 07:00
CARRY_MORNING_CEILING = 9                  # afternoon-only days still fire by 09:00

# REQ-68 per-event prep lead. The context-recall prep fires this long before a
# ≤48h event. A module constant (not a literal) so the after-event guard is
# unit-testable (a pathological lead must drop the prep, never fire it late).
PREP_LEAD = timedelta(minutes=30)


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
    # ── v2 execution-axis columns (REQ-30/31/32/33) ──
    ("attempt",             "INTEGER NOT NULL DEFAULT 0"),  # execution attempts since last success
    ("next_fire_at",        "TEXT"),                        # cron catch-up watermark
    ("closed_at",           "TEXT"),                        # closure-axis terminal timestamp
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
    # Backfill next_fire_at for live cron rows that predate the column
    # (REQ-32). One-time per row; safe to re-run (only touches NULLs).
    try:
        rows = db.execute(
            "SELECT id, trigger_config FROM intentions "
            "WHERE trigger_type = 'cron' AND status IN ('pending', 'triggered') "
            "AND next_fire_at IS NULL"
        ).fetchall()
        if rows:
            from dashboard.scheduler import cron_next
            for iid, cfg_raw in rows:
                try:
                    expr = (json.loads(cfg_raw) or {}).get("expression", "")
                    nxt = cron_next(expr) if expr else None
                    if nxt:
                        db.execute("UPDATE intentions SET next_fire_at = ? WHERE id = ?",
                                   (nxt.isoformat(), iid))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            db.commit()
    except Exception as e:  # backfill must never block table init
        print(f"[intentions._migrate] next_fire_at backfill: {e}", file=sys.stderr)


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

    # Reject unfireable rows at the boundary (REQ-53): a date intent with an
    # empty/unparseable datetime would be created 'pending' but can never
    # fire, never expire, and silently bloats the snapshot wall forever.
    next_fire_at = None
    if trigger_type == "date":
        target = (trigger_config or {}).get("datetime", "")
        try:
            datetime.fromisoformat(str(target))
        except (ValueError, TypeError):
            raise ValueError(
                f"date intent needs a parseable trigger_config.datetime, got {target!r}")
    elif trigger_type == "cron":
        expr = (trigger_config or {}).get("expression", "")
        try:
            from dashboard.scheduler import cron_next
            nxt = cron_next(expr) if expr else None
        except Exception:
            nxt = None
        if nxt is None:
            raise ValueError(
                f"cron intent needs a valid 5-field expression, got {expr!r}")
        next_fire_at = nxt.isoformat()

    db = _get_db()
    iid = intent_id or f"int_{uuid.uuid4().hex[:10]}"
    now = now_local_str("%Y-%m-%dT%H:%M:%S")

    db.execute(
        """INSERT OR REPLACE INTO intentions
           (id, name, source, status, trigger_type, trigger_config,
            prompt, context, action_type, action_config, conditions,
            priority, chain_next, purpose, tags, created_at, expires_at,
            category, input_ctx, decision, closure_question, closure_status,
            closure_result, closure_touches, closure_followup_id, parent_intent_id,
            attempt, next_fire_at)
           VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (iid, name, source, trigger_type,
         json.dumps(trigger_config, ensure_ascii=False), prompt,
         json.dumps(context or {}, ensure_ascii=False), action_type,
         json.dumps(action_config or {}, ensure_ascii=False),
         json.dumps(conditions or [], ensure_ascii=False),
         priority, chain_next, purpose,
         json.dumps(tags or [], ensure_ascii=False), now, expires_at,
         category, input_ctx, decision, closure_question, closure_status,
         closure_result, closure_touches, closure_followup_id, parent_intent_id,
         next_fire_at),
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


def _skip_stale_cron_occurrence(db, intent: dict, expr: str,
                                missed_dt: datetime, now: datetime) -> None:
    """Retire a cron occurrence that is too stale to fire (REQ-32 ceiling)."""
    try:
        from dashboard.scheduler import cron_next
        nxt = cron_next(expr, after=now.replace(tzinfo=None)) if expr else None
        db.execute(
            "UPDATE intentions SET next_fire_at = ?, last_error = ? WHERE id = ?",
            (nxt.isoformat() if nxt else None,
             f"occurrence {missed_dt.isoformat()} skipped (>{CRON_STALENESS} stale)",
             intent["id"]),
        )
        db.commit()
        _emit_intent("intent_occurrence_skipped", intent["id"],
                     missed=missed_dt.isoformat(), name=intent.get("name", ""))
    except Exception as e:
        print(f"[intentions] stale-occurrence skip failed for {intent.get('id')}: {e}",
              file=sys.stderr)


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
                    target_dt = None
                    pass  # Skip intents with malformed datetime
                # Stale closure expiry (REQ-60): a closure follow-up
                # (source='closure') that is overdue by more than
                # CLOSURE_STALE_DAYS is no longer worth surfacing — the dinner
                # was days ago. Asking "后来怎么样" 2+ days late is the nag
                # Pascal got (int_023339f780__fu fired 6/15 for a 6/13 meal).
                # Expire it instead of firing.
                if (triggered and target_dt
                        and intent.get("source") == "closure"
                        and (now - target_dt) > CLOSURE_STALE_DAYS):
                    # Guarded like _skip_stale_cron_occurrence (red-team fix):
                    # a DB-lock OperationalError here must NOT crash the whole
                    # due-check — that would strand the cycle (no reminders, no
                    # breach cards). On write failure, leave it pending and try
                    # again next cycle; just don't surface it now.
                    try:
                        db.execute(
                            "UPDATE intentions SET status = 'expired', last_error = ? WHERE id = ?",
                            (f"closure follow-up stale (>{CLOSURE_STALE_DAYS.days}d past) — not surfaced",
                             intent["id"]))
                        db.commit()
                        _emit_intent("intent_expired", intent["id"],
                                     reason="closure_stale", name=intent.get("name", ""))
                    except Exception as e:
                        print(f"[intentions] stale-closure expire failed for "
                              f"{intent['id']}: {e}", file=sys.stderr)
                    triggered = False

        elif trigger_type == "cron":
            # Catch-up semantics (REQ-32): fire when now >= next_fire_at, so a
            # missed minute (batch-deferred check, busy runner, sleep) fires
            # on the NEXT check instead of silently losing the occurrence.
            expr = trigger_config.get("expression", "")
            nfa = intent.get("next_fire_at")
            if nfa:
                try:
                    nfa_dt = _coerce(datetime.fromisoformat(nfa))
                except (ValueError, TypeError):
                    nfa_dt = None
                if nfa_dt and now >= nfa_dt:
                    if now - nfa_dt > CRON_STALENESS:
                        # Too late to be useful (e.g. host slept all evening):
                        # skip the occurrence, recompute, never fire 21:00
                        # content at 03:00.
                        _skip_stale_cron_occurrence(db, intent, expr, nfa_dt, now)
                    else:
                        triggered = True
            else:
                # Legacy row without next_fire_at: exact-minute match once,
                # then mark_executed/backfill stamps next_fire_at.
                from dashboard.scheduler import cron_matches
                triggered = cron_matches(expr, now)
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
    """Mark an intent as triggered (being processed). Counts the attempt."""
    _init()
    db = _get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "UPDATE intentions SET status = 'triggered', triggered_at = ?, "
        "attempt = attempt + 1 WHERE id = ?",
        (now, intent_id),
    )
    db.commit()
    row = db.execute("SELECT attempt, name FROM intentions WHERE id = ?",
                     (intent_id,)).fetchone()
    _emit_intent("intent_fired", intent_id,
                 attempt=row["attempt"] if row else 1,
                 name=row["name"] if row else "")


def _trigger_dt(intent: dict) -> datetime | None:
    """Parsed trigger datetime of a one-shot date intent, or None."""
    try:
        cfg = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
        return _coerce(datetime.fromisoformat((cfg or {}).get("datetime", "")))
    except (ValueError, TypeError, KeyError):
        return None


def _queue_breach(intent: dict, now: datetime) -> None:
    """Append a dropped-commitment notification to the breach queue (REQ-31).

    intentions_pre.sh drains this queue into the next intention-check cycle so
    Pascal hears '我没能按时把「X」提醒出来' instead of silence. The original
    prompt rides along — the reminder's value isn't lost with its schedule.
    Atomic-ish append (O_APPEND single line); never raises.
    """
    try:
        BREACH_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "id": intent["id"], "name": intent.get("name", ""),
            "prompt": intent.get("prompt", ""), "purpose": intent.get("purpose", ""),
            "trigger_time": _intent_when(intent),
            "attempt": intent.get("attempt") or 0,
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S"), "notify_attempts": 0,
        }
        with open(BREACH_QUEUE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[intentions] breach queue append failed: {e}", file=sys.stderr)


def lifecycle_sweep(stale_minutes: int = 10) -> int:
    """Sweep stuck and zombie lifecycle states. Returns count reset to pending.

    Replaces reset_stale_triggered, whose anti-resurrection exception expired
    EVERY stuck one-shot (a date intent is by definition past-due the moment
    it fires) — one failed cycle meant permanent silent commitment-dropping,
    the literal mechanism behind 创建后没有闭环 (18/19 expired rows in the
    6/13 audit died this way). Graduated policy (REQ-31):

    Stuck 'triggered' rows (cycle crashed between mark_triggered and
    mark_executed):
      - cron/interval → back to pending; the occurrence SURVIVES because
        next_fire_at is not advanced until a successful fire (REQ-32).
      - one-shot date, ancient (> STORM_AGE past trigger) → expire silently —
        the 2026-06-08 resurrection-storm class, unchanged behavior.
      - one-shot date, attempt < MAX_ATTEMPTS and within RETRY_GRACE of the
        trigger → back to pending for another try next cycle.
      - retries exhausted → expire + user-visible breach notification via the
        breach queue + still run the closure axis (REQ-33: the reminder
        failed, but the real-world event happened — for hard/external the
        next-day '后来怎么样' is MORE valuable, not less).

    Awaiting-closure TTL (REQ-33): 'awaiting' rows with no live follow-up
    past their category TTL → closure_status 'na' with closed_at stamped, so
    'awaiting' is a guaranteed-terminating state (no more permanent zombies).
    """
    _init()
    db = _get_db()
    now = now_local()
    cutoff = (now - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    stuck = db.execute(
        "SELECT * FROM intentions "
        "WHERE status = 'triggered' AND triggered_at IS NOT NULL AND triggered_at < ?",
        (cutoff,),
    ).fetchall()

    reset = 0
    terminal_moments: list[dict] = []
    for row in stuck:
        intent = dict(row)
        if intent.get("trigger_type") != "date":
            # Recurring: the occurrence survives (next_fire_at untouched).
            db.execute(
                "UPDATE intentions SET status = 'pending', last_error = ? WHERE id = ?",
                (f"auto-reset after {stale_minutes}m stuck in triggered", intent["id"]),
            )
            _emit_intent("intent_retry", intent["id"],
                         attempt=intent.get("attempt") or 0, kind=intent.get("trigger_type"))
            reset += 1
            continue

        target = _trigger_dt(intent)
        age = (now - target) if target else (STORM_AGE + timedelta(seconds=1))
        attempt = intent.get("attempt") or 0

        if age > STORM_AGE:
            # Ancient at sweep time — junk/storm class. Expire silently.
            db.execute(
                "UPDATE intentions SET status = 'expired', last_error = ? WHERE id = ?",
                ("auto-expired: stuck in triggered, trigger >24h past (storm class)",
                 intent["id"]),
            )
            if intent.get("closure_status") == "awaiting":
                db.execute(
                    "UPDATE intentions SET closure_status = 'na', closed_at = ? WHERE id = ?",
                    (now.strftime("%Y-%m-%dT%H:%M:%S"), intent["id"]))
            _emit_intent("intent_expired", intent["id"], attempt=attempt,
                         notified=False, reason="storm_class")
        elif attempt < MAX_ATTEMPTS and age < RETRY_GRACE:
            db.execute(
                "UPDATE intentions SET status = 'pending', last_error = ? WHERE id = ?",
                (f"retry {attempt}/{MAX_ATTEMPTS} after stuck in triggered", intent["id"]),
            )
            _emit_intent("intent_retry", intent["id"], attempt=attempt, kind="date")
            reset += 1
        else:
            # Retries exhausted — expire LOUDLY: breach queue + closure axis.
            db.execute(
                "UPDATE intentions SET status = 'expired', last_error = ? WHERE id = ?",
                (f"expired after {attempt} attempts — breach notification queued",
                 intent["id"]),
            )
            _queue_breach(intent, now)
            _emit_intent("intent_expired", intent["id"], attempt=attempt,
                         notified=True, reason="retries_exhausted")
            terminal_moments.append(dict(intent))
    db.commit()

    # Closure spawns AFTER the commit: _on_moment_terminal opens its own
    # connection — running it inside the sweep's open transaction deadlocks
    # ('database is locked').
    for intent in terminal_moments:
        try:
            _on_moment_terminal(intent, how="expired")
        except Exception as e:
            print(f"[intentions] closure-on-expiry failed for {intent['id']}: {e}",
                  file=sys.stderr)

    # ── Awaiting-closure TTL pass (REQ-33) ──────────────────────────────
    awaiting = db.execute(
        "SELECT * FROM intentions WHERE closure_status = 'awaiting'"
    ).fetchall()
    for row in awaiting:
        it = dict(row)
        pol = CLOSURE_POLICY.get(it.get("category", "none"), CLOSURE_POLICY["none"])
        ttl = timedelta(days=pol.get("awaiting_ttl_days", 3))
        anchor_raw = it.get("executed_at") or it.get("triggered_at") or it.get("created_at")
        try:
            anchor = _coerce(datetime.fromisoformat(anchor_raw)) if anchor_raw else None
        except (ValueError, TypeError):
            anchor = None
        if not anchor or (now - anchor) <= ttl:
            continue
        fu_id = it.get("closure_followup_id")
        if fu_id:
            fu = get_intent(fu_id)
            if fu and fu.get("status") in ("pending", "triggered"):
                continue  # follow-up still live — let it ask first
        db.execute(
            "UPDATE intentions SET closure_status = 'na', closed_at = ?, "
            "closure_result = ? WHERE id = ?",
            (now.strftime("%Y-%m-%dT%H:%M:%S"),
             f"ttl: no signal within {pol.get('awaiting_ttl_days', 3)}d window",
             it["id"]),
        )
        _emit_intent("intent_closure", it["id"], outcome="na", via="ttl")
    db.commit()
    return reset


# Backward-compatible alias (older call sites and tests).
reset_stale_triggered = lifecycle_sweep


def mark_executed(intent_id: str, result: str = ""):
    """Mark an intent as executed. Handle recurring (cron) reset and chains."""
    _init()
    db = _get_db()
    now = now_local_str("%Y-%m-%dT%H:%M:%S")

    intent = get_intent(intent_id)
    if not intent:
        return

    if intent["trigger_type"] == "cron":
        # Recurring: reset to pending for the next occurrence. attempt resets
        # (it counts tries of ONE occurrence) and next_fire_at advances —
        # only here, on success, so a failed cycle never loses the occurrence.
        # Anchor the recompute to the FIRED occurrence (the row's current
        # next_fire_at), NOT wall-clock now (red-team fix 2026-06-13): a slow
        # or retried cycle finishes minutes-to-hours after the occurrence;
        # cron_next(after=now) would jump past every occurrence between the
        # fired one and now, silently skipping beats (hourly intent loses the
        # 14:00 beat if post runs at 14:03). Anchoring at the fired occurrence
        # means a late-but-within-staleness fire is caught up next cycle, as
        # REQ-32 promises; the >6h-stale case is still retired by
        # _skip_stale_cron_occurrence in get_due_intents.
        nxt = None
        try:
            from dashboard.scheduler import cron_next
            cfg = json.loads(intent["trigger_config"]) if isinstance(intent["trigger_config"], str) else intent["trigger_config"]
            expr = (cfg or {}).get("expression", "")
            anchor = None
            nfa = intent.get("next_fire_at")
            if nfa:
                try:
                    anchor = datetime.fromisoformat(nfa).replace(tzinfo=None)
                except (ValueError, TypeError):
                    anchor = None
            nxt = cron_next(expr, after=anchor) if expr else None
        except Exception:
            pass
        # last_error is for ERRORS, not status (REQ-61): cron success used to
        # store the run narration here ('小时报 11:30-12:21：Pascal 在深度
        # 投入…'), polluting every error scan and the funnel. Clear it on
        # success so a non-NULL last_error always means a real failure.
        db.execute(
            "UPDATE intentions SET status = 'pending', executed_at = ?, last_error = NULL, "
            "attempt = 0, next_fire_at = ? WHERE id = ?",
            (now, nxt.isoformat() if nxt else None, intent_id),
        )
    else:
        # One-shot: mark executed (attempt resets — execution succeeded)
        db.execute(
            "UPDATE intentions SET status = 'executed', executed_at = ?, "
            "last_error = ?, attempt = 0 WHERE id = ?",
            (now, result, intent_id),
        )
    db.commit()
    _emit_intent("intent_executed", intent_id,
                 kind=intent["trigger_type"], name=intent.get("name", ""))

    # Closure axis: spawn the follow-up for a closure-bearing one-shot moment.
    try:
        _on_moment_terminal(intent, how="executed")
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


def _on_moment_terminal(intent: dict, how: str) -> None:
    """Closure-axis transition when a MOMENT reaches a terminal state.

    Extracted from mark_executed (REQ-33) so the spawn also runs when the
    reminder FAILED but the underlying real-world event happened (a dinner, a
    class): asking '后来怎么样' next day is more valuable then, not less.
    Guards: one-shot date moments only (cron/interval per-fire spawn would
    build the nag-mountain the healing frame forbids), closure_question set,
    not already awaiting/closed, not itself a follow-up. On how='expired' the
    spawn is further gated to hard/external — healing/autonomous keep their
    never-nag guarantee even on failure.
    """
    if not (intent.get("trigger_type") == "date"
            and intent.get("closure_question")
            and intent.get("closure_status", "none") == "none"
            and not intent.get("parent_intent_id")
            # Never spawn a closure-of-a-closure (REQ-60): a follow-up row
            # (source='closure') reaching a terminal state must not breed a
            # second-level __fu. parent_intent_id already catches this, but
            # source is the clearer invariant.
            and intent.get("source") != "closure"):
        return
    if how == "expired" and intent.get("category") not in ("hard", "external"):
        return
    _spawn_closure_followup(intent)


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


def record_closure(parent_id: str, outcome: str = "done", result: str = "",
                   via: str = "cli") -> bool:
    """Record a closure OUTPUT on an awaiting parent. The single write path.

    Hardens its own boundary (does not trust callers): str().strip() the id,
    whitelist the outcome (a polluted value can never corrupt the orthogonal
    closure_status axis), idempotent no-op on unknown/already-terminal rows, and
    NULL-guards the follow-up before cancelling it (no double-ask). Returns
    False on no-op so callers can tell whether a write happened.

    `via` is telemetry only (button|reply|followup|review|cli|ttl|dashboard) —
    which path closed the loop, the learning signal REQ-34/35 feed on.
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
        "closure_touches = closure_touches + 1, closed_at = ? WHERE id = ?",
        (outcome, str(result), now_local_str("%Y-%m-%dT%H:%M:%S"), parent_id),
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
    # Closing the loop (e.g. via a ✅/❌/🚫 button tap) also retires any pending
    # breach apology for this intent — otherwise the breach card could re-fire
    # after the user already answered (2026-06-15: the dinner-closure breach
    # nagged even though buttons were present to close it).
    try:
        clear_breaches([parent_id])
    except Exception as e:
        print(f"[intentions] clear_breaches on closure failed: {e}", file=sys.stderr)
    _emit_intent("intent_closure", parent_id, outcome=outcome, via=via)
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

def _load_cal_index(db) -> tuple[set, dict]:
    """Snapshot every calendar intent's tags → (existing_tags, tag→(id,cfg)).

    REQ-68 idempotency: the dedup must look at ALL statuses, not just
    pending/triggered. The 小明-饭 churn (one dinner → 11 rows, two preps at
    the exact same 17:00:16) came partly from re-syncing after a prep had
    already fired: once it left 'pending', the old query no longer saw it, so
    the very next sync INSERTed a fresh duplicate. Including executed/expired/
    cancelled in the dedup snapshot makes 'never a second prep/closure for the
    same (date,title,role)' an invariant the INSERT path cannot violate.
    JSON parse, not LIKE: json.dumps may store Chinese as \\uXXXX.
    """
    existing_tags: set = set()
    tag_to_row: dict = {}
    for row in db.execute(
        "SELECT id, tags, trigger_config FROM intentions WHERE source = 'calendar'"
    ).fetchall():
        try:
            row_tags = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or [])
        except (json.JSONDecodeError, TypeError):
            continue
        existing_tags.update(row_tags)
        for t in row_tags:
            tag_to_row[t] = (row[0], row[2])
    return existing_tags, tag_to_row


def generate_calendar_intents(calendar_md: str) -> list[str]:
    """Parse calendar markdown and create prep / closure / carry intents.

    Three intent ROLES, each keyed on (event-date, title, role) so a re-sync
    upserts in place rather than churning duplicates (REQ-68):
    - prep (role='prep', category='context'): context recall ~30min before, or
      next-day 09:00 for events >48h out. Skipped if its fire time would land
      AT/AFTER the event start — a prep that fires after the event is useless.
    - closure (role='close', category='external'): post-event 后闭环 for social
      events only. Asks, never nags.
    - carry (role='carry', category='context'): ONE morning checklist per day,
      merging every "要带的东西" (伞/球拍/要还的) across that day's events,
      anchored to the FIRST out-of-home event so it fires while Pascal is still
      home (REQ-70 — the 复动肌骨 伞 was anchored to lunchtime and missed the
      morning he actually left).

    Idempotency invariants (REQ-68): for each (date,title,role) there is at most
    ONE row, EVER; a reschedule UPDATEs the existing row's trigger time in place
    (supersede); no path can INSERT a second row for the same key.

    Returns list of created intent IDs.
    """
    import re
    _init()
    db = _get_db()
    created = []
    now = now_local()

    # ── Pass 1: parse events, grouped by date (carry needs the whole day) ──
    # Format: "  HH:MM-HH:MM  Title  (optional details)"
    lines = calendar_md.strip().splitlines()
    current_date = None
    by_date: dict[str, list[dict]] = {}

    for line in lines:
        date_match = re.match(r'^(Today|Tomorrow|Day \d+)\s+\((\d{4}-\d{2}-\d{2})', line)
        if date_match:
            current_date = date_match.group(2)
            continue
        if not current_date:
            continue
        event_match = re.match(r'^\s+(\d{2}:\d{2})-(\d{2}:\d{2})\s+(.+?)(?:\s+\((.+)\))?\s*$', line)
        if not event_match:
            continue
        start_time, end_time = event_match.group(1), event_match.group(2)
        title = event_match.group(3).strip()
        details = event_match.group(4) or ""
        try:
            event_dt = _coerce(datetime.fromisoformat(f"{current_date}T{start_time}:00"))
        except (ValueError, TypeError):
            continue
        by_date.setdefault(current_date, []).append({
            "start_time": start_time, "end_time": end_time, "title": title,
            "details": details, "event_dt": event_dt,
        })

    # Dedup snapshot across ALL statuses (see _load_cal_index). Refreshed lazily
    # below as we INSERT, so two identical event lines in ONE markdown can't
    # both slip through before the DB sees the first.
    existing_tags, tag_to_row = _load_cal_index(db)

    # ── Pass 2: per-event prep + closure ──
    for current_date in sorted(by_date):
        for ev in by_date[current_date]:
            start_time, end_time = ev["start_time"], ev["end_time"]
            title, details, event_dt = ev["title"], ev["details"], ev["event_dt"]

            # Skip past events
            if event_dt < now:
                continue

            # Dedup key is date+title WITHOUT start_time (REQ-53/leak-8): a
            # rescheduled event must update the existing prep/closure in place,
            # not spawn a duplicate pair. Role is encoded in the tag prefix.
            intent_tag = f"cal:{current_date}:{title[:20]}"
            close_tag = f"cal-close:{current_date}:{title[:20]}"

            # ── Prep intent (category='context' — legitimately closure-free; a
            #    prep correctly classified is NOT a 美化版日历提醒) ──
            hours_until = (event_dt - now).total_seconds() / 3600
            prep_dt, prep_prompt = None, ""
            if hours_until > 48:
                prep_dt = (event_dt - timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
                prep_prompt = f"明天 {start_time} 有 {title}。帮 Pascal 做准备：回忆相关上下文、准备需要带的东西、确认地点和时间。"
            elif hours_until > 2:
                prep_dt = event_dt - PREP_LEAD
                prep_prompt = f"{title} 在 {start_time} 开始（还有 30 分钟）。快速回顾：这个会/活动的目的是什么？有什么需要提前准备的？"

            # REQ-68.2: a prep whose computed fire time is AT/AFTER the event
            # start is useless (the 18:00 prep that fired AFTER the 17:30 dinner
            # start). Never create or supersede it; log and drop.
            if prep_dt is not None and prep_dt >= event_dt:
                print(f"[calendar-intents] skip prep for {title!r} @ {current_date}: "
                      f"prep_dt {prep_dt.isoformat()} >= event {event_dt.isoformat()}",
                      file=sys.stderr)
                prep_dt = None

            if prep_dt and prep_dt >= now:
                if intent_tag in existing_tags:
                    # Same event (date+title) already has a prep — if the event
                    # TIME changed, supersede in place instead of duplicating
                    # (REQ-68: reschedules update, never a wrong-time prep card
                    # plus a fresh duplicate). Only revive a still-live row.
                    row = tag_to_row.get(intent_tag)
                    if row:
                        iid_existing, cfg_raw = row
                        try:
                            stored = (json.loads(cfg_raw) or {}).get("datetime", "")
                        except (json.JSONDecodeError, TypeError):
                            stored = ""
                        cur = get_intent(iid_existing)
                        if (stored and stored != prep_dt.isoformat()
                                and cur and cur.get("status") in ("pending", "triggered")):
                            update_intent(
                                iid_existing,
                                trigger_config={"datetime": prep_dt.isoformat()},
                                prompt=prep_prompt,
                                expires_at=event_dt.isoformat(),
                            )
                            tag_to_row[intent_tag] = (iid_existing, json.dumps(
                                {"datetime": prep_dt.isoformat()}, ensure_ascii=False))
                else:
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
                    existing_tags.add(intent_tag)
                    tag_to_row[intent_tag] = (iid, json.dumps(
                        {"datetime": prep_dt.isoformat()}, ensure_ascii=False))

            # ── Post-event closure for social / 外联 events (category='external') ──
            # Logistics-only events (康复课/workshop) stay prep-only. The closure
            # asks, AFTER the event, whether there is anything to follow up —
            # only cards on a real lead (external policy), never nags.
            if _SOCIAL_RE.search(title):
                try:
                    event_end_dt = _coerce(datetime.fromisoformat(f"{current_date}T{end_time}:00"))
                except (ValueError, TypeError):
                    event_end_dt = event_dt
                close_dt = snap_to_golden(event_end_dt + timedelta(minutes=90))
                if close_tag in existing_tags:
                    # REQ-68.1: extend the supersede path to the closure role —
                    # a rescheduled social event updates its 后闭环 in place
                    # instead of leaving a stale one + INSERTing a duplicate.
                    row = tag_to_row.get(close_tag)
                    if row:
                        iid_existing, cfg_raw = row
                        try:
                            stored = (json.loads(cfg_raw) or {}).get("datetime", "")
                        except (json.JSONDecodeError, TypeError):
                            stored = ""
                        cur = get_intent(iid_existing)
                        if (stored and stored != close_dt.isoformat()
                                and cur and cur.get("status") in ("pending", "triggered")):
                            update_intent(
                                iid_existing,
                                trigger_config={"datetime": close_dt.isoformat()},
                                expires_at=(event_end_dt + timedelta(hours=36)).isoformat(),
                            )
                            tag_to_row[close_tag] = (iid_existing, json.dumps(
                                {"datetime": close_dt.isoformat()}, ensure_ascii=False))
                elif close_dt >= now:
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
                        # An unanswered closure ask must not go stale-pending
                        # forever (REQ-53): cap at 36h past event end.
                        expires_at=(event_end_dt + timedelta(hours=36)).isoformat(),
                    )
                    created.append(cid)
                    existing_tags.add(close_tag)
                    tag_to_row[close_tag] = (cid, json.dumps(
                        {"datetime": close_dt.isoformat()}, ensure_ascii=False))

    # ── Pass 3: ONE morning carry checklist per day (REQ-70) ──
    created += _generate_carry_intents(by_date, now, existing_tags, tag_to_row)

    return created


def _generate_carry_intents(by_date: dict, now: datetime,
                            existing_tags: set, tag_to_row: dict) -> list[str]:
    """Emit one morning "要带的东西" checklist per day, anchored to first leave.

    REQ-70: a carry/bring reminder must fire in the MORNING before Pascal
    leaves home, not at the event's own prep time. For each day we:
      1. collect every event whose title/details suggest something to bring;
      2. anchor the fire time to CARRY_LEAD before the day's FIRST out-of-home
         event, clamped into [CARRY_MORNING_FLOOR, CARRY_MORNING_CEILING] —
         so an afternoon-only event still gets a 09:00-by reminder, not a
         13:30 one (the exact 复动肌骨 12:30 失败);
      3. merge all carry items for the day into ONE intent (one card, not N).
    Upsert-keyed on (date, role='carry') so a re-sync never duplicates.
    Carry intents carry expires_at = first-event start (travel-pause hygiene:
    a dated calendar reminder never lingers past the day it was for — the
    no-expires_at 接狗 cron class needs structured trip dates, REQ-71/agent).
    """
    out: list[str] = []
    for current_date in sorted(by_date):
        events = sorted(by_date[current_date], key=lambda e: e["event_dt"])
        # First event still in the future = the day's first leave-home moment.
        future = [e for e in events if e["event_dt"] >= now]
        if not future:
            continue
        first = future[0]
        carry_items = [e for e in events
                       if e["event_dt"] >= now
                       and _CARRY_RE.search(f"{e['title']} {e['details']}")]
        if not carry_items:
            continue

        carry_tag = f"cal-carry:{current_date}"

        # Anchor: CARRY_LEAD before first leave, clamped to a sane morning hour.
        anchor = first["event_dt"] - CARRY_LEAD
        floor = first["event_dt"].replace(hour=CARRY_MORNING_FLOOR, minute=0,
                                          second=0, microsecond=0)
        ceiling = first["event_dt"].replace(hour=CARRY_MORNING_CEILING, minute=0,
                                            second=0, microsecond=0)
        if anchor < floor:
            anchor = floor
        elif anchor > ceiling:
            # Event is much later in the day → still fire by the morning ceiling.
            anchor = ceiling
        # Never schedule into the past (e.g. this morning already gone).
        if anchor < now:
            continue

        items_desc = "、".join(
            f"{e['title']}（{e['start_time']}）" for e in carry_items)
        carry_prompt = (
            f"早上出门前提醒 Pascal 今天要带的东西。今天最早 {first['start_time']} 出门，"
            f"涉及携带：{items_desc}。一句话清单提醒他别忘带（伞/球拍/要还的东西等），"
            f"出门前一次性说清，别等到事件本身的准备时间。"
        )

        if carry_tag in existing_tags:
            # Upsert: re-sync updates the morning anchor in place (first-leave
            # time may have shifted) instead of duplicating the checklist.
            row = tag_to_row.get(carry_tag)
            if row:
                iid_existing, cfg_raw = row
                try:
                    stored = (json.loads(cfg_raw) or {}).get("datetime", "")
                except (json.JSONDecodeError, TypeError):
                    stored = ""
                cur = get_intent(iid_existing)
                if (stored and stored != anchor.isoformat()
                        and cur and cur.get("status") in ("pending", "triggered")):
                    update_intent(
                        iid_existing,
                        trigger_config={"datetime": anchor.isoformat()},
                        prompt=carry_prompt,
                        expires_at=first["event_dt"].isoformat(),
                    )
                    tag_to_row[carry_tag] = (iid_existing, json.dumps(
                        {"datetime": anchor.isoformat()}, ensure_ascii=False))
            continue

        cid = create_intent(
            name=f"出门带东西清单 {current_date}",
            trigger_type="date",
            trigger_config={"datetime": anchor.isoformat()},
            prompt=carry_prompt,
            context={"event_date": current_date,
                      "first_leave": first["start_time"],
                      "carry_items": [e["title"] for e in carry_items]},
            action_type="notify",
            action_config={"type": "carry_reminder"},
            purpose=f"{current_date} 出门前一次性提醒要带的东西",
            tags=[carry_tag, "calendar-carry"],
            source="calendar",
            category="context",
            # Travel-pause hygiene: a dated carry reminder expires at first
            # leave — it must never linger past the day it was for.
            expires_at=first["event_dt"].isoformat(),
        )
        out.append(cid)
        existing_tags.add(carry_tag)
        tag_to_row[carry_tag] = (cid, json.dumps(
            {"datetime": anchor.isoformat()}, ensure_ascii=False))
    return out


# ---------------------------------------------------------------------------
# Envelope contract — SINGLE SOURCE for the shape Claude must return from the
# intention-check task (REQ-53/leak-10). The same schema text is asserted to
# appear verbatim in HEARTBEAT.md's intention-check block by a unit test, so
# prompt and parser cannot drift apart silently (the drift class that shipped
# 'reply HEARTBEAT_OK' instructions against a state machine requiring an
# envelope). intentions_post.py imports validate_envelope for its manifest
# reconciliation.
# ---------------------------------------------------------------------------

ENVELOPE_SCHEMA_DOC = (
    '{"intents": {"<intent_id>": {"response": "<text>", "action": "notify|silent|chain|failed", '
    '"closure": {"parent": "<parent_id>", "outcome": "done|recorded|na", "result": "<one line>"}}}}'
)


def validate_envelope(data, expected_ids: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Check a parsed envelope against the ids the manifest says are inflight.

    Returns (covered_ids, missing_ids, errors). An id is covered when it
    appears in data['intents'] with a dict value. Never raises.
    """
    errors: list[str] = []
    covered: list[str] = []
    if not isinstance(data, dict):
        return [], list(expected_ids), ["envelope is not a dict"]
    intents = data.get("intents")
    if not isinstance(intents, dict):
        return [], list(expected_ids), ["envelope has no 'intents' dict"]
    for iid, slot in intents.items():
        if not isinstance(slot, dict):
            errors.append(f"{iid}: slot is not a dict")
            continue
        covered.append(str(iid))
    missing = [i for i in expected_ids if i not in covered]
    return covered, missing, errors


# ---------------------------------------------------------------------------
# Inflight manifest — the deterministic execution-ack (REQ-30). Written by
# intentions_pre.sh after mark_triggered; resolved by intentions_post.py on
# EVERY outcome (envelope, garbage, or __NO_ENVELOPE__). The absence of a
# Claude envelope is itself a deterministic signal: ids not covered get the
# bounded-retry policy applied immediately instead of waiting to be swept.
# ---------------------------------------------------------------------------

def write_inflight(ids: list[str], breach_ids: list[str] | None = None) -> None:
    """Persist the intent ids handed to the current Claude cycle, plus the
    breach ids that rode this cycle's PRE apology prompt (so post marks
    exactly those shown — not a blanket wipe that would eat reconcile's
    freshly-queued breaches)."""
    INFLIGHT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INFLIGHT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(
        {"ts": now_local_str("%Y-%m-%dT%H:%M:%S"), "ids": list(ids),
         "breach_ids": list(breach_ids or [])},
        ensure_ascii=False))
    import os
    os.replace(tmp, INFLIGHT_FILE)


def read_inflight() -> list[str]:
    try:
        return list(json.loads(INFLIGHT_FILE.read_text()).get("ids", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def read_inflight_breaches() -> list[str]:
    try:
        return list(json.loads(INFLIGHT_FILE.read_text()).get("breach_ids", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def clear_inflight() -> None:
    try:
        INFLIGHT_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def reconcile_inflight(covered_ids: list[str]) -> dict:
    """Resolve manifest ids the envelope did NOT cover (REQ-30c).

    Applies the bounded-retry policy immediately — no 10-minute sweeper wait:
    rows still 'triggered' that weren't covered go back to 'pending' while
    they have retry budget, or expire loudly (breach queue + closure axis)
    when exhausted. Covered ids are the caller's job (mark_executed et al).
    Returns {"retried": [...], "expired": [...]} and clears the manifest.
    """
    _init()
    inflight = read_inflight()
    # "breached" = ids this call newly appended to the breach queue. The caller
    # must NOT mark these shown this cycle — they were queued AFTER the pre's
    # apology-card prompt, so they haven't ridden a card yet (red-team fix).
    out = {"retried": [], "expired": [], "breached": []}
    if not inflight:
        return out
    db = _get_db()
    now = now_local()
    covered = set(covered_ids)
    terminal_moments: list[dict] = []
    for iid in inflight:
        if iid in covered:
            continue
        it = get_intent(iid)
        if not it or it.get("status") != "triggered":
            continue  # already resolved by someone else
        attempt = it.get("attempt") or 0
        if it.get("trigger_type") != "date":
            db.execute(
                "UPDATE intentions SET status = 'pending', last_error = ? WHERE id = ?",
                ("retry: envelope missing", iid))
            _emit_intent("intent_retry", iid, attempt=attempt,
                         kind=it.get("trigger_type"))
            out["retried"].append(iid)
            continue
        target = _trigger_dt(it)
        age = (now - target) if target else (STORM_AGE + timedelta(seconds=1))
        if attempt < MAX_ATTEMPTS and age < RETRY_GRACE:
            db.execute(
                "UPDATE intentions SET status = 'pending', last_error = ? WHERE id = ?",
                (f"retry {attempt}/{MAX_ATTEMPTS}: envelope missing", iid))
            _emit_intent("intent_retry", iid, attempt=attempt, kind="date")
            out["retried"].append(iid)
        else:
            db.execute(
                "UPDATE intentions SET status = 'expired', last_error = ? WHERE id = ?",
                (f"expired after {attempt} attempts (envelope missing) — breach queued", iid))
            _queue_breach(it, now)
            _emit_intent("intent_expired", iid, attempt=attempt,
                         notified=True, reason="retries_exhausted")
            terminal_moments.append(it)
            out["expired"].append(iid)
            out["breached"].append(iid)
    db.commit()
    # Closure spawns after the commit — see lifecycle_sweep (lock contention).
    for it in terminal_moments:
        try:
            _on_moment_terminal(it, how="expired")
        except Exception as e:
            print(f"[intentions] closure-on-expiry failed for {it['id']}: {e}",
                  file=sys.stderr)
    clear_inflight()
    return out


# ---------------------------------------------------------------------------
# Breach queue — dropped commitments awaiting their apology card (REQ-31)
# ---------------------------------------------------------------------------

# A breach apology is shown ONCE, not retried (2026-06-15 fix). The old
# default of 3 meant the same "我没能按时提醒" card fired on 3 separate cycles
# — Pascal received the identical 「和小明哥哥吃饭」饭后闭环 apology 3 times
# across two days. A rendered card IS delivered (genuine send failures are the
# REQ-11 delivery-ACK layer's job, not this counter), and breach cards carry
# ✅/❌/🚫 buttons, so one apology is enough; re-showing is pure nagging.
BREACH_MAX_SHOWS = 1


def peek_breaches(max_notify_attempts: int = BREACH_MAX_SHOWS) -> list[dict]:
    """Return breach entries still owed a notification — WITHOUT mutating.

    Red-team fix (2026-06-13): the old drain_breaches bumped notify_attempts
    on every PRE invocation, but breaches were only cleared on a PARSED
    envelope. A no-envelope / parse-fail cycle (Claude produced nothing) thus
    burned a delivery attempt without ever rendering the apology card — three
    such cycles silently dropped the breach. The counter must count CARDS
    RENDERED, not pre-script runs. So peek is read-only; mark_breaches_shown
    is the only writer, called only when a card actually went out. Shown at
    most BREACH_MAX_SHOWS times (1) — a breach apology must never nag.
    """
    if not BREACH_QUEUE.exists():
        return []
    entries = []
    try:
        for line in BREACH_QUEUE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(e.get("notify_attempts", 0)) < max_notify_attempts:
                entries.append(e)
    except OSError as e:
        print(f"[intentions] breach peek failed: {e}", file=sys.stderr)
        return []
    return entries


def mark_breaches_shown(ids: list[str], max_notify_attempts: int = BREACH_MAX_SHOWS) -> None:
    """Bump notify_attempts for the breach ids that just rode a RENDERED card.

    Entries reaching max_notify_attempts are dropped (shown enough). Only the
    ids passed in are touched — a breach freshly queued by reconcile_inflight
    in the same post-cycle is NOT in this set, so the old blanket
    clear_breaches() wipe (which deleted reconcile's just-queued breach) is
    gone. Atomic tmp+rename. Never raises.
    """
    if not BREACH_QUEUE.exists() or not ids:
        return
    shown = set(ids)
    keep = []
    try:
        for line in BREACH_QUEUE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("id") in shown:
                e["notify_attempts"] = int(e.get("notify_attempts", 0)) + 1
                if e["notify_attempts"] >= max_notify_attempts:
                    continue  # shown enough — retire
            keep.append(e)
        tmp = BREACH_QUEUE.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in keep)
                       + ("\n" if keep else ""), encoding="utf-8")
        import os
        os.replace(tmp, BREACH_QUEUE)
    except OSError as e:
        print(f"[intentions] breach mark-shown failed: {e}", file=sys.stderr)


# Back-compat alias: drain_breaches now means "peek" (non-mutating). Callers
# that relied on the bump must migrate to mark_breaches_shown.
drain_breaches = peek_breaches


def clear_breaches(ids: list[str] | None = None) -> None:
    """Remove breach entries by id once their card went out. ids=None is a
    no-op now (the old blanket wipe deleted reconcile's freshly-queued
    breaches — use mark_breaches_shown with explicit ids instead)."""
    if not BREACH_QUEUE.exists() or not ids:
        return
    try:
        drop = set(ids)
        keep = []
        for line in BREACH_QUEUE.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("id") not in drop:
                    keep.append(line)
            except json.JSONDecodeError:
                continue
        tmp = BREACH_QUEUE.with_suffix(".tmp")
        tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
        import os
        os.replace(tmp, BREACH_QUEUE)
    except OSError as e:
        print(f"[intentions] breach clear failed: {e}", file=sys.stderr)


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


def closure_stats() -> dict:
    """Per-category closure funnel — the learning record made usable (REQ-33).

    Answers what the system previously could not: which categories actually
    close? are reading-closure intents systematically dying (wrong TYPE, not
    just broken pipeline)? Consumed by the nightly 每晚intent复盘 and the
    dashboard funnel. Moments only (follow-up rows excluded).
    """
    _init()
    db = _get_db()
    out: dict = {}
    rows = db.execute(
        "SELECT category, status, closure_status, closure_question, "
        "created_at, executed_at, closed_at FROM intentions "
        "WHERE parent_intent_id IS NULL OR parent_intent_id = ''"
    ).fetchall()
    for r in rows:
        it = dict(r)
        cat = it.get("category") or "none"
        c = out.setdefault(cat, {
            "created": 0, "fired": 0, "executed": 0, "expired": 0,
            "closure_bearing": 0, "closed_done": 0, "closed_recorded": 0,
            "closed_na": 0, "awaiting": 0, "hours_to_close": [],
        })
        c["created"] += 1
        if it.get("executed_at") or it.get("status") in ("executed", "expired"):
            c["fired"] += 1
        if it.get("status") == "executed":
            c["executed"] += 1
        if it.get("status") == "expired":
            c["expired"] += 1
        if (it.get("closure_question") or "").strip():
            c["closure_bearing"] += 1
        cs = it.get("closure_status") or "none"
        if cs == "done":
            c["closed_done"] += 1
        elif cs == "recorded":
            c["closed_recorded"] += 1
        elif cs == "na":
            c["closed_na"] += 1
        elif cs == "awaiting":
            c["awaiting"] += 1
        if it.get("closed_at") and it.get("executed_at"):
            try:
                dt_open = datetime.fromisoformat(it["executed_at"])
                dt_close = datetime.fromisoformat(it["closed_at"])
                c["hours_to_close"].append(
                    round((dt_close - dt_open).total_seconds() / 3600, 1))
            except (ValueError, TypeError):
                pass
    for cat, c in out.items():
        hrs = sorted(c.pop("hours_to_close"))
        c["median_hours_to_close"] = hrs[len(hrs) // 2] if hrs else None
    return out


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
        if rest and rest[0] == "--closure":
            print(json.dumps(closure_stats(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(intent_stats(), ensure_ascii=False))
        return 0

    if cmd == "reset-stale":
        mins = int(rest[0]) if rest and rest[0].isdigit() else 10
        n = lifecycle_sweep(stale_minutes=mins)
        print(f"swept: {n} intent(s) back to pending for retry "
              "(exhausted ones expired with breach notification)")
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
