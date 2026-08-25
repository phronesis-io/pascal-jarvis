"""perception-collect Tier-0 conversion (2026-08-24).

The old prompt — "HEARTBEAT_OK unless errors>0 with the same source failing
repeatedly" — was a deterministic check answered by a solo full-memory model
call every 15 minutes (~43% of all heartbeat LLM traffic, 98% bare
HEARTBEAT_OK). tasks/perception_collect_post.py now replays that contract in
code from core.perception's own per-source error_count streaks.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tasks.perception_collect_post as pcp
from core.heartbeat import HeartbeatRunner, parse_heartbeat

ROOT = Path(__file__).resolve().parent.parent
TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 24, 10, 0, tzinfo=TZ)

SUMMARY_OK = "collected=3 deduped=1 errors=0"
SUMMARY_ERR = "collected=0 deduped=0 errors=1 notes: src_a: http_error"


def _write_state(tmp_path, state: dict) -> None:
    (tmp_path / "perception_state.json").write_text(
        json.dumps(state), encoding="utf-8")


def test_errors_zero_stays_silent_even_with_a_long_streak(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 9,
                                      "error_type": "http_error"}})
    assert pcp.run(SUMMARY_OK, jarvis_dir=tmp_path, now=NOW) == ""


def test_single_failure_is_not_repeated_failure(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 1,
                                      "error_type": "http_error"}})
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) == ""


def test_repeated_failure_yields_one_plain_notice_card(tmp_path):
    _write_state(tmp_path, {
        "src_a": {"error_count": 4, "error_type": "http_error"},
        "src_b": {"error_count": 0, "error_type": None},
    })
    out = pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW)

    lines = out.splitlines()
    assert lines[0].startswith("TITLE: ")
    assert len(lines[0][len("TITLE: "):]) <= 40
    # WORKED receipt: the delivery layer's require_work_receipt gate drops
    # receipt-less prose, so the card must carry one honest line.
    assert lines[1].startswith("WORKED: ")
    assert "src_a" in out and "src_b" not in out
    assert "知道就行" in out
    # 纯周知 card contract: no OPTIONS, ≤3 body lines, no SRE jargon leaks.
    assert "OPTIONS" not in out
    assert len(lines) <= 5  # TITLE + WORKED + ≤3 body lines
    assert "error_type" not in out and "http" not in out
    assert "python3" not in out


def test_known_source_uses_plain_display_name_in_every_visible_line(tmp_path):
    _write_state(tmp_path, {
        "pgc_pulse": {"error_count": 4, "error_type": "timeout"},
    })

    out = pcp.run(
        "collected=0 deduped=0 errors=1 notes: pgc_pulse: timeout",
        jarvis_dir=tmp_path,
        now=NOW,
    )

    assert "PGC 指标日报" in out
    assert "pgc_pulse" not in out


def test_same_source_is_suppressed_for_24h_then_realerts(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 4,
                                      "error_type": "http_error"}})
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) != ""
    # Still failing 15 minutes later — silent, not a 15-minute buzzer.
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path,
                   now=NOW + timedelta(minutes=15)) == ""
    # Past the 24h window and still failing — one more card.
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path,
                   now=NOW + timedelta(hours=25)) != ""


def test_a_different_source_failing_later_alerts_again(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 4,
                                      "error_type": "http_error"}})
    assert "src_a" in pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW)

    _write_state(tmp_path, {
        "src_a": {"error_count": 5, "error_type": "http_error"},
        "src_b": {"error_count": 3, "error_type": "timeout"},
    })
    later = NOW + timedelta(hours=1)
    out = pcp.run("collected=0 deduped=0 errors=2 "
                  "notes: src_a: http_error; src_b: timeout",
                  jarvis_dir=tmp_path, now=later)
    assert "src_b" in out
    assert "src_a" not in out  # still inside its own 24h window


def test_frozen_streak_of_unnamed_source_never_rides_anothers_error(tmp_path):
    """Adversarial-review repro (2026-08-24): a source disabled in
    sources.yaml keeps its error_count forever (run_collect never prunes
    state). An unrelated single error must NOT card the dead source with a
    false 我在自动重试 claim — only sources named in THIS pass's notes
    qualify (the old prompt's "notes mention it" clause)."""
    _write_state(tmp_path, {"mail_163": {"error_count": 5,
                                         "error_type": "http_error"}})
    out = pcp.run("collected=2 deduped=0 errors=1 "
                  "notes: pgc_pulse: collect crashed: timeout",
                  jarvis_dir=tmp_path, now=NOW)
    assert out == ""
    # Named while genuinely failing this pass → the same streak does card.
    out = pcp.run("collected=0 deduped=0 errors=1 notes: mail_163: http_error",
                  jarvis_dir=tmp_path, now=NOW + timedelta(minutes=15))
    assert "mail_163" in out


def test_bookkeeping_for_sources_gone_from_state_is_pruned(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 4,
                                      "error_type": "http_error"}})
    pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW)
    _write_state(tmp_path, {"src_b": {"error_count": 0, "error_type": None}})
    pcp.run(SUMMARY_OK, jarvis_dir=tmp_path, now=NOW + timedelta(hours=1))
    saved = json.loads(
        (tmp_path / "data" / "perception_alert_state.json").read_text())
    assert saved == {"failing_since": {}, "last_alert": {}}


def test_card_names_when_the_streak_started(tmp_path):
    # First failing pass records the streak start; the card 2h later says so.
    _write_state(tmp_path, {"src_a": {"error_count": 1,
                                      "error_type": "http_error"}})
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) == ""
    _write_state(tmp_path, {"src_a": {"error_count": 8,
                                      "error_type": "http_error"}})
    out = pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path,
                  now=NOW + timedelta(hours=2))
    assert "从 10:00 起" in out


def test_missing_or_malformed_state_never_crashes(tmp_path):
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) == ""
    (tmp_path / "perception_state.json").write_text("{not json",
                                                    encoding="utf-8")
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) == ""
    _write_state(tmp_path, {"src_a": "corrupt", "src_b": {"error_count": "9"}})
    assert pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW) == ""


def test_alert_bookkeeping_lives_in_data_dir(tmp_path):
    _write_state(tmp_path, {"src_a": {"error_count": 4,
                                      "error_type": "http_error"}})
    pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW)
    assert (tmp_path / "data" / "perception_alert_state.json").exists()


# ── Delivery-layer gate: the card must survive memorialize_output ─────────


def test_card_passes_the_work_receipt_gate_end_to_end(tmp_path, monkeypatch):
    """Regression (adversarial review 2026-08-24, BLOCKER): heartbeat_loop
    memorializes perception-collect output with require_work_receipt=True and
    memorial.flush_prose drops any card without a WORKED line BEFORE create()
    — the first version of this post produced cards that could never reach
    Pascal. Pipe the post's REAL output through the real gate."""
    import core.memorial as memorial

    _write_state(tmp_path, {"src_a": {"error_count": 4,
                                      "error_type": "http_error"}})
    out = pcp.run(SUMMARY_ERR, jarvis_dir=tmp_path, now=NOW)
    assert out

    # Isolated memorial JARVIS_DIR + mocked send channels (test_memorial's
    # env-fixture contract — nothing real is sent, nothing touches prod).
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_test")
    monkeypatch.setattr(memorial, "_send_text", lambda *a, **k: "om_test")
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)

    rendered = memorial.memorialize_output(
        out, "perception-collect", require_work_receipt=True)

    assert rendered.strip(), "card was dropped by the work-receipt gate"
    states = memorial.list_memorials()
    assert len(states) == 1
    assert states[0]["work_receipt"]  # receipt adopted, not leaked into body
    assert "WORKED" not in states[0]["body"]
    assert "知道就行" in states[0]["body"]


# ── Tier-0 wiring: no model call, post output becomes the cycle's card ────


def test_production_wiring_declares_tier0_with_the_deterministic_post():
    assert "perception-collect" in HeartbeatRunner.TIER0_TASKS
    tasks = {t["name"]: t for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
    entry = tasks["perception-collect"]
    assert entry["pre"] == "tasks/perception_collect_pre.sh"
    assert entry["post"] == "tasks/perception_collect_post.py"


def test_run_cycle_routes_perception_collect_through_tier0(
        tmp_path, monkeypatch):
    """End to end through the scheduler: pre summary → deterministic post →
    staged card, with the model bypassed entirely."""
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(
        "### perception-collect\n- interval: 15m\n"
        "- pre: tasks/perception_collect_pre.sh\n"
        "- post: tasks/perception_collect_post.py\n"
        "- prompt: deterministic\n")
    jarvis_dir = tmp_path / "jarvis"
    (jarvis_dir / "tasks").mkdir(parents=True)
    (tmp_path / "memory").mkdir()

    pre = jarvis_dir / "tasks" / "perception_collect_pre.sh"
    pre.write_text("#!/bin/bash\n"
                   "echo 'collected=0 deduped=0 errors=1 notes: src_a: x'\n")
    pre.chmod(0o755)
    # Shim to the real post so this exercises the shipped logic; the real
    # file's own sys.path bootstrap makes core.* importable.
    (jarvis_dir / "tasks" / "perception_collect_post.py").write_text(
        "import runpy\n"
        f"runpy.run_path({str(ROOT / 'tasks' / 'perception_collect_post.py')!r},"
        " run_name='__main__')\n")
    (jarvis_dir / "perception_state.json").write_text(json.dumps(
        {"src_a": {"error_count": 4, "error_type": "http_error"}}))
    monkeypatch.setenv("JARVIS_DIR", str(jarvis_dir))

    runner = HeartbeatRunner(
        jarvis_dir=jarvis_dir, heartbeat_file=hb,
        state_file=tmp_path / "state.json", memory_dir=tmp_path / "memory",
        model="sonnet", idle_judge=False)
    monkeypatch.setattr(
        runner, "claude_call",
        lambda *a, **k: pytest.fail("Tier 0 must never call the model"))

    result = runner.run_cycle(force=True)

    assert "感知源「src_a」" in result
    assert "知道就行" in result
    assert (jarvis_dir / "data" / "perception_alert_state.json").exists()
    events = [json.loads(line) for line in
              (jarvis_dir / "sched_events.jsonl").read_text().splitlines()]
    finish = [e for e in events if e["event"] == "task_finish"
              and e["task"] == "perception-collect"]
    assert finish and finish[0]["tier"] == 0 and finish[0]["status"] == "ok"
