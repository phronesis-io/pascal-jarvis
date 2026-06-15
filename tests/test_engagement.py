"""Tests for core.engagement — heartbeat response tracking."""

import json
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
    double-credit. We can't easily force a true race in a unit test, but we
    pin that the lock path exists and the serial cap still holds."""
    import json, time, inspect
    from core import engagement
    # The source must acquire an exclusive flock around the critical section.
    src = inspect.getsource(engagement.record_response)
    assert "LOCK_EX" in src and "flock" in src
    # And the cap still holds when called serially under the lock.
    log = tmp_path / "engagement_log.jsonl"
    now = int(time.time())
    log.write_text(json.dumps({"type": "sent", "source": "checkin", "epoch": now}) + "\n")
    engagement.record_response(log, "回了")
    engagement.record_response(log, "又说一句")
    rows = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    credited = [r for r in rows if r.get("type") == "response" and r["source"] == "checkin"]
    assert len(credited) == 1
