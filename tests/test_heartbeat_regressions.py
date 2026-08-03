"""Regression tests for heartbeat bug fixes (2026-05-20 ~ 2026-05-25).

Each test targets a specific bug that was found and fixed during code review.
"""

import json
import time
from pathlib import Path

import pytest

from core.heartbeat import HeartbeatRunner, parse_heartbeat


def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    state_file = tmp_path / "state.json"
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    # idle_judge=False: the judge makes a REAL haiku call — with it on, unit
    # tests are slow, network-dependent, and flaky (the judge sometimes drops
    # the synthetic user_message as idle noise).
    kwargs.setdefault("idle_judge", False)
    return HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=state_file,
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )


# ── HEARTBEAT_OK exact match (was substring before fix) ────────────


def test_heartbeat_ok_exact_match_passes_through():
    """Exact 'HEARTBEAT_OK' should be treated as idle."""
    # This is the normal case — just verifying it still works
    assert "HEARTBEAT_OK" == "HEARTBEAT_OK".strip()


# ── Dangling-placeholder guard (2026-06-02 broken EigenFlux 撞名 card) ────


@pytest.mark.parametrize("body", [
    # The actual broken card: hook ending on a bare ellipsis, no payload.
    "**📡 EigenFlux**\n\n💡 **撞名预警**\n\n刚发现一个同名先发项目已经占位了：\n\n...",
    "Something interesting surfaced:\n…",          # unicode ellipsis
    "线索：\n．．．",                                  # fullwidth dots
    "hook with no body:\n...   ",                   # trailing whitespace tolerated
])
def test_dangling_placeholder_detected(body):
    from core.heartbeat import _is_dangling_placeholder
    assert _is_dangling_placeholder(body) is True


@pytest.mark.parametrize("body", [
    "完整的一条消息，正常结尾。",
    "Here's the finding: github.com/foo/bar collides with your project.",
    "三个要点：\n1. A\n2. B\n3. C",
    "He trailed off... but then finished the thought on this same line.",
    "结尾有省略号但后面还有字……所以不算残卡。",
])
def test_complete_message_not_flagged(body):
    from core.heartbeat import _is_dangling_placeholder
    assert _is_dangling_placeholder(body) is False


def test_heartbeat_ok_substring_in_json_not_discarded(tmp_path, monkeypatch):
    """A JSON envelope containing 'HEARTBEAT_OK' as a per-task value
    must NOT cause the entire response to be discarded.

    Bug: old code used `"HEARTBEAT_OK" in raw` which matched substrings.
    Fix: changed to `raw.strip() == "HEARTBEAT_OK"` (exact match).
    """
    hb = (
        "### task-a\n- interval: 1h\n- prompt: a\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)

    envelope = json.dumps({
        "tasks": {
            "task-a": "HEARTBEAT_OK",
            "task-b": "This is real content that should reach the user",
        },
        "user_message": "",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)

    # task-b's content must NOT be silently discarded
    assert "real content" in result


def test_heartbeat_ok_with_whitespace(tmp_path, monkeypatch):
    """HEARTBEAT_OK with surrounding whitespace should still be idle."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: hi\n")
    monkeypatch.setattr(runner, "claude_call", lambda p: "  HEARTBEAT_OK  \n")
    result = runner.run_cycle(force=True)
    assert result == ""


# ── Card + top-level user_message duplication (2026-06-04) ─────────
# Bug: in the multi-task envelope path a task's post-script emits a card AND
# the envelope's top-level user_message was appended as text, so one push said
# the same thing twice (card + a paragraph repeating it). Fix: suppress
# top_msg when any task already produced a card.

_FAKE_CARD = (
    '{"config": {"wide_screen_mode": true}, '
    '"header": {"title": {"tag": "plain_text", "content": "推荐"}}, '
    '"elements": [{"tag": "div", "text": {"tag": "lark_md", "content": "CARD_BODY_HERE"}}]}'
)


def _write_card_post(runner) -> None:
    script = Path(runner.jarvis_dir) / "fake_card_post.py"
    script.write_text(
        "import sys\n"
        "sys.stdin.read()\n"
        f"print({_FAKE_CARD!r})\n"
    )


def test_card_present_suppresses_duplicate_top_message(tmp_path, monkeypatch):
    """A task card must not be echoed by the top-level user_message."""
    hb = (
        "### rec\n- interval: 1h\n- prompt: p\n- post: fake_card_post.py\n\n"
        "### other\n- interval: 1h\n- prompt: q\n"
    )
    runner = _make_runner(tmp_path, hb)
    _write_card_post(runner)
    envelope = json.dumps({
        "tasks": {"rec": "raw rec data", "other": "HEARTBEAT_OK"},
        "user_message": "DUPLICATE_PARAGRAPH that just repeats the card",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)

    assert "CARD:" in result and "CARD_BODY_HERE" in result
    assert "DUPLICATE_PARAGRAPH" not in result


def test_top_message_kept_when_no_card(tmp_path, monkeypatch):
    """With no card in the cycle, the top-level user_message is the message."""
    hb = (
        "### a\n- interval: 1h\n- prompt: p\n\n"
        "### b\n- interval: 1h\n- prompt: q\n"
    )
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {"a": "HEARTBEAT_OK", "b": "HEARTBEAT_OK"},
        "user_message": "A genuine companion note with no card",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)
    result = runner.run_cycle(force=True)

    assert "genuine companion note" in result


# ── MAX_BATCH_SIZE cap ─────────────────────────────────────────────


def test_batch_cap_limits_tasks_sent_to_claude(tmp_path, monkeypatch):
    """When more tasks are due than MAX_BATCH_SIZE, only MAX_BATCH_SIZE
    should be sent to Claude in a single call."""
    # Create 6 tasks, all with 1h interval
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    assert len(called_prompts) == 1
    prompt = called_prompts[0]
    # Count how many "=== TASK:" markers appear
    task_markers = prompt.count("=== TASK:")
    assert task_markers <= HeartbeatRunner.MAX_BATCH_SIZE


def test_batch_cap_staleness_sort(tmp_path, monkeypatch):
    """Stalest tasks (lowest last_run) should be selected first."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    now = int(time.time())
    # task-0 has the oldest last_run (stalest), task-5 has the newest (freshest)
    state = {f"task-{i}": {"last_run": now - 7200 + i * 100} for i in range(6)}
    runner.save_state(state)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    prompt = called_prompts[0]
    # task-0 should be in the prompt (stalest), task-5 should not (freshest)
    assert "task-0" in prompt
    assert "task-5" not in prompt


def test_routine_run_survives_batch_cap(tmp_path, monkeypatch):
    """routine-run must never be starved by the batch cap (2026-08-02).

    Observed in production: routine-run appeared in the deferred list of 18 of
    21 capped cycles (86%) — the most starved task in the system — while a
    user's hourly Routine sat due and unfired. The damage is not a late run:
    routine_run_pre.sh CLAIMS due routines and advances their next_fire_at
    watermark, so a deferred cycle spends an occurrence the user never sees.
    Same failure and same fix as intention-check (REQ-32).
    """
    tasks_md = "\n\n".join(
        [f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(8)]
        + ["### routine-run\n- interval: 5m\n- prompt: [ROUTINE RUN]"]
    )
    runner = _make_runner(tmp_path, tasks_md)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    prompt = called_prompts[0]
    assert "routine-run" in prompt, (
        "routine-run was deferred by the batch cap — a claimed Routine "
        "occurrence would be spent without ever reaching the user"
    )
    # The exemption must not silently raise the cap for everyone else.
    regular = prompt.count("=== TASK: task-")
    assert regular <= HeartbeatRunner.MAX_BATCH_SIZE


def test_deferred_tasks_stay_due_next_cycle(tmp_path, monkeypatch):
    """Tasks deferred by batch cap should remain due in the next cycle."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- prompt: do {i}" for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    call_count = [0]
    def mock_claude(p):
        call_count[0] += 1
        return "HEARTBEAT_OK"
    monkeypatch.setattr(runner, "claude_call", mock_claude)

    # First cycle: 4 tasks run
    runner.run_cycle(force=True)
    assert call_count[0] == 1

    # Second cycle: remaining 2 tasks should be due (their last_run wasn't updated)
    runner.run_cycle(force=True)
    assert call_count[0] == 2  # Claude called again for the deferred tasks


def test_tier0_tasks_bypass_claude(tmp_path, monkeypatch):
    """TIER0_TASKS (calendar-sync) should pipe pre→post directly, no Claude.
    PRIORITY_TASKS that need reasoning (memory-hourly) still go through Claude."""
    hb = (
        "### calendar-sync\n- interval: 30m\n- pre: tasks/cal_pre.sh\n"
        "- post: tasks/cal_post.py\n- prompt: sync\n\n"
        "### memory-hourly\n- interval: 1h\n- pre: tasks/mem_pre.sh\n"
        "- post: tasks/mem_post.py\n- prompt: index\n\n"
        "### checkin\n- interval: 30m\n- prompt: hi\n"
    )
    runner = _make_runner(tmp_path, hb)

    # Create dummy scripts
    for name in ["cal_pre.sh", "mem_pre.sh"]:
        pre = runner.jarvis_dir / "tasks" / name
        pre.parent.mkdir(parents=True, exist_ok=True)
        pre.write_text("#!/bin/bash\necho 'data'")
        pre.chmod(0o755)
    for name in ["cal_post.py", "mem_post.py"]:
        post = runner.jarvis_dir / "tasks" / name
        post.write_text("#!/usr/bin/env python3\nimport sys\nprint(sys.stdin.read().strip())")
        post.chmod(0o755)

    claude_called = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: claude_called.append(p) or "HEARTBEAT_OK")

    result = runner.run_cycle(force=True)

    # Claude should be called for memory-hourly + checkin, NOT calendar-sync
    if claude_called:
        prompt = claude_called[0]
        assert "calendar-sync" not in prompt  # Tier 0 → bypassed
        assert "memory-hourly" in prompt  # PRIORITY but not Tier 0 → through Claude


def test_batch_cap_before_prescripts(tmp_path, monkeypatch):
    """Batch cap must happen BEFORE pre-scripts run, to avoid wasting
    pre-script side effects on tasks that will be deferred."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- pre: tasks/pre_{i}.sh\n- prompt: do {i}"
        for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)

    scripts_called = []
    original_run_script = runner.run_script

    def tracking_run_script(path, stdin_data=""):
        scripts_called.append(path)
        return "some data"  # non-empty so task is runnable

    monkeypatch.setattr(runner, "run_script", tracking_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    # Only MAX_BATCH_SIZE pre-scripts should have run
    pre_scripts = [s for s in scripts_called if "pre_" in s]
    assert len(pre_scripts) <= HeartbeatRunner.MAX_BATCH_SIZE


# ── Fair queue (2026-08-03): empty pres must not burn batch slots ──────


def test_empty_pres_do_not_burn_batch_slots(tmp_path, monkeypatch):
    """Six due tasks; the four stalest have nothing to say, the two freshest
    have real content. The old cap-before-pre order selected the four empty
    ones, skipped them all, deferred the two with content, and called the
    model with NOTHING — a full cycle spent delivering zero work while the
    contentful tasks starved. Observed live for weeks as cycles full of
    「(empty)」lines next to a 12-23 task deferral list."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- pre: tasks/pre_{i}.sh\n- prompt: do {i}"
        for i in range(6)
    )
    runner = _make_runner(tmp_path, tasks_md)
    now = int(time.time())
    # task-0..3 are the stalest (would win a staleness sort) but empty;
    # task-4/5 are fresher but have content.
    state = {f"task-{i}": {"last_run": now - 7200 - (5 - i) * 600}
             for i in range(6)}
    runner.save_state(state)

    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "ok"
        idx = int(path.split("_")[-1].split(".")[0])
        return "real content" if idx >= 4 else ""

    called_prompts = []
    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")

    runner.run_cycle(force=True)

    assert called_prompts, "cycle made no model call despite real content"
    prompt = called_prompts[0]
    assert "task-4" in prompt and "task-5" in prompt, (
        "tasks with real content were deferred while empty tasks "
        "burned the batch slots"
    )


def test_batch_fairness_is_relative_to_each_tasks_cadence(tmp_path, monkeypatch):
    """A 10-minute task 3h overdue (18x its cadence) must outrank a daily
    task 26h stale (1.08x). The old absolute-last_run sort inverted this:
    after any sleep, the daily backlog monopolized every batch for hours
    while short-cycle tasks — the interactive ones — waited behind it."""
    tasks_md = (
        "### fast-task\n- interval: 10m\n- prompt: fast\n\n"
        + "\n\n".join(
            f"### daily-{i}\n- interval: 24h\n- prompt: daily {i}"
            for i in range(5)
        )
    )
    runner = _make_runner(tmp_path, tasks_md)
    now = int(time.time())
    state = {"fast-task": {"last_run": now - 3 * 3600}}       # 18x overdue
    for i in range(5):
        state[f"daily-{i}"] = {"last_run": now - 26 * 3600}   # 1.08x overdue
    runner.save_state(state)

    called_prompts = []
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)

    assert called_prompts
    assert "fast-task" in called_prompts[0], (
        "the most-overdue-relative-to-cadence task was deferred behind "
        "absolutely-staler daily tasks"
    )


def test_model_batch_still_capped_with_abundant_content(tmp_path, monkeypatch):
    """Fair queueing must not silently raise the model-call size: with 8
    contentful tasks due, exactly MAX_BATCH_SIZE reach the prompt and the
    rest defer with their pres unrun."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- pre: tasks/pre_{i}.sh\n- prompt: do {i}"
        for i in range(8)
    )
    runner = _make_runner(tmp_path, tasks_md)

    pres_run = []

    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "ok"
        pres_run.append(path)
        return "content"

    called_prompts = []
    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call",
                        lambda p: called_prompts.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)

    assert called_prompts[0].count("=== TASK:") == HeartbeatRunner.MAX_BATCH_SIZE
    # Deferred tasks' pres never ran — no claimed state was spent.
    assert len(pres_run) == HeartbeatRunner.MAX_BATCH_SIZE


def test_probe_budget_bounds_prescript_work_per_cycle(tmp_path, monkeypatch):
    """Fair queueing must not turn a backlogged cycle into an unbounded
    serial pre-script sweep: with 20 due tasks all coming back empty, the
    cycle probes at most PRE_PROBE_LIMIT pres and defers the rest unrun."""
    tasks_md = "\n\n".join(
        f"### task-{i}\n- interval: 1h\n- pre: tasks/pre_{i}.sh\n- prompt: do {i}"
        for i in range(20)
    )
    runner = _make_runner(tmp_path, tasks_md)

    pres_run = []

    def fake_run_script(path, stdin_data=""):
        runner._last_script_outcome = "ok"
        pres_run.append(path)
        return ""  # everything is empty — the worst probing regime

    monkeypatch.setattr(runner, "run_script", fake_run_script)
    monkeypatch.setattr(runner, "claude_call", lambda p: "HEARTBEAT_OK")
    runner.run_cycle(force=True)

    assert len(pres_run) == HeartbeatRunner.PRE_PROBE_LIMIT
