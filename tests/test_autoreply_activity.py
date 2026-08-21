"""Writer↔reader contract for the EigenFlux auto-reply ledger.

Red team 2026-08-21 (finding 1): the ledger writer stamped ts as
'%Y-%m-%d %H:%M' while the activity-log reader parsed '%Y-%m-%dT%H:%M:%S',
so 100% of rows were silently skipped and every unattended outbound message
was invisible to Pascal. These tests write through the REAL writer
(core.ef_stream_loop._record_auto_reply) and read through the REAL reader the
shell hook runs (core.autoreply_activity), so the two files cannot drift
apart unnoticed again.

Finding 8: the reader keeps a consumed-offset cursor instead of a 45-minute
wall-clock window, so rows written while the hourly gate was closed are
delayed, never lost.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import core.autoreply_activity as ara
import core.ef_stream_loop as efsl

ROOT = Path(__file__).resolve().parent.parent


def _write_row(tmp_path, **overrides):
    efsl._record_auto_reply(
        tmp_path,
        title=overrides.get("title", "Peer 来信"),
        conv_id=overrides.get("conv_id", "conv-1"),
        sender_id=overrides.get("sender_id", "agent-1"),
        incoming=overrides.get("incoming", "question"),
        reply=overrides.get("reply", "answer"),
        note=overrides.get("note", "技术追问，已答"),
        ids=overrides.get("ids", ["msg-1"]),
        msg_id=overrides.get("msg_id", "srv-1"),
    )


# -- Finding 1: the write format and the read format are the same contract --

def test_writer_ts_parses_with_the_reader_format(tmp_path):
    _write_row(tmp_path)
    raw = (tmp_path / ara.LEDGER_RELPATH).read_text(encoding="utf-8").strip()
    row = json.loads(raw)
    # Must not raise: seconds-resolution ISO, exactly what the reader parses.
    datetime.strptime(row["ts"], ara.TS_FORMAT)


def test_row_written_by_stream_loop_appears_in_activity_report(tmp_path):
    _write_row(tmp_path, note="技术追问，已答")
    block = ara.report(tmp_path)
    assert "EIGENFLUX AUTO-REPLIES" in block
    assert "Peer: 技术追问，已答" in block


def test_shell_entrypoint_reports_fresh_row(tmp_path):
    """The exact invocation tasks/activity_log_pre.sh uses."""
    _write_row(tmp_path, note="端到端一行")
    result = subprocess.run(
        [sys.executable, "-m", "core.autoreply_activity"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env={**os.environ, "JARVIS_DIR": str(tmp_path)},
        timeout=30,
    )
    assert result.returncode == 0
    assert "端到端一行" in result.stdout


# -- Finding 8: cursor semantics — delay is allowed, loss is not ------------

def test_report_returns_all_rows_since_cursor_not_a_time_window(tmp_path):
    # Rows far older than any wall-clock window (e.g. written overnight
    # while the hourly gate was closed) must still be reported once.
    ledger = tmp_path / ara.LEDGER_RELPATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as fh:
        for day in ("18", "19", "20"):
            fh.write(json.dumps({
                "ts": f"2026-08-{day}T03:00:00",
                "title": f"Night{day} 来信",
                "conv_id": "conv-n",
                "note": f"夜里第{day}条",
            }, ensure_ascii=False) + "\n")

    block = ara.report(tmp_path)
    for day in ("18", "19", "20"):
        assert f"夜里第{day}条" in block

    # Consumed: the next run reports nothing instead of repeating.
    assert ara.report(tmp_path) == ""

    # A new row after consumption is reported alone.
    _write_row(tmp_path, note="新的一条")
    follow_up = ara.report(tmp_path)
    assert "新的一条" in follow_up
    assert "夜里第18条" not in follow_up


def test_unparseable_ts_row_is_shown_not_dropped(tmp_path):
    ledger = tmp_path / ara.LEDGER_RELPATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({
        "ts": "2026-08-21 09:00",  # legacy minute format
        "title": "Legacy 来信",
        "note": "旧格式也要可见",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    block = ara.report(tmp_path)
    assert "旧格式也要可见" in block


def test_partial_trailing_line_stays_unconsumed(tmp_path):
    ledger = tmp_path / ara.LEDGER_RELPATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    full = json.dumps({"ts": "2026-08-21T09:00:00", "title": "A 来信",
                       "note": "完整行"}, ensure_ascii=False)
    partial = '{"ts": "2026-08-21T10:00:00", "title": "B 来信", "note": "写'
    ledger.write_text(full + "\n" + partial, encoding="utf-8")

    first = ara.report(tmp_path)
    assert "完整行" in first
    assert "写" not in first

    # The writer finishes the row; the next run picks it up whole.
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('了一半"}\n')
    second = ara.report(tmp_path)
    assert "写了一半" in second


def test_truncated_ledger_resets_cursor_instead_of_skipping(tmp_path):
    _write_row(tmp_path, note="第一批")
    assert "第一批" in ara.report(tmp_path)

    # Rotation/cleanup shrinks the file below the stored offset.
    ledger = tmp_path / ara.LEDGER_RELPATH
    ledger.write_text("", encoding="utf-8")
    assert ara.report(tmp_path) == ""

    _write_row(tmp_path, note="轮转后第一条")
    assert "轮转后第一条" in ara.report(tmp_path)


def test_ledger_paths_are_shared_between_writer_and_reader():
    # The loop imports its ledger constants FROM the reader module; a future
    # rename in one place cannot silently strand the other.
    assert efsl.AUTOREPLY_LEDGER == ara.LEDGER_RELPATH
    assert efsl.AUTOREPLY_TS_FORMAT == ara.TS_FORMAT
