"""Tests for core.responsiveness — perceived-latency feedback policy (REQ-59).

The bot.sh activity subshell consumes these constants. The current policy is
one natural progress line after 20 seconds, no internal tool narration, and a
plain-language background handoff at 90 seconds.
"""

from core import responsiveness as r


def test_poll_schedule_first_fast_then_steady():
    assert r.poll_interval(0) == r.POLL_FIRST_S == 10
    assert r.poll_interval(1) == r.POLL_STEADY_S == 10
    assert r.poll_interval(5) == r.POLL_STEADY_S
    assert r.poll_interval(-1) == r.POLL_FIRST_S  # defensive: treat <=0 as first


def test_decide_truth_table():
    assert r.decide_action(19, ack_sent=False) == "none"
    assert r.decide_action(20, ack_sent=False) == "ack"
    assert r.decide_action(90, ack_sent=True) == "none"


def test_progress_feedback_is_bounded_and_plain():
    assert r.ACK_AFTER_S == 20
    assert r.PROMOTE_AFTER_S == 90
    assert r.PROMOTE_IDLE_AFTER_S == 60
    assert r.PROMOTE_HARD_AFTER_S == 600
    assert r.PROGRESS_ACK == "我还在处理，查清楚后马上告诉你。"
    assert "tool" not in r.PROGRESS_ACK.lower()


def test_cli_env_is_shell_evalable():
    import subprocess, sys
    out = subprocess.run([sys.executable, "-m", "core.responsiveness", "env"],
                         capture_output=True, text=True).stdout
    assert "JV_POLL_FIRST=10" in out
    assert "JV_POLL_STEADY=10" in out
    assert "JV_ACK_AFTER=20" in out
    assert "JV_PROMOTE_AFTER=90" in out
    assert "JV_PROMOTE_IDLE_AFTER=60" in out
    assert "JV_PROMOTE_HARD_AFTER=600" in out
    assert "JV_PROGRESS_ACK='我还在处理，查清楚后马上告诉你。'" in out


def test_cli_decide_matches_module():
    import subprocess, sys
    for elapsed, sent in (("0", "0"), ("20", "0"), ("90", "1")):
        out = subprocess.run(
            [sys.executable, "-m", "core.responsiveness", "decide", elapsed, sent],
            capture_output=True, text=True).stdout.strip()
        assert out == r.decide_action(int(elapsed), sent == "1")


def test_bot_sh_consumes_policy_without_exposing_internal_controls():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.responsiveness env" in bot
    assert "$JV_POLL_FIRST" in bot and "$JV_POLL_STEADY" in bot
    assert "$JV_ACK_AFTER" in bot and "$JV_PROMOTE_AFTER" in bot
    assert "core.responsiveness promote" in bot
    assert 'stat -f%m "$session_file"' in bot
    notice = "这件事比预期久，我先放到后台继续做。你可以接着聊，做完我会回来告诉你。"
    assert notice in bot
    assert "（job `$_bg_job_id`）" not in bot
    assert "发 cancel $_bg_job_id" not in bot
    assert 'lark_reply_text "$message_id" "🔧' not in bot
    assert "正在执行的工具调用列表" not in bot


def test_promotion_waits_for_an_idle_window_but_has_a_hard_cap():
    assert r.should_promote(89, 89) is False
    assert r.should_promote(90, 59) is False
    assert r.should_promote(90, 60) is True
    assert r.should_promote(599, 0) is False
    assert r.should_promote(600, 0) is True


def test_cli_promotion_matches_module():
    import subprocess, sys
    out = subprocess.run(
        [sys.executable, "-m", "core.responsiveness", "promote", "120", "30"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out == "none"
    out = subprocess.run(
        [sys.executable, "-m", "core.responsiveness", "promote", "120", "60"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert out == "promote"
