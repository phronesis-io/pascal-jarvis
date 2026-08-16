"""REQ-83 — calendar sync must distinguish "no events" from "fetch failed".

6/29-30 the user token lapsed ×7 and `2>/dev/null` rendered every failure as
"(no events)"; worse, the snapshot was rewritten with a FRESH synced
timestamp, laundering stale data as current. Contract pinned here:

  - fetch failure → pre exits nonzero with EMPTY stdout (post never runs),
    the previous snapshot survives, annotated "数据截至 X" (评审红线);
  - repeated failures → exactly one annotation (idempotent, no stacking);
  - recovery → snapshot rewritten, annotation gone;
  - genuinely empty agenda → normal "(no events)" success path.

A fake lark-cli on PATH drives both modes; everything runs in tmp_path.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRE = ROOT / "tasks" / "calendar_sync_pre.sh"

SNAPSHOT = """---
name: 今日日程
description: Lark 日历自动同步，含今天和明天的日程
type: reference
---

# Calendar (synced 2026-07-01 10:00)

Today (2026-07-01 Wednesday):
  09:30-10:30  重要会议 @ 会议室A
"""


def _fake_lark_cli(tmp_path: Path, mode: str) -> None:
    """mode file read at call time so one test can flip fail → ok."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "lark_mode").write_text(mode, encoding="utf-8")
    fake = tmp_path / "bin" / "lark-cli"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f"echo call >> '{tmp_path}/lark_calls'\n"
        f"mode=$(cat '{tmp_path}/lark_mode')\n"
        "if [ \"$mode\" = fail ]; then\n"
        "  echo 'Error: user access token expired, please re-auth' >&2\n"
        "  exit 1\n"
        "fi\n"
        "echo '[]'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_pre(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "JARVIS_DIR": str(tmp_path / "jarvis"),
        "MEMORY_DIR": str(tmp_path / "mem"),
    }
    (tmp_path / "jarvis").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(PRE)], capture_output=True,
                          text=True, env=env, timeout=120)


def _seed_snapshot(tmp_path: Path) -> Path:
    hot = tmp_path / "mem" / "hot"
    hot.mkdir(parents=True, exist_ok=True)
    cal = hot / "calendar_today.md"
    cal.write_text(SNAPSHOT, encoding="utf-8")
    return cal


def test_fetch_failure_nonzero_empty_stdout_keeps_annotated_snapshot(tmp_path):
    cal = _seed_snapshot(tmp_path)
    _fake_lark_cli(tmp_path, "fail")

    result = _run_pre(tmp_path)

    assert result.returncode != 0, "failed fetch must exit nonzero"
    assert result.stdout.strip() == "", (
        "failed fetch must produce EMPTY stdout so the post never rewrites "
        f"the snapshot with a fresh timestamp; got: {result.stdout!r}"
    )
    assert "lark-cli auth login" in result.stderr
    assert (tmp_path / "lark_calls").read_text().splitlines() == ["call"], (
        "an identity-level failure applies to the whole calendar; the sync "
        "must stop instead of issuing the other 29 doomed requests"
    )

    text = cal.read_text(encoding="utf-8")
    assert "重要会议" in text, "previous snapshot must survive a failed sync"
    assert "synced 2026-07-01 10:00" in text, "synced timestamp must NOT refresh"
    assert "> ⚠️ 日历同步失败" in text
    assert "截至 2026-07-01 10:00" in text, "annotation must cite last GOOD sync"
    # raw cache untouched → even a rogue post run has nothing new to launder
    assert not (tmp_path / "mem" / "system" / ".calendar_raw_output.txt").exists()


def test_repeated_failures_annotate_exactly_once(tmp_path):
    cal = _seed_snapshot(tmp_path)
    _fake_lark_cli(tmp_path, "fail")

    first = _run_pre(tmp_path)
    second = _run_pre(tmp_path)
    assert first.returncode != 0 and second.returncode != 0

    text = cal.read_text(encoding="utf-8")
    assert text.count("> ⚠️ 日历同步失败") == 1, "annotation must not stack"
    assert "重要会议" in text


def test_recovery_rewrites_snapshot_and_clears_annotation(tmp_path):
    cal = _seed_snapshot(tmp_path)
    _fake_lark_cli(tmp_path, "fail")
    assert _run_pre(tmp_path).returncode != 0
    assert "> ⚠️ 日历同步失败" in cal.read_text(encoding="utf-8")

    (tmp_path / "lark_mode").write_text("ok", encoding="utf-8")
    result = _run_pre(tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip() != ""
    text = cal.read_text(encoding="utf-8")
    assert "> ⚠️ 日历同步失败" not in text, "success must wash the annotation away"
    assert "synced 2026-07-01 10:00" not in text, "synced timestamp must refresh"
    assert "重要会议" not in text, "snapshot fully rewritten from fresh fetch"


def test_empty_agenda_is_success_not_failure(tmp_path):
    (tmp_path / "mem" / "hot").mkdir(parents=True)  # hot/ exists in prod
    _fake_lark_cli(tmp_path, "ok")

    result = _run_pre(tmp_path)

    assert result.returncode == 0
    assert "(no events)" in result.stdout
    cal = tmp_path / "mem" / "hot" / "calendar_today.md"
    text = cal.read_text(encoding="utf-8")
    assert "(no events)" in text
    assert "> ⚠️ 日历同步失败" not in text
