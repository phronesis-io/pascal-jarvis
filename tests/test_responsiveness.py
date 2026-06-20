"""Tests for core.responsiveness — perceived-latency feedback policy (REQ-59).

The bot.sh activity-stream subshell consumes these (constants via `env`, the
decision table via `decide`), so this module is the tested spec for the
"make the wait feel alive" behavior Pascal asked for after the 2026-06-15
latency investigation (keep opus, surface feedback within ~6s).

2026-06-20: the textual "💭 收到了，正在想" ack was removed — it was redundant
with the instant Typing reaction and felt annoying. Pure thinking now stays
silent; only real tool activity is narrated.
"""

from core import responsiveness as r


def test_poll_schedule_first_fast_then_steady():
    # First poll fast so feedback lands within ~6s; subsequent settle to 20s.
    assert r.poll_interval(0) == r.POLL_FIRST_S == 6
    assert r.poll_interval(1) == r.POLL_STEADY_S == 20
    assert r.poll_interval(5) == r.POLL_STEADY_S
    assert r.poll_interval(-1) == r.POLL_FIRST_S  # defensive: treat <=0 as first


def test_decide_truth_table():
    # tools present → narrate (the only textual sign of life we emit)
    assert r.decide_action(has_new_tools=True) == "narrate"
    # no tools → stay silent; the instant Typing reaction covers "alive"
    assert r.decide_action(has_new_tools=False) == "none"


def test_no_thinking_ack_surface():
    # The thinking ack was removed entirely — guard against it creeping back.
    assert not hasattr(r, "THINKING_ACK")
    assert not hasattr(r, "ack_text")


def test_cli_env_is_shell_evalable():
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "core.responsiveness", "env"],
                         capture_output=True, text=True).stdout
    assert "JV_POLL_FIRST=6" in out
    assert "JV_POLL_STEADY=20" in out
    # the thinking ack var must be gone
    assert "JV_THINKING_ACK" not in out


def test_cli_decide_matches_module():
    import subprocess, sys
    for tools in ("0", "1"):
        out = subprocess.run(
            [sys.executable, "-m", "core.responsiveness", "decide", tools],
            capture_output=True, text=True).stdout.strip()
        assert out == r.decide_action(tools == "1")


def test_bot_sh_consumes_the_module_and_drops_ack():
    """Wiring guard: bot.sh pulls polling consts from the tested module, and the
    annoying thinking ack is gone (no var, no literal)."""
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.responsiveness env" in bot
    assert "$JV_POLL_FIRST" in bot and "$JV_POLL_STEADY" in bot
    # regression guard: the removed ack must not return
    assert "JV_THINKING_ACK" not in bot
    assert "收到了，正在想" not in bot
