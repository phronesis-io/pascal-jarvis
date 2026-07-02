"""Tests for tasks/journal_capture.py — REQ-86 shadow attribution (log-only).

The direct (non-quote) message path must ONLY append attribution decisions to
data/journal_capture_shadow.jsonl and never touch the journal. Window logic:
within N hours of the daily-reflect card → would_capture=true; outside the
window or with no card on record → false. Malformed input never raises.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "journal_capture.py"


def _setup(tmp_path, card_age_seconds=None):
    """Create a tmp JARVIS_DIR; optionally with a daily-reflect sent row."""
    jd = tmp_path / "jarvis"
    jd.mkdir(exist_ok=True)
    if card_age_seconds is not None:
        epoch = int(time.time() - card_age_seconds)
        row = {"ts": time.strftime("%Y-%m-%d %H:%M"), "source": "daily-reflect",
               "type": "sent", "epoch": epoch, "message_ids": ["om_reflect1"]}
        with open(jd / "engagement_log.jsonl", "a", encoding="utf-8") as f:
            # Noise rows around the reflect card, like the real log has.
            f.write(json.dumps({"ts": row["ts"], "source": "checkin",
                                "type": "sent", "epoch": epoch - 60}) + "\n")
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.write(json.dumps({"ts": row["ts"], "source": "conversation",
                                "type": "response"}) + "\n")
    return jd


def _run(jd, reply="我觉得今天节奏不错", parent="", chat_type="p2p",
         msg_type="text", sender="ou_pascal", user_id="ou_pascal", extra=None):
    env = {
        **os.environ,
        "JARVIS_DIR": str(jd),
        "JV_PARENT": parent,
        "JV_REPLY": reply,
        "JV_CHAT_TYPE": chat_type,
        "JV_MSG_TYPE": msg_type,
        "JV_SENDER": sender,
        "JV_USER_ID": user_id,
        **(extra or {}),
    }
    return subprocess.run([sys.executable, str(SCRIPT)], env=env,
                          capture_output=True, text=True)


def _shadow_rows(jd):
    path = jd / "data" / "journal_capture_shadow.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_direct_reply_within_window_would_capture(tmp_path):
    jd = _setup(tmp_path, card_age_seconds=2 * 3600)  # 2h ago, window 4h
    proc = _run(jd)
    assert proc.returncode == 0, proc.stderr
    rows = _shadow_rows(jd)
    assert len(rows) == 1
    assert rows[0]["would_capture"] is True
    assert rows[0]["msg"] == "我觉得今天节奏不错"
    assert 115 <= rows[0]["card_age_min"] <= 125
    assert "daily-reflect" in rows[0]["reason"]


def test_direct_reply_outside_window_would_not_capture(tmp_path):
    jd = _setup(tmp_path, card_age_seconds=6 * 3600)  # 6h ago > 4h window
    proc = _run(jd)
    assert proc.returncode == 0, proc.stderr
    rows = _shadow_rows(jd)
    assert len(rows) == 1
    assert rows[0]["would_capture"] is False
    assert "outside" in rows[0]["reason"]


def test_no_card_on_record_would_not_capture(tmp_path):
    jd = _setup(tmp_path)  # no engagement_log at all
    proc = _run(jd)
    assert proc.returncode == 0, proc.stderr
    rows = _shadow_rows(jd)
    assert len(rows) == 1
    assert rows[0]["would_capture"] is False
    assert rows[0]["card_age_min"] is None


def test_window_is_configurable(tmp_path):
    jd = _setup(tmp_path, card_age_seconds=2 * 3600)
    proc = _run(jd, extra={"JV_JOURNAL_SHADOW_WINDOW_H": "1"})
    assert proc.returncode == 0, proc.stderr
    rows = _shadow_rows(jd)
    assert rows[0]["would_capture"] is False  # 2h old > 1h window


def test_non_candidates_are_not_logged(tmp_path):
    jd = _setup(tmp_path, card_age_seconds=600)
    assert _run(jd, chat_type="group").returncode == 0       # group chat
    assert _run(jd, msg_type="image").returncode == 0        # non-text
    assert _run(jd, sender="ou_other").returncode == 0       # not Pascal
    assert _shadow_rows(jd) == []


def test_quote_reply_path_does_not_shadow_log(tmp_path):
    # A quote reply to a NON-reflect message: neither journaled nor shadow-logged.
    jd = _setup(tmp_path, card_age_seconds=600)
    proc = _run(jd, parent="om_some_other_card")
    assert proc.returncode == 0, proc.stderr
    assert _shadow_rows(jd) == []
    # parent="null" (jq artifact) is treated as a direct message.
    proc = _run(jd, parent="null")
    assert proc.returncode == 0, proc.stderr
    assert len(_shadow_rows(jd)) == 1


def test_never_raises_on_corrupt_log_and_bad_env(tmp_path):
    jd = tmp_path / "jarvis"
    jd.mkdir()
    (jd / "engagement_log.jsonl").write_text(
        "not json at all\n{\"half\": \n" + json.dumps(
            {"ts": "2026-07-01 09:00", "source": "daily-reflect",
             "type": "sent", "epoch": "not-a-number"}) + "\n")
    proc = _run(jd, extra={"JV_JOURNAL_SHADOW_WINDOW_H": "garbage"})
    assert proc.returncode == 0, proc.stderr
    rows = _shadow_rows(jd)
    assert len(rows) == 1
    assert rows[0]["would_capture"] is False


def test_empty_reply_is_a_silent_noop(tmp_path):
    jd = _setup(tmp_path, card_age_seconds=600)
    proc = _run(jd, reply="")
    assert proc.returncode == 0, proc.stderr
    assert _shadow_rows(jd) == []
