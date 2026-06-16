"""Tests for core.heartbeat — parsing, scheduling, pipeline protection."""

import json
import time
from pathlib import Path

import pytest

from core.heartbeat import HeartbeatRunner, parse_heartbeat, parse_interval


def test_parse_interval():
    assert parse_interval("10s") == 10
    assert parse_interval("5m") == 300
    assert parse_interval("2h") == 7200
    assert parse_interval("1d") == 86400
    assert parse_interval("invalid") == 600  # default


def test_parse_heartbeat_basic(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### task-one
- interval: 10m
- pre: tasks/pre.sh
- post: tasks/post.py
- prompt: Do something.

### task-two
- interval: 1h
- prompt: |
    Multiline prompt
    with two lines.
""")
    tasks = parse_heartbeat(hb)
    assert len(tasks) == 2
    assert tasks[0]["name"] == "task-one"
    assert tasks[0]["interval"] == 600
    assert tasks[0]["pre"] == "tasks/pre.sh"
    assert tasks[0]["post"] == "tasks/post.py"
    assert tasks[0]["prompt"] == "Do something."
    assert tasks[1]["interval"] == 3600
    assert "Multiline prompt" in tasks[1]["prompt"]
    assert "with two lines." in tasks[1]["prompt"]


def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    state_file = tmp_path / "state.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    kwargs.setdefault("idle_judge", False)  # never hit the network in unit tests
    return HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=state_file,
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )


def test_state_persistence(tmp_path):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    runner.save_state({"t": {"last_run": 12345}})
    assert runner.load_state() == {"t": {"last_run": 12345}}


def test_no_task_due_returns_empty(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    now = int(time.time())
    runner.save_state({"t": {"last_run": now}})  # just ran
    result = runner.run_cycle(force=False)
    assert result == ""


def test_only_task_filter(tmp_path, monkeypatch):
    """only_task parameter should limit cycle to one named task."""
    runner = _make_runner(tmp_path,
        "### task-a\n- interval: 1h\n- prompt: a\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    called_with = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called_with.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True, only_task="task-a")
    assert len(called_with) == 1
    assert "task-a" in called_with[0]
    assert "task-b" not in called_with[0]


def test_pipeline_protection_one_memory_task_per_cycle(tmp_path, monkeypatch):
    """Only one of the PIPELINE_TASKS may run per cycle to prevent races.
    memory-hourly is a PRIORITY_TASK (Tier 0) so it bypasses Claude.
    memory-daily would go to Claude but is blocked by pipeline protection."""
    hb = """
### memory-hourly
- interval: 1h
- post: tasks/memory_hourly_post.py
- prompt: h

### memory-daily
- interval: 12h
- prompt: d
"""
    runner = _make_runner(tmp_path, hb)
    called_with = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called_with.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)
    # memory-hourly is PRIORITY (exempt from batch cap) but NOT Tier 0
    # → goes through Claude. memory-daily blocked by pipeline_picked.
    assert len(called_with) == 1
    assert "memory-hourly" in called_with[0]
    assert "memory-daily" not in called_with[0]


def test_empty_pre_script_output_skips_task(tmp_path, monkeypatch):
    """If pre-script returns empty, task is skipped with shortened retry."""
    hb = """
### checkin
- interval: 1h
- pre: nonexistent.sh
- prompt: hi
"""
    runner = _make_runner(tmp_path, hb)
    called = []
    monkeypatch.setattr(runner, "claude_call", lambda p: called.append(p) or "x")

    runner.run_cycle(force=True)
    assert called == []  # Claude not called
    state = runner.load_state()
    # Retry delay should be applied (last_run < now but > 0)
    assert "checkin" in state


def test_heartbeat_ok_updates_state(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert "t" in state
    assert state["t"]["last_run"] > 0


def test_work_dir_defaults_to_jarvis_dir(tmp_path):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    assert runner.work_dir == runner.jarvis_dir


def test_work_dir_explicit(tmp_path):
    work = tmp_path / "custom_work"
    work.mkdir()
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n",
                          work_dir=work)
    assert runner.work_dir == work


def test_multi_task_envelope_parsing(tmp_path, monkeypatch):
    """When >1 tasks run, Claude returns a JSON envelope — route responses correctly."""
    hb = """
### task-a
- interval: 1h
- prompt: a

### task-b
- interval: 1h
- prompt: b
"""
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {
            "task-a": "response for a",
            "task-b": "response for b",
        },
        "user_message": "",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)

    result = runner.run_cycle(force=True)
    assert "response for a" in result
    assert "response for b" in result


def test_multi_task_envelope_ignores_braced_trailer(tmp_path, monkeypatch):
    """A valid envelope followed by model notes must not poison the batch."""
    hb = """
### task-a
- interval: 1h
- prompt: a

### task-b
- interval: 1h
- prompt: b
"""
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {
            "task-a": "response for a",
            "task-b": "response for b",
        },
        "user_message": "",
    })
    monkeypatch.setattr(
        runner,
        "claude_call",
        lambda p: f"{envelope}\n\nNote: {{\"debug\": \"not part of payload\"}}",
    )

    result = runner.run_cycle(force=True)
    state = runner.load_state()
    assert "response for a" in result
    assert "response for b" in result
    assert state["task-a"]["last_status"] == "ok"
    assert state["task-b"]["last_status"] == "ok"


def test_idle_sentinel_detection():
    """Standalone HEARTBEAT_OK line = idle; prose mention = not idle."""
    from core.heartbeat import _has_idle_sentinel
    leaked = "**🎯 Intent**\n\nNothing worth notifying Pascal.\n\nHEARTBEAT_OK"
    assert _has_idle_sentinel(leaked)
    assert _has_idle_sentinel("HEARTBEAT_OK")
    assert not _has_idle_sentinel("明天下午多了个会，要不要挪一下？")
    # Token mentioned inside prose (this very topic) must NOT be killed.
    assert not _has_idle_sentinel("当模型输出 HEARTBEAT_OK 时就不该打扰你")


def test_single_task_idle_sentinel_suppressed(tmp_path, monkeypatch):
    """Single-task path: reasoning text ending in HEARTBEAT_OK is dropped, not sent."""
    hb = "### intent-check\n- interval: 1h\n- prompt: check\n"
    runner = _make_runner(tmp_path, hb)
    leaked = "The only due intent is a test. Not worth notifying.\n\nHEARTBEAT_OK"
    monkeypatch.setattr(runner, "claude_call", lambda p: leaked)
    result = runner.run_cycle(force=True)
    assert "test" not in result and "HEARTBEAT_OK" not in result


def test_idle_judge_drops_noise(tmp_path, monkeypatch):
    """Judge ON + NOISE verdict → message dropped."""
    hb = "### t\n- interval: 1h\n- prompt: hi\n"
    runner = _make_runner(tmp_path, hb, idle_judge=True)
    monkeypatch.setattr(runner, "claude_call", lambda p: "现在没什么要紧的")
    monkeypatch.setattr(runner, "_judge_is_idle_noise", lambda m: True)
    assert "现在没什么要紧的" not in runner.run_cycle(force=True)


def test_idle_judge_delivers_on_conservative_verdict(tmp_path, monkeypatch):
    """Judge ON + DELIVER verdict (fail-open default) → message kept."""
    hb = "### t\n- interval: 1h\n- prompt: hi\n"
    runner = _make_runner(tmp_path, hb, idle_judge=True)
    monkeypatch.setattr(runner, "claude_call", lambda p: "明天的会改到三点了")
    monkeypatch.setattr(runner, "_judge_is_idle_noise", lambda m: False)
    assert "明天的会改到三点了" in runner.run_cycle(force=True)


# ── SILENT_TASKS: permanently silent housekeeping (behavioral_rules.md) ──
# daily-plan / self-diagnostic / thinking-review output is log-only — it must
# never be staged for delivery, alone or batched with other tasks. Regression
# for 2026-06-12: a daily-plan card reached the user via the morning digest.


def test_silent_task_output_never_delivered(tmp_path, monkeypatch):
    """Single silent task: cycle returns empty, no source sidecar written."""
    hb = "### daily-plan\n- interval: 24h\n- prompt: plan\n"
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", lambda p: "🌅 今日 plan card")
    assert runner.run_cycle(force=True) == ""
    assert not (runner.jarvis_dir / ".heartbeat_last_source").exists()


def test_all_silent_tasks_suppressed(tmp_path, monkeypatch):
    """Every name in SILENT_TASKS is suppressed on the single-task path."""
    for name in sorted(HeartbeatRunner.SILENT_TASKS):
        sub = tmp_path / name
        sub.mkdir()
        runner = _make_runner(sub, f"### {name}\n- interval: 1h\n- prompt: p\n")
        monkeypatch.setattr(runner, "claude_call", lambda p: "output of a task")
        assert runner.run_cycle(force=True) == ""


def test_silent_task_filtered_from_mixed_batch(tmp_path, monkeypatch):
    """Multi-task cycle: silent task's slice dropped, normal task delivered."""
    hb = (
        "### daily-plan\n- interval: 24h\n- prompt: plan\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {"daily-plan": "🌅 今日 plan card", "task-b": "response for b"},
        "user_message": "",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)
    assert "response for b" in result
    assert "plan card" not in result
    # sidecar credits only the delivering task → engagement/queue stay clean
    source = (runner.jarvis_dir / ".heartbeat_last_source").read_text()
    assert source == "task-b"


def test_all_silent_batch_drops_summary_too(tmp_path, monkeypatch):
    """When every task in the call is silent, the envelope's top-level
    user_message can only describe silent content — nothing is delivered."""
    hb = (
        "### daily-plan\n- interval: 24h\n- prompt: plan\n\n"
        "### thinking-review\n- interval: 7d\n- prompt: review\n"
    )
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {"daily-plan": "plan text", "thinking-review": "review text"},
        "user_message": "今天的计划和思考回顾如下…",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    assert runner.run_cycle(force=True) == ""
    assert not (runner.jarvis_dir / ".heartbeat_last_source").exists()


def test_mixed_batch_summary_never_leaks_silent_content(tmp_path, monkeypatch):
    """user_message is a COMBINED summary across all tasks in the call, so in
    a mixed batch it can carry silent-task content even though the per-task
    slice was dropped (the 6/12 leak, one hop later). With ANY silent task in
    the batch the summary must be dropped; non-silent tasks still deliver
    through their own slices."""
    hb = (
        "### daily-plan\n- interval: 24h\n- prompt: plan\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {"daily-plan": "🌅 今日 plan card", "task-b": "response for b"},
        "user_message": "今日计划：14:30 周会；另外 task-b 的结果如下。",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)
    assert "response for b" in result          # non-silent slice delivered
    assert "周会" not in result                 # summary (with plan) dropped
    assert "今日计划" not in result
    assert (runner.jarvis_dir / ".heartbeat_last_source").read_text() == "task-b"


def test_silent_task_full_output_archived(tmp_path, monkeypatch):
    """Suppressed output is preserved in FULL (silent_outputs.jsonl) — the
    80-char jarvis.log prefix alone would destroy thinking-review /
    self-diagnostic products, which have no post-script log of their own."""
    hb = "### self-diagnostic\n- interval: 4h\n- prompt: diag\n"
    runner = _make_runner(tmp_path, hb)
    long_report = "⚠️ 系统体检发现问题：" + "watermark STARVED 详情 " * 20
    monkeypatch.setattr(runner, "claude_call", lambda p: long_report)
    assert runner.run_cycle(force=True) == ""  # still never delivered
    archive = runner.jarvis_dir / "silent_outputs.jsonl"
    rows = [json.loads(l) for l in archive.read_text().splitlines()]
    assert rows[-1]["task"] == "self-diagnostic"
    assert rows[-1]["text"] == long_report  # full text, not a prefix


# ===========================================================================
# v2: ACK-required tasks + envelope parse failure semantics (REQ-30/36)
# ===========================================================================

def _ack_runner(tmp_path):
    """Runner with an ACK-required task whose post records its stdin."""
    hb = """
### intention-check
- interval: 1m
- pre: tasks/pre.sh
- post: tasks/post.py
- prompt: process intents

### other-task
- interval: 1h
- prompt: other
"""
    runner = _make_runner(tmp_path, hb)
    (runner.jarvis_dir / "tasks").mkdir(parents=True, exist_ok=True)
    pre = runner.jarvis_dir / "tasks" / "pre.sh"
    pre.write_text("#!/bin/bash\necho '{\"count\":1}'\n")
    pre.chmod(0o755)
    stdin_log = runner.jarvis_dir / "post_stdin.log"
    post = runner.jarvis_dir / "tasks" / "post.py"
    post.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(stdin_log)!r}).open('a').write(sys.stdin.read() + '\\n')\n"
    )
    return runner, stdin_log


def test_heartbeat_ok_still_acks_intention_check(tmp_path, monkeypatch):
    """REQ-30: a bare HEARTBEAT_OK reply must STILL invoke the ACK task's post
    with __NO_ENVELOPE__ — this exact reply (which the old prompt instructed)
    was the #1 silent intent killer (50% of fired one-shots died)."""
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    assert "__NO_ENVELOPE__" in stdin_log.read_text()


def test_empty_response_acks_intention_check(tmp_path, monkeypatch):
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "")
    runner.run_cycle(force=True)
    assert "__NO_ENVELOPE__" in stdin_log.read_text()


def test_killed_response_acks_intention_check(tmp_path, monkeypatch):
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "__KILLED__")
    runner.run_cycle(force=True)
    assert "__NO_ENVELOPE__" in stdin_log.read_text()


def test_parse_failure_acks_and_records_failure(tmp_path, monkeypatch):
    """REQ-36: an unparseable multi-task envelope is a FAILURE — circuit
    breaker sees it, last_run gets a short retry (≤5min), status
    parse_failed — never record_success over destroyed output."""
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "{this is not json")
    runner.run_cycle(force=True)
    # ACK post still ran
    assert "__NO_ENVELOPE__" in stdin_log.read_text()
    state = runner.load_state()
    # Failure recorded for the batch (consecutive_failures bumped)
    assert state["other-task"]["circuit"]["consecutive_failures"] == 1
    # Fast retry: last_run rewound so the task re-fires within ~5 minutes
    import time as _t
    other_interval = 3600
    eta = state["other-task"]["last_run"] + other_interval - int(_t.time())
    assert eta <= 300


def test_missing_slice_acks_intention_check(tmp_path, monkeypatch):
    """REQ-30: envelope parses but omits the ACK task's slice → post still
    invoked with __NO_ENVELOPE__ so the manifest reconciles."""
    runner, stdin_log = _ack_runner(tmp_path)
    envelope = json.dumps({"tasks": {"other-task": "all good"}, "user_message": ""})
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    runner.run_cycle(force=True)
    assert "__NO_ENVELOPE__" in stdin_log.read_text()


def test_ack_task_prompt_forbids_bare_heartbeat_ok(tmp_path, monkeypatch):
    """REQ-30d: when an ACK task is in the batch, the wrapper prompt must not
    invite the bare-HEARTBEAT_OK reply that breaks the state machine."""
    runner, _ = _ack_runner(tmp_path)
    captured = {}
    def _capture(p):
        captured["prompt"] = p
        return "HEARTBEAT_OK"
    monkeypatch.setattr(runner, "claude_call", _capture)
    runner.run_cycle(force=True)
    p = captured["prompt"]
    assert "NEVER reply with a bare HEARTBEAT_OK" in p
    assert "reply with exactly: HEARTBEAT_OK" not in p


def test_degenerate_ack_slice_routes_to_no_envelope(tmp_path, monkeypatch):
    """Red-team fix: an envelope present but with a degenerate intention-check
    slice (empty string) must route to the ACK __NO_ENVELOPE__ reconcile path,
    not fall through and double-process."""
    runner, stdin_log = _ack_runner(tmp_path)
    envelope = json.dumps({"tasks": {"intention-check": "  ", "other-task": "ok"},
                           "user_message": ""})
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    runner.run_cycle(force=True)
    contents = stdin_log.read_text()
    # ACK post invoked exactly once with the no-envelope sentinel
    assert contents.count("__NO_ENVELOPE__") == 1


def test_circuit_trip_does_not_message_user(tmp_path, monkeypatch):
    """REQ-62: a tripped circuit is an OPS event (log + sched_event), never a
    chat message. Pascal got raw '连续失败已自动暂停…冷却后自动恢复' verbatim."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    # Force the task's circuit to be one failure from tripping, then fail.
    from core.task_protocol import TaskState
    st = runner.load_state()
    ts = TaskState()
    ts.circuit.consecutive_failures = ts.circuit.FAILURE_THRESHOLD - 1
    st["t"] = ts.to_dict()
    runner.save_state(st)
    monkeypatch.setattr(runner, "claude_call", lambda p: "")   # empty → failure
    out = runner.run_cycle(force=True)
    assert "连续失败" not in out and "自动暂停" not in out
    assert out == ""                                            # nothing to chat
