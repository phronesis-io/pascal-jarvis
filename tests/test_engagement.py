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
