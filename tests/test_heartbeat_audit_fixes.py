"""Regression tests for the 2026-07-08 audit fixes (cluster B-heartbeat).

F6  — weekly-review joins EMPTY_RETRY_DELAYS (Sunday-window pre-gate + full
      7d re-arm made it structurally unable to fire for 26 days).
F5  — the embedded-JSON guard must never deliver a '```json' fence husk
      (the entire first card personal-site ever sent Pascal), and must keep
      real prose on BOTH sides of a blocked payload — with the kept residue
      re-screened once so a trailing raw JSON stub can't leak either
      (red-team follow-up on the both-sides recovery).
F16 — benign CLI notice lines (the connectors banner) must not mask the real
      error in _last_call_error / sched_events' error field.
F3  — BOTH overflow signatures ('Autocompact is thrashing', 'Prompt is too
      long') are deterministic per-call context overflows: named explicitly
      in the error surface, exempt from the shared-call streak (backoff can
      never heal them), one WARN per incident.
F18 — every retry-backdate site must resolve the SAME effective interval as
      the due-check (sidecar override -> state -> base), clamped so last_run
      never lands in the future.
F31 — the shared-call backoff hold announces once per window (re-armed on
      escalation), stays silent on per-tick repeats, and reports its lapse.

All fixtures live in tmp_path; the structured logger is stubbed so no test
ever writes to the live logs (the 072cf2f lesson).
"""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

import core.heartbeat as hb_mod
from core.heartbeat import (HeartbeatRunner, _drop_benign_notices,
                            _error_excerpt, _fence_residue)

_CONNECTORS = ("⚠ claude.ai connectors are disabled because "
               "ANTHROPIC_API_KEY or another auth source is set.")
_THRASH = ("Autocompact is thrashing: the context refilled to the limit "
           "within 3 turns of the previous compact, 3 times in a row")


def _make_runner(tmp_path, heartbeat_content: str, **kwargs) -> HeartbeatRunner:
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text(heartbeat_content)
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    kwargs.setdefault("idle_judge", False)  # never hit the network in unit tests
    runner = HeartbeatRunner(
        jarvis_dir=jarvis_dir,
        heartbeat_file=hb,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="sonnet",
        **kwargs,
    )
    return runner


@pytest.fixture()
def log_lines(monkeypatch):
    """Stub the structured logger; capture (level, msg) for assertions."""
    lines = []

    def _fake(component, msg, level="info", file=None, **kwargs):
        lines.append((level, msg))

    monkeypatch.setattr(hb_mod, "_structured_log", _fake)
    return lines


def _sched_events(runner):
    f = runner.jarvis_dir / "sched_events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def _write_empty_pre(runner, name="pre_empty.sh"):
    script = runner.jarvis_dir / name
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return name


def _set_overrides(runner, overrides: dict):
    (runner.jarvis_dir / "interval_overrides.json").write_text(
        json.dumps(overrides))


# ── F6 — weekly-review empty-pre retry ────────────────────────────────────


def test_weekly_review_gets_30min_empty_pre_retry():
    """The Sunday 10-12 pre gate needs sub-interval retries; the 7d default
    re-armed every attempt onto the same Saturday due-point, forever."""
    assert HeartbeatRunner.EMPTY_RETRY_DELAYS["weekly-review"] == 1800


def test_tier0_operations_have_bounded_failure_retries():
    assert HeartbeatRunner.EMPTY_RETRY_DELAYS["delegation-reconcile"] == 120
    assert HeartbeatRunner.EMPTY_RETRY_DELAYS["iteration-observe"] == 1800
    assert HeartbeatRunner.EMPTY_RETRY_DELAYS["log-maintenance"] == 900
    assert HeartbeatRunner.EMPTY_RETRY_DELAYS["provider-canary"] == 1800


# ── F5 — fence-husk delivery guard ────────────────────────────────────────


def test_fence_residue_kills_husk_keeps_prose():
    assert _fence_residue("```json") == ""
    assert _fence_residue("```\n") == ""
    assert _fence_residue(": \n```JSON") == ""
    assert _fence_residue("Here is my idea:\n```json") == "Here is my idea:"
    assert _fence_residue("附：明天再看\n```") == "附：明天再看"


_SUGGESTION_JSON = json.dumps({
    "suggestion": "Add the five EigenFlux research blog posts as a "
                  "Publications section on the personal site",
    "reason": "they are already public and searchable",
})


def test_fenced_whole_json_message_blocked_without_husk(tmp_path, monkeypatch,
                                                        log_lines):
    """The 7/8 personal-site shape: the entire reply is a fenced JSON
    envelope. Nothing may reach the user — before the fix the '```json'
    opener was delivered as the whole card."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = f"```json\n{_SUGGESTION_JSON}\n```"
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == ""
    assert any("Blocked raw JSON" in msg for _, msg in log_lines)


def test_indented_card_json_example_is_not_executed(tmp_path, monkeypatch,
                                                     log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = "    " + json.dumps({
        "config": {}, "elements": [{"actions": [{
            "text": {"content": "执行"},
            "value": {"action": "dangerous-example"},
        }]}],
    }, ensure_ascii=False)
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    assert runner.run_cycle(force=True) == ""


def test_indented_card_envelope_keeps_its_code_indentation(
        tmp_path, monkeypatch, log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = "    CARD:" + json.dumps({
        "config": {}, "elements": [{"actions": [{
            "text": {"content": "执行"},
            "value": {"action": "dangerous-example"},
        }]}],
    }, ensure_ascii=False)
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    assert runner.run_cycle(force=True).startswith("    CARD:")


def test_embedded_json_husk_residue_suppressed(tmp_path, monkeypatch, log_lines):
    """>50% branch: when the non-JSON residue is only fence markers and
    punctuation, delivery is suppressed and the suppression is logged."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = f": \n```json\n{_SUGGESTION_JSON}"  # junk prefix defeats whole-msg net
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == ""
    assert any("Blocked embedded JSON" in msg for _, msg in log_lines)
    assert any("delivery suppressed" in msg for _, msg in log_lines)


def test_embedded_json_keeps_leading_prose_drops_fence(tmp_path, monkeypatch,
                                                       log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = f"Here is my idea:\n```json\n{_SUGGESTION_JSON}\n```"
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == "Here is my idea:"
    assert "```" not in out


def test_embedded_json_keeps_trailing_prose(tmp_path, monkeypatch, log_lines):
    """Trailing prose used to be silently eaten by the prefix-only slice."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = f"```json\n{_SUGGESTION_JSON}\n```\n记得看一下这个建议"
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == "记得看一下这个建议"


def test_trailing_json_stub_residue_suppressed(tmp_path, monkeypatch,
                                               log_lines):
    """Red-team follow-up: [big fenced payload][small trailing JSON stub] is
    a routine model stutter. The both-sides recovery kept the stub as the
    residue and delivered raw JSON — the old prefix-only slice never could.
    The residue must be re-screened and the delivery suppressed."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = f'```json\n{_SUGGESTION_JSON}\n```\n{{"status": "ok"}}'
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == ""
    assert any("Blocked embedded JSON" in msg for _, msg in log_lines)
    assert any("delivery suppressed" in msg for _, msg in log_lines)


def test_trailing_json_stub_keeps_surrounding_prose(tmp_path, monkeypatch,
                                                    log_lines):
    """When real prose rides beside the trailing stub, the stub dies and the
    prose is still delivered."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = (f'```json\n{_SUGGESTION_JSON}\n```\n记得看一下\n'
           '{"status": "ok"}')
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == "记得看一下"
    assert "{" not in out


def test_non_json_code_fence_delivered_verbatim(tmp_path, monkeypatch,
                                                log_lines):
    """A legit code snippet keeps its fences — the stripped form is only a
    probe, delivery always uses the original message."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    raw = "试试这个：\n```python\nprint(1)\n```"
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: raw)
    out = runner.run_cycle(force=True)
    assert out == raw


def test_unfenced_raw_json_still_blocked(tmp_path, monkeypatch, log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: _SUGGESTION_JSON)
    assert runner.run_cycle(force=True) == ""


# ── F16 — benign notice filtering at err_text construction ───────────────


def test_drop_benign_notices_unit():
    assert _drop_benign_notices("") == ""
    text = f"{_CONNECTORS}\n{_THRASH}"
    assert _drop_benign_notices(text) == _THRASH
    # case-insensitive on the notice signature
    assert _drop_benign_notices(
        "Connectors Are Disabled today\nreal error") == "real error"
    # a failure whose entire output IS the notice stays diagnosable
    assert _drop_benign_notices(_CONNECTORS) == _CONNECTORS


def _fake_cli(monkeypatch, stderr="", stdout="", returncode=1, raise_exc=None):
    """Replace the claude subprocess with a canned result (or exception)."""
    def fake_run(cmd, **kwargs):
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(returncode=returncode, stdout=stdout,
                               stderr=stderr)

    monkeypatch.setattr(hb_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(hb_mod, "load_tiered_memory", lambda d, **kw: "")
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "false")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "false")


def test_claude_call_error_surfaces_substantive_line(tmp_path, monkeypatch,
                                                     log_lines):
    """The connectors banner masked all 40 of 7/8's failures as one identical
    benign string; the first line must now be the real cause."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _fake_cli(monkeypatch, stderr=f"{_CONNECTORS}\n{_THRASH}")
    assert runner.claude_call("hi") == ""
    assert "connectors" not in runner._last_call_error
    assert _error_excerpt(runner._last_call_error).startswith(
        "Autocompact is thrashing")


def test_claude_call_banner_only_error_not_emptied(tmp_path, monkeypatch,
                                                   log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _fake_cli(monkeypatch, stderr=_CONNECTORS)
    assert runner.claude_call("hi") == ""
    assert runner._last_call_error == _CONNECTORS


def test_non_cli_sentinel_errors_pass_through_unfiltered(tmp_path, monkeypatch,
                                                         log_lines):
    """Timeout / CLI-missing sentinels are not CLI output — they must reach
    the error field intact (filtering lives at err_text construction only)."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _fake_cli(monkeypatch,
              raise_exc=subprocess.TimeoutExpired(cmd="claude", timeout=300))
    assert runner.claude_call("hi") == ""
    assert runner._call_timed_out is True
    assert runner._last_call_error == "claude call timed out (300s)"
    assert runner._call_context_overflow is False

    # The timeout is a real-request health observation and deliberately cools
    # primary for the next call. This test's second half is an independent
    # sentinel scenario, so reopen the route instead of accidentally testing
    # the cooldown short-circuit.
    monkeypatch.setattr(
        "core.heartbeat_model.provider_health_rows", lambda _root: [],
    )
    _fake_cli(monkeypatch, raise_exc=FileNotFoundError("no claude"))
    assert runner.claude_call("hi") == ""
    assert runner._last_call_error == "claude CLI not found"


# ── F3 — context overflow is deterministic, never trips shared backoff ───


def test_claude_call_flags_context_overflow(tmp_path, monkeypatch, log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _fake_cli(monkeypatch, stderr=f"{_CONNECTORS}\n{_THRASH}")
    runner.claude_call("hi")
    assert runner._call_context_overflow is True

    # The second live signature of the same class (7/8 22:48-23:05
    # memory-tidy batches): the API rejects the payload outright.
    _fake_cli(monkeypatch, stderr=f"{_CONNECTORS}\nPrompt is too long")
    runner.claude_call("hi")
    assert runner._call_context_overflow is True

    _fake_cli(monkeypatch, stderr="API Error: Unable to connect")
    runner.claude_call("hi")
    assert runner._call_context_overflow is False


def _overflow_call(runner):
    """A claude_call stub mimicking a thrash-killed shared call."""
    def _call(prompt, timeout=None):
        runner._call_timed_out = False
        runner._call_context_overflow = True
        runner._last_call_error = _THRASH
        return ""
    return _call


def test_overflow_failures_never_trip_shared_backoff(tmp_path, monkeypatch,
                                                     log_lines):
    """Hourly thrash tripped the roster-wide 300s hold twice on 7/8 —
    repeated overflow cycles must fail the batch (fast retry, diagnosable
    error, one WARN naming the tasks) without ever feeding the streak."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    monkeypatch.setattr(runner, "claude_call", _overflow_call(runner))
    for _ in range(4):
        runner.run_cycle(force=True)

    state = runner.load_state()
    assert state["t"]["last_status"] == "failed"
    assert "__shared_call__" not in state
    events = _sched_events(runner)
    assert not [e for e in events if e["event"] == "shared_call_backoff"]
    fails = [e for e in events
             if e["event"] == "task_finish" and e.get("status") == "failed"]
    assert len(fails) == 4
    assert all(e["error"].startswith("Autocompact is thrashing") for e in fails)
    warns = [msg for lvl, msg in log_lines
             if lvl == "warn" and "Context overflow" in msg]
    assert warns and all("'t'" in w for w in warns)


def test_overflow_failure_leaves_existing_streak_untouched(tmp_path,
                                                           monkeypatch,
                                                           log_lines):
    """Neither +1 (would wedge the roster) nor a reset (an overflow proves
    nothing about channel health)."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    runner.save_state({"__shared_call__": {"consecutive_failures": 2,
                                           "last_failure": 123}})
    monkeypatch.setattr(runner, "claude_call", _overflow_call(runner))
    runner.run_cycle(force=True)
    assert runner.load_state()["__shared_call__"] == {
        "consecutive_failures": 2, "last_failure": 123}


def test_prompt_too_long_batch_never_trips_shared_backoff(tmp_path,
                                                          monkeypatch,
                                                          log_lines):
    """End-to-end replay of the 7/8 22:48-23:05 live wedge: the CLI dies
    with 'Prompt is too long' (behind the connectors banner), which must be
    classified as a deterministic overflow — failed status, diagnosable
    error, and NO shared-call streak/backoff."""
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _fake_cli(monkeypatch, stderr=f"{_CONNECTORS}\nPrompt is too long")
    for _ in range(4):
        runner.run_cycle(force=True)

    state = runner.load_state()
    assert state["t"]["last_status"] == "failed"
    assert "__shared_call__" not in state
    events = _sched_events(runner)
    assert not [e for e in events if e["event"] == "shared_call_backoff"]
    fails = [e for e in events
             if e["event"] == "task_finish" and e.get("status") == "failed"]
    assert len(fails) == 4
    assert all(e["error"] == "Prompt is too long" for e in fails)
    assert any(lvl == "warn" and "Context overflow" in msg
               for lvl, msg in log_lines)


def test_overflow_on_heavy_solo_logs_named_warn(tmp_path, monkeypatch,
                                                log_lines):
    runner = _make_runner(
        tmp_path,
        "### deep-research\n- interval: 1h\n- heavy: true\n- prompt: r\n")
    monkeypatch.setattr(runner, "claude_call", _overflow_call(runner))
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert state["deep-research"]["last_status"] == "failed"
    # per-task breaker accounting for heavy solo failures is unchanged
    assert state["deep-research"]["circuit"]["consecutive_failures"] == 1
    assert "__shared_call__" not in state
    assert any(lvl == "warn" and "deep-research" in msg
               and "Context overflow" in msg for lvl, msg in log_lines)


# ── F18 — backdating uses the due-check's effective interval ─────────────


def test_empty_pre_backdate_honors_reduce_override(tmp_path, monkeypatch,
                                                   log_lines):
    """Live 7/8 state: checkin base 1800, override 7200 — the designed 300s
    re-probe actually ran at 5700s because the backdate used the base."""
    runner = _make_runner(
        tmp_path, "### checkin\n- interval: 30m\n- pre: pre_empty.sh\n- prompt: x\n")
    _write_empty_pre(runner)
    _set_overrides(runner, {"checkin": 7200})
    now = int(time.time())
    runner.run_cycle(force=True)
    last_run = runner.load_state()["checkin"]["last_run"]
    # due again in ~300s under the 7200s effective interval
    assert abs((now - 6900) - last_run) <= 10


def test_empty_pre_backdate_clamped_under_increase_override(tmp_path,
                                                            monkeypatch,
                                                            log_lines):
    """An increase override (effective < base) used to leave the backdated
    age >= effective, making the task due again on every 10s tick."""
    runner = _make_runner(
        tmp_path, "### checkin\n- interval: 30m\n- pre: pre_empty.sh\n- prompt: x\n")
    _write_empty_pre(runner)
    _set_overrides(runner, {"checkin": 450})
    now = int(time.time())
    runner.run_cycle(force=True)
    last_run = runner.load_state()["checkin"]["last_run"]
    assert last_run <= now + 1                 # never in the future
    assert now - last_run < 450                # NOT immediately due again


def test_empty_pre_unlisted_task_defaults_to_full_effective_wait(tmp_path,
                                                                 monkeypatch,
                                                                 log_lines):
    """Tasks outside EMPTY_RETRY_DELAYS keep 'no fast retry' semantics under
    an override: last_run = now, due after one full effective interval."""
    runner = _make_runner(
        tmp_path, "### mytask\n- interval: 30m\n- pre: pre_empty.sh\n- prompt: x\n")
    _write_empty_pre(runner)
    _set_overrides(runner, {"mytask": 7200})
    now = int(time.time())
    runner.run_cycle(force=True)
    assert abs(runner.load_state()["mytask"]["last_run"] - now) <= 10


def test_shared_call_failure_backdate_honors_override(tmp_path, monkeypatch,
                                                      log_lines):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    _set_overrides(runner, {"t": 7200})

    def failing(prompt, timeout=None):
        runner._call_timed_out = False
        runner._call_context_overflow = False
        runner._last_call_error = "API Error: boom"
        return ""

    monkeypatch.setattr(runner, "claude_call", failing)
    now = int(time.time())
    runner.run_cycle(force=True)
    last_run = runner.load_state()["t"]["last_run"]
    assert abs((now - 6900) - last_run) <= 10  # due again in ~300s, not ~3900s


def test_parse_failed_backdate_honors_override(tmp_path, monkeypatch,
                                               log_lines):
    hb = ("### task-a\n- interval: 1h\n- prompt: a\n\n"
          "### task-b\n- interval: 1h\n- prompt: b\n")
    runner = _make_runner(tmp_path, hb)
    _set_overrides(runner, {"task-a": 7200})
    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "utterly not json {{{")
    now = int(time.time())
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert abs((now - 6900) - state["task-a"]["last_run"]) <= 10
    assert abs((now - 3300) - state["task-b"]["last_run"]) <= 10


def test_heavy_solo_failure_backdate_honors_override(tmp_path, monkeypatch,
                                                     log_lines):
    runner = _make_runner(
        tmp_path,
        "### deep-research\n- interval: 1h\n- heavy: true\n- prompt: r\n")
    _set_overrides(runner, {"deep-research": 7200})

    def failing(prompt, timeout=None):
        runner._call_timed_out = False
        runner._call_context_overflow = False
        runner._last_call_error = "API Error: boom"
        return ""

    monkeypatch.setattr(runner, "claude_call", failing)
    now = int(time.time())
    runner.run_cycle(force=True)
    last_run = runner.load_state()["deep-research"]["last_run"]
    assert abs((now - 6900) - last_run) <= 10


# ── F31 — backoff announces once per window, once per escalation ─────────


def test_backoff_hold_announced_once_per_window(tmp_path, monkeypatch,
                                                log_lines):
    """~1450 near-identical skip events + warns in one night: repeated ticks
    inside an unchanged window must be silent after the first."""
    runner = _make_runner(tmp_path, "### task-a\n- interval: 1h\n- prompt: a\n")
    now = int(time.time())
    runner.save_state({
        "task-a": {"last_run": 0},
        "__shared_call__": {"consecutive_failures": 3, "last_failure": now,
                            "backoff_until": now + 300},
    })
    monkeypatch.setattr(
        runner, "claude_call",
        lambda p, timeout=None: pytest.fail("claude dialed during backoff"))
    for _ in range(3):
        runner.run_cycle(force=True)

    skips = [e for e in _sched_events(runner)
             if e.get("reason") == "shared_call_backoff"]
    assert len(skips) == 1
    assert skips[0]["task"] == "*"
    assert skips[0]["skipped"] == ["task-a"]
    holds = [msg for _, msg in log_lines if "Shared-call backoff active" in msg]
    assert len(holds) == 1
    shared = runner.load_state()["__shared_call__"]
    assert shared["announced_until"] == shared["backoff_until"]


def test_backoff_lapse_logged_once_and_escalation_reannounces(tmp_path,
                                                              monkeypatch,
                                                              log_lines):
    """A lapsed window says so once; the next failed call escalates
    (backoff_until moves) and the NEW window is announced again."""
    runner = _make_runner(tmp_path, "### task-a\n- interval: 1h\n- prompt: a\n")
    now = int(time.time())
    runner.save_state({
        "task-a": {"last_run": 0},
        "__shared_call__": {"consecutive_failures": 3,
                            "last_failure": now - 400,
                            "backoff_until": now - 10,
                            "announced_until": now - 10},  # lapsed, announced
    })

    def failing(prompt, timeout=None):
        runner._call_timed_out = False
        runner._call_context_overflow = False
        runner._last_call_error = "API Error: boom"
        return ""

    monkeypatch.setattr(runner, "claude_call", failing)
    runner.run_cycle(force=True)   # lapse INFO, then 4th failure -> 600s window
    lapses = [msg for _, msg in log_lines if "backoff lapsed" in msg]
    assert len(lapses) == 1
    shared = runner.load_state()["__shared_call__"]
    assert shared["consecutive_failures"] == 4
    assert shared["backoff_until"] > now
    assert "announced_until" not in shared     # new window not yet announced

    runner.run_cycle(force=True)   # first gated tick of the escalated window
    runner.run_cycle(force=True)   # repeat tick: silent
    skips = [e for e in _sched_events(runner)
             if e.get("reason") == "shared_call_backoff"]
    assert len(skips) == 1
    holds = [msg for _, msg in log_lines if "Shared-call backoff active" in msg]
    assert len(holds) == 1


def test_backoff_lapse_then_success_clears_cleanly(tmp_path, monkeypatch,
                                                   log_lines):
    runner = _make_runner(tmp_path, "### task-a\n- interval: 1h\n- prompt: a\n")
    now = int(time.time())
    runner.save_state({
        "task-a": {"last_run": 0},
        "__shared_call__": {"consecutive_failures": 4,
                            "last_failure": now - 700,
                            "backoff_until": now - 100,
                            "announced_until": now - 100},
    })
    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    assert "__shared_call__" not in runner.load_state()
    lapses = [msg for _, msg in log_lines if "backoff lapsed" in msg]
    assert len(lapses) == 1

    runner.run_cycle(force=False)  # nothing due, nothing to re-announce
    lapses = [msg for _, msg in log_lines if "backoff lapsed" in msg]
    assert len(lapses) == 1
