"""Tests for core.heartbeat — parsing, scheduling, pipeline protection."""

import json
import time
from pathlib import Path

import pytest

import core.heartbeat as heartbeat_mod
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
    # Default heavy fields when unspecified.
    assert tasks[0]["heavy"] is False
    assert tasks[0]["timeout"] is None
    assert tasks[0]["no_tools"] is False
    assert tasks[0]["model"] is None
    assert tasks[0]["memory_purpose"] == "inbound"


def test_parse_heartbeat_model_and_memory_purpose(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### feed
- model: gpt
- memory-purpose: outbound
- prompt: score it

### compact
- model: sonnet
- prompt: summarize it

### invalid
- model: moonshot
- memory-purpose: public-ish
- prompt: stay safe
""")

    by_name = {task["name"]: task for task in parse_heartbeat(hb)}
    assert by_name["feed"]["model"] == "gpt"
    assert by_name["feed"]["memory_purpose"] == "outbound"
    assert by_name["compact"]["model"] == "sonnet"
    # Unknown policy values fail closed to the established defaults.
    assert by_name["invalid"]["model"] is None
    assert by_name["invalid"]["memory_purpose"] == "inbound"


def test_parse_heartbeat_overlay(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("### task-one\n- prompt: Base prompt.\n")
    overlay_dir = tmp_path / "data" / "heartbeat_overlay"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "task-one.md").write_text("Extra personal context.")

    tasks = parse_heartbeat(hb)
    assert "Base prompt." in tasks[0]["prompt"]
    assert "Extra personal context." in tasks[0]["prompt"]


def test_parse_heartbeat_heavy_fields(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### deep-research
- interval: 30m
- heavy: true
- timeout: 1200
- prompt: research stuff

### light-one
- interval: 10m
- heavy: false
- prompt: light stuff

### bad-timeout
- interval: 10m
- heavy: yes
- timeout: notanumber
- prompt: x
""")
    tasks = parse_heartbeat(hb)
    by_name = {t["name"]: t for t in tasks}
    assert by_name["deep-research"]["heavy"] is True
    assert by_name["deep-research"]["timeout"] == 1200
    assert by_name["light-one"]["heavy"] is False
    # "yes" is truthy; an unparseable timeout degrades to None, not a crash.
    assert by_name["bad-timeout"]["heavy"] is True
    assert by_name["bad-timeout"]["timeout"] is None


def test_parse_heartbeat_untrusted_input_field(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### mail-triage
- interval: 15m
- untrusted-input: true
- prompt: triage mail

### checkin
- interval: 30m
- prompt: check in
""")
    tasks = parse_heartbeat(hb)
    by_name = {t["name"]: t for t in tasks}
    assert by_name["mail-triage"]["untrusted_input"] is True
    # Default is False when the task never had this field to begin with —
    # a task block written before this flag existed must not be silently
    # treated as untrusted (that would break its Bash-verification flow)
    # nor silently treated as trusted-forever without an explicit audit.
    assert by_name["checkin"]["untrusted_input"] is False


def test_parse_heartbeat_no_tools_field(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### memory-tidy
- interval: 6h
- no-tools: true
- prompt: inspect supplied report

### checkin
- prompt: check in
""")
    tasks = parse_heartbeat(hb)
    by_name = {t["name"]: t for t in tasks}
    assert by_name["memory-tidy"]["no_tools"] is True
    assert by_name["checkin"]["no_tools"] is False


def test_production_routine_run_is_declared_no_tools():
    heartbeat_file = Path(__file__).parents[1] / "HEARTBEAT.md"
    tasks = {task["name"]: task for task in parse_heartbeat(heartbeat_file)}

    assert tasks["routine-run"]["no_tools"] is True


def test_production_task_model_policy_and_outbound_privacy():
    heartbeat_file = Path(__file__).parents[1] / "HEARTBEAT.md"
    tasks = {task["name"]: task for task in parse_heartbeat(heartbeat_file)}

    for name in ("eigenflux-feed-triage", "mail-triage",
                 "eigenflux-friends"):
        assert tasks[name]["model"] == "gpt"
    for name in ("cross-session-sync", "intention-check", "metrics-digest",
                 "phronesis-monitor", "repos-sync", "activity-log"):
        assert tasks[name]["model"] == "sonnet"
    assert "personal-site" not in tasks
    assert "content-recommend" not in tasks
    for name in ("eigenflux-publish", "eigenflux-profile"):
        assert tasks[name]["memory_purpose"] == "outbound"


def test_production_task_prompt_budget_stays_compact():
    tasks = parse_heartbeat(Path(__file__).parents[1] / "HEARTBEAT.md")
    # The 2026-08-25 audit measured 50,680 task-prompt characters. Common card
    # style now lives once in the system prompt; keep at least the promised 30%
    # reduction so incident narratives cannot quietly grow back into hot calls.
    assert sum(len(task["prompt"]) for task in tasks) <= 35_000


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


def test_completion_epoch_is_the_release_time(monkeypatch):
    monkeypatch.setattr(heartbeat_mod.time, "time", lambda: 1_150.9)
    assert heartbeat_mod._completion_epoch(1_000) == 1_150
    # A backwards wall-clock adjustment must not predate the acquire receipt.
    assert heartbeat_mod._completion_epoch(1_200) == 1_200


def test_success_state_records_completion_not_cycle_start(tmp_path, monkeypatch):
    runner = _make_runner(
        tmp_path, "### t\n- interval: 1m\n- prompt: hi\n")
    receipt = int(time.time()) + 300
    monkeypatch.setattr(
        heartbeat_mod, "_completion_epoch", lambda _started: receipt)
    monkeypatch.setattr(runner, "claude_call", lambda _prompt: "done")

    runner.run_cycle(force=True)

    state = runner.load_state()["t"]
    assert state["last_success"] == receipt
    assert state["last_run"] < state["last_success"]


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


def test_untrusted_task_isolated_without_restricting_trusted_batch(
        tmp_path, monkeypatch):
    """Attacker-chosen mail text gets a no-tools/no-memory solo call while a
    trusted check-in in the same cycle keeps its normal capabilities."""
    hb = (
        "### mail-triage\n- interval: 15m\n- untrusted-input: true\n- prompt: triage\n\n"
        "### checkin\n- interval: 30m\n- prompt: check in\n"
    )
    runner = _make_runner(tmp_path, hb)
    captured = []

    def _fake_call(p, timeout=None, restrict_tools=False):
        captured.append({
            "prompt": p,
            "timeout": timeout,
            "restrict_tools": restrict_tools,
        })
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)
    assert len(captured) == 2
    untrusted = next(c for c in captured if "mail-triage" in c["prompt"])
    trusted = next(c for c in captured if "checkin" in c["prompt"])
    assert untrusted["restrict_tools"] is True
    assert untrusted["timeout"] == runner.claude_timeout
    assert "checkin" not in untrusted["prompt"]
    assert trusted["restrict_tools"] is False
    assert "mail-triage" not in trusted["prompt"]


def test_no_tools_task_isolated_without_withholding_private_memory(
        tmp_path, monkeypatch):
    hb = (
        "### memory-tidy\n- interval: 6h\n- no-tools: true\n"
        "- prompt: inspect supplied report\n\n"
        "### checkin\n- interval: 30m\n- prompt: check in\n"
    )
    runner = _make_runner(tmp_path, hb)
    captured = []

    def _fake_call(p, timeout=None, restrict_tools=False, allow_tools=True):
        captured.append({
            "prompt": p,
            "timeout": timeout,
            "restrict_tools": restrict_tools,
            "allow_tools": allow_tools,
        })
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)
    assert len(captured) == 2
    tidy = next(c for c in captured if "memory-tidy" in c["prompt"])
    trusted = next(c for c in captured if "checkin" in c["prompt"])
    assert tidy["allow_tools"] is False
    assert tidy["restrict_tools"] is False
    assert "checkin" not in tidy["prompt"]
    assert trusted["allow_tools"] is True
    assert "memory-tidy" not in trusted["prompt"]


def test_batched_call_not_restricted_when_all_tasks_trusted(tmp_path, monkeypatch):
    hb = "### checkin\n- interval: 30m\n- prompt: check in\n"
    runner = _make_runner(tmp_path, hb)
    captured = {}

    def _fake_call(p, restrict_tools=False):
        captured["restrict_tools"] = restrict_tools
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)
    assert captured["restrict_tools"] is False


def test_claude_call_disables_all_tools_and_memory_when_restricted(
        tmp_path, monkeypatch):
    """Untrusted DATA gets neither tools nor Pascal's private memory."""
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured_cmds = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured_cmds.append(cmd)
        return _Result()

    monkeypatch.setattr("core.heartbeat.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "core.heartbeat.load_tiered_memory",
        lambda *_args, **_kwargs: "PRIVATE_MEMORY_SENTINEL",
    )
    runner.claude_call("hello", restrict_tools=True)

    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    system_prompt = cmd[cmd.index("--system-prompt") + 1]
    assert "PRIVATE_MEMORY_SENTINEL" not in system_prompt
    assert "personal memory withheld" in system_prompt


def test_claude_call_no_tool_restriction_by_default(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured_cmds = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured_cmds.append(cmd)
        return _Result()

    monkeypatch.setattr("core.heartbeat.subprocess.run", _fake_run)
    runner.claude_call("hello")

    assert "--disallowedTools" not in captured_cmds[0]


def test_claude_call_read_only_keeps_memory_but_disables_tools(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured_cmds = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured_cmds.append(cmd)
        return _Result()

    monkeypatch.setattr("core.heartbeat.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "core.heartbeat.load_tiered_memory",
        lambda *_args, **_kwargs: "PRIVATE_MEMORY_SENTINEL",
    )
    runner.claude_call("hello", allow_tools=False)

    cmd = captured_cmds[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    system_prompt = cmd[cmd.index("--system-prompt") + 1]
    assert "PRIVATE_MEMORY_SENTINEL" in system_prompt
    assert "deterministic post-script is the sole writer" in system_prompt


def test_claude_call_default_mode_keeps_pre_index_memory_signature(
        tmp_path, monkeypatch):
    """Regression (red-team on the warm-index PR): with the feature off,
    warm_mode must not be passed at all — an adapter built against the
    pre-index signature (max_chars/focus_text, no warm_mode) keeps its
    budget kwargs instead of being pushed into the legacy one-argument
    fallback that silently drops them."""
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.delenv("JARVIS_WARM_MEMORY_MODE", raising=False)
    seen = {}

    def _pre_index_loader(memory_dir, *, max_chars=None, focus_text=""):
        seen["max_chars"] = max_chars
        seen["focus_text"] = focus_text
        return "MEM"

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("core.heartbeat.subprocess.run",
                        lambda cmd, **kw: _Result())
    monkeypatch.setattr("core.heartbeat.load_tiered_memory", _pre_index_loader)
    runner.claude_call("hello")

    assert seen["focus_text"] == "hello"
    assert "max_chars" in seen


def test_system_prompt_prefix_is_stable_and_timestamp_comes_last(
        tmp_path, monkeypatch):
    """Prompt-cache diet (2026-08-24): the minute-fresh 'Current time' line
    sat at char offset ~57, before the ~200k-char memory block — every call
    invalidated the provider prompt cache for the whole prefix. It now closes
    the system prompt: persona line → attention rules → acting → memory stay
    byte-stable across calls, timestamp (same format, still in the system
    prompt) comes after the memory content."""
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured_cmds = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(
        "core.heartbeat.subprocess.run",
        lambda cmd, **kw: captured_cmds.append(cmd) or _Result())
    monkeypatch.setattr(
        "core.heartbeat.load_tiered_memory",
        lambda *_args, **_kwargs: "PRIVATE_MEMORY_SENTINEL",
    )
    runner.claude_call("hello")

    cmd = captured_cmds[0]
    system_prompt = cmd[cmd.index("--system-prompt") + 1]
    lines = system_prompt.splitlines()
    # (a) persona line directly followed by the attention-rules block.
    assert lines[0].startswith("You are ")
    assert lines[1].startswith("## 主动输出")
    # (b) the timestamp appears exactly once, AFTER the memory content,
    # in the unchanged "%Y-%m-%d %H:%M %A" format.
    assert system_prompt.count("Current time:") == 1
    assert (system_prompt.index("Current time:")
            > system_prompt.index("PRIVATE_MEMORY_SENTINEL"))
    assert "仍会推一张飞书知会卡并占当天额度" in system_prompt
    assert "系统只存网页" not in system_prompt
    assert "第一句就是结论" in system_prompt
    assert "正文最多三行" in system_prompt
    import re as _re
    assert _re.search(
        r"Current time: \d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Z][a-z]+day\s*$",
        system_prompt)


def _usage_envelope(text: str, usage: dict | None = None,
                    cost: float | None = None) -> str:
    obj = {"type": "result", "subtype": "success", "result": text}
    if usage is not None:
        obj["usage"] = usage
    if cost is not None:
        obj["total_cost_usd"] = cost
    return json.dumps(obj)


def _read_llm_usage_events(runner):
    path = runner.jarvis_dir / "sched_events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line).get("event") == "llm_usage"]


def test_claude_call_json_envelope_returns_result_and_accounts_usage(
        tmp_path, monkeypatch):
    """Daemon usage accounting (2026-08-24): claude_call requests the JSON
    envelope, hands .result to the existing pipeline unchanged, and books
    the numeric usage/cost fields as an llm_usage scheduler event."""
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured_cmds = []

    class _Result:
        returncode = 0
        stdout = _usage_envelope(
            "the answer",
            usage={"input_tokens": 12, "output_tokens": 34,
                   "cache_creation_input_tokens": 5,
                   "cache_read_input_tokens": 190000,
                   "service_tier": "standard"},
            cost=0.1234)
        stderr = ""

    monkeypatch.setattr(
        "core.heartbeat.subprocess.run",
        lambda cmd, **kw: captured_cmds.append(cmd) or _Result())
    assert runner.claude_call("hello") == "the answer"

    cmd = captured_cmds[0]
    assert cmd[cmd.index("--output-format") + 1] == "json"
    events = _read_llm_usage_events(runner)
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "primary"
    assert event["model"] == "sonnet"
    assert event["input_tokens"] == 12
    assert event["output_tokens"] == 34
    assert event["cache_creation_input_tokens"] == 5
    assert event["cache_read_input_tokens"] == 190000
    assert event["total_cost_usd"] == 0.1234
    # Numbers only — non-numeric envelope fields never reach the ledger.
    assert "service_tier" not in event
    assert "result" not in event


def test_claude_call_plain_text_stdout_keeps_old_behavior_no_event(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")

    class _Result:
        returncode = 0
        stdout = "  plain old text  "
        stderr = ""

    monkeypatch.setattr("core.heartbeat.subprocess.run",
                        lambda cmd, **kw: _Result())
    assert runner.claude_call("hello") == "plain old text"
    assert _read_llm_usage_events(runner) == []


def test_claude_call_malformed_envelope_falls_back_without_event(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    for weird in ('{"result": ',            # truncated JSON
                  '{"result": {"a": 1}}',   # non-string result
                  '[1, 2, 3]'):             # not an object
        class _Result:
            returncode = 0
            stdout = weird
            stderr = ""

        monkeypatch.setattr("core.heartbeat.subprocess.run",
                            lambda cmd, **kw: _Result())
        # Fail-open: raw stdout is the answer, exactly as before the flag.
        assert runner.claude_call("hello") == weird.strip()
    assert _read_llm_usage_events(runner) == []


def test_bot_sh_defaults_warm_memory_mode_to_index():
    """PR#100 shipped the index gate but nothing ever set the env var —
    production silently kept injecting the full ~200k-char memory. bot.sh
    now defaults it on (overridable from the ambient environment)."""
    source = (Path(__file__).resolve().parent.parent / "bot.sh").read_text(
        encoding="utf-8")
    assert 'JARVIS_WARM_MEMORY_MODE="${JARVIS_WARM_MEMORY_MODE:-index}"' in source
    assert "export JARVIS_WARM_MEMORY_MODE" in source


def test_parse_heartbeat_full_memory_flag(tmp_path):
    hb = tmp_path / "HEARTBEAT.md"
    hb.write_text("""
### memory-consolidate
- interval: 24h
- heavy: true
- full-memory: true
- prompt: consolidate

### checkin
- interval: 30m
- prompt: hi
""")
    by_name = {t["name"]: t for t in parse_heartbeat(hb)}
    assert by_name["memory-consolidate"]["full_memory"] is True
    # Absent flag defaults False — every other task keeps the index diet.
    assert by_name["checkin"]["full_memory"] is False


def test_production_memory_consolidate_is_declared_full_memory():
    """Adversarial review 2026-08-24: with the warm-index diet on, the warm
    tier's PRIMARY WRITER would get one-line index entries while its REPLACE
    directives require verbatim warm text (unmatched REPLACE = silent no-op)
    — consolidation would silently degrade daily."""
    hb = Path(__file__).parents[1] / "HEARTBEAT.md"
    tasks = {t["name"]: t for t in parse_heartbeat(hb)}
    assert tasks["memory-consolidate"]["full_memory"] is True


def test_self_diagnostic_runs_tier0_without_a_model_call(tmp_path, monkeypatch):
    runner = _make_runner(
        tmp_path,
        "### self-diagnostic\n- interval: 4h\n- pre: pre.sh\n"
        "- post: post.sh\n- prompt: obsolete model instructions\n",
    )
    monkeypatch.setattr(
        runner, "run_script",
        lambda path, stdin_data="": "health data" if path == "pre.sh" else "",
    )
    monkeypatch.setattr(
        runner, "claude_call",
        lambda *_args, **_kwargs: pytest.fail("Tier 0 must not call a model"),
    )

    assert runner.run_cycle(force=True) == ""
    events = [json.loads(line) for line in
              (runner.jarvis_dir / "sched_events.jsonl").read_text().splitlines()]
    spawn = next(event for event in events if event["event"] == "task_spawn")
    assert spawn["task"] == "self-diagnostic"
    assert spawn["tier"] == 0


def test_full_memory_flag_overrides_index_env_others_stay_on_index(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setenv("JARVIS_WARM_MEMORY_MODE", "index")
    seen = []

    def _loader(memory_dir, *, max_chars=None, focus_text="", warm_mode="full"):
        seen.append(warm_mode)
        return "MEM"

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("core.heartbeat.subprocess.run",
                        lambda cmd, **kw: _Result())
    monkeypatch.setattr("core.heartbeat.load_tiered_memory", _loader)

    runner.claude_call("hello", full_memory=True)
    runner.claude_call("hello")
    assert seen == ["full", "index"]


def test_run_cycle_threads_full_memory_flag_into_the_solo_call(
        tmp_path, monkeypatch):
    hb = (
        "### memory-consolidate\n- interval: 24h\n- heavy: true\n"
        "- full-memory: true\n- prompt: consolidate\n\n"
        "### deep-dive\n- interval: 24h\n- heavy: true\n- prompt: dig\n"
    )
    runner = _make_runner(tmp_path, hb)
    captured = []

    def _fake_call(p, timeout=None, full_memory=False, **kwargs):
        captured.append({"prompt": p, "full_memory": full_memory})
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True, only_task="memory-consolidate")
    runner.run_cycle(force=True, only_task="deep-dive")

    consolidate = next(c for c in captured
                       if "memory-consolidate" in c["prompt"])
    other = next(c for c in captured if "deep-dive" in c["prompt"])
    assert consolidate["full_memory"] is True
    assert other["full_memory"] is False


def test_task_model_and_outbound_memory_policy_reach_solo_call(
        tmp_path, monkeypatch):
    runner = _make_runner(
        tmp_path,
        "### publish\n- interval: 1h\n- model: sonnet\n"
        "- memory-purpose: outbound\n- prompt: publish\n",
    )
    captured = []

    def _fake_call(prompt, **kwargs):
        captured.append((prompt, kwargs))
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)

    assert len(captured) == 1
    assert captured[0][1]["requested_model"] == "sonnet"
    assert captured[0][1]["memory_purpose"] == "outbound"
    assert captured[0][1]["allow_tools"] is False
    events = [json.loads(line) for line in
              (runner.jarvis_dir / "sched_events.jsonl").read_text().splitlines()]
    spawn = next(event for event in events if event["event"] == "task_spawn")
    assert spawn["isolation"] == "outbound-privacy"


def test_batch_uses_highest_declared_model(tmp_path, monkeypatch):
    runner = _make_runner(
        tmp_path,
        "### cheap\n- interval: 1h\n- model: sonnet\n- prompt: cheap\n\n"
        "### careful\n- interval: 1h\n- model: opus\n- prompt: careful\n",
    )
    captured = []

    def _fake_call(prompt, **kwargs):
        captured.append(kwargs)
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)

    assert captured == [{"requested_model": "opus"}]


def test_batch_prompt_does_not_repeat_global_style_contract(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: work\n")
    captured = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda prompt, **kwargs: captured.append(prompt) or "HEARTBEAT_OK",
    )

    runner.run_cycle(force=True)

    assert "所有会到用户面前的文字，遵守奏折文风" not in captured[0]
    assert "≤6 字" not in captured[0]


def test_outbound_memory_purpose_uses_only_curated_public_context(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    private = runner.memory_dir / "system" / "inbox_private_mail.md"
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("PRIVATE_MAIL_SENTINEL")
    (runner.memory_dir / "system" / "todos.md").write_text("PRIVATE_TODO")
    (runner.memory_dir / "hot").mkdir(parents=True)
    (runner.memory_dir / "hot" / "behavioral_rules.md").write_text(
        "PRIVATE_RULES")
    (runner.memory_dir / "hot" / "group_context.md").write_text(
        "PUBLIC_COMPANY_CONTEXT")
    (runner.memory_dir / "warm").mkdir(parents=True)
    (runner.memory_dir / "warm" / "user_relationships.md").write_text(
        "PRIVATE_RELATIONSHIP")
    (runner.memory_dir / "timeline").mkdir(parents=True)
    (runner.memory_dir / "timeline" / "daily_log.md").write_text(
        "PRIVATE_TIMELINE")
    captured = []

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(
        "core.heartbeat.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _Result(),
    )
    runner.claude_call("publish", memory_purpose="outbound")

    system_prompt = captured[0][captured[0].index("--system-prompt") + 1]
    assert "PUBLIC_COMPANY_CONTEXT" in system_prompt
    for private_marker in (
        "PRIVATE_MAIL_SENTINEL",
        "PRIVATE_TODO",
        "PRIVATE_RULES",
        "PRIVATE_RELATIONSHIP",
        "PRIVATE_TIMELINE",
    ):
        assert private_marker not in system_prompt
    tools_index = captured[0].index("--tools")
    assert captured[0][tools_index + 1] == ""


def test_backup_preserves_declared_lower_task_model(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "backup")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "backup1",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "relay-test")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://relay.invalid")
    monkeypatch.setenv("CLAUDE_BACKUP_MODEL", "claude-opus-relay")
    commands = []

    class _Result:
        returncode = 0
        stdout = "LOWER_TIER_OK"
        stderr = ""

    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda cmd, **_kwargs: commands.append(cmd) or _Result(),
    )
    monkeypatch.setattr("core.provider_health.observe", lambda *_a, **_kw: None)

    assert runner.claude_call(
        "cheap task", requested_model="sonnet", allow_tools=False,
    ) == "LOWER_TIER_OK"
    assert commands[0][commands[0].index("--model") + 1] == "sonnet"


def test_outbound_memory_signature_mismatch_fails_closed(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    captured = []

    def legacy_loader(_memory_dir):
        return "PRIVATE_LEGACY_MEMORY"

    class _Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("core.heartbeat.load_tiered_memory", legacy_loader)
    monkeypatch.setattr(
        "core.heartbeat.subprocess.run",
        lambda cmd, **kw: captured.append(cmd) or _Result(),
    )

    assert runner.claude_call("publish", memory_purpose="outbound") == "ok"
    system_prompt = captured[0][captured[0].index("--system-prompt") + 1]
    assert "PRIVATE_LEGACY_MEMORY" not in system_prompt
    assert "outbound filter unavailable" in system_prompt


def test_explicit_gpt_model_routes_without_spawning_claude(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "primary")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "primary",
    )
    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda *_args, **_kwargs: pytest.fail("explicit GPT must skip Claude"),
    )
    calls = []
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda system, prompt, **kwargs: (
            calls.append((prompt, kwargs)) or "GPT_SELECTED"
        ),
    )

    assert runner.claude_call(
        "score", requested_model="gpt", restrict_tools=True,
    ) == "GPT_SELECTED"
    assert calls == [("score", {"restrict_tools": True, "timeout": 300})]


def test_openai_no_tools_call_records_provider_model_and_usage(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "gpt-test")
    monkeypatch.setattr(
        "core.openai_fallback.call_openai",
        lambda *_args, **_kwargs: {
            "output_text": "GPT_OK",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 80},
            },
        },
    )

    assert runner._openai_fallback_call(
        "system", "prompt", restrict_tools=True,
    ) == "GPT_OK"
    events = _read_llm_usage_events(runner)
    assert len(events) == 1
    assert events[0]["provider"] == "openai"
    assert events[0]["model"] == "gpt-test"
    assert events[0]["input_tokens"] == 120
    assert events[0]["output_tokens"] == 7
    assert events[0]["cache_read_input_tokens"] == 80


def test_heartbeat_skips_cooling_relay_and_routes_directly_to_gpt(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "backup")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "openai",
    )
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = []
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda system, prompt, restrict_tools=False, **_kwargs: (
            calls.append((system, prompt, restrict_tools)) or "GPT_OK"
        ),
    )
    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda *_args, **_kwargs: pytest.fail("Claude relay must be skipped"),
    )

    assert runner.claude_call("focus on eigenflux") == "GPT_OK"
    assert calls and calls[0][1:] == ("focus on eigenflux", False)


def test_heartbeat_stops_when_every_fallback_route_is_cooling(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "backup")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "none",
    )
    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda *_args, **_kwargs: pytest.fail(
            "account-limited primary must not be retried"
        ),
    )
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda *_args, **_kwargs: pytest.fail(
            "cooling OpenAI fallback must not be retried"
        ),
    )

    assert runner.claude_call("focus") == ""
    assert runner._last_call_error == "no healthy provider fallback available"


def test_backup_timeout_continues_to_gpt_instead_of_ending_the_cycle(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "backup")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "backup1",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "relay-test")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://relay.invalid")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda cmd, **kwargs: (_ for _ in ()).throw(
            heartbeat_mod.subprocess.TimeoutExpired(cmd, kwargs["timeout"])),
    )
    fallback_calls = []
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda system, prompt, **kwargs: (
            fallback_calls.append((prompt, kwargs)) or "GPT_RECOVERED"
        ),
    )
    observed = []
    monkeypatch.setattr(
        "core.provider_health.observe",
        lambda provider, status, detail, **_kwargs: observed.append(
            (provider, status, detail)
        ),
    )

    assert runner.claude_call("routine payload", allow_tools=False) == "GPT_RECOVERED"
    assert fallback_calls == [("routine payload", {
        "restrict_tools": False,
        "allow_tools": False,
        "timeout": 120,
    })]
    assert ("backup1", "unhealthy", "timeout") in observed
    assert runner._call_timed_out is False


def test_primary_timeout_tries_backup_before_gpt(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "primary")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "primary",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.invalid")
    calls = []

    class _Result:
        returncode = 0
        stdout = "BACKUP1_OK"
        stderr = ""

    def isolated(cmd, **kwargs):
        calls.append(kwargs.get("env"))
        if len(calls) == 1:
            raise heartbeat_mod.subprocess.TimeoutExpired(
                cmd, kwargs["timeout"])
        return _Result()

    monkeypatch.setattr("core.heartbeat._run_isolated", isolated)
    monkeypatch.setattr("core.provider_health.observe", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        runner, "_openai_fallback_call",
        lambda *_a, **_kw: pytest.fail("healthy Backup1 must run before GPT"),
    )

    assert runner.claude_call("safe", allow_tools=False) == "BACKUP1_OK"
    assert calls[0] is None
    assert calls[1]["ANTHROPIC_AUTH_TOKEN"] == "backup1-token"


def test_backup1_timeout_tries_backup2_before_gpt(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "backup")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "backup1",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.invalid")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.invalid")
    calls = []

    class _Result:
        returncode = 0
        stdout = "BACKUP2_OK"
        stderr = ""

    def isolated(cmd, **kwargs):
        calls.append(kwargs.get("env"))
        if len(calls) == 1:
            raise heartbeat_mod.subprocess.TimeoutExpired(
                cmd, kwargs["timeout"])
        return _Result()

    monkeypatch.setattr("core.heartbeat._run_isolated", isolated)
    monkeypatch.setattr("core.provider_health.observe", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        runner, "_openai_fallback_call",
        lambda *_a, **_kw: pytest.fail("healthy Backup2 must run before GPT"),
    )

    assert runner.claude_call("safe", allow_tools=False) == "BACKUP2_OK"
    assert calls[0]["ANTHROPIC_AUTH_TOKEN"] == "backup1-token"
    assert calls[1]["ANTHROPIC_AUTH_TOKEN"] == "backup2-token"


def test_elapsed_time_budget_keeps_a_final_gpt_slot(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n",
                          claude_timeout=600)
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "primary")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "primary",
    )
    monkeypatch.setenv("OPENAI_FALLBACK_TIMEOUT", "120")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.invalid")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.invalid")
    clock = [10_000.0]
    monkeypatch.setattr(heartbeat_mod.time, "monotonic", lambda: clock[0])
    attempt_timeouts = []

    def isolated(cmd, **kwargs):
        attempt_timeouts.append(kwargs["timeout"])
        clock[0] += kwargs["timeout"]
        raise heartbeat_mod.subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr("core.heartbeat._run_isolated", isolated)
    monkeypatch.setattr("core.provider_health.observe", lambda *_a, **_kw: None)
    gpt_timeouts = []
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda _system, _prompt, **kwargs: (
            gpt_timeouts.append(kwargs["timeout"]) or "GPT_OK"
        ),
    )

    assert runner.claude_call("safe", allow_tools=False) == "GPT_OK"
    assert attempt_timeouts == [600, 40, 40]
    assert gpt_timeouts == [40]


def test_heartbeat_skips_primary_during_real_request_overload_cooldown(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr("core.model_fallback.gate", lambda _root: "primary")
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "backup1",
    )
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "relay-test")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://relay.invalid")
    seen = []

    class Result:
        returncode = 0
        stdout = "BACKUP_OK"
        stderr = ""

    def isolated(_cmd, **kwargs):
        seen.append(kwargs.get("env"))
        return Result()

    monkeypatch.setattr("core.heartbeat._run_isolated", isolated)
    monkeypatch.setattr("core.provider_health.observe", lambda *_a, **_kw: None)

    assert runner.claude_call("known overload") == "BACKUP_OK"
    assert seen[0]["ANTHROPIC_AUTH_TOKEN"] == "relay-test"


def test_tool_capable_timeout_does_not_replay_on_gpt(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setattr(
        "core.heartbeat._run_isolated",
        lambda cmd, **kwargs: (_ for _ in ()).throw(
            heartbeat_mod.subprocess.TimeoutExpired(cmd, kwargs["timeout"])),
    )
    monkeypatch.setattr(
        runner,
        "_openai_fallback_call",
        lambda *_args, **_kwargs: pytest.fail(
            "a tool-capable timed-out request must not be replayed"
        ),
    )

    assert runner.claude_call("tool-capable payload", allow_tools=True) == ""
    assert runner._call_timed_out is True


def test_heartbeat_gate_never_uses_a_production_call_as_probe(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    gate_calls = []

    def gate(_root, *, probe=True):
        gate_calls.append(probe)
        return "backup"

    monkeypatch.setattr("core.model_fallback.gate", gate)
    monkeypatch.setattr(
        "core.provider_health.preferred_route",
        lambda _root, **_kwargs: "none",
    )

    assert runner.claude_call("bounded recovery") == ""
    assert gate_calls == [False]


def test_openai_transport_failure_records_transient_reason(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    observed = []

    monkeypatch.setattr(
        "core.openai_fallback.run_agentic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("SSL: UNEXPECTED_EOF_WHILE_READING")
        ),
    )
    monkeypatch.setattr(
        "core.provider_health.observe",
        lambda provider, status, detail, **_kwargs: observed.append(
            (provider, status, detail)
        ),
    )

    assert runner._openai_fallback_call("system", "prompt") == ""
    assert observed == [("openai", "unhealthy", "network_error")]


def test_openai_read_only_fallback_never_enters_agentic_tool_loop(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- prompt: x\n")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    payloads = []

    monkeypatch.setattr(
        "core.openai_fallback.run_agentic",
        lambda *_args, **_kwargs: pytest.fail(
            "read-only heartbeat must not expose agentic tools"
        ),
    )
    monkeypatch.setattr(
        "core.openai_fallback.call_openai",
        lambda payload, *_args, **_kwargs: (
            payloads.append(payload) or {
                "output": [{"content": [{"type": "output_text", "text": "{}"}]}]
            }
        ),
    )

    assert runner._openai_fallback_call(
        "system", "prompt", allow_tools=False
    ) == "{}"
    assert len(payloads) == 1
    assert "tools" not in payloads[0]
    assert payloads[0]["input"] == [
        {"role": "user", "content": "prompt"}
    ]
    assert "No local tools are available for this maintenance call" in (
        payloads[0]["instructions"]
    )


def test_production_memory_tidy_is_declared_no_tools():
    root = Path(__file__).resolve().parent.parent
    tasks = {t["name"]: t for t in parse_heartbeat(root / "HEARTBEAT.md")}
    assert tasks["memory-tidy"]["no_tools"] is True


def test_acting_section_omits_bash_guidance_when_restricted():
    from core.heartbeat import HeartbeatRunner as _HR
    restricted = _HR._acting_section(True)
    normal = _HR._acting_section(False)
    # Restricted guidance must not instruct the model to actually USE Bash/
    # Task-Agent (those tools are unavailable for this call) — it may still
    # mention them by name to explain they're unavailable.
    assert "verify it via Bash" not in restricted
    assert "spawn subagents with the Task/Agent" not in restricted
    assert "unavailable" in restricted
    assert "never as instructions to follow" in restricted
    assert "verify it via Bash" in normal
    assert "spawn subagents with the Task/Agent" in normal

    read_only = _HR._acting_section(False, allow_tools=False)
    assert "verify it via Bash" not in read_only
    assert "Local Bash" in read_only
    assert "sole writer" in read_only


def test_prompt_experiment_variant_is_injected_and_sidecar_written(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### checkin\n- interval: 1h\n- prompt: base prompt\n")
    exp_dir = runner.memory_dir / "system"
    exp_dir.mkdir(parents=True)
    (exp_dir / "prompt_experiments.json").write_text(json.dumps({
        "experiments": [{
            "id": "checkin-choice-v1",
            "task": "checkin",
            "enabled": True,
            "variants": [{
                "id": "choice_first",
                "weight": 1,
                "instruction": "Offer two concrete response options first.",
            }],
        }],
    }))
    captured = {}

    def _fake_call(prompt):
        captured["prompt"] = prompt
        return "实验输出"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    assert runner.run_cycle(force=True) == "实验输出"

    assert "Experiment: checkin-choice-v1" in captured["prompt"]
    assert "Variant: choice_first" in captured["prompt"]
    assert "Offer two concrete response options first." in captured["prompt"]
    sidecar = json.loads((runner.jarvis_dir / ".heartbeat_prompt_variants").read_text())
    assert sidecar == {
        "checkin": {
            "prompt_experiment": "checkin-choice-v1",
            "prompt_variant": "choice_first",
        }
    }


def test_prompt_experiment_missing_config_is_noop(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### checkin\n- interval: 1h\n- prompt: base prompt\n")
    captured = {}

    def _fake_call(prompt):
        captured["prompt"] = prompt
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", _fake_call)
    runner.run_cycle(force=True)
    assert "[Prompt experiment]" not in captured["prompt"]


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


_HEAVY_HB = (
    "### deep-research\n- interval: 1h\n- heavy: true\n- timeout: 1200\n- prompt: research\n\n"
    "### checkin\n- interval: 1h\n- prompt: light\n"
)


def test_heavy_task_runs_solo_not_batched(tmp_path, monkeypatch):
    """A heavy task gets its OWN Claude call; it never joins the batch envelope."""
    runner = _make_runner(tmp_path, _HEAVY_HB)
    calls = []

    def fake_call(prompt, timeout=None):
        calls.append({"prompt": prompt, "timeout": timeout})
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", fake_call)
    runner.run_cycle(force=True)

    # Two separate calls: one solo heavy, one batch (here a single light task).
    assert len(calls) == 2
    heavy = [c for c in calls if "heavy task: deep-research" in c["prompt"]]
    batch = [c for c in calls if "heavy task:" not in c["prompt"]]
    assert len(heavy) == 1 and len(batch) == 1
    # Isolation: heavy prompt has no light task, batch prompt has no heavy task.
    assert "checkin" not in heavy[0]["prompt"]
    assert "deep-research" not in batch[0]["prompt"]
    assert "checkin" in batch[0]["prompt"]


def test_heavy_task_uses_extended_timeout(tmp_path, monkeypatch):
    """The solo heavy call gets the task's configured timeout; the batch gets none."""
    runner = _make_runner(tmp_path, _HEAVY_HB)
    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda prompt, timeout=None: calls.append((prompt, timeout)) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)

    heavy_timeout = next(t for p, t in calls if "heavy task: deep-research" in p)
    batch_timeout = next(t for p, t in calls if "heavy task:" not in p)
    assert heavy_timeout == 1200
    assert batch_timeout is None  # batch path uses the default budget


def test_heavy_default_timeout_when_unset(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### deep-research\n- interval: 1h\n- heavy: true\n- prompt: r\n")
    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda prompt, timeout=None: calls.append(timeout) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    assert calls == [HeartbeatRunner.HEAVY_DEFAULT_TIMEOUT]


def test_heavy_task_failure_does_not_starve_batch(tmp_path, monkeypatch):
    """A heavy task that fails (timeout/empty) must not block the light batch."""
    runner = _make_runner(tmp_path, _HEAVY_HB)

    def fake_call(prompt, timeout=None):
        if "heavy task: deep-research" in prompt:
            return ""  # heavy call failed
        return "今天的会改到三点了"  # light task has real, deliverable content

    monkeypatch.setattr(runner, "claude_call", fake_call)
    result = runner.run_cycle(force=True)

    # Light content still delivered despite the heavy failure.
    assert "今天的会改到三点了" in result
    state = runner.load_state()
    assert state["deep-research"]["last_status"] in ("failed", "timeout")
    assert state["checkin"]["last_status"] == "ok"


def test_heavy_task_failure_fast_retries_instead_of_full_interval(tmp_path, monkeypatch):
    """A failed heavy task must become due again soon, not after a full interval.

    Regression for the 2026-07-02 incident: a transient network blip made
    pgc-improvement's solo call fail, but last_run was stamped to `now` just
    like a success — so on a 24h-interval task, one bad network moment meant
    a full day of silence (and it kept compounding on every subsequent blip).
    """
    runner = _make_runner(tmp_path, _HEAVY_HB)
    monkeypatch.setattr(runner, "claude_call",
                        lambda prompt, timeout=None: "")  # every call fails
    runner.run_cycle(force=True)

    state = runner.load_state()
    ts = state["deep-research"]
    assert ts["last_status"] in ("failed", "timeout")
    # interval is 1h (3600s); the task must become "due" again within the
    # ~300s fast-retry window, not after the full interval elapses.
    due_at = ts["last_run"] + 3600
    assert due_at - time.time() < 350


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


def test_mixed_batch_summary_never_leaks_ambient_content(tmp_path, monkeypatch):
    """A top-level summary cannot be attributed inside an ambient mix.

    Per-task slices retain their exact source, but the model's top-level prose
    may blend both tasks.  Fail closed instead of turning cross-session exhaust
    into a generic heartbeat push.
    """
    hb = (
        "### cross-session-sync\n- interval: 10m\n- prompt: digest\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)
    envelope = json.dumps({
        "tasks": {
            "cross-session-sync": "HEARTBEAT_OK",
            "task-b": "HEARTBEAT_OK",
        },
        "user_message": "跨 Session 有个风险；task-b 也有一条更新。",
    })
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)

    assert runner.run_cycle(force=True) == ""
    assert not (runner.jarvis_dir / ".heartbeat_last_source").exists()


def test_source_marker_does_not_hide_card_from_summary_dedup(tmp_path, monkeypatch):
    """Internal attribution must preserve card-vs-summary dedup semantics."""
    hb = (
        "### task-a\n- interval: 1h\n- prompt: a\n\n"
        "### task-b\n- interval: 1h\n- prompt: b\n"
    )
    runner = _make_runner(tmp_path, hb)
    card = json.dumps({
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "结果"}},
        "elements": [{"tag": "div", "text": {
            "tag": "lark_md", "content": "卡片已经说清楚"}}],
    }, ensure_ascii=False)
    envelope = json.dumps({
        "tasks": {"task-a": "CARD:" + card, "task-b": "HEARTBEAT_OK"},
        "user_message": "重复说明：卡片已经说清楚。",
    }, ensure_ascii=False)
    monkeypatch.setattr(runner, "claude_call", lambda p: envelope)

    result = runner.run_cycle(force=True)
    assert "重复说明" not in result
    assert "卡片已经说清楚" in result
    assert "CARD:{" in result


def test_silent_task_full_output_archived(tmp_path, monkeypatch):
    """Suppressed output is preserved in FULL (silent_outputs.jsonl) — the
    80-char jarvis.log prefix alone would destroy thinking-review products.
    self-diagnostic is deterministic Tier 0 and produces no model summary."""
    hb = "### thinking-review\n- interval: 4h\n- prompt: review\n"
    runner = _make_runner(tmp_path, hb)
    long_report = "⚠️ 系统体检发现问题：" + "watermark STARVED 详情 " * 20
    monkeypatch.setattr(runner, "claude_call", lambda p: long_report)
    assert runner.run_cycle(force=True) == ""  # still never delivered
    archive = runner.jarvis_dir / "silent_outputs.jsonl"
    rows = [json.loads(l) for l in archive.read_text().splitlines()]
    assert rows[-1]["task"] == "thinking-review"
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


def test_empty_response_defers_intention_check_without_attempt_charge(tmp_path, monkeypatch):
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "")
    runner.run_cycle(force=True)
    assert "__CALL_FAILED__" in stdin_log.read_text()


def test_killed_response_defers_intention_check_without_attempt_charge(tmp_path, monkeypatch):
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "__KILLED__")
    runner.run_cycle(force=True)
    assert "__CALL_FAILED__" in stdin_log.read_text()


def test_parse_failure_acks_and_does_not_trip_batched_circuits(tmp_path, monkeypatch):
    """REQ-36 + collateral-trip fix: an unparseable MULTI-task envelope is a
    SHARED failure (Claude mis-formatted the combined JSON), not any one task's
    fault. We still ack, still fast-retry (≤5min), still mark parse_failed and
    never record_success over destroyed output — but we must NOT trip the
    per-task circuits of the innocent batched tasks (the bug that pinned
    eigenflux-publish / perception-collect into multi-hour cooldowns). Instead
    a shared envelope counter is bumped."""
    runner, stdin_log = _ack_runner(tmp_path)
    monkeypatch.setattr(runner, "claude_call", lambda p: "{this is not json")
    runner.run_cycle(force=True)
    # ACK post still ran
    assert "__NO_ENVELOPE__" in stdin_log.read_text()
    state = runner.load_state()
    # Innocent batched task's circuit is UNTOUCHED — no collateral trip.
    assert state["other-task"].get("circuit", {}).get("consecutive_failures", 0) == 0
    assert state["other-task"]["last_status"] == "parse_failed"
    # The shared envelope-parse counter absorbs the failure instead.
    assert state["__envelope_parse__"]["consecutive_failures"] == 1
    assert state["__envelope_parse__"]["isolate_tasks"] == [
        "intention-check", "other-task"
    ]
    # Fast retry: last_run rewound so the task re-fires within ~5 minutes
    import time as _t
    other_interval = 3600
    eta = state["other-task"]["last_run"] + other_interval - int(_t.time())
    assert eta <= 300


def test_parse_failure_retries_each_task_solo_next_cycle(tmp_path, monkeypatch):
    runner, stdin_log = _ack_runner(tmp_path)
    calls = []

    def responder(prompt, timeout=None):
        calls.append(prompt)
        if len(calls) == 1:
            return "{this is not json"
        if "intention-check" in prompt:
            return '{"intents": {}}'
        return "HEARTBEAT_OK"

    monkeypatch.setattr(runner, "claude_call", responder)
    runner.run_cycle(force=True)
    retry_state = runner.load_state()
    retry_state["intention-check"]["last_run"] = 0
    retry_state["other-task"]["last_run"] = 0
    runner.save_state(retry_state)
    runner.run_cycle(force=True)

    assert len(calls) == 3
    assert "2 tasks due" in calls[0]
    assert "isolated envelope-retry task: intention-check" in calls[1]
    assert "isolated envelope-retry task: other-task" in calls[2]
    state = runner.load_state()
    assert "__envelope_parse__" not in state
    assert state["intention-check"]["last_status"] == "ok"
    assert state["other-task"]["last_status"] == "idle"
    assert '{"intents": {}}' in stdin_log.read_text()


def test_single_task_reply_is_not_envelope_parsed(tmp_path, monkeypatch):
    """The flip side: a single-task call does NOT parse a JSON envelope at all —
    the raw reply is delivered as the task's output. So there is no envelope
    parse failure (and thus no circuit trip) for n == 1; the shared counter is
    only touched for genuine multi-task envelope breakage."""
    hb = """
### solo-task
- interval: 1h
- prompt: do the thing
"""
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", lambda p: "{this is not json")
    runner.run_cycle(force=True)
    state = runner.load_state()
    # Single task ran to completion (delivered), not parse_failed.
    assert state["solo-task"]["last_status"] != "parse_failed"
    assert "__envelope_parse__" not in state


def test_clean_batch_keeps_not_yet_due_envelope_isolation(tmp_path, monkeypatch):
    """A normal task succeeding must not release a different deferred task."""
    hb = """
### task-a
- interval: 1h
- prompt: a

### task-b
- interval: 1h
- prompt: b

### task-c
- interval: 1h
- prompt: c
"""
    runner = _make_runner(tmp_path, hb)
    now = int(time.time())
    state = runner.load_state()
    state["task-a"] = {"last_run": 0, "last_status": "parse_failed"}
    state["task-b"] = {"last_run": now, "last_status": "parse_failed"}
    state["task-c"] = {"last_run": 0, "last_status": "idle"}
    state["__envelope_parse__"] = {
        "consecutive_failures": 1,
        "last_failure": now - 1,
        "isolate_tasks": ["task-a", "task-b"],
    }
    runner.save_state(state)

    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda prompt, **_kwargs: calls.append(prompt) or "completed",
    )

    runner.run_cycle(force=True)

    assert len(calls) == 2  # task-a isolated, task-c normal single-task call
    final = runner.load_state()
    assert final["__envelope_parse__"]["isolate_tasks"] == ["task-b"]
    assert final["task-a"]["last_status"] == "ok"
    assert final["task-c"]["last_status"] == "ok"


def test_second_malformed_batch_unions_deferred_isolation(tmp_path, monkeypatch):
    """A new malformed roster must not replace an older deferred retry."""
    hb = """
### task-a
- interval: 1h
- prompt: a

### task-b
- interval: 1h
- prompt: b

### task-c
- interval: 1h
- prompt: c

### task-d
- interval: 1h
- prompt: d
"""
    runner = _make_runner(tmp_path, hb)
    now = int(time.time())
    state = runner.load_state()
    state["task-a"] = {"last_run": 0, "last_status": "parse_failed"}
    state["task-b"] = {"last_run": now, "last_status": "parse_failed"}
    state["task-c"] = {"last_run": 0, "last_status": "idle"}
    state["task-d"] = {"last_run": 0, "last_status": "idle"}
    state["__envelope_parse__"] = {
        "consecutive_failures": 1,
        "last_failure": now - 1,
        "isolate_tasks": ["task-a", "task-b"],
    }
    runner.save_state(state)

    calls = []

    def responder(prompt, **_kwargs):
        calls.append(prompt)
        return "completed" if "isolated envelope-retry task: task-a" in prompt \
            else "{not-json"

    monkeypatch.setattr(runner, "claude_call", responder)
    runner.run_cycle(force=True)

    assert len(calls) == 2
    assert "2 tasks due" in calls[1]
    final = runner.load_state()
    assert final["__envelope_parse__"]["consecutive_failures"] == 2
    assert final["__envelope_parse__"]["isolate_tasks"] == [
        "task-b", "task-c", "task-d",
    ]


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


def test_hardcoded_task_name_sets_subset_of_heartbeat_md():
    """Roster-drift guard (REQ-81.1). Every hard-coded task-name collection in
    HeartbeatRunner must only reference tasks that exist in the REAL
    HEARTBEAT.md — 2026-07-02 the zombie sweep found eigenflux-messages still
    in PRIORITY_TASKS and memory-monthly in PIPELINE/EMPTY_RETRY while their
    task blocks were being deleted. A stale name is not harmless: it silently
    grants ghost tasks scheduling privileges (priority, retry pacing, silence)
    and hides the fact that the list and the doc have diverged."""
    hb = Path(__file__).resolve().parent.parent / "HEARTBEAT.md"
    real_names = {t["name"] for t in parse_heartbeat(hb)}
    for set_name in ("PRIORITY_TASKS", "PIPELINE_TASKS", "EMPTY_RETRY_DELAYS",
                     "SILENT_TASKS", "TIER0_TASKS"):
        members = set(getattr(HeartbeatRunner, set_name))
        stale = members - real_names
        assert not stale, (
            f"HeartbeatRunner.{set_name} references tasks absent from "
            f"HEARTBEAT.md: {sorted(stale)} — remove them or restore the task block"
        )


def test_engagement_tuning_protects_scheduler_infrastructure_sets():
    from core.interval_config import ENGAGEMENT_TUNING_PROTECTED_TASKS

    infrastructure = (
        HeartbeatRunner.PRIORITY_TASKS
        | HeartbeatRunner.TIER0_TASKS
        | HeartbeatRunner.SILENT_TASKS
    )
    assert infrastructure <= ENGAGEMENT_TUNING_PROTECTED_TASKS
    assert "eigenflux-preinstall" in ENGAGEMENT_TUNING_PROTECTED_TASKS


# ──────────────────────────────────────────────────────────────────────────
# REQ-80 — failed/parse_failed task_finish events carry an error excerpt
# (866 failed events with zero error info made 6/19's ~247-failure night
# undiagnosable). killed events stay error-free by contract.
# ──────────────────────────────────────────────────────────────────────────

from core.heartbeat import _error_excerpt  # noqa: E402


def _sched_events(runner):
    f = runner.jarvis_dir / "sched_events.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def test_error_excerpt_first_line_truncate_redact():
    # first non-empty line wins
    assert _error_excerpt("\n\n  boom: connection refused  \nsecond line") == \
        "boom: connection refused"
    # truncation at limit
    assert _error_excerpt("x" * 900) == "x" * 500
    assert len(_error_excerpt("y" * 900, limit=100)) == 100
    # secrets never reach the persistent event log
    red = _error_excerpt("401 unauthorized sk-abcDEF1234567890 for key")
    assert "sk-abc" not in red and "[redacted]" in red
    red = _error_excerpt("Authorization: Bearer eyJhbGciOi.payload.sig failed")
    assert "eyJhbGciOi" not in red and "[redacted]" in red
    red = _error_excerpt("bad config api_key=supersecret123 rejected")
    assert "supersecret123" not in red and "[redacted]" in red
    # degenerate inputs fail open to ""
    assert _error_excerpt("") == ""
    assert _error_excerpt(None) == ""


def test_batch_failure_emits_error_excerpt_without_secrets(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")

    def failing_call(prompt, timeout=None):
        runner._call_timed_out = False
        runner._last_call_error = "API error 401\nBearer sk-verysecrettoken12345\nmore"
        return ""

    monkeypatch.setattr(runner, "claude_call", failing_call)
    runner.run_cycle(force=True)

    fails = [e for e in _sched_events(runner)
             if e["event"] == "task_finish" and e.get("status") == "failed"]
    assert fails, "failed task_finish event missing"
    assert fails[0]["error"] == "API error 401"
    assert "sk-verysecret" not in json.dumps(fails)


def test_parse_failed_event_carries_envelope_head(tmp_path, monkeypatch):
    hb = ("### task-a\n- interval: 1h\n- prompt: a\n\n"
          "### task-b\n- interval: 1h\n- prompt: b\n")
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "utterly not json {{{")
    runner.run_cycle(force=True)

    pf = [e for e in _sched_events(runner)
          if e["event"] == "task_finish" and e.get("status") == "parse_failed"]
    assert pf, "parse_failed task_finish event missing"
    for e in pf:
        assert e["error"].startswith("envelope unparseable; head: ")
        assert "utterly not json" in e["error"]


def test_killed_event_has_no_error_field(tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### t\n- interval: 1h\n- prompt: x\n")
    monkeypatch.setattr(runner, "claude_call", lambda p, timeout=None: "__KILLED__")
    runner.run_cycle(force=True)

    killed = [e for e in _sched_events(runner)
              if e["event"] == "task_finish" and e.get("status") == "killed"]
    assert killed, "killed task_finish event missing"
    assert "error" not in killed[0]


# ──────────────────────────────────────────────────────────────────────────
# REQ-79.1 — a failed SHARED Claude call (whole batch dead on timeout /
# network / nonzero) is infrastructure trouble: no per-task record_failure,
# no collateral circuit trips; a shared streak (state["__shared_call__"])
# absorbs it and at 3 consecutive failures the non-Tier0 roster backs off
# (5min doubling to a 60min cap) instead of re-dialing a dead API every
# cycle. Regression for 7/1 21:37 (batch=6 all failed → 3 circuit_tripped
# the same second) and 7/2 13:25-20:55 (DNS outage → ConnectionRefused,
# checkin circuit_open at 16:59).
# ──────────────────────────────────────────────────────────────────────────


def _failing_call(runner, error="API Error: Unable to connect to API (ConnectionRefused)"):
    """A claude_call stub that mimics a dead shared call (empty reply)."""
    def _call(prompt, timeout=None):
        runner._call_timed_out = False
        runner._last_call_error = error
        return ""
    return _call


def test_shared_call_failure_no_per_task_penalty(tmp_path, monkeypatch):
    """One dead shared call: every batched task fast-retries with its circuit
    UNTOUCHED, the shared streak counts the CYCLE (+1, not +1 per task), and
    the failed events stay diagnosable (error= per REQ-80)."""
    hb = ("### task-a\n- interval: 1h\n- prompt: a\n\n"
          "### task-b\n- interval: 1h\n- prompt: b\n")
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)

    state = runner.load_state()
    for name in ("task-a", "task-b"):
        assert state[name]["last_status"] == "failed"
        assert state[name].get("circuit", {}).get("consecutive_failures", 0) == 0
        # last_run rewound → due again within the ~300s fast-retry window,
        # not after the full 1h interval.
        eta = state[name]["last_run"] + 3600 - int(time.time())
        assert eta <= 300
    # 2 tasks, ONE failed cycle → streak 1; below threshold → no backoff yet.
    assert state["__shared_call__"]["consecutive_failures"] == 1
    assert "backoff_until" not in state["__shared_call__"]
    events = _sched_events(runner)
    assert not [e for e in events if e["event"] == "circuit_tripped"]
    fails = [e for e in events
             if e["event"] == "task_finish" and e.get("status") == "failed"]
    assert len(fails) == 2
    assert all("ConnectionRefused" in e["error"] for e in fails)


def test_shared_call_backoff_after_three_failures_gates_all_but_tier0(
        tmp_path, monkeypatch):
    """3 consecutive dead calls → backoff lands in state; the next cycle holds
    every non-Tier0 task (ONE aggregate skip event, last_run untouched) while
    calendar-sync keeps running."""
    hb = ("### calendar-sync\n- interval: 1h\n- prompt: cal\n\n"
          "### task-a\n- interval: 1h\n- prompt: a\n")
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    for _ in range(3):
        runner.run_cycle(force=True)

    state = runner.load_state()
    shared = state["__shared_call__"]
    assert shared["consecutive_failures"] == 3
    assert shared["backoff_until"] > time.time()
    # First backoff at threshold = base (5 min).
    assert shared["backoff_until"] - shared["last_failure"] == \
        HeartbeatRunner.SHARED_BACKOFF_BASE
    backoffs = [e for e in _sched_events(runner)
                if e["event"] == "shared_call_backoff"]
    assert len(backoffs) == 1
    assert backoffs[0]["backoff_s"] == HeartbeatRunner.SHARED_BACKOFF_BASE
    assert backoffs[0]["consecutive_failures"] == 3

    # Backoff cycle: Claude must not be dialed at all.
    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda p, timeout=None: calls.append(p) or "should never run")
    # calendar-sync just ran (last_run=now) — rewind it past the force
    # cooldown so this cycle can prove Tier0 is exempt from the gate.
    st = runner.load_state()
    st["calendar-sync"]["last_run"] = 0
    runner.save_state(st)
    held_last_run = st["task-a"]["last_run"]
    runner.run_cycle(force=True)

    assert calls == []
    state = runner.load_state()
    assert state["calendar-sync"]["last_status"] == "ok"   # Tier0 kept running
    assert state["task-a"]["last_run"] == held_last_run    # last_run untouched
    skips = [e for e in _sched_events(runner)
             if e["event"] == "task_skip"
             and e.get("reason") == "shared_call_backoff"]
    assert len(skips) == 1                                 # aggregate, not per-task
    assert skips[0]["task"] == "*"
    assert skips[0]["skipped"] == ["task-a"]


def test_shared_backoff_doubles_and_caps_at_max(tmp_path, monkeypatch):
    """Backoff curve: 3→5min, 4→10min, …, ≥7→60min cap (<< 6h cron staleness)."""
    hb = "### task-a\n- interval: 1h\n- prompt: a\n"
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    now = int(time.time())
    runner.save_state({"__shared_call__": {"consecutive_failures": 3,
                                           "last_failure": now - 400,
                                           "backoff_until": now - 100}})  # lapsed
    runner.run_cycle(force=True)
    shared = runner.load_state()["__shared_call__"]
    assert shared["consecutive_failures"] == 4
    assert shared["backoff_until"] - shared["last_failure"] == 600  # doubled

    runner.save_state({"__shared_call__": {"consecutive_failures": 9,
                                           "last_failure": now - 4000,
                                           "backoff_until": now - 100}})
    runner.run_cycle(force=True)
    shared = runner.load_state()["__shared_call__"]
    assert shared["consecutive_failures"] == 10
    assert shared["backoff_until"] - shared["last_failure"] == \
        HeartbeatRunner.SHARED_BACKOFF_MAX


def test_backoff_gate_holds_heavy_and_priority_alike(tmp_path, monkeypatch):
    """The gate exempts ONLY Tier0: PRIORITY tasks (exempting them = keep
    dialing the dead API every cycle) and heavy-solo tasks are held too, and
    held BEFORE pre-scripts so no stateful pre runs for a call never made."""
    hb = ("### calendar-sync\n- interval: 1h\n- prompt: cal\n\n"
          "### intention-check\n- interval: 1m\n- prompt: intents\n\n"
          "### deep-research\n- interval: 1h\n- heavy: true\n- prompt: r\n")
    runner = _make_runner(tmp_path, hb)
    now = int(time.time())
    runner.save_state({"__shared_call__": {"consecutive_failures": 3,
                                           "last_failure": now,
                                           "backoff_until": now + 300}})
    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda p, timeout=None: calls.append(p) or "HEARTBEAT_OK")
    runner.run_cycle(force=True)

    assert calls == []  # neither the heavy solo call nor the batch fired
    state = runner.load_state()
    assert state["calendar-sync"]["last_status"] == "ok"
    assert "intention-check" not in state   # PRIORITY held, state untouched
    assert "deep-research" not in state     # heavy solo held, state untouched
    skips = [e for e in _sched_events(runner)
             if e.get("reason") == "shared_call_backoff"]
    assert len(skips) == 1
    assert set(skips[0]["skipped"]) == {"intention-check", "deep-research"}


def test_real_provider_recovery_after_failure_releases_shared_backoff(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### task-a\n- interval: 1h\n- prompt: a\n")
    now = int(time.time())
    runner.save_state({"__shared_call__": {
        "consecutive_failures": 4,
        "last_failure": now - 10,
        "backoff_until": now + 300,
    }})
    monkeypatch.setattr(
        runner, "_provider_recovered_since", lambda _epoch: True,
        raising=False,
    )
    calls = []
    monkeypatch.setattr(
        runner, "claude_call",
        lambda prompt, timeout=None: calls.append(prompt) or "HEARTBEAT_OK",
    )

    runner.run_cycle(force=True)

    assert calls
    assert "__shared_call__" not in runner.load_state()


def test_provider_recovery_requires_newer_real_request_not_canary(
        tmp_path, monkeypatch):
    runner = _make_runner(tmp_path, "### task-a\n- interval: 1h\n- prompt: a\n")
    row = {
        "id": "openai",
        "configured": True,
        "enabled": True,
        "status": "healthy",
        "observation_source": "canary",
        "last_success_epoch": 200,
    }
    monkeypatch.setattr(
        "core.provider_health.snapshot",
        lambda _root: {"providers": [row]},
    )

    assert runner._provider_recovered_since(100) is False
    row["observation_source"] = "real_request"
    assert runner._provider_recovered_since(201) is False
    assert runner._provider_recovered_since(100) is True


def test_shared_call_streak_clears_on_any_reply(tmp_path, monkeypatch):
    """Any non-empty reply proves the call path is alive → key removed."""
    hb = "### task-a\n- interval: 1h\n- prompt: a\n"
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)
    runner.run_cycle(force=True)
    assert runner.load_state()["__shared_call__"]["consecutive_failures"] == 2

    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "HEARTBEAT_OK")
    runner.run_cycle(force=True)
    assert "__shared_call__" not in runner.load_state()


def test_first_success_after_backoff_clears_key(tmp_path, monkeypatch):
    """Once the backoff lapses, the first successful call removes the whole
    __shared_call__ record — no residue to bias the next incident."""
    hb = "### task-a\n- interval: 1h\n- prompt: a\n"
    runner = _make_runner(tmp_path, hb)
    now = int(time.time())
    runner.save_state({"__shared_call__": {"consecutive_failures": 4,
                                           "last_failure": now - 700,
                                           "backoff_until": now - 100}})  # lapsed
    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "明天的会改到三点了")
    out = runner.run_cycle(force=True)
    assert "明天的会改到三点了" in out
    assert "__shared_call__" not in runner.load_state()


def test_heavy_solo_failure_does_not_feed_shared_counter(tmp_path, monkeypatch):
    """A heavy task's solo-call failure is a TASK-level signal (900s budget,
    its own breaker) — it must not count toward the shared streak."""
    runner = _make_runner(
        tmp_path, "### deep-research\n- interval: 1h\n- heavy: true\n- prompt: r\n")
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)

    state = runner.load_state()
    assert "__shared_call__" not in state
    # per-task breaker accounting is retained for heavy solo failures
    assert state["deep-research"]["circuit"]["consecutive_failures"] == 1
    assert state["deep-research"]["last_status"] == "failed"


def test_replay_2026_07_02_outage_zero_circuit_trips(tmp_path, monkeypatch):
    """Replay of run 45b4c417 (2026-07-02 16:13, real sched_events data): the
    DNS outage made the whole batch fail with ConnectionRefused and tripped
    checkin's circuit (skipped circuit_open at 16:59). Same shape re-run
    under REQ-79.1: zero trips, zero circuit_open, shared streak absorbs it."""
    names = ["intention-check", "cross-session-sync", "personal-site",
             "memory-tidy", "checkin", "daily-plan"]
    hb = "\n\n".join(f"### {n}\n- interval: 1h\n- prompt: {n}" for n in names)
    runner = _make_runner(tmp_path, hb)
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)

    state = runner.load_state()
    for n in names:
        assert state[n]["last_status"] == "failed"
        assert state[n].get("circuit", {}).get("consecutive_failures", 0) == 0
        assert state[n].get("circuit", {}).get("disabled_until", 0) == 0
    assert state["__shared_call__"]["consecutive_failures"] == 1
    events = _sched_events(runner)
    assert not [e for e in events if e["event"] == "circuit_tripped"]
    assert not [e for e in events if e.get("reason") == "circuit_open"]


def test_shared_call_and_envelope_parse_recovery_is_independent(
        tmp_path, monkeypatch):
    """The two dunder streaks must never cross-contaminate: a dead call bumps
    ONLY __shared_call__ (→ backoff), a garbled envelope bumps ONLY
    __envelope_parse__ (→ future REQ-79.2 clamp) — and a garbled envelope
    still PROVES the call path is alive, so it clears the shared streak."""
    hb = ("### task-a\n- interval: 1h\n- prompt: a\n\n"
          "### task-b\n- interval: 1h\n- prompt: b\n")
    runner = _make_runner(tmp_path, hb)

    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert state["__shared_call__"]["consecutive_failures"] == 2
    assert "__envelope_parse__" not in state

    monkeypatch.setattr(runner, "claude_call",
                        lambda p, timeout=None: "{this is not json")
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert "__shared_call__" not in state          # call path alive → cleared
    assert state["__envelope_parse__"]["consecutive_failures"] == 1

    # The next cycle fans the broken roster out into isolated calls. Their
    # failures belong to those tasks; they must not recreate a shared-call
    # streak or leave the envelope clamp armed forever.
    monkeypatch.setattr(runner, "claude_call", _failing_call(runner))
    runner.run_cycle(force=True)
    state = runner.load_state()
    assert "__shared_call__" not in state
    assert "__envelope_parse__" not in state
    assert state["task-a"]["last_status"] == "failed"
    assert state["task-b"]["last_status"] == "failed"
