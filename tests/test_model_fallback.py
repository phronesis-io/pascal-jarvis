"""REQ-77 model-crash / spend-limit graceful fallback + sticky provider gate."""
import json
import time

from core import model_fallback as mf
from core import provider_health as ph


def test_model_error_detection():
    assert mf.is_model_error("There's an issue with the selected model (claude-fable-5)")
    assert mf.is_model_error("You've hit your monthly spend limit")
    assert mf.is_model_error(
        "You've hit your session limit · resets 6pm (Asia/Shanghai)"
    )
    assert mf.is_model_error("Rate limit exceeded")
    assert mf.is_model_error("too many requests")
    assert mf.is_model_error("invalid model")
    assert not mf.is_model_error("connection reset by peer")
    assert not mf.is_model_error("")


def test_fallback_chain():
    # model-unavailable → one-step degrade
    assert mf.fallback_for_stderr("opus", "issue with the selected model") == "sonnet"
    assert mf.fallback_for_stderr("sonnet", "invalid model") == "haiku"
    assert mf.fallback_for_stderr("haiku", "invalid model") is None  # exhausted


def test_hard_spend_limit_skips_same_provider_retry():
    # 2026-07-07: a monthly spend limit is account-wide — the old opus→haiku
    # detour just burned a second doomed call per reply. No same-provider
    # fallback; is_model_error stays True so callers hit their backup branch.
    assert mf.fallback_for_stderr("opus", "monthly spend limit") is None
    assert mf.fallback_for_stderr("sonnet", "spend limit") is None
    assert mf.fallback_for_stderr("haiku", "spend limit") is None
    assert mf.is_model_error("monthly spend limit")


def test_weekly_usage_limit_is_account_wide():
    error = "You've hit your weekly limit · resets Aug 15 at 3am (Asia/Shanghai)"
    assert mf.limit_reason(error) == "spend_limit"
    assert mf.is_account_limit(error)
    assert mf.is_model_error(error)
    assert mf.fallback_for_stderr("opus", error) is None
    assert mf.is_account_limit("You have reached your weekly limit")
    assert mf.is_account_limit("You've exceeded your daily usage limit")
    assert mf.is_account_limit("Weekly usage limit reached")


def test_descriptive_period_limits_do_not_trip_provider_gate():
    assert not mf.is_account_limit("The API has a daily limit of 100 requests")
    assert not mf.is_account_limit("This plan has a weekly limit of five items")


def test_session_limit_skips_same_provider_and_identifies_reason():
    error = "HTTP 429: You've hit your session limit · resets 6pm (Asia/Shanghai)"
    assert mf.is_account_limit(error)
    assert mf.limit_reason(error) == "session_limit"
    assert mf.fallback_for_stderr("opus", error) is None
    assert mf.limit_reason("You've hit your monthly spend limit") == "spend_limit"
    assert mf.limit_reason("rate limit exceeded") is None


def test_rate_limit_still_jumps_to_cheapest():
    # Transient throttling may be per-model/tier — the haiku detour stays.
    assert mf.fallback_for_stderr("opus", "rate limit exceeded") == "haiku"
    assert mf.fallback_for_stderr("sonnet", "too many requests") == "haiku"
    assert mf.fallback_for_stderr("haiku", "rate limit reached") is None


def test_is_spend_limit_hard_exhaustion_only():
    # The sticky gate trips on THIS predicate — a transient 429 must never
    # divert whole processes to the backup provider for 30 min.
    assert mf.is_spend_limit("You've hit your monthly spend limit")
    assert mf.is_spend_limit("credit balance is too low")
    assert not mf.is_spend_limit("rate limit exceeded")
    assert not mf.is_spend_limit("too many requests")
    assert not mf.is_spend_limit("")


def test_cli_limit_reason():
    import subprocess, sys
    r = subprocess.run(
        [sys.executable, "-m", "core.model_fallback", "--limit-reason"],
        input="You've hit your session limit · resets 6pm (Asia/Shanghai)",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "session_limit"

    r = subprocess.run(
        [sys.executable, "-m", "core.model_fallback", "--limit-reason"],
        input="rate limit exceeded",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 1
    assert r.stdout.strip() == ""


def test_transient_error_no_fallback():
    assert mf.fallback_for_stderr("opus", "connection reset") is None
    assert mf.fallback_for_stderr("opus", "") is None


def test_provider_overload_is_safe_cross_provider_but_not_same_provider():
    error = "API Error: 529 Overloaded: the service is temporarily busy"
    assert mf.is_provider_overload(error) is True
    assert mf.is_preexecution_error(error) is True
    assert mf.is_model_error(error) is False
    assert mf.fallback_for_stderr("opus", error) is None


def test_fable_never_in_chain():
    assert "fable" not in [m.lower() for m in mf.DEGRADE_CHAIN]


def test_cli(tmp_path):
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "opus"],
                       input="issue with the selected model", capture_output=True, text=True)
    assert r.stdout.strip() == "sonnet"


def test_cli_is_model_error_predicate():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "--is-model-error"],
                       input="You've hit your monthly spend limit",
                       capture_output=True, text=True)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "--is-model-error"],
                       input="connection reset by peer",
                       capture_output=True, text=True)
    assert r.returncode == 1


def test_cli_is_preexecution_error_accepts_provider_overload():
    import subprocess, sys
    r = subprocess.run(
        [sys.executable, "-m", "core.model_fallback",
         "--is-preexecution-error"],
        input="API Error: 529 Overloaded",
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0


def test_cli_is_spend_limit_predicate():
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "--is-spend-limit"],
                       input="You've hit your monthly spend limit",
                       capture_output=True, text=True)
    assert r.returncode == 0
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "--is-spend-limit"],
                       input="rate limit exceeded",
                       capture_output=True, text=True)
    assert r.returncode == 1


# ── Sticky provider gate (2026-07-07 spend-limit incident) ──────────────────


def _gate_state(tmp_path):
    return json.loads((tmp_path / "data" / "provider_state.json").read_text())


def _set_gate_state(tmp_path, state):
    (tmp_path / "data" / "provider_state.json").write_text(json.dumps(state))


def _deadletter_rows(tmp_path):
    f = tmp_path / "data" / ".delivery_deadletter.jsonl"
    if not f.exists():
        return []
    return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]


def test_gate_trip_probe_clear_cycle(tmp_path):
    assert mf.gate(tmp_path) == "primary"
    mf.trip("spend_limit", tmp_path)
    assert mf.gate(tmp_path) == "backup"        # probe stamp is fresh
    # Probe due: exactly ONE caller wins the election (the stamp is rewritten
    # inside gate), concurrent callers keep getting backup.
    st = _gate_state(tmp_path)
    st["last_primary_probe"] = time.time() - mf.PROBE_INTERVAL_S - 1
    _set_gate_state(tmp_path, st)
    assert mf.gate(tmp_path) == "probe"
    assert mf.gate(tmp_path) == "backup"
    # Auxiliary callers (background jobs, idle judge) never win the election —
    # they can't clear() on success, so a won slot would never reopen primary.
    st = _gate_state(tmp_path)
    st["last_primary_probe"] = 0
    _set_gate_state(tmp_path, st)
    assert mf.gate(tmp_path, probe=False) == "backup"
    assert mf.gate(tmp_path) == "probe"         # slot still available for main
    mf.clear(tmp_path)
    assert mf.gate(tmp_path) == "primary"


def test_failed_probe_retrip_rearms_timer(tmp_path):
    mf.trip("spend_limit", tmp_path)
    since0 = _gate_state(tmp_path)["spend_limit_since"]
    st = _gate_state(tmp_path)
    st["last_primary_probe"] = time.time() - mf.PROBE_INTERVAL_S - 1
    _set_gate_state(tmp_path, st)
    assert mf.gate(tmp_path) == "probe"
    mf.trip("spend_limit", tmp_path)            # elected probe failed again
    assert mf.gate(tmp_path) == "backup"
    # the incident start survives re-trips (it anchors the page's 自...起)
    assert _gate_state(tmp_path)["spend_limit_since"] == since0


def test_trip_pages_pascal_once_per_cooldown(tmp_path):
    mf.trip("spend_limit", tmp_path)
    mf.trip("spend_limit", tmp_path)            # within 6h cooldown — no re-page
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "provider_failover"
    assert "备用通道" in rows[0]["detail"]      # plain Chinese, no provider names
    # clear after a paged trip → one recovery note
    mf.clear(tmp_path)
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 2
    assert "恢复" in rows[1]["detail"]
    # re-trip inside the cooldown: gate re-arms but Pascal is NOT paged again
    # (and the later clear stays silent too — no lone 恢复了 for a page he
    # never saw)
    mf.trip("spend_limit", tmp_path)
    assert mf.gate(tmp_path) == "backup"
    mf.clear(tmp_path)
    assert len(_deadletter_rows(tmp_path)) == 2


def test_session_limit_trip_uses_temporary_recovery_copy(tmp_path):
    mf.trip("session_limit", tmp_path)
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 1
    assert "本次会话额度" in rows[0]["detail"]
    assert "自动探测并切回" in rows[0]["detail"]
    assert "本月额度用完" not in rows[0]["detail"]


def test_trip_pages_once_per_episode_not_every_6h(tmp_path):
    """2026-07-08 red-team fix: one ongoing outage = ONE page. The old
    6h-cadence check re-paged Pascal ~4×/day for the whole month-end spend
    limit (every failed 30-min probe re-trips). Same-episode re-trips never
    re-page however old the first page is; a genuinely NEW episode (clear +
    quiet period) pages exactly once again."""
    mf.trip("spend_limit", tmp_path)
    assert len(_deadletter_rows(tmp_path)) == 1
    # +7h into the SAME episode: the elected prober fails again → re-trip.
    # Cooldown has expired, but the episode is unchanged — NO re-page.
    t0 = time.time() - 7 * 3600
    st = _gate_state(tmp_path)
    st["spend_limit_since"] = t0
    st["notified_at"] = t0
    _set_gate_state(tmp_path, st)
    mf.trip("spend_limit", tmp_path)
    assert len(_deadletter_rows(tmp_path)) == 1
    assert _gate_state(tmp_path)["notified_at"] == t0  # stamp untouched
    # Recovery pages 恢复了 (episode was paged) …
    mf.clear(tmp_path)
    assert len(_deadletter_rows(tmp_path)) == 2
    # … and a NEW episode after a quiet period pages once again.
    mf.trip("spend_limit", tmp_path)
    rows = _deadletter_rows(tmp_path)
    assert len(rows) == 3
    assert "备用通道" in rows[2]["detail"]
    # But NOT a fourth time on the next failed probe of that episode.
    mf.trip("spend_limit", tmp_path)
    assert len(_deadletter_rows(tmp_path)) == 3


def test_cli_gate_verbs(tmp_path):
    import os
    import subprocess, sys
    env = {**os.environ, "JARVIS_DIR": str(tmp_path)}
    run = lambda *args: subprocess.run(
        [sys.executable, "-m", "core.model_fallback", *args],
        capture_output=True, text=True, env=env)
    assert run("--gate").stdout.strip() == "primary"
    assert run("--trip", "spend_limit").returncode == 0
    assert run("--gate").stdout.strip() == "backup"
    assert run("--gate", "no-probe").stdout.strip() == "backup"
    assert run("--clear").returncode == 0
    assert run("--gate").stdout.strip() == "primary"


def test_bot_sh_wires_reply_closure_and_model_fallback():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.reply_closure" in bot         # REQ-64 wired
    assert "core.model_fallback" in bot        # REQ-77 wired
    assert "--limit-reason" in bot             # session/spend limit reason preserved
    assert '"$_cur_model"' in bot              # main path uses degradable model
    assert "core.openai_fallback" in bot       # Claude-limit escape hatch
    assert "core.codex_fallback" in bot        # ChatGPT-login Codex escape hatch
    assert bot.count("run_codex_locked") == 3  # definition + preferred + fallback
    assert '"$_lock_file" "$_codex_pid" "$_lock_token"; then' in bot
    assert "openai_fallback_flags=(--no-tools)" in bot
    assert '${openai_fallback_flags[@]+"${openai_fallback_flags[@]}"}' in bot
    assert "CLAUDE_BACKUP_AUTH_TOKEN" in bot   # Claude Code-compatible backup
    assert "ANTHROPIC_BASE_URL" in bot
    # 7/7: English "Model: Claude backup opus" footer was jargon in Pascal's
    # chat — plain-Chinese caption on non-primary replies only, silent on
    # primary.
    assert "Model: ${_answer_provider}" not in bot
    assert "（备用通道）" in bot
    assert "（Codex 接手）" in bot
    assert "（GPT 兜底）" in bot


def test_codex_locked_runner_publishes_a_killable_pid(tmp_path):
    import os
    import signal
    import subprocess
    from pathlib import Path

    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    start = bot.index("run_codex_locked() {")
    end = bot.index("\n}\n\n# ── Message Handler", start) + 3
    function = bot[start:end]
    lock = tmp_path / "session.lock"
    lock.write_text("acquiring test-token", encoding="utf-8")
    script = tmp_path / "codex-lock-test.sh"
    script.write_text(
        f'source "{Path(__file__).parent.parent / "scripts" / "process_lifecycle.sh"}"\n' +
        "process_start_token() { printf 'test-start\\n'; }\n" +
        "python3() { exec sleep 30; }\n" + function + "\n" +
        'run_codex_locked "hello" "conv" "system" "model" "30" ' +
        f'"{tmp_path}" "" "{lock}" "test-token" "{tmp_path / "answer"}"\n',
        encoding="utf-8",
    )
    process = subprocess.Popen(["bash", str(script)])
    child_pid = None
    for _ in range(100):
        fields = lock.read_text(encoding="utf-8").rstrip("\n").split("\t")
        if len(fields) == 3 and fields[0].isdigit():
            child_pid = int(fields[0])
            assert fields[1]
            assert fields[2] == "test-token"
            break
        time.sleep(0.02)
    assert child_pid is not None
    os.kill(child_pid, signal.SIGTERM)
    lock.unlink(missing_ok=True)
    assert process.wait(timeout=5) == 143


def test_codex_locked_runner_stops_when_identity_receipt_cannot_publish(
        tmp_path):
    import subprocess
    from pathlib import Path

    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    start = bot.index("run_codex_locked() {")
    end = bot.index("\n}\n\n# ── Message Handler", start) + 3
    function = bot[start:end]
    lock = tmp_path / "session.lock"
    lock.write_text("acquiring test-token", encoding="utf-8")
    script = tmp_path / "codex-lock-fail-closed.sh"
    script.write_text(
        f'source "{Path(__file__).parent.parent / "scripts" / "process_lifecycle.sh"}"\n'
        "process_start_token() { return 1; }\n"
        "python3() { exec sleep 30; }\n" + function + "\n"
        'run_codex_locked "hello" "conv" "system" "model" "30" '
        f'"{tmp_path}" "" "{lock}" "test-token" "{tmp_path / "answer"}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["/bin/bash", str(script)], check=False, timeout=5,
    )

    assert result.returncode == 74
    assert lock.read_text(encoding="utf-8") == "acquiring test-token"


def test_bot_sh_scopes_complete_backup_config_and_reports_chain_failure():
    from pathlib import Path
    import re
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()

    for name in (
        "CLAUDE_BACKUP_MODEL",
        "CLAUDE_BACKUP2_ENABLED",
        "CLAUDE_BACKUP2_BASE_URL",
        "CLAUDE_BACKUP2_MODEL",
        "BACKUP_MAX_SESSION_SIZE",
        "BACKUP_MAX_MEMORY_CHARS",
        "CODEX_FALLBACK_ENABLED",
        "CODEX_FALLBACK_MODEL",
        "CODEX_FALLBACK_TIMEOUT",
    ):
        assert re.search(rf"^export [^\n]*\b{name}\b", bot, re.MULTILINE)
    for secret in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_BACKUP_AUTH_TOKEN",
        "CLAUDE_BACKUP2_AUTH_TOKEN",
        "OPENAI_API_KEY",
    ):
        assert not re.search(
            rf"^export (?!-n\b)[^\n]*\b{secret}\b", bot, re.MULTILINE
        )
    assert "export -n ANTHROPIC_API_KEY CLAUDE_BACKUP_AUTH_TOKEN" in bot
    assert bot.count("exec_model_worker python3 -m core.heartbeat_loop") == 2
    assert bot.count("with_primary_model_credential claude -p") == 2
    assert bot.count("with_openai_credential env") == 2
    assert "回复被安全过滤器拦截" not in bot
    assert "本次操作没有执行成功" in bot
    assert 'log_warn "Session compact failed' in bot
    assert 'log_info "Session compact completed' in bot
    assert '"Claude primary"' in bot
    assert '"Claude backup"' in bot
    assert '"GPT fallback"' in bot
    assert "_answer_is_error" in bot        # non-empty provider errors still fallback
    assert "_model_error_text" in bot       # stdout errors feed model fallback
    assert "_busy_notice_sent" in bot       # queued follow-ups get one Lark ack
    assert "前一条还在处理" in bot


def test_bot_shell_credential_boundaries_execute_on_macos_bash(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    start = bot.index("# Provider credentials remain shell-private")
    end = bot.index("# Sidecar event backend", start)
    boundary = bot[start:end]
    probe = (
        'printf "%s|%s|%s|%s\\n" '
        '"${ANTHROPIC_API_KEY-unset}" '
        '"${CLAUDE_BACKUP_AUTH_TOKEN-unset}" '
        '"${CLAUDE_BACKUP2_AUTH_TOKEN-unset}" '
        '"${OPENAI_API_KEY-unset}"'
    )
    script = tmp_path / "credential-boundary.sh"
    script.write_text(
        "set -u\n"
        + boundary
        + f'printf "ambient="; /bin/bash -c \'{probe}\'\n'
        + f'printf "primary="; with_primary_model_credential /bin/bash -c \'{probe}\'\n'
        + f'printf "openai="; with_openai_credential /bin/bash -c \'{probe}\'\n'
        + f'printf "router="; (exec_model_worker /bin/bash -c \'{probe}\')\n',
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "ANTHROPIC_API_KEY": "primary-secret",
        "CLAUDE_BACKUP_AUTH_TOKEN": "backup1-secret",
        "CLAUDE_BACKUP2_AUTH_TOKEN": "backup2-secret",
        "OPENAI_API_KEY": "openai-secret",
    }

    result = subprocess.run(
        ["/bin/bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "ambient=unset|unset|unset|unset",
        "primary=primary-secret|unset|unset|unset",
        "openai=unset|unset|unset|openai-secret",
        "router=primary-secret|backup1-secret|backup2-secret|openai-secret",
    ]


def test_bot_backup2_credentials_are_scoped_to_one_message():
    from pathlib import Path

    bot = (Path(__file__).parent.parent / "bot.sh").read_text()

    assert (
        'local _claude_backup_token="${CLAUDE_BACKUP_AUTH_TOKEN:-}"'
        in bot
    )
    assert (
        'local _claude_backup_base_url="${CLAUDE_BACKUP_BASE_URL:-}"'
        in bot
    )
    assert (
        'CLAUDE_BACKUP_AUTH_TOKEN="$CLAUDE_BACKUP2_AUTH_TOKEN"'
        not in bot
    )
    assert (
        'CLAUDE_BACKUP_BASE_URL="$CLAUDE_BACKUP2_BASE_URL"'
        not in bot
    )
    assert 'ANTHROPIC_AUTH_TOKEN="$_claude_backup_token"' in bot
    assert 'ANTHROPIC_BASE_URL="$_claude_backup_base_url"' in bot


def test_bot_sh_wires_sticky_provider_gate():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.model_fallback --gate" in bot          # attempt 1 consults gate
    assert "core.model_fallback --limit-reason" in bot  # trip on hard account limit
    assert "core.model_fallback --trip" in bot
    assert "core.model_fallback --clear" in bot         # probe success reopens
    assert "core.model_fallback --is-preexecution-error" in bot
    assert "core.provider_health classify" in bot
    assert '--context "$_route_context" --gate "$_provider_gate"' in bot
    assert "--gate no-probe" in bot                     # background jobs follow flag
    assert "python3 -m core.aux_model" in bot
    assert "--allow-tools --timeout 6000" in bot
    # 2026-07-08 red-team fix: the backup-tried OpenAI arm needs a COMPLETED
    # backup failure in this run (the gate presets _claude_backup_tried=1
    # before attempt 1) — one transient relay blip must retry backup, not
    # jump to a context-free GPT reply.
    assert '[ "$_claude_backup_tried" -eq 1 ]' in bot
    assert '[ "$_claude_backup2_tried" -eq 1 ]' in bot
    assert '&& [ "$_attempt" -ge 2 ]' in bot
    assert 'local _attempt_sequence="1 2 3 4 5"' in bot
    assert "for _attempt in $_attempt_sequence; do" in bot
    assert 'none) _health_routed=1; _no_healthy_provider=1' in bot
    assert '[ "$_health_routed" -eq 1 ]' in bot
    fallback_call = bot.index(
        '_fallback=$(printf \'%s\' "$_model_error_text"'
    )
    backup_guard = bot.rfind(
        'if [ "$_use_claude_backup" -eq 0 ]; then',
        0,
        fallback_call,
    )
    assert backup_guard >= 0
    assert bot.index("\n      fi", fallback_call) > fallback_call


def test_bot_progress_never_exposes_tool_or_claude_error_narration():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert 'lark_reply_text "$message_id" "🔧' not in bot
    assert "正在执行的工具调用列表" not in bot


def test_heartbeat_claude_call_retries_fallback_and_never_returns_error_stdout(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core.heartbeat import HeartbeatRunner

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="opus",
        idle_judge=False,
    )
    runner._claude_bin = "claude"

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        model = cmd[cmd.index("--model") + 1]
        if model == "opus":
            return CompletedProcess(
                cmd, 1,
                stdout="There's an issue with the selected model (claude-fable-5)",
                stderr="",
            )
        return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert calls[0][calls[0].index("--model") + 1] == "opus"
    assert calls[1][calls[1].index("--model") + 1] == "sonnet"


def test_heartbeat_claude_call_suppresses_nonfallback_error_stdout(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core.heartbeat import HeartbeatRunner

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="haiku",
        idle_judge=False,
    )
    runner._claude_bin = "claude"
    # Deterministic: no backup/OpenAI escape hatches in this test's env.
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd, 1,
            stdout="You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            stderr="",
        ),
    )

    assert runner.claude_call("prompt") == ""


def test_heartbeat_claude_call_uses_backup_provider_after_session_limit(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core.heartbeat import HeartbeatRunner

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="opus",
        idle_judge=False,
    )
    runner._claude_bin = "claude"
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")

    calls = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        calls.append((cmd, env))
        if env.get("ANTHROPIC_AUTH_TOKEN") == "backup-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        return CompletedProcess(
            cmd, 1,
            stdout="HTTP 429: You've hit your session limit · resets 6pm (Asia/Shanghai)",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert any(env.get("ANTHROPIC_BASE_URL") == "https://backup.example"
               for _, env in calls)
    # Session limit is account-wide: exactly ONE doomed primary call (no haiku
    # detour), and the sticky gate is tripped for every other process.
    primary_models = [cmd[cmd.index("--model") + 1] for cmd, env in calls
                      if env.get("ANTHROPIC_AUTH_TOKEN") != "backup-token"]
    assert primary_models == ["opus"]
    assert mf.gate(tmp_path) == "backup"


def test_heartbeat_claude_call_reaches_backup2_after_backup1(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.example")
    monkeypatch.setenv("CLAUDE_BACKUP_MODEL", "backup1-model")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_MODEL", "backup2-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        token = env.get("ANTHROPIC_AUTH_TOKEN", "")
        calls.append((cmd, token, env.get("ANTHROPIC_BASE_URL", "")))
        if token == "backup2-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        return CompletedProcess(
            cmd, 1, stdout="You've hit your monthly spend limit", stderr=""
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert [token for _, token, _ in calls] == [
        "",
        "backup1-token",
        "backup2-token",
    ]
    assert calls[-1][0][calls[-1][0].index("--model") + 1] == "backup2-model"
    assert calls[-1][2] == "https://backup2.example"
    assert runner.last_provider == "Claude backup2"


def test_heartbeat_reaches_backup2_after_backup1_transport_error(
    tmp_path, monkeypatch
):
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup1-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup1.example")
    monkeypatch.setenv("CLAUDE_BACKUP_MODEL", "backup1-model")
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_MODEL", "backup2-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        token = (kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", "")
        calls.append(token)
        if token == "backup2-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        if token == "backup1-token":
            return CompletedProcess(
                cmd, 1, stdout="", stderr="connection reset by relay"
            )
        return CompletedProcess(
            cmd, 1, stdout="You've hit your monthly spend limit", stderr=""
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt", allow_tools=False) == "HEARTBEAT_OK"
    assert calls == ["", "backup1-token", "backup2-token"]
    backup1 = next(
        row for row in ph.snapshot(tmp_path)["providers"]
        if row["id"] == "backup1"
    )
    assert backup1["detail"] == "real request: network_error"


def test_heartbeat_primary_dns_failure_reaches_backup(tmp_path, monkeypatch):
    """A primary ENOTFOUND is channel trouble, not a terminal model response."""
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        token = (kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", "")
        calls.append(token)
        if token == "backup-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        return CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(
                "API Error: Can't reach the API server — check your internet "
                "or DNS (ENOTFOUND)"
            ),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt", allow_tools=False) == "HEARTBEAT_OK"
    assert calls == ["", "backup-token"]
    primary = next(
        row for row in ph.snapshot(tmp_path)["providers"]
        if row["id"] == "primary"
    )
    assert primary["detail"] == "real request: network_error"


def test_heartbeat_primary_529_reaches_backup_even_with_tools(
        tmp_path, monkeypatch):
    """529 is an admission failure, so no tool ran and replay is safe."""
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []

    def fake_run(cmd, **kwargs):
        token = (kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", "")
        calls.append(token)
        if token == "backup-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        return CompletedProcess(
            cmd, 1, stdout="", stderr="API Error: 529 Overloaded"
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt", allow_tools=True) == "HEARTBEAT_OK"
    assert calls == ["", "backup-token"]
    primary = next(
        row for row in ph.snapshot(tmp_path)["providers"]
        if row["id"] == "primary"
    )
    assert primary["detail"] == "real request: server_overloaded"


def test_heartbeat_primary_dns_failure_reaches_gpt_without_relay(
        tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core import openai_fallback

    runner = _gate_runner(tmp_path)
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd, 1, stdout="", stderr="getaddrinfo failed (ENOTFOUND)"),
    )
    calls = []
    monkeypatch.setattr(
        openai_fallback,
        "call_openai",
        lambda *_args, **_kwargs: calls.append("gpt") or {
            "output": [{
                "content": [{"type": "output_text", "text": "HEARTBEAT_OK"}]
            }]
        },
    )

    assert runner.claude_call("prompt", allow_tools=False) == "HEARTBEAT_OK"
    assert calls == ["gpt"]


def test_heartbeat_tool_capable_transport_failure_does_not_replay(
        tmp_path, monkeypatch):
    """A request that may have used tools cannot be replayed on another model."""
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", ""))
        return CompletedProcess(
            cmd, 1, stdout="", stderr="getaddrinfo failed (ENOTFOUND)")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt", allow_tools=True) == ""
    assert calls == [""]


def test_heartbeat_nontransport_request_error_does_not_fan_out(
        tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((kwargs.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN", ""))
        return CompletedProcess(
            cmd, 1, stdout="", stderr="invalid_request: prompt rejected"),

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == ""
    assert calls == [""]


def test_heartbeat_claude_call_uses_openai_after_claude_chain_exhausted(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core.heartbeat import HeartbeatRunner
    from core import openai_fallback

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="opus",
        idle_judge=False,
    )
    runner._claude_bin = "claude"
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "gpt-test")

    claude_calls = []

    def fake_run(cmd, **kwargs):
        claude_calls.append(cmd)
        return CompletedProcess(
            cmd, 1,
            stdout="You've hit your monthly spend limit",
            stderr="",
        )

    openai_calls = []

    def fake_openai(system_prompt, user_input, model, max_output_tokens,
                    api_key, base_url, timeout, user_agent=""):
        openai_calls.append((model, system_prompt, user_input))
        return "HEARTBEAT_OK"

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(openai_fallback, "run_agentic", fake_openai)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    # No haiku detour on spend limit: one doomed opus call, then straight past
    # the (unconfigured) backup tier to OpenAI.
    assert [c[c.index("--model") + 1] for c in claude_calls] == ["opus"]
    assert openai_calls
    assert openai_calls[0][0] == "gpt-test"


def test_heartbeat_weekly_limit_reaches_openai_fallback(tmp_path, monkeypatch):
    from subprocess import CompletedProcess
    from core import openai_fallback

    runner = _gate_runner(tmp_path)
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "gpt-test")
    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd,
            1,
            stdout=(
                "You've hit your weekly limit · resets Aug 15 at 3am "
                "(Asia/Shanghai)"
            ),
            stderr="",
        ),
    )
    calls = []

    def fake_openai(system_prompt, user_input, model, max_output_tokens,
                    api_key, base_url, timeout, user_agent=""):
        calls.append(model)
        return "HEARTBEAT_OK"

    monkeypatch.setattr(openai_fallback, "run_agentic", fake_openai)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert calls == ["gpt-test"]
    assert runner.last_provider == "GPT fallback"


def _gate_runner(tmp_path):
    from core.heartbeat import HeartbeatRunner
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    heartbeat_file = tmp_path / "HEARTBEAT.md"
    heartbeat_file.write_text("### t\n- interval: 1h\n- prompt: hi\n")
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat_file,
        state_file=tmp_path / "state.json",
        memory_dir=memory_dir,
        model="opus",
        idle_judge=False,
    )
    runner._claude_bin = "claude"
    return runner


def test_heartbeat_claude_call_starts_on_backup_when_gate_tripped(tmp_path, monkeypatch):
    """Tripped gate ⇒ ZERO primary probes — the 7/7 outage burned 2 doomed
    subprocess spawns per cycle re-discovering the same account-wide limit."""
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    mf.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")

    calls = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        calls.append((cmd, env))
        return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert len(calls) == 1
    assert calls[0][1].get("ANTHROPIC_AUTH_TOKEN") == "backup-token"
    assert calls[0][1].get("ANTHROPIC_BASE_URL") == "https://backup.example"
    # backup success is NOT proof primary recovered — flag must stay
    assert mf.gate(tmp_path, probe=False) == "backup"


def test_heartbeat_starts_on_backup2_when_gate_tripped_and_backup1_missing(
    tmp_path, monkeypatch,
):
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    mf.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.delenv("CLAUDE_BACKUP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_BACKUP_BASE_URL", raising=False)
    monkeypatch.setenv("CLAUDE_BACKUP2_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP2_AUTH_TOKEN", "backup2-token")
    monkeypatch.setenv("CLAUDE_BACKUP2_BASE_URL", "https://backup2.example")
    monkeypatch.setenv("CLAUDE_BACKUP2_MODEL", "backup2-model")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env") or {}))
        return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert len(calls) == 1
    command, env = calls[0]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "backup2-token"
    assert env["ANTHROPIC_BASE_URL"] == "https://backup2.example"
    assert command[command.index("--model") + 1] == "backup2-model"
    assert runner.last_provider == "Claude backup2"


def test_heartbeat_does_not_use_full_request_to_probe_primary(
        tmp_path, monkeypatch):
    """A tripped gate stays on backup until the bounded canary clears it."""
    import json as _json
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    mf.trip("spend_limit", tmp_path)
    state_path = tmp_path / "data" / "provider_state.json"
    st = _json.loads(state_path.read_text())
    st["last_primary_probe"] = time.time() - mf.PROBE_INTERVAL_S - 1
    state_path.write_text(_json.dumps(st))
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert len(calls) == 1
    assert calls[0][1]["ANTHROPIC_AUTH_TOKEN"] == "backup-token"
    assert mf.gate(tmp_path, probe=False) == "backup"


def test_heartbeat_backup_auth_error_still_reaches_openai(tmp_path, monkeypatch):
    """An auth error from the backup relay matches no model-error signature
    (kept tight after the red-team fix) — but with primary known-dead it must
    still fall through to the OpenAI tier, not dead-end silently."""
    from subprocess import CompletedProcess
    from core import openai_fallback

    runner = _gate_runner(tmp_path)
    mf.trip("spend_limit", tmp_path)
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")
    monkeypatch.setenv("OPENAI_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_FALLBACK_MODEL", "gpt-test")

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd, 1, stdout="",
            stderr="authentication_error: invalid x-api-key",
        ),
    )
    openai_calls = []

    def fake_openai(system_prompt, user_input, model, max_output_tokens,
                    api_key, base_url, timeout, user_agent=""):
        openai_calls.append(model)
        return "HEARTBEAT_OK"

    monkeypatch.setattr(openai_fallback, "run_agentic", fake_openai)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert openai_calls == ["gpt-test"]


def test_heartbeat_tripped_gate_starts_on_backup_without_production_probe(
        tmp_path, monkeypatch):
    """A due recovery check must not prepend a full-context primary call."""
    from subprocess import CompletedProcess

    runner = _gate_runner(tmp_path)
    mf.trip("spend_limit", tmp_path)
    state_path = tmp_path / "data" / "provider_state.json"
    st = json.loads(state_path.read_text())
    st["last_primary_probe"] = time.time() - mf.PROBE_INTERVAL_S - 1
    state_path.write_text(json.dumps(st))
    monkeypatch.setenv("CLAUDE_BACKUP_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_BACKUP_AUTH_TOKEN", "backup-token")
    monkeypatch.setenv("CLAUDE_BACKUP_BASE_URL", "https://backup.example")

    calls = []

    def fake_run(cmd, **kwargs):
        env = kwargs.get("env") or {}
        calls.append(env)
        if env.get("ANTHROPIC_AUTH_TOKEN") == "backup-token":
            return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")
        return CompletedProcess(
            cmd, 1, stdout="",
            stderr="Failed to authenticate. API Error: 403 Request not allowed",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert len(calls) == 1
    assert calls[0].get("ANTHROPIC_AUTH_TOKEN") == "backup-token"
    # The canary owns recovery; a backup success changes nothing.
    assert mf.gate(tmp_path, probe=False) == "backup"
