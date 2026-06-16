"""Regression tests for the 2026-06-04 restart-loop incident.

Incident: Pascal said「重启吧」. The Claude handling it ran restart.sh from
WORK_DIR (the repo's parent), which has no `core/` package. restart.sh launched
bot.sh WITHOUT cd'ing to JARVIS_DIR first, so the new bot inherited CWD=WORK_DIR.
Every `python3 -m core.X` helper then died with `ModuleNotFoundError: No module
named 'core'`, taking heartbeat + ef-stream down and spiralling into a restart
loop. Each restart SIGTERM-killed the in-flight Claude (exit 143); the 143 branch
blindly told the user「说继续即可接着干」, and saying 继续 just re-ran the restart.
Chinese「结束」could not stop it (only English stop/cancel was recognized).

These are static-source guards — the fixes live in shell, so we assert on the
script text the way test_daemon_regressions.py asserts on daemon constants.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT_SH = (ROOT / "bot.sh").read_text()
RESTART_SH = (ROOT / "restart.sh").read_text()


def test_bot_anchors_cwd_to_jarvis_dir():
    """RC: bot.sh must cd into JARVIS_DIR so `python3 -m core.X` always resolves.

    Without this, a launch/exec from any other directory brings the bot up broken
    (ModuleNotFoundError: No module named 'core') and starts a restart loop.
    """
    assert 'cd "$JARVIS_DIR"' in BOT_SH, "bot.sh must cd to JARVIS_DIR at startup"
    # The cd must come before the bg helpers are spawned, else it's useless.
    cd_idx = BOT_SH.index('cd "$JARVIS_DIR"')
    helper_idx = BOT_SH.index("python3 -m core.heartbeat_loop")
    assert cd_idx < helper_idx, "cd to JARVIS_DIR must precede `python3 -m core.*` helpers"


def test_restart_sh_cds_before_launching_bot():
    """RC defense-in-depth: restart.sh must cd to JARVIS_DIR before launching bot.sh."""
    launch = 'nohup bash "$JARVIS_DIR/bot.sh"'
    assert launch in RESTART_SH
    prefix = RESTART_SH[: RESTART_SH.index(launch)]
    # The cd must be in start_bot(), i.e. the closest cd before the launch line.
    assert 'cd "$JARVIS_DIR"' in prefix, "restart.sh must cd to JARVIS_DIR before launching bot.sh"


def test_restart_sh_settles_before_clearing_deploy_guard():
    """restart.sh must own the post-start verdict instead of racing daemon checks."""
    assert "settle_bot()" in RESTART_SH
    assert "python3 -m core.components --critical" in RESTART_SH
    assert "Settling bot for ${seconds}s" in RESTART_SH
    full = RESTART_SH[RESTART_SH.index("--full|-f)"):]
    assert full.index("_set_deploy_guard") < full.index("kill_bot")
    assert full.index("start_bot") < full.index("settle_bot")


def test_chinese_stop_words_recognized():
    """User-facing: Chinese「结束/停/停止/取消」must hit the stop/cancel bypass."""
    for word in ("结束", "停", "停止", "取消"):
        assert word in BOT_SH, f"stop bypass must recognize Chinese「{word}」"


def test_143_message_gated_on_watchdog_marker():
    """143 = SIGTERM. Only a genuine 6000s watchdog kill should tell the user 「继续」.

    A restart/external SIGTERM also yields 143; emitting the「说继续」nag there is
    the exact loop the incident produced. The watchdog drops a marker file; the
    user-facing 143 message must be gated on it.
    """
    assert '"${ANSWER_FILE}.watchdog"' in BOT_SH, "watchdog must drop a marker on a real timeout"
    assert "_watchdog_killed" in BOT_SH, "143 branch must distinguish watchdog vs external kill"
    # The「继续」message must be conditioned on the watchdog flag, not on 143 alone.
    nag = "进度已存入 session"
    idx = BOT_SH.index(nag)
    window = BOT_SH[max(0, idx - 400) : idx]
    assert "_watchdog_killed" in window, "the 「继续」 nag must be gated on _watchdog_killed"


def test_lark_listener_reconnects_without_exiting_bot():
    """The Lark long-connection is allowed to drop or be replaced.

    bot.sh must restart the listener instead of letting the foreground pipe end,
    because an EXIT cleanup kills admin.py and takes :3456 down.
    """
    assert "run_lark_listener_once()" in BOT_SH
    assert "Lark listener exited" in BOT_SH
    assert "reconnecting in 5s" in BOT_SH
    assert BOT_SH.index("while true; do\n  _listener_rc=0") > \
        BOT_SH.index("run_lark_listener_once()")


def test_lark_listener_reconnect_captures_nonzero_exit():
    """A listener failure must be logged/reconnected, not fall through ambiguously."""
    assert "run_lark_listener_once || _listener_rc=$?" in BOT_SH
    bad = "while true; do\n  run_lark_listener_once\n  _listener_rc=$?"
    assert bad not in BOT_SH


def test_sigterm_cleanup_exits_bot():
    """SIGTERM must not merely run cleanup and continue the reconnect loop."""
    assert "trap cleanup EXIT" in BOT_SH
    assert "trap 'cleanup; exit 0' INT TERM" in BOT_SH
    assert "_CLEANED_UP" in BOT_SH


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
