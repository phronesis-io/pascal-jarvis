"""Tests for memory_consolidate_post.py — directives applied, diary NEVER on stdout.

Post-script stdout becomes a Lark message: on 2026-07-07 21:08 the internal
third-person diary was delivered to Pascal (HEARTBEAT.md classifies the Memory
Pipeline as silent). The diary must land in silent_outputs.jsonl instead.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "tasks" / "memory_consolidate_post.py"


def _run(stdin: str, memory_dir: Path, jarvis_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, "MEMORY_DIR": str(memory_dir),
             "JARVIS_DIR": str(jarvis_dir)},
    )


def test_diary_archived_not_printed(tmp_path):
    """The diary goes to silent_outputs.jsonl; stdout stays EMPTY so
    heartbeat.py's `if post_output:` gate never stages a Lark message."""
    memory = tmp_path / "memory"
    (memory / "hot").mkdir(parents=True)
    (memory / "hot" / "user_profile.md").write_text("# Profile\n")
    stdin = (
        "今天 Pascal 主要在推进 PGC。\n"
        "→ UPDATE: hot/user_profile.md: 新事实一条\n"
        "另一条日记行。"
    )
    result = _run(stdin, memory, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""          # nothing user-facing, ever
    # Directive still applied
    assert "新事实一条" in (memory / "hot" / "user_profile.md").read_text()
    # Diary archived in full (same shape as the SILENT_TASKS archive)
    rows = [json.loads(l) for l in
            (tmp_path / "silent_outputs.jsonl").read_text().splitlines() if l.strip()]
    assert rows and rows[-1]["task"] == "memory-consolidate"
    assert "今天 Pascal 主要在推进 PGC。" in rows[-1]["text"]
    assert "另一条日记行。" in rows[-1]["text"]
    assert "→ UPDATE:" not in rows[-1]["text"]  # directives are not diary


def test_idle_sentinel_produces_nothing(tmp_path):
    memory = tmp_path / "memory"
    memory.mkdir()
    result = _run("HEARTBEAT_OK", memory, tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert not (tmp_path / "silent_outputs.jsonl").exists()
