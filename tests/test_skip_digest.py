"""REQ-78 (batch 1) — skip digest: silently-skipped occurrences get surfaced.

Pins the four load-bearing properties:
  1. FILTER — only intent_occurrence_skipped (all) and intent_expired with
     reason=expires_at_lapsed are consumed; other expired reasons
     (retries_exhausted already has _queue_breach; storm_class/closure_stale
     are deliberate silence) and out-of-window events are not.
  2. IDEMPOTENCY — first run queues one breach line and records consumed
     keys; a re-run (the watchdog-restart scenario) queues nothing new.
  3. ORDER — consumed state is written BEFORE the breach append, so a crash
     between the two loses the digest instead of duplicating it (宁丢勿重).
  4. CHAIN — the digest entry is a first-class breach: peek_breaches sees it,
     mark_breaches_shown retires it after BREACH_MAX_SHOWS=1.

All paths go through tmp_path; the real data/ files are never touched.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import core.skip_digest as sd
from core.sched_events import emit


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%d %H:%M:%S")


def _emit_skip(jarvis_dir: Path, intent_id: str, name: str,
               hours_ago: float = 1) -> None:
    # emit() stamps ts=now; rewrite the line for backdated events instead.
    emit(jarvis_dir, "intent_occurrence_skipped", task=intent_id,
         missed=_ts(hours_ago), name=name)


def _write_event(jarvis_dir: Path, entry: dict) -> None:
    f = jarvis_dir / "sched_events.jsonl"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _breach_lines(jarvis_dir: Path) -> list[dict]:
    q = jarvis_dir / "data" / ".intent_breach_queue.jsonl"
    if not q.exists():
        return []
    return [json.loads(line) for line in
            q.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_filter_consumes_only_skip_classes(tmp_path):
    jd = tmp_path
    _emit_skip(jd, "int_a", "信用卡还款提醒")
    _write_event(jd, {"ts": _ts(2), "event": "intent_expired", "task": "int_b",
                      "reason": "expires_at_lapsed", "name": "prep 过期"})
    # NOT consumed: other expired reasons + unrelated events
    _write_event(jd, {"ts": _ts(2), "event": "intent_expired", "task": "int_c",
                      "reason": "retries_exhausted", "name": "已有 breach 通道"})
    _write_event(jd, {"ts": _ts(2), "event": "intent_expired", "task": "int_d",
                      "reason": "storm_class", "name": "风暴静默"})
    _write_event(jd, {"ts": _ts(2), "event": "intent_expired", "task": "int_e",
                      "reason": "closure_stale", "name": "闭环过期"})
    _write_event(jd, {"ts": _ts(2), "event": "task_finish", "task": "checkin",
                      "status": "failed"})
    # NOT consumed: outside the 24h window
    _write_event(jd, {"ts": _ts(30), "event": "intent_occurrence_skipped",
                      "task": "int_old", "missed": _ts(30), "name": "太旧"})

    events = sd.collect_unconsumed(jd, state={})
    assert {e["task"] for e in events} == {"int_a", "int_b"}


def test_first_run_queues_digest_and_rerun_is_idempotent(tmp_path):
    jd = tmp_path
    _emit_skip(jd, "int_a", "信用卡还款提醒", hours_ago=3)
    _emit_skip(jd, "int_b", "Tushare token 提醒", hours_ago=2)

    assert sd.queue_digest(jd, force=True) == 2
    lines = _breach_lines(jd)
    assert len(lines) == 1
    entry = lines[0]
    assert entry["id"].startswith("skipdigest_")
    assert entry["name"] == "停摆期间跳过了 2 件事"
    assert "信用卡还款提醒" in entry["prompt"]
    assert "Tushare token 提醒" in entry["prompt"]
    assert "原定" in entry["prompt"]
    assert "不逐条补发" in entry["prompt"]
    assert entry["notify_attempts"] == 0

    # consumed keys persisted
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 2

    # watchdog-restart scenario: same events, second run adds NOTHING
    assert sd.queue_digest(jd, force=True) == 0
    assert len(_breach_lines(jd)) == 1


def test_scan_gate_defers_within_interval(tmp_path):
    jd = tmp_path
    sd._save_state(sd._state_file(jd),
                   {"last_scan": time.time(), "consumed": {}})
    _emit_skip(jd, "int_a", "x")
    assert sd.queue_digest(jd) == 0          # gated (last_scan fresh)
    assert _breach_lines(jd) == []
    assert sd.queue_digest(jd, force=True) == 1   # force bypasses the gate


def test_consumed_written_before_breach_append(tmp_path, monkeypatch):
    """Crash between state write and queue append must lose, not duplicate."""
    jd = tmp_path
    _emit_skip(jd, "int_a", "重要提醒")

    real_open = open

    def exploding_open(path, *args, **kwargs):
        if ".intent_breach_queue" in str(path):
            raise OSError("disk full at the worst moment")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", exploding_open)
    assert sd.queue_digest(jd, force=True) == 0   # fail-open, no raise
    monkeypatch.undo()

    # consumed was already recorded → no breach line now, and none later
    state = json.loads((jd / "data" / ".skip_digest_state.json").read_text())
    assert len(state["consumed"]) == 1
    assert _breach_lines(jd) == []
    assert sd.queue_digest(jd, force=True) == 0
    assert _breach_lines(jd) == []


def test_diag_line_two_states(tmp_path):
    jd = tmp_path
    assert sd.diag_line(jd).startswith("✓")
    _emit_skip(jd, "int_a", "x")
    line = sd.diag_line(jd)
    assert line.startswith("⚠️") and "1 个" in line
    # consuming the events does NOT clear the diag — the stall still happened
    sd.queue_digest(jd, force=True)
    assert sd.diag_line(jd).startswith("⚠️")


def test_digest_entry_rides_full_breach_chain(tmp_path, monkeypatch):
    """peek_breaches surfaces the digest once; mark_breaches_shown retires it."""
    import core.intentions as intentions
    jd = tmp_path
    monkeypatch.setattr(intentions, "BREACH_QUEUE",
                        jd / "data" / ".intent_breach_queue.jsonl")

    _emit_skip(jd, "int_a", "信用卡还款提醒")
    assert sd.queue_digest(jd, force=True) == 1

    breaches = intentions.peek_breaches()
    assert len(breaches) == 1
    b = breaches[0]
    assert b["id"].startswith("skipdigest_")
    assert b["name"] == "停摆期间跳过了 1 件事"
    # the fields intentions_pre.sh renders into the card payload all exist
    for field in ("prompt", "purpose", "trigger_time", "attempt"):
        assert field in b

    # peek is non-mutating — still owed until a card actually renders
    assert len(intentions.peek_breaches()) == 1

    # card rendered → shown once → retired (BREACH_MAX_SHOWS=1, never nags)
    intentions.mark_breaches_shown([b["id"]])
    assert intentions.peek_breaches() == []


def test_queue_digest_never_raises_on_garbage_dir():
    # nonexistent dir, no permissions assumptions — must fail open
    assert sd.queue_digest(Path("/nonexistent/nowhere"), force=True) == 0
    assert sd.diag_line(Path("/nonexistent/nowhere")).startswith("✓") or \
        sd.diag_line(Path("/nonexistent/nowhere")).startswith("⚠️")
