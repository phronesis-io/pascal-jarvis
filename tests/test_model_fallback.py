"""REQ-77 model-crash / spend-limit graceful fallback."""
from core import model_fallback as mf


def test_model_error_detection():
    assert mf.is_model_error("There's an issue with the selected model (claude-fable-5)")
    assert mf.is_model_error("You've hit your monthly spend limit")
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


def test_spend_limit_jumps_to_cheapest():
    assert mf.fallback_for_stderr("opus", "monthly spend limit") == "haiku"
    assert mf.fallback_for_stderr("sonnet", "spend limit") == "haiku"
    assert mf.fallback_for_stderr("opus", "rate limit exceeded") == "haiku"
    assert mf.fallback_for_stderr("haiku", "spend limit") is None  # already cheapest


def test_transient_error_no_fallback():
    assert mf.fallback_for_stderr("opus", "connection reset") is None
    assert mf.fallback_for_stderr("opus", "") is None


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


def test_bot_sh_wires_reply_closure_and_model_fallback():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.reply_closure" in bot         # REQ-64 wired
    assert "core.model_fallback" in bot        # REQ-77 wired
    assert '"$_cur_model"' in bot              # main path uses degradable model
    assert "core.openai_fallback" in bot       # Claude-limit escape hatch
    assert "CLAUDE_BACKUP_AUTH_TOKEN" in bot   # Claude Code-compatible backup
    assert "ANTHROPIC_BASE_URL" in bot
    assert "Model: ${_answer_provider} ${_answer_model}" in bot
    assert '"Claude primary"' in bot
    assert '"Claude backup"' in bot
    assert '"GPT fallback"' in bot
    assert "_answer_is_error" in bot        # non-empty provider errors still fallback
    assert "_model_error_text" in bot       # stdout errors feed model fallback
    assert "_busy_notice_sent" in bot       # queued follow-ups get one Lark ack
    assert "前一条还在处理" in bot


def test_bot_progress_narration_never_sends_claude_error_text():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    leak = 'lark_reply_text "$message_id" "🔧 $_n"'
    idx = bot.index(leak)
    guard_window = bot[max(0, idx - 220):idx]
    assert '! looks_like_error "$_n"' in guard_window


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
                stdout="You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
                stderr="",
            )
        return CompletedProcess(cmd, 0, stdout="HEARTBEAT_OK", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert calls[0][calls[0].index("--model") + 1] == "opus"
    assert calls[1][calls[1].index("--model") + 1] == "haiku"


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

    monkeypatch.setattr(
        "subprocess.run",
        lambda cmd, **kwargs: CompletedProcess(
            cmd, 1,
            stdout="You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            stderr="",
        ),
    )

    assert runner.claude_call("prompt") == ""


def test_heartbeat_claude_call_uses_backup_provider_after_primary_chain(tmp_path, monkeypatch):
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
            stdout="You've hit your monthly spend limit",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert any(env.get("ANTHROPIC_BASE_URL") == "https://backup.example"
               for _, env in calls)


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

    def fake_openai(payload, api_key, base_url, timeout, user_agent=""):
        openai_calls.append((payload, api_key, base_url, timeout, user_agent))
        return {"output_text": "HEARTBEAT_OK"}

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(openai_fallback, "call_openai", fake_openai)

    assert runner.claude_call("prompt") == "HEARTBEAT_OK"
    assert [c[c.index("--model") + 1] for c in claude_calls] == ["opus", "haiku"]
    assert openai_calls
    assert openai_calls[0][0]["model"] == "gpt-test"
