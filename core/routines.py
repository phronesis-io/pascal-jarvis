"""User-authored Routines — recurring work Pascal can create by talking.

Before this module every proactive behavior in Jarvis was developer-authored:
a `tasks/<name>_pre.sh` + `_post.py` pair, a block in HEARTBEAT.md, and a
deploy. "每周五下班前把这周的产量汇总给我" cost a release. A Routine is the
same idea reduced to a durable record the user owns:

    trigger  — when it runs (cron, or a fixed interval)
    evidence — which read-only sources are gathered BEFORE the model runs
    autonomy — how far it may go without asking
    instruction — what to produce, in the user's own words

**Autonomy is a contract enforced in code, not a request in a prompt.**

    observe  — runs and records into the audit trail. Never delivers, never
               acts. The read-only mode: use it to watch a routine's judgment
               for a week before letting it interrupt anyone.
    propose  — delivers one memorial card. Any consequence needs 批红. Default.
    act      — propose, plus a bounded allow-list of internal, reversible
               actions it may take on its own. External mutations are NOT on
               that list and never will be: those belong to verified
               Delegation, which owns read-back evidence.

A Routine is not a new scheduler and not a new state machine over user
attention. Firing reuses the same next_fire_at catch-up primitive as Intents,
and everything it shows the user is an ordinary Memorial routed by
core.delivery.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core.routine_evidence import EvidenceError, PROVIDER_HELP, validate_spec
from core.timeutil import now_local, now_local_str

ROOT = Path(__file__).resolve().parent.parent

AUTONOMY_OBSERVE = "observe"
AUTONOMY_PROPOSE = "propose"
AUTONOMY_ACT = "act"
AUTONOMY_LEVELS = (AUTONOMY_OBSERVE, AUTONOMY_PROPOSE, AUTONOMY_ACT)

AUTONOMY_HELP = {
    AUTONOMY_OBSERVE: "只看不说：跑完只进审计记录，永不打扰",
    AUTONOMY_PROPOSE: "提方案等你点头：出一张卡，动作要你确认（默认）",
    AUTONOMY_ACT: "可自己动手：限内部可逆动作（建 intent / 记任务 / 写笔记）",
}

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_ARCHIVED = "archived"

# The only actions an `act` routine may take on its own. Each is internal to
# Jarvis and reversible by the user. Anything that mutates an outside system
# stays off this list on purpose — see the module docstring.
ALLOWED_ACTIONS = ("create_intent", "add_task", "note")

MAX_ACTIONS_PER_RUN = 5
MAX_TITLE = 40
MAX_BODY = 1200
MAX_ACTIVE_ROUTINES = 40

_sys_path_added = False


def _get_db():
    global _sys_path_added
    if not _sys_path_added:
        sys.path.insert(0, str(ROOT))
        _sys_path_added = True
    from dashboard.db import get_db
    return get_db()


_initialized = False


def _init() -> None:
    global _initialized
    if _initialized:
        return
    db = _get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            trigger_type TEXT NOT NULL DEFAULT 'cron',
            trigger_expr TEXT NOT NULL DEFAULT '',
            autonomy TEXT NOT NULL DEFAULT 'propose',
            instruction TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT 'user',
            next_fire_at TEXT,
            last_run_at TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_routines_status ON routines(status);

        CREATE TABLE IF NOT EXISTS routine_runs (
            id TEXT PRIMARY KEY,
            routine_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            autonomy TEXT NOT NULL DEFAULT '',
            evidence_sources TEXT NOT NULL DEFAULT '[]',
            output TEXT NOT NULL DEFAULT '',
            actions TEXT NOT NULL DEFAULT '[]',
            memorial_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_routine_runs_rid
            ON routine_runs(routine_id, started_at);
    """)
    db.commit()
    _initialized = True


# ── validation ───────────────────────────────────────────────────────────────


class RoutineError(Exception):
    """A routine definition is unusable. The message is shown to the user."""


def _validate_trigger(trigger_type: str, expr: str) -> str:
    trigger_type = str(trigger_type).strip().lower()
    expr = str(expr).strip()
    if trigger_type == "cron":
        from dashboard.scheduler import cron_next
        if len(expr.split()) != 5:
            raise RoutineError(f"cron 表达式要 5 段，收到 {expr!r}")
        if cron_next(expr) is None:
            raise RoutineError(f"cron 表达式 {expr!r} 在一年内不会触发，八成写错了")
        return expr
    if trigger_type == "interval":
        try:
            seconds = int(expr)
        except ValueError as exc:
            raise RoutineError(f"interval 要一个秒数，收到 {expr!r}") from exc
        # Below 5 minutes a routine competes with the heartbeat itself for the
        # model budget and stops being a routine; above 30 days it is a
        # calendar entry, not a rhythm.
        if not 300 <= seconds <= 30 * 86400:
            raise RoutineError("interval 必须在 300 秒到 30 天之间")
        return str(seconds)
    raise RoutineError(f"trigger 只支持 cron 或 interval，收到 {trigger_type!r}")


def _validate_evidence(evidence) -> list[str]:
    if isinstance(evidence, str):
        evidence = [p for p in evidence.split(",") if p.strip()]
    out: list[str] = []
    for spec in list(evidence or []):
        try:
            out.append(validate_spec(spec))
        except EvidenceError as exc:
            raise RoutineError(str(exc)) from exc
    if len(out) > 8:
        raise RoutineError("一个例程最多 8 个证据源，再多提示词就装不下了")
    return out


def _validate_autonomy(autonomy: str) -> str:
    autonomy = str(autonomy or AUTONOMY_PROPOSE).strip().lower()
    if autonomy not in AUTONOMY_LEVELS:
        raise RoutineError(
            f"autonomy 只能是 {'/'.join(AUTONOMY_LEVELS)}，收到 {autonomy!r}")
    return autonomy


def _next_fire(trigger_type: str, expr: str,
               after: datetime | None = None) -> datetime | None:
    after = after or now_local()
    if trigger_type == "cron":
        from dashboard.scheduler import cron_next
        return cron_next(expr, after=after)
    return after + timedelta(seconds=int(expr))


# ── CRUD ─────────────────────────────────────────────────────────────────────


def create_routine(name: str, trigger_type: str, trigger_expr: str,
                   instruction: str, *, autonomy: str = AUTONOMY_PROPOSE,
                   evidence=None, created_by: str = "user") -> dict:
    """Register a routine. Raises RoutineError with a user-readable reason."""
    _init()
    name = str(name).strip()
    instruction = str(instruction).strip()
    if not name:
        raise RoutineError("例程要有名字")
    if len(name) > 40:
        raise RoutineError("名字最多 40 字")
    if not instruction:
        raise RoutineError("要说清这个例程每次该产出什么")
    trigger_type = str(trigger_type).strip().lower()
    expr = _validate_trigger(trigger_type, trigger_expr)
    autonomy = _validate_autonomy(autonomy)
    specs = _validate_evidence(evidence)

    db = _get_db()
    if db.execute(
        "SELECT 1 FROM routines WHERE name = ? AND status != ?",
        (name, STATUS_ARCHIVED),
    ).fetchone():
        raise RoutineError(f"已经有一个叫「{name}」的例程了")
    active_count = db.execute(
        "SELECT COUNT(*) FROM routines WHERE status = ?", (STATUS_ACTIVE,)
    ).fetchone()[0]
    if active_count >= MAX_ACTIVE_ROUTINES:
        raise RoutineError(
            f"活跃例程已达上限 {MAX_ACTIVE_ROUTINES}，先停用几个再建")

    rid = f"rt_{uuid.uuid4().hex[:8]}"
    nxt = _next_fire(trigger_type, expr)
    db.execute(
        "INSERT INTO routines (id, name, status, trigger_type, trigger_expr,"
        " autonomy, instruction, evidence, created_at, created_by, next_fire_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (rid, name, STATUS_ACTIVE, trigger_type, expr, autonomy, instruction,
         json.dumps(specs, ensure_ascii=False), now_local_str(), created_by,
         nxt.strftime("%Y-%m-%d %H:%M") if nxt else None),
    )
    db.commit()
    return get_routine(rid)


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["evidence"] = json.loads(d.get("evidence") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["evidence"] = []
    return d


def get_routine(rid: str) -> dict | None:
    _init()
    row = _get_db().execute("SELECT * FROM routines WHERE id = ?", (rid,)).fetchone()
    return _row_to_dict(row) if row else None


def find_routine(ref: str) -> dict | None:
    """Resolve by id or by exact non-archived name — the user types the name."""
    _init()
    hit = get_routine(ref)
    if hit:
        return hit
    row = _get_db().execute(
        "SELECT * FROM routines WHERE name = ? AND status != ? "
        "ORDER BY created_at DESC LIMIT 1", (ref, STATUS_ARCHIVED)).fetchone()
    return _row_to_dict(row) if row else None


def list_routines(status: str | None = STATUS_ACTIVE) -> list[dict]:
    _init()
    db = _get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM routines WHERE status = ? ORDER BY created_at",
            (status,)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM routines WHERE status != ? ORDER BY status, created_at",
            (STATUS_ARCHIVED,)).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_routine(rid: str, **fields) -> dict:
    """Edit a routine. Unknown fields are rejected rather than ignored."""
    _init()
    current = get_routine(rid)
    if not current:
        raise RoutineError(f"没有这个例程：{rid}")

    sets, params = [], []
    if "name" in fields:
        name = str(fields.pop("name")).strip()
        if not name:
            raise RoutineError("名字不能空")
        sets.append("name = ?")
        params.append(name)
    if "instruction" in fields:
        instruction = str(fields.pop("instruction")).strip()
        if not instruction:
            raise RoutineError("产出说明不能空")
        sets.append("instruction = ?")
        params.append(instruction)
    if "autonomy" in fields:
        sets.append("autonomy = ?")
        params.append(_validate_autonomy(fields.pop("autonomy")))
    if "evidence" in fields:
        sets.append("evidence = ?")
        params.append(json.dumps(_validate_evidence(fields.pop("evidence")),
                                 ensure_ascii=False))
    if "trigger_type" in fields or "trigger_expr" in fields:
        ttype = str(fields.pop("trigger_type", current["trigger_type"])).lower()
        expr = _validate_trigger(ttype, fields.pop("trigger_expr",
                                                   current["trigger_expr"]))
        nxt = _next_fire(ttype, expr)
        sets += ["trigger_type = ?", "trigger_expr = ?", "next_fire_at = ?"]
        params += [ttype, expr, nxt.strftime("%Y-%m-%d %H:%M") if nxt else None]
    if fields:
        raise RoutineError(f"不认识的字段：{', '.join(sorted(fields))}")
    if not sets:
        return current

    params.append(rid)
    db = _get_db()
    db.execute(f"UPDATE routines SET {', '.join(sets)} WHERE id = ?", params)
    db.commit()
    return get_routine(rid)


def set_status(rid: str, status: str) -> dict:
    _init()
    if status not in (STATUS_ACTIVE, STATUS_PAUSED, STATUS_ARCHIVED):
        raise RoutineError(f"未知状态 {status!r}")
    current = get_routine(rid)
    if not current:
        raise RoutineError(f"没有这个例程：{rid}")
    db = _get_db()
    if status == STATUS_ACTIVE:
        # Resuming re-arms from *now*. Firing every occurrence missed while
        # paused would dump a week of cards the moment it comes back.
        nxt = _next_fire(current["trigger_type"], current["trigger_expr"])
        db.execute("UPDATE routines SET status = ?, next_fire_at = ? WHERE id = ?",
                   (status, nxt.strftime("%Y-%m-%d %H:%M") if nxt else None, rid))
    else:
        db.execute("UPDATE routines SET status = ? WHERE id = ?", (status, rid))
    db.commit()
    return get_routine(rid)


# ── firing ───────────────────────────────────────────────────────────────────


def claim_due(now: datetime | None = None, limit: int = 3) -> list[dict]:
    """Atomically claim every due routine and open its audit run.

    The claim advances next_fire_at and writes the run row in one transaction,
    so a crash between claim and delivery loses at most that occurrence's
    output — never re-fires it forever, and never silently drops it without a
    `running` row someone can find.

    `limit` bounds how many routines share one heartbeat model call.
    """
    _init()
    now = now or now_local()
    now_s = now.strftime("%Y-%m-%d %H:%M")
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM routines WHERE status = ? AND next_fire_at IS NOT NULL"
        " AND next_fire_at <= ? ORDER BY next_fire_at LIMIT ?",
        (STATUS_ACTIVE, now_s, limit),
    ).fetchall()

    claimed = []
    for row in rows:
        r = _row_to_dict(row)
        nxt = _next_fire(r["trigger_type"], r["trigger_expr"], after=now)
        run_id = f"rr_{uuid.uuid4().hex[:10]}"
        try:
            # Optimistic lock: the WHERE pins the exact next_fire_at we read,
            # so of two processes racing the same occurrence only one gets
            # rowcount 1. Re-reading the row instead would be useless — after
            # a successful UPDATE it shows the new value either way.
            cur = db.execute(
                "UPDATE routines SET next_fire_at = ?, last_run_at = ?,"
                " run_count = run_count + 1 WHERE id = ? AND next_fire_at = ?",
                (nxt.strftime("%Y-%m-%d %H:%M") if nxt else None, now_s,
                 r["id"], row["next_fire_at"]),
            )
            if cur.rowcount != 1:
                # Another process won the claim; leave the occurrence to them.
                db.rollback()
                continue
            db.execute(
                "INSERT INTO routine_runs (id, routine_id, started_at, status,"
                " autonomy) VALUES (?,?,?,?,?)",
                (run_id, r["id"], now_local_str(), "running", r["autonomy"]),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        r["run_id"] = run_id
        claimed.append(r)
    return claimed


def finish_run(run_id: str, status: str, *, output: str = "",
               memorial_id: str = "", actions: list | None = None,
               error: str = "", evidence_sources: list | None = None) -> None:
    """Close an audit run. Every claimed run must reach a terminal row."""
    _init()
    db = _get_db()
    db.execute(
        "UPDATE routine_runs SET finished_at = ?, status = ?, output = ?,"
        " memorial_id = ?, actions = ?, error = ?, evidence_sources = ?"
        " WHERE id = ?",
        (now_local_str(), status, str(output)[:4000], memorial_id,
         json.dumps(actions or [], ensure_ascii=False), str(error)[:500],
         json.dumps(evidence_sources or [], ensure_ascii=False), run_id),
    )
    row = db.execute("SELECT routine_id FROM routine_runs WHERE id = ?",
                     (run_id,)).fetchone()
    if row:
        db.execute("UPDATE routines SET last_status = ?, last_error = ? WHERE id = ?",
                   (status, str(error)[:500], row["routine_id"]))
    db.commit()


def sweep_stuck_runs(max_age_minutes: int = 60) -> int:
    """Close runs whose process died before finishing.

    Without this a crashed cycle leaves a permanent `running` row and the audit
    view claims work is in flight that nothing is doing.
    """
    _init()
    cutoff = (now_local() - timedelta(minutes=max_age_minutes)
              ).strftime("%Y-%m-%d %H:%M")
    db = _get_db()
    stuck = db.execute(
        "SELECT id FROM routine_runs WHERE status = 'running' AND started_at < ?",
        (cutoff,)).fetchall()
    for row in stuck:
        finish_run(row["id"], "failed", error="进程未收尾（已由巡检关闭）")
    return len(stuck)


def list_runs(rid: str | None = None, limit: int = 20) -> list[dict]:
    _init()
    db = _get_db()
    if rid:
        rows = db.execute(
            "SELECT * FROM routine_runs WHERE routine_id = ?"
            " ORDER BY started_at DESC LIMIT ?", (rid, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM routine_runs ORDER BY started_at DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for key in ("actions", "evidence_sources"):
            try:
                d[key] = json.loads(d.get(key) or "[]")
            except (json.JSONDecodeError, TypeError):
                d[key] = []
        out.append(d)
    return out


# ── autonomy enforcement ─────────────────────────────────────────────────────


def authorize_actions(autonomy: str, requested) -> tuple[list[dict], list[str]]:
    """Split a model's requested actions into (permitted, refused reasons).

    This is the enforcement point for the autonomy contract. It is deliberately
    a pure function over the routine's stored level and the model's proposal:
    the model can ask for anything, and what it gets is decided here.
    """
    permitted: list[dict] = []
    refused: list[str] = []
    items = [a for a in list(requested or []) if isinstance(a, dict)]
    if not items:
        return permitted, refused
    if autonomy != AUTONOMY_ACT:
        return permitted, [
            f"{a.get('type', '?')}：例程是 {autonomy} 级，动作要你点头" for a in items]
    for action in items[:MAX_ACTIONS_PER_RUN]:
        atype = str(action.get("type", "")).strip()
        if atype not in ALLOWED_ACTIONS:
            refused.append(f"{atype or '(空)'}：不在 act 白名单内")
            continue
        permitted.append(action)
    if len(items) > MAX_ACTIONS_PER_RUN:
        refused.append(f"超出单次 {MAX_ACTIONS_PER_RUN} 个动作上限，多余的已丢弃")
    return permitted, refused


def execute_actions(routine: dict, actions: list[dict]) -> list[dict]:
    """Run already-authorized actions. Returns one result record per action.

    Never raises: a failed action becomes a failed record in the audit trail so
    the run's report can say what did not happen.
    """
    results = []
    for action in actions:
        atype = str(action.get("type", ""))
        record = {"type": atype, "ok": False, "detail": ""}
        try:
            if atype == "create_intent":
                from core import intentions
                name = str(action.get("name", "")).strip()[:60]
                when = str(action.get("when", "")).strip()
                if not name or not when:
                    raise ValueError("create_intent 需要 name 和 when")
                iid = intentions.create_intent(
                    name=name, trigger_type="date",
                    trigger_config={"datetime": when},
                    prompt=str(action.get("prompt", name))[:500],
                    source=f"routine:{routine['name']}",
                    action_type="notify",
                )
                record.update(ok=True, detail=f"intent {iid} @ {when}")
            elif atype == "add_task":
                from core.routine_evidence import _memory_dir
                from core.tasks import TaskManager
                title = str(action.get("title", "")).strip()[:120]
                if not title:
                    raise ValueError("add_task 需要 title")
                task = TaskManager(_memory_dir()).capture(
                    title=title, source=f"routine:{routine['name']}")
                record.update(ok=True, detail=f"task {task.get('id')}")
            elif atype == "note":
                from core.routine_evidence import _memory_dir
                text = str(action.get("text", "")).strip()[:1000]
                if not text:
                    raise ValueError("note 需要 text")
                path = _memory_dir() / "system" / "routine_notes.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"\n## {now_local_str()} · {routine['name']}\n{text}\n")
                record.update(ok=True, detail=f"note → {path.name}")
            else:
                raise ValueError(f"未授权动作 {atype!r}")
        except Exception as exc:
            record["detail"] = f"{type(exc).__name__}: {exc}"
        results.append(record)
    return results


# ── heartbeat integration ────────────────────────────────────────────────────


def _inflight_path() -> Path:
    import os
    base = Path(os.environ.get("JARVIS_DIR") or ROOT)
    return base / "data" / "routine_inflight.json"


def record_evidence(run_id: str, sources: list[str]) -> None:
    """Stamp what was actually read, before any model call.

    Written at claim time on purpose: if the model call dies, the audit row
    still says which sources were gathered, so a later reader can tell an
    empty world from an unread one.
    """
    _init()
    db = _get_db()
    db.execute("UPDATE routine_runs SET evidence_sources = ? WHERE id = ?",
               (json.dumps(sources, ensure_ascii=False), run_id))
    db.commit()


def defer_inflight_infrastructure(reason: str = "模型调用失败",
                                  retry_minutes: int = 5) -> dict:
    """Close claimed runs as deferred and re-arm them after channel failure.

    A Routine occurrence is claimed before the model call.  Quota, network and
    timeout failures are not content decisions, so they must never become
    ``no_output`` or spend the occurrence.  The current audit row stays
    terminal and the Routine is made due again on a short bounded cadence; the
    heartbeat's shared backoff remains the outer retry throttle.
    """
    _init()
    try:
        inflight = json.loads(_inflight_path().read_text(encoding="utf-8"))
        if not isinstance(inflight, list):
            inflight = []
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        inflight = []

    db = _get_db()
    now = now_local()
    finished_at = now.strftime("%Y-%m-%d %H:%M")
    retry_at = (now + timedelta(minutes=max(1, int(retry_minutes)))).strftime(
        "%Y-%m-%d %H:%M")
    error = f"模型基础设施失败，已安排重试：{str(reason or 'unknown')[:420]}"
    deferred: list[str] = []

    try:
        for entry in inflight:
            if not isinstance(entry, dict):
                continue
            run_id = str(entry.get("run_id") or "")
            routine_id = str(entry.get("routine_id") or "")
            if not run_id or not routine_id:
                continue
            cur = db.execute(
                "UPDATE routine_runs SET finished_at = ?, status = 'deferred',"
                " error = ? WHERE id = ? AND routine_id = ? AND status = 'running'",
                (finished_at, error[:500], run_id, routine_id),
            )
            if cur.rowcount != 1:
                continue
            db.execute(
                "UPDATE routines SET next_fire_at = CASE"
                " WHEN next_fire_at IS NULL OR next_fire_at > ? THEN ?"
                " ELSE next_fire_at END, last_status = 'deferred', last_error = ?"
                " WHERE id = ? AND status = ?",
                (retry_at, retry_at, error[:500], routine_id, STATUS_ACTIVE),
            )
            deferred.append(run_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

    # Clear the receipt only after the database transaction is durable.  If
    # SQLite fails, keeping the manifest is what makes the claim recoverable.
    from core.safety import atomic_write
    atomic_write(_inflight_path(), "[]")

    return {"deferred": deferred}


def emit_due_block(now: datetime | None = None) -> str:
    """Pre-hook body: claim due routines and render their grounded evidence.

    Returns "" when nothing is due, which is how a heartbeat task declares
    itself idle.
    """
    from core.routine_evidence import collect

    sweep_stuck_runs()
    claimed = claim_due(now=now)
    if not claimed:
        _inflight_path().parent.mkdir(parents=True, exist_ok=True)
        _inflight_path().write_text("[]", encoding="utf-8")
        return ""

    inflight = []
    blocks = [f"=== ROUTINES DUE ({len(claimed)}) ==="]
    for r in claimed:
        evidence_text, gathered = collect(r["evidence"])
        record_evidence(r["run_id"], gathered)
        inflight.append({"run_id": r["run_id"], "routine_id": r["id"],
                         "name": r["name"], "autonomy": r["autonomy"]})
        blocks.append(
            f"\n[run {r['run_id']}] 例程「{r['name']}」\n"
            f"自主级别：{r['autonomy']} — {AUTONOMY_HELP[r['autonomy']]}\n"
            f"要产出：{r['instruction']}\n"
            f"--- 证据（已由确定性代码采集，不要凭记忆改写）---\n"
            f"{evidence_text or '（这个例程没有声明证据源）'}\n"
            f"--- 证据结束 ---")
    _inflight_path().parent.mkdir(parents=True, exist_ok=True)
    _inflight_path().write_text(json.dumps(inflight, ensure_ascii=False),
                                encoding="utf-8")
    return "\n".join(blocks)


def _card_options(routine: dict) -> list[dict]:
    # `action` must be the {"type", "params"} dict core.memorial._execute_action
    # dispatches on. The 'type:k=v' string form is CLI --option syntax and is
    # only parsed there; passing it here would raise inside the callback thread
    # and the button would silently fail.
    return [
        {"key": "ack", "label": "知道了", "action": None},
        {"key": "pause", "label": "这条以后别发了",
         "action": {"type": "routine_pause", "params": {"id": routine["id"]}}},
    ]


def apply_run_result(payload: dict) -> list[dict]:
    """Post-hook body: enforce autonomy and close every claimed run.

    Returns one summary record per run. Runs the model failed to mention are
    closed as `no_output` rather than left `running` — an audit trail that
    quietly accumulates in-flight rows is how silent task death hides.
    """
    _init()
    inflight = []
    try:
        inflight = json.loads(_inflight_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        inflight = []

    results = payload.get("routines") or {}
    if not isinstance(results, dict):
        results = {}

    out = []
    for entry in inflight:
        run_id = str(entry.get("run_id", ""))
        routine = get_routine(str(entry.get("routine_id", "")))
        row = _get_db().execute(
            "SELECT status, evidence_sources FROM routine_runs WHERE id = ?",
            (run_id,)).fetchone()
        if not row or row["status"] != "running":
            continue  # already closed — a re-run of the hook must not re-deliver
        if not routine:
            finish_run(run_id, "failed", error="例程在本轮期间被删除")
            continue

        item = results.get(run_id) or results.get(routine["id"]) or {}
        if not isinstance(item, dict):
            item = {}
        title = str(item.get("title") or routine["name"]).strip()[:MAX_TITLE]
        body = str(item.get("body") or item.get("user_message") or "").strip()[:MAX_BODY]

        if not body:
            finish_run(run_id, "no_output",
                       error="模型没有为这个例程产出内容")
            out.append({"run_id": run_id, "status": "no_output"})
            continue

        permitted, refused = authorize_actions(routine["autonomy"],
                                               item.get("actions"))
        action_results = execute_actions(routine, permitted) if permitted else []

        # observe never reaches the user. That is the whole point of the level:
        # it is how a new routine earns the right to interrupt.
        if routine["autonomy"] == AUTONOMY_OBSERVE:
            finish_run(run_id, "observed", output=body, actions=action_results)
            out.append({"run_id": run_id, "status": "observed"})
            continue

        card_body = body
        if action_results:
            done = "\n".join(
                f"- {'✓' if a['ok'] else '✗'} {a['type']}：{a['detail']}"
                for a in action_results)
            card_body = f"{body}\n\n已自动执行：\n{done}"
        if refused:
            card_body += "\n\n未执行（超出授权）：\n" + "\n".join(
                f"- {reason}" for reason in refused)

        memorial_id = ""
        try:
            from core import memorial
            memorial_id, _ = memorial.create(
                source=f"routine:{routine['name']}"[:40],
                title=title, body=card_body,
                options=_card_options(routine),
                dedup_key=f"routine:{routine['id']}:{run_id}",
                context=json.dumps({"kind": "routine_run",
                                    "routine_id": routine["id"],
                                    "run_id": run_id}, ensure_ascii=False),
            )
            finish_run(run_id, "delivered", output=body,
                       memorial_id=memorial_id, actions=action_results)
            out.append({"run_id": run_id, "status": "delivered",
                        "memorial_id": memorial_id})
        except Exception as exc:
            # The work happened; only the card failed. Record it as failed so
            # the run is not counted as reaching Pascal.
            finish_run(run_id, "failed", output=body, actions=action_results,
                       error=f"发卡失败：{type(exc).__name__}: {exc}")
            out.append({"run_id": run_id, "status": "failed"})

    try:
        _inflight_path().write_text("[]", encoding="utf-8")
    except OSError:
        pass
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _fmt(r: dict) -> str:
    trig = (r["trigger_expr"] if r["trigger_type"] == "cron"
            else f"每 {int(r['trigger_expr']) // 60} 分钟")
    ev = "、".join(r["evidence"]) or "无"
    return (f"{r['id']}  {r['name']}\n"
            f"    触发：{r['trigger_type']} {trig}    下次：{r['next_fire_at'] or '—'}\n"
            f"    自主：{r['autonomy']}（{AUTONOMY_HELP[r['autonomy']]}）\n"
            f"    证据：{ev}\n"
            f"    状态：{r['status']}  跑过 {r['run_count']} 次"
            + (f"  上次：{r['last_status']}" if r["last_status"] else ""))


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="core.routines", description="用户自建例程")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="新建例程")
    c.add_argument("--name", required=True)
    c.add_argument("--trigger", default="cron", choices=("cron", "interval"))
    c.add_argument("--expr", required=True, help="cron 五段式，或 interval 秒数")
    c.add_argument("--instruction", required=True, help="每次该产出什么")
    c.add_argument("--autonomy", default=AUTONOMY_PROPOSE, choices=AUTONOMY_LEVELS)
    c.add_argument("--evidence", default="", help="逗号分隔，如 calendar,cards:7")

    lst = sub.add_parser("list", help="列出例程")
    lst.add_argument("--all", action="store_true", help="含已暂停的")

    for name, helptext in (("pause", "暂停"), ("resume", "恢复"),
                           ("archive", "归档（不可见）")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("ref", help="例程 id 或名字")

    e = sub.add_parser("edit", help="修改例程")
    e.add_argument("ref")
    e.add_argument("--name")
    e.add_argument("--instruction")
    e.add_argument("--autonomy", choices=AUTONOMY_LEVELS)
    e.add_argument("--evidence")
    e.add_argument("--trigger", choices=("cron", "interval"))
    e.add_argument("--expr")

    r = sub.add_parser("runs", help="审计：跑过哪些、结果是什么")
    r.add_argument("ref", nargs="?")
    r.add_argument("--limit", type=int, default=15)

    sub.add_parser("sources", help="有哪些证据源可用")
    # Internal: the routine-run pre-hook's entry point. Claims due routines and
    # prints their grounded evidence block. Not part of the user-facing verbs.
    sub.add_parser("due", help=argparse.SUPPRESS)

    args = p.parse_args(argv)

    try:
        if args.cmd == "create":
            row = create_routine(
                args.name, args.trigger, args.expr, args.instruction,
                autonomy=args.autonomy, evidence=args.evidence)
            print("已建立例程：")
            print(_fmt(row))
        elif args.cmd == "list":
            rows = list_routines(status=None if args.all else STATUS_ACTIVE)
            if not rows:
                print("还没有例程。用 create 建一个。")
            for row in rows:
                print(_fmt(row))
                print()
        elif args.cmd in ("pause", "resume", "archive"):
            row = find_routine(args.ref)
            if not row:
                print(f"没找到例程：{args.ref}", file=sys.stderr)
                return 1
            target = {"pause": STATUS_PAUSED, "resume": STATUS_ACTIVE,
                      "archive": STATUS_ARCHIVED}[args.cmd]
            print(_fmt(set_status(row["id"], target)))
        elif args.cmd == "edit":
            row = find_routine(args.ref)
            if not row:
                print(f"没找到例程：{args.ref}", file=sys.stderr)
                return 1
            changes = {k: v for k, v in (
                ("name", args.name), ("instruction", args.instruction),
                ("autonomy", args.autonomy), ("evidence", args.evidence),
                ("trigger_type", args.trigger), ("trigger_expr", args.expr),
            ) if v is not None}
            print(_fmt(update_routine(row["id"], **changes)))
        elif args.cmd == "runs":
            rid = None
            if args.ref:
                row = find_routine(args.ref)
                if not row:
                    print(f"没找到例程：{args.ref}", file=sys.stderr)
                    return 1
                rid = row["id"]
            runs = list_runs(rid, limit=args.limit)
            if not runs:
                print("还没有跑过。")
            for run in runs:
                head = f"{run['started_at']}  {run['status']}"
                if run["memorial_id"]:
                    head += f"  → 卡 {run['memorial_id']}"
                print(head)
                if run["output"]:
                    print(f"    {run['output'][:160]}")
                for act in run["actions"]:
                    mark = "✓" if act.get("ok") else "✗"
                    print(f"    {mark} {act.get('type')}: {act.get('detail', '')}")
                if run["error"]:
                    print(f"    ! {run['error']}")
        elif args.cmd == "due":
            block = emit_due_block()
            if block:
                print(block)
        elif args.cmd == "sources":
            print("可用证据源：")
            for key, desc in PROVIDER_HELP.items():
                print(f"  {key:<24} {desc}")
            print("\n自主级别：")
            for key, desc in AUTONOMY_HELP.items():
                print(f"  {key:<24} {desc}")
    except RoutineError as exc:
        print(f"不行：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
