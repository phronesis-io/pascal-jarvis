"""`--daemon` mode of core.usage_stats — the daemon LLM-call ledger reader.

Before 2026-08-24 no daemon claude call recorded token usage anywhere: the
CLI only parsed ~/.claude interactive transcripts and missed the entire
heartbeat population. core.heartbeat now emits `llm_usage` scheduler events;
this reader aggregates them per day from sched_events.jsonl and rotations.
"""

import json

from core.usage_stats import _cli, load_daemon_usage


def _write_events(path, events):
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
        encoding="utf-8")


def _usage(ts, **fields):
    return {"ts": ts, "event": "llm_usage", "task": "", "run_id": "abc",
            "provider": "primary", "model": "opus", **fields}


def test_load_daemon_usage_aggregates_per_day_across_rotations(tmp_path):
    _write_events(tmp_path / "sched_events.jsonl.2", [
        _usage("2026-08-20 09:00:00", input_tokens=10, output_tokens=1),
    ])
    _write_events(tmp_path / "sched_events.jsonl.1", [
        _usage("2026-08-21 09:00:00", input_tokens=100, output_tokens=5,
               cache_read_input_tokens=1000,
               cache_creation_input_tokens=50, total_cost_usd=0.25),
        {"ts": "2026-08-21 09:00:01", "event": "task_finish",
         "task": "checkin", "run_id": "abc", "status": "ok"},
    ])
    _write_events(tmp_path / "sched_events.jsonl", [
        _usage("2026-08-21 10:00:00", input_tokens=200, output_tokens=7,
               cache_read_input_tokens=3000, total_cost_usd=0.5),
        "a JSON string, not an event object",  # non-dict line: skipped
    ])
    # One corrupt line must not lose the rest of the ledger.
    with open(tmp_path / "sched_events.jsonl", "a", encoding="utf-8") as f:
        f.write("{corrupt\n")

    rows = load_daemon_usage(tmp_path)

    assert [r["day"] for r in rows] == ["2026-08-20", "2026-08-21"]
    day = rows[1]
    assert day["calls"] == 2
    assert day["in"] == 300
    assert day["out"] == 12
    assert day["cache_read"] == 4000
    assert day["cache_creation"] == 50
    assert day["cost"] == 0.75
    assert rows[0] == {"day": "2026-08-20", "calls": 1, "in": 10, "out": 1,
                       "cache_read": 0, "cache_creation": 0, "cost": 0.0,
                       "has_cost": False}


def test_load_daemon_usage_empty_dir_is_empty_not_an_error(tmp_path):
    assert load_daemon_usage(tmp_path) == []


def test_cli_daemon_mode_prints_table_and_is_read_only(
        tmp_path, monkeypatch, capsys):
    _write_events(tmp_path / "sched_events.jsonl", [
        _usage("2026-08-23 09:00:00", input_tokens=100, output_tokens=5,
               total_cost_usd=0.25),
        _usage("2026-08-24 09:00:00", input_tokens=7, output_tokens=3),
    ])
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    before = sorted(p.name for p in tmp_path.iterdir())

    assert _cli(["--daemon"]) == 0
    out = capsys.readouterr().out
    assert "2026-08-23" in out and "2026-08-24" in out
    assert "cost($)" in out  # at least one row carries cost
    assert sorted(p.name for p in tmp_path.iterdir()) == before

    # Optional day cap: `--daemon 1` keeps only the newest day.
    assert _cli(["--daemon", "1"]) == 0
    out = capsys.readouterr().out
    assert "2026-08-24" in out and "2026-08-23" not in out


def test_cli_daemon_mode_reports_missing_ledger_honestly(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    assert _cli(["--daemon"]) == 0
    assert "no llm_usage events" in capsys.readouterr().out
