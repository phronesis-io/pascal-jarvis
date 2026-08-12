"""REQ-78 batch 4 — per-item backfill for skipped 硬约束 (账单/续费) occurrences.

Pins the batch-4 properties on top of the batch-1 suite (test_skip_digest.py):
  1. SPLIT — occurrences whose intent is category='hard' get one breach entry
     EACH with the original prompt riding along; everything else (other
     categories, unknown ids, no db at all) still folds into the aggregate
     digest, whose copy now carves out the backfill class.
  2. 宁重勿丢 — the per-item append happens BEFORE the consumed-state save
     (the OPPOSITE of the aggregate's 宁丢勿重): a crash between the two
     leaves the reminder already queued, and the redo scan dedupes by the
     deterministic entry id instead of appending a second line.
  3. CHAIN — backfill entries are first-class breaches: peek_breaches sees
     them, mark_breaches_shown retires them.

All paths go through tmp_path (events, state, queue, and a seeded sqlite db);
the real data/ files are never touched — classification opens the db mode=ro.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import core.skip_digest as sd
from core.sched_events import emit


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _missed_iso(hours_ago: float) -> str:
    # production `missed` is missed_dt.isoformat() — "T" separator
    return (datetime.now() - timedelta(hours=hours_ago)).replace(
        microsecond=0).isoformat()


def _emit_skip(jarvis_dir: Path, intent_id: str, name: str,
               hours_ago: float = 3) -> None:
    emit(jarvis_dir, "intent_occurrence_skipped", task=intent_id,
         missed=_missed_iso(hours_ago), name=name)


def _seed_db(jarvis_dir: Path, rows: list[tuple]) -> None:
    """(id, name, prompt, category) rows — only the columns _intent_row reads."""
    db = jarvis_dir / "data" / "jarvis.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE intentions "
                "(id TEXT PRIMARY KEY, name TEXT, prompt TEXT, category TEXT)")
    con.executemany("INSERT INTO intentions VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()


def _breach_lines(jarvis_dir: Path) -> list[dict]:
    q = jarvis_dir / "data" / ".intent_breach_queue.jsonl"
    if not q.exists():
        return []
    return [json.loads(line) for line in
            q.read_text(encoding="utf-8").splitlines() if line.strip()]


BILL_PROMPT = "信用卡账单 ¥12,345.67 今天到期，提醒 Pascal 还款。"


def test_hard_category_split_from_aggregate(tmp_path):
    jd = tmp_path
    _seed_db(jd, [
        ("int_bill", "七月信用卡还款", BILL_PROMPT, "hard"),
        ("int_prep", "会议 prep", "喂上下文", "context"),
    ])
    _emit_skip(jd, "int_bill", "七月信用卡还款", hours_ago=5)
    _emit_skip(jd, "int_prep", "会议 prep", hours_ago=4)
    _emit_skip(jd, "int_ghost", "已删除的提醒", hours_ago=2)  # no db row

    assert sd.queue_digest(jd, force=True) == 3
    lines = _breach_lines(jd)
    assert len(lines) == 2

    item = next(l for l in lines if l["id"].startswith("skipitem_"))
    assert item["id"].startswith("skipitem_int_bill_")
    assert item["name"] == "补发提醒：七月信用卡还款"
    assert BILL_PROMPT in item["prompt"]          # original prompt rides along
    assert "补上" in item["prompt"] and "停摆" in item["prompt"]
    assert "迟到了约 5 小时" in item["prompt"]
    assert item["notify_attempts"] == 0
    # the fields intentions_pre.sh renders into the card payload all exist
    for field in ("prompt", "purpose", "trigger_time", "attempt"):
        assert field in item

    digest = next(l for l in lines if l["id"].startswith("skipdigest_"))
    assert digest["name"] == "停摆期间跳过了 2 件事"
    assert "会议 prep" in digest["prompt"]
    assert "已删除的提醒" in digest["prompt"]
    assert "七月信用卡还款" not in digest["prompt"]   # not double-reported
    assert "不逐条补发" in digest["prompt"]           # untagged promise kept
    assert "单独补发" in digest["prompt"]             # …with the carve-out

    # all three consumed; rerun adds nothing (watchdog-restart scenario)
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 3
    assert sd.queue_digest(jd, force=True) == 0
    assert len(_breach_lines(jd)) == 2


def test_autonomous_and_healing_skips_are_audited_without_user_breach(tmp_path):
    jd = tmp_path
    _seed_db(jd, [
        ("int_hourly", "小时报", "写入内部时间线", "autonomous"),
        ("int_heal", "温和观察", "只记录不催", "healing"),
    ])
    _emit_skip(jd, "int_hourly", "小时报")
    _emit_skip(jd, "int_heal", "温和观察")

    assert sd.queue_digest(jd, force=True) == 2
    assert _breach_lines(jd) == []
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 2
    assert sd.diag_line(jd).startswith("✓")


def test_no_db_degrades_to_aggregate_only(tmp_path):
    jd = tmp_path
    _emit_skip(jd, "int_bill", "信用卡还款提醒")
    assert sd.queue_digest(jd, force=True) == 1
    lines = _breach_lines(jd)
    assert len(lines) == 1
    assert lines[0]["id"].startswith("skipdigest_")   # batch-1 behavior


def test_crash_between_deliver_and_consume_redelivers_without_dup(tmp_path,
                                                                  monkeypatch):
    """宁重勿丢: append-first ordering + deterministic-id dedupe on the redo."""
    jd = tmp_path
    _seed_db(jd, [("int_bill", "七月信用卡还款", BILL_PROMPT, "hard")])
    _emit_skip(jd, "int_bill", "七月信用卡还款")

    def boom(path, state):
        raise OSError("crash between deliver and consume")

    monkeypatch.setattr(sd, "_save_state", boom)
    assert sd.queue_digest(jd, force=True) == 0   # fail-open, no raise
    monkeypatch.undo()

    # the reminder was DELIVERED (queued) before the crash — never lost
    lines = _breach_lines(jd)
    assert len(lines) == 1 and lines[0]["id"].startswith("skipitem_int_bill_")
    # …but NOT consumed, so the next scan retries it
    assert not (jd / "data" / ".skip_digest_state.json").exists()

    # redo scan: event re-collected, append deduped by id — exactly one line
    assert sd.queue_digest(jd, force=True) == 1
    lines = _breach_lines(jd)
    assert len(lines) == 1
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 1

    # steady state: nothing more to consume, still one line
    assert sd.queue_digest(jd, force=True) == 0
    assert len(_breach_lines(jd)) == 1


def test_backfill_entry_rides_full_breach_chain(tmp_path, monkeypatch):
    import core.intentions as intentions
    jd = tmp_path
    monkeypatch.setattr(intentions, "BREACH_QUEUE",
                        jd / "data" / ".intent_breach_queue.jsonl")
    _seed_db(jd, [("int_bill", "七月信用卡还款", BILL_PROMPT, "hard")])
    _emit_skip(jd, "int_bill", "七月信用卡还款")

    assert sd.queue_digest(jd, force=True) == 1
    breaches = intentions.peek_breaches()
    assert len(breaches) == 1
    b = breaches[0]
    assert b["id"].startswith("skipitem_int_bill_")
    assert BILL_PROMPT in b["prompt"]

    # peek is non-mutating; one rendered card retires it (BREACH_MAX_SHOWS=1)
    assert len(intentions.peek_breaches()) == 1
    intentions.mark_breaches_shown([b["id"]])
    assert intentions.peek_breaches() == []


def test_lookup_failure_defers_instead_of_consuming(tmp_path):
    """F-13: a FAILED lookup (garbage/unreadable db — distinct from a KNOWN
    absent row) must not consume the event. The old fail-open-to-aggregate
    permanently downgraded a 硬约束 bill on a transient sqlite error, while
    the aggregate copy promised 会单独补发. Now the event is deferred
    untouched and the next scan reclassifies it — here, into the per-item
    backfill it always deserved."""
    jd = tmp_path
    db = jd / "data" / "jarvis.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("this is not a sqlite file")
    _emit_skip(jd, "int_bill", "信用卡还款提醒")

    # scan with the broken db: nothing consumed, nothing queued, no crash
    assert sd.queue_digest(jd, force=True) == 0
    assert _breach_lines(jd) == []
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert state["consumed"] == {}

    # db recovers → the SAME event is reclassified as hard and backfilled
    db.unlink()
    _seed_db(jd, [("int_bill", "信用卡还款提醒", BILL_PROMPT, "hard")])
    assert sd.queue_digest(jd, force=True) == 1
    lines = _breach_lines(jd)
    assert len(lines) == 1 and lines[0]["id"].startswith("skipitem_int_bill_")
    assert BILL_PROMPT in lines[0]["prompt"]


def test_partial_lookup_failure_defers_only_failed(tmp_path, monkeypatch):
    """One event's failed lookup defers ONLY that event — the rest of the
    scan (classified events) proceeds and is consumed normally."""
    jd = tmp_path
    _seed_db(jd, [("int_prep", "会议 prep", "喂上下文", "context")])
    _emit_skip(jd, "int_bill", "信用卡还款提醒", hours_ago=5)
    _emit_skip(jd, "int_prep", "会议 prep", hours_ago=4)

    real = sd._intent_row

    def flaky(jd_, iid):
        if iid == "int_bill":
            return sd._LOOKUP_FAILED
        return real(jd_, iid)

    monkeypatch.setattr(sd, "_intent_row", flaky)
    assert sd.queue_digest(jd, force=True) == 1     # only int_prep consumed
    lines = _breach_lines(jd)
    assert len(lines) == 1 and lines[0]["id"].startswith("skipdigest_")
    assert "会议 prep" in lines[0]["prompt"]
    assert "信用卡还款提醒" not in lines[0]["prompt"]   # deferred, not folded
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 1

    # lookup heals (now classifies as hard) → per-item backfill on rescan;
    # no new events needed — the deferred one is simply re-collected.
    monkeypatch.undo()
    con = sqlite3.connect(jd / "data" / "jarvis.db")
    con.execute("INSERT INTO intentions VALUES (?,?,?,?)",
                ("int_bill", "信用卡还款提醒", BILL_PROMPT, "hard"))
    con.commit()
    con.close()
    assert sd.queue_digest(jd, force=True) == 1
    ids = [l["id"] for l in _breach_lines(jd)]
    assert any(i.startswith("skipitem_int_bill_") for i in ids)


def test_dry_run_writes_nothing_and_previews_both_classes(tmp_path, capsys):
    jd = tmp_path
    _seed_db(jd, [("int_bill", "七月信用卡还款", BILL_PROMPT, "hard")])
    _emit_skip(jd, "int_bill", "七月信用卡还款")
    _emit_skip(jd, "int_other", "别的提醒")

    assert sd.queue_digest(jd, force=True, dry_run=True) == 2
    out = capsys.readouterr().out
    assert "would backfill" in out and "would consume" in out
    assert _breach_lines(jd) == []
    assert not (jd / "data" / ".skip_digest_state.json").exists()


# ── F-6: breach-queue writers share one exclusive lock ────────────────────


def test_backfill_append_holds_shared_writer_lock(tmp_path, monkeypatch):
    """The backfill append must run under core.intentions.breach_queue_lock —
    the same lock the bot process's clear_breaches rewrite holds — so an
    append can no longer vanish under a concurrent read→tmp→os.replace."""
    from contextlib import contextmanager
    import core.intentions as intentions

    locked_paths = []
    real_lock = intentions.breach_queue_lock

    @contextmanager
    def spy(path):
        locked_paths.append(Path(path))
        with real_lock(path):
            yield

    monkeypatch.setattr(intentions, "breach_queue_lock", spy)
    jd = tmp_path
    _seed_db(jd, [("int_bill", "七月信用卡还款", BILL_PROMPT, "hard")])
    _emit_skip(jd, "int_bill", "七月信用卡还款")
    _emit_skip(jd, "int_other", "别的提醒")     # aggregate append too

    assert sd.queue_digest(jd, force=True) == 2
    queue = jd / "data" / ".intent_breach_queue.jsonl"
    assert locked_paths == [queue, queue]       # backfill + aggregate appends
    assert len(_breach_lines(jd)) == 2


def test_rewrite_blocks_while_writer_lock_held(tmp_path, monkeypatch):
    """Mutual exclusion is real, cross-thread/fd: while a writer holds the
    sidecar lock, clear_breaches (the rewrite that used to destroy racing
    appends — F-6's ¥66k-class loss shape) blocks instead of rewriting."""
    import threading
    import time as _time
    import core.intentions as intentions

    q = tmp_path / "data" / ".intent_breach_queue.jsonl"
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(
        json.dumps({"id": "keep_me", "notify_attempts": 0}) + "\n"
        + json.dumps({"id": "drop_me", "notify_attempts": 0}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(intentions, "BREACH_QUEUE", q)

    done = threading.Event()

    def rewriter():
        intentions.clear_breaches(["drop_me"])
        done.set()

    with intentions.breach_queue_lock(q):
        t = threading.Thread(target=rewriter)
        t.start()
        _time.sleep(0.3)
        # rewrite must NOT have happened while we hold the lock
        assert not done.is_set()
        assert len(_breach_lines(tmp_path)) == 2
    t.join(timeout=5)
    assert done.is_set()
    ids = [l["id"] for l in _breach_lines(tmp_path)]
    assert ids == ["keep_me"]                   # rewrite landed after release
