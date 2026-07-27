"""activity_log_pre.sh exit status (2026-07-27 phantom-failure incident).

The script's last command was `[ -n "$conversation_context" ] && echo …`, and
the file has no `set -e` and had no trailing `exit 0`. A 45-minute window with
a calendar event but no user messages therefore printed a correct report and
exited 1. The scheduler recorded `pre_nonzero`, last_success went stale, and
brain-health reported "activity-log 最近一直在失败" — 30 of 551 runs, and one
of the four tasks named in the 2026-07-27 morning brain-dead alert.

The task's contract: producing output is success. These tests pin the exit
status for every combination of signals, because the failure was invisible in
the output itself.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRE = ROOT / "tasks" / "activity_log_pre.sh"


def _run(tmp_path: Path, *, calendar: str | None,
         hour: str = "10") -> subprocess.CompletedProcess:
    """Run the pre-hook with a synthetic memory dir and no session dir.

    No SESSION_DIR means conversation_context is always empty — the exact
    shape that used to exit 1. `hour` is faked through a PATH shim so the
    waking-hours guard does not make the test time-dependent.
    """
    memory = tmp_path / "mem" / "hot"
    memory.mkdir(parents=True)
    if calendar is not None:
        (memory / "calendar_today.md").write_text(calendar, encoding="utf-8")

    shim = tmp_path / "bin"
    shim.mkdir()
    date_shim = shim / "date"
    date_shim.write_text(
        "#!/bin/bash\n"
        f'if [ "$1" = "+%H" ]; then echo {hour}; exit 0; fi\n'
        'exec /bin/date "$@"\n',
        encoding="utf-8",
    )
    date_shim.chmod(0o755)

    return subprocess.run(
        ["bash", str(PRE)],
        capture_output=True, text=True,
        env={
            "JARVIS_DIR": str(tmp_path),
            "MEMORY_DIR": str(tmp_path / "mem"),
            "PATH": f"{shim}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        },
    )


def _calendar_with_event_now() -> str:
    """An event that STARTED inside the 45-minute window.

    The parser only catches events whose start or end falls in the window, and
    it uses the real clock inside python3 — the PATH shim only fakes bash's
    waking-hours `date +%H`. So the fixture is built from real now.
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    start = now.strftime("%H:%M")
    end = (now + timedelta(minutes=30)).strftime("%H:%M")
    return f"## Today\n- {start}-{end} 综合门诊复查\n"


def test_calendar_event_without_conversation_succeeds(tmp_path):
    """The incident: correct output, exit 1."""
    result = _run(tmp_path, calendar=_calendar_with_event_now())

    assert "Calendar events in window" in result.stdout
    assert result.returncode == 0, (
        "a window that produced a report must not be recorded as pre_nonzero")


def test_no_signals_at_all_succeeds_quietly(tmp_path):
    result = _run(tmp_path, calendar=None)

    assert result.stdout.strip() == ""
    assert result.returncode == 0


def test_outside_waking_hours_succeeds(tmp_path):
    result = _run(tmp_path, calendar=_calendar_with_event_now(), hour="03")

    assert result.stdout.strip() == ""
    assert result.returncode == 0


def test_script_parses(tmp_path):
    assert subprocess.run(["bash", "-n", str(PRE)]).returncode == 0
