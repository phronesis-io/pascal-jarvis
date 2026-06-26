"""Tests for core.engagement — heartbeat response tracking."""

import json
import os
import subprocess
import time
from pathlib import Path

from core.engagement import record_response


def test_record_response_with_recent_sent(tmp_path):
    log = tmp_path / "engagement.jsonl"
    sent = {"ts": "2026-01-01 12:00", "source": "checkin", "type": "sent",
            "epoch": int(time.time()) - 300}  # 5 min ago
    log.write_text(json.dumps(sent) + "\n")

    result = record_response(log, "user replied")
    assert result is True

    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(entries) == 2
    assert entries[1]["type"] == "response"
    assert entries[1]["reaction"] == "engaged"  # <10min
    assert entries[1]["source"] == "checkin"


def test_record_response_late_reply(tmp_path):
    log = tmp_path / "engagement.jsonl"
    sent = {"ts": "2026-01-01 12:00", "source": "content-recommend", "type": "sent",
            "epoch": int(time.time()) - 1200}  # 20 min ago
    log.write_text(json.dumps(sent) + "\n")

    record_response(log, "finally watched it")
    entries = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert entries[1]["reaction"] == "late_reply"


def test_response_inherits_prompt_experiment_metadata(tmp_path):
    log = tmp_path / "engagement.jsonl"
    sent = {
        "ts": "2026-01-01 12:00",
        "source": "checkin",
        "type": "sent",
        "epoch": int(time.time()) - 60,
        "prompt_experiment": "checkin-choice-v1",
        "prompt_variant": "choice_first",
    }
    log.write_text(json.dumps(sent) + "\n")

    record_response(log, "好的")

    response = [json.loads(l) for l in log.read_text().splitlines()][1]
    assert response["source"] == "checkin"
    assert response["prompt_experiment"] == "checkin-choice-v1"
    assert response["prompt_variant"] == "choice_first"


def test_record_response_skips_old_sent(tmp_path):
    log = tmp_path / "engagement.jsonl"
    sent = {"ts": "2026-01-01 12:00", "source": "checkin", "type": "sent",
            "epoch": int(time.time()) - 7200}  # 2 hours ago
    log.write_text(json.dumps(sent) + "\n")

    result = record_response(log, "reply")
    assert result is False  # >1h gap, should not record


def test_record_response_no_log_file(tmp_path):
    result = record_response(tmp_path / "missing.jsonl", "reply")
    assert result is False


def test_record_response_no_sent_entries(tmp_path):
    log = tmp_path / "engagement.jsonl"
    log.write_text(json.dumps({"type": "response"}) + "\n")
    result = record_response(log, "reply")
    assert result is False


# ── REQ-63: attribution caps one response per sent ───────────────────────

def test_first_reply_credits_source_followons_are_conversation(tmp_path):
    """The 107% bug: a multi-message conversation after one card billed every
    message to that source. Now only the first reply credits it."""
    import json, time
    from core.engagement import record_response
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    log.write_text(json.dumps({"type": "sent", "source": "calendar-sync", "epoch": now}) + "\n")

    # First reply → credited to calendar-sync
    record_response(log, "好的改一下")
    # Two follow-on free-form messages → conversation, NOT calendar-sync
    record_response(log, "凯瑞老师那边怎么说")
    record_response(log, "我背还有点痛")

    rows = [json.loads(l) for l in log.read_text().splitlines()]
    responses = [r for r in rows if r["type"] == "response"]
    by_source = {}
    for r in responses:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    assert by_source.get("calendar-sync", 0) == 1   # exactly one, not 3 (no 107%)
    assert by_source.get("conversation", 0) == 2


def test_quote_reply_always_attributes_to_card(tmp_path):
    """A quote-reply is an explicit, attributable reply — it credits the source
    even if it's not the first message after the send."""
    import json, time
    from core.engagement import record_response
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    log.write_text(json.dumps({"type": "sent", "source": "intention-check", "epoch": now}) + "\n")
    record_response(log, "随便说点别的")                    # first → credits source
    record_response(log, "[Replying to: <card>闭环问题</card>] 做了")  # quote → still credits
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    intent_responses = [r for r in rows if r.get("type") == "response" and r["source"] == "intention-check"]
    assert len(intent_responses) == 2  # the free-form first + the quote-reply both count for the card


def test_record_response_is_flock_serialized(tmp_path):
    """Red-team P1-B: the read-scan-append must hold an exclusive flock so two
    concurrent recorders can't both read responded_already=False and
    double-credit. Verify by holding an exclusive lock and confirming that
    record_response blocks (returns only after the lock is released)."""
    import json, time, fcntl, threading
    from core import engagement
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    log.write_text(json.dumps({"type": "sent", "source": "checkin", "epoch": now}) + "\n")

    lock_acquired_by_thread = threading.Event()
    lock_released = threading.Event()

    def hold_lock():
        fh = open(log, "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        lock_acquired_by_thread.set()
        lock_released.wait(timeout=5)
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_acquired_by_thread.wait(timeout=5)

    result_box = [None]

    def try_record():
        result_box[0] = engagement.record_response(log, "回了")

    recorder = threading.Thread(target=try_record)
    recorder.start()
    recorder.join(timeout=0.5)
    assert recorder.is_alive(), "record_response should block while another lock is held"

    lock_released.set()
    recorder.join(timeout=5)
    t.join(timeout=5)
    assert result_box[0] is True

    # Serial cap still holds
    engagement.record_response(log, "又说一句")
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    credited = [r for r in rows if r.get("type") == "response" and r["source"] == "checkin"]
    assert len(credited) == 1


def test_engagement_analyze_pre_reports_prompt_experiment_breakdown(tmp_path):
    script = Path(__file__).resolve().parent.parent / "tasks" / "engagement_analyze_pre.sh"
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    rows = []
    for i in range(10):
        variant = "choice_first" if i < 5 else "plain"
        rows.append({
            "ts": "2026-06-18 12:00",
            "source": "checkin",
            "type": "sent",
            "epoch": now - 600 + i,
            "prompt_experiment": "checkin-choice-v1",
            "prompt_variant": variant,
        })
        rows.append({
            "ts": "2026-06-18 12:01",
            "source": "checkin",
            "type": "response",
            "reaction": "engaged" if i < 7 else "ignored",
            "gap_seconds": 60,
            "prompt_experiment": "checkin-choice-v1",
            "prompt_variant": variant,
        })
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    env = {**os.environ, "JARVIS_DIR": str(tmp_path), "ENGAGEMENT_LOG": str(log)}
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "=== PROMPT EXPERIMENT BREAKDOWN ===" in result.stdout
    assert "checkin-choice-v1/choice_first/checkin" in result.stdout
    assert "checkin-choice-v1/plain/checkin" in result.stdout


def test_engagement_analyze_pre_counts_late_reply_in_source_rate(tmp_path):
    script = Path(__file__).resolve().parent.parent / "tasks" / "engagement_analyze_pre.sh"
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    rows = []
    reactions = ["engaged", "late_reply", "ignored"] * 4
    for i, reaction in enumerate(reactions):
        rows.append({
            "ts": "2026-06-18 12:00",
            "source": "checkin",
            "type": "sent",
            "epoch": now - 1200 + i,
        })
        rows.append({
            "ts": "2026-06-18 12:10",
            "source": "checkin",
            "type": "response",
            "reaction": reaction,
            "gap_seconds": 1200 if reaction == "late_reply" else 60,
        })
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    env = {**os.environ, "JARVIS_DIR": str(tmp_path), "ENGAGEMENT_LOG": str(log)}
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "checkin: sent=12, engaged=4, late_reply=4, ignored=4" in result.stdout
    assert "weighted_rate=50%" in result.stdout
