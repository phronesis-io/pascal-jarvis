"""Tests for core.responsiveness — perceived-latency feedback policy (REQ-59).

The bot.sh activity-stream subshell consumes these (constants via `env`, the
decision table via `decide`), so this module is the tested spec for the
"make the wait feel alive" behavior Pascal asked for after the 2026-06-15
latency investigation (keep opus, surface feedback within ~6s).
"""

from core import responsiveness as r


def test_poll_schedule_first_fast_then_steady():
    # First poll fast so feedback lands within ~6s; subsequent settle to 20s.
    assert r.poll_interval(0) == r.POLL_FIRST_S == 6
    assert r.poll_interval(1) == r.POLL_STEADY_S == 20
    assert r.poll_interval(5) == r.POLL_STEADY_S
    assert r.poll_interval(-1) == r.POLL_FIRST_S  # defensive: treat <=0 as first


def test_decide_truth_table():
    # tools present → always narrate (strongest sign of life)
    assert r.decide_action(has_new_tools=True, thinking_sent=False) == "narrate"
    assert r.decide_action(has_new_tools=True, thinking_sent=True) == "narrate"
    # no tools, nothing said yet → one-time thinking ack
    assert r.decide_action(has_new_tools=False, thinking_sent=False) == "thinking"
    # no tools, already acked → stay quiet (no spam)
    assert r.decide_action(has_new_tools=False, thinking_sent=True) == "none"


def test_ack_text_is_nonempty_and_calm():
    t = r.ack_text()
    assert t and t == r.THINKING_ACK
    assert "💭" in t  # the thinking glyph the user recognizes
    assert t == "💭 收到了，正在想。"
    assert "稍等" not in t
    assert "想得稍微深一点" not in t


def test_cli_env_is_shell_evalable():
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "core.responsiveness", "env"],
                         capture_output=True, text=True).stdout
    assert "JV_POLL_FIRST=6" in out
    assert "JV_POLL_STEADY=20" in out
    assert "JV_THINKING_ACK=" in out
    # must be safely quoted (contains spaces / unicode) so eval can't break
    assert "JV_THINKING_ACK='" in out or 'JV_THINKING_ACK="' in out


def test_cli_decide_matches_module():
    import subprocess, sys
    for tools in ("0", "1"):
        for sent in ("0", "1"):
            out = subprocess.run(
                [sys.executable, "-m", "core.responsiveness", "decide", tools, sent],
                capture_output=True, text=True).stdout.strip()
            assert out == r.decide_action(tools == "1", sent == "1")


def test_bot_sh_consumes_the_module():
    """Wiring guard: bot.sh must pull the policy from the tested module, not
    re-hardcode the constants (so the tuned numbers/text can't silently drift
    from the tested spec)."""
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.responsiveness env" in bot
    assert "$JV_POLL_FIRST" in bot and "$JV_POLL_STEADY" in bot
    assert "$JV_THINKING_ACK" in bot
