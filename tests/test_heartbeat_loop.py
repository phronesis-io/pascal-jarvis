"""Tests for core.heartbeat_loop — the main cycle logic (now in Python)."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

from core.heartbeat_loop import (
    _route_output, _write_outbox, _record_engagement, _trim_file,
)


def test_trim_file(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text("\n".join(f"line{i}" for i in range(100)) + "\n")
    _trim_file(f, 10)
    assert len(f.read_text().strip().splitlines()) == 10
    assert "line99" in f.read_text()  # keeps last lines


def test_trim_file_no_op(tmp_path):
    f = tmp_path / "test.jsonl"
    f.write_text("line1\nline2\n")
    _trim_file(f, 10)
    assert f.read_text() == "line1\nline2\n"


def test_trim_file_missing(tmp_path):
    _trim_file(tmp_path / "missing.jsonl", 10)  # should not crash


def test_write_outbox(tmp_path):
    _write_outbox("Hello world", tmp_path)
    outbox = tmp_path / "heartbeat_outbox.jsonl"
    assert outbox.exists()
    entry = json.loads(outbox.read_text().strip())
    assert entry["role"] == "assistant"
    assert "Hello world" in entry["text"]


def test_record_engagement_with_source(tmp_path):
    # Write source sidecar file
    (tmp_path / ".heartbeat_last_source").write_text("checkin,content-recommend")
    _record_engagement(tmp_path)

    elog = tmp_path / "engagement_log.jsonl"
    entries = [json.loads(l) for l in elog.read_text().splitlines() if l.strip()]
    assert len(entries) == 2
    sources = {e["source"] for e in entries}
    assert sources == {"checkin", "content-recommend"}
    assert all(e["type"] == "sent" for e in entries)


def test_record_engagement_no_source(tmp_path):
    _record_engagement(tmp_path)
    elog = tmp_path / "engagement_log.jsonl"
    entries = [json.loads(l) for l in elog.read_text().splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["source"] == "heartbeat"


def test_route_output_plain_text():
    """Plain text should be sent via lark_send_text."""
    with patch("core.heartbeat_loop._lark_send_text") as mock_send:
        mock_send.return_value = True
        _route_output("Hello from heartbeat", "user123", Path("/tmp"))
        mock_send.assert_called_once()
        assert "Hello from heartbeat" in mock_send.call_args[0][0]


def test_route_output_card():
    """CARD: prefixed lines should be sent via lark_send_card."""
    card = json.dumps({"config": {}, "header": {"title": {"content": "Test"}}})
    with patch("core.heartbeat_loop._lark_send_card") as mock_card:
        mock_card.return_value = True
        _route_output(f"CARD:{card}", "user123", Path("/tmp"))
        mock_card.assert_called_once()


def test_route_output_blocks_raw_json():
    """Raw JSON that isn't a card should be blocked."""
    with patch("core.heartbeat_loop._lark_send_text") as mock_send:
        _route_output('{"internal": "data"}', "user123", Path("/tmp"))
        mock_send.assert_not_called()


def test_route_output_mixed():
    """Mixed card + text should split correctly."""
    card = json.dumps({"config": {}, "header": {"title": {"content": "T"}}})
    output = f"CARD:{card}\nSome plain text"
    with patch("core.heartbeat_loop._lark_send_card") as mock_card, \
         patch("core.heartbeat_loop._lark_send_text") as mock_text:
        mock_card.return_value = True
        mock_text.return_value = True
        _route_output(output, "user123", Path("/tmp"))
        mock_card.assert_called_once()
        mock_text.assert_called_once()
        assert "plain text" in mock_text.call_args[0][0]
