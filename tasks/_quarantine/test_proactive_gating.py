"""REQ-75 — event-gate low-value proactive sources.

Two proactive sources had poor engagement and were interrupting the owner without
earning it:
  * free-time-nudge: ~7% engagement (1/14) + a double-fire (a pre-block fire and
    an in-progress fire ~60min apart inside the SAME free block).
  * content-recommend: 0% engagement (0/7) as a standalone unanswered push.

the owner's principle: every proactive message must earn its interruption. These
tests pin the GATING LOGIC of the two pre-scripts (the empty-output contract:
empty pre output → the heartbeat skips the task).

2026-07-02 (REQ-89): free-time-nudge is unscheduled (removed from HEARTBEAT.md;
0 conversions over 11 sends). The pre-script file is kept for easy revival, so
the empty-output contract + syntax tests remain. The four "fires when content
exists" tests (nonempty_when_watchlater / nonempty_when_todo / double_fire /
state_stamp) were wall-clock dependent (flaky near midnight / block boundaries)
and pinned behavior of a task that no longer runs — deleted, not skipped.

Isolation: every test uses tmp_path for JARVIS_DIR/MEMORY_DIR and a synthetic
tmp jarvis.yaml. The real watchlater store and the real
.free_time_nudge_state are NEVER touched (conftest's _guard_repo_files also
back-stops this).
"""

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FREE_TIME_PRE = ROOT / "tasks" / "free_time_nudge_pre.sh"
CONTENT_PRE = ROOT / "tasks" / "content_recommend_pre.sh"


# ──────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────

def _write_yaml(jarvis_dir: Path, work_start: int = 0, work_end: int = 24) -> None:
    """Synthetic jarvis.yaml with wide working hours so the test is
    independent of the wall-clock hour (the pre-script reads working hours via
    scripts/config_env.sh → core.config, which would otherwise clamp to 9-22)."""
    (jarvis_dir / "jarvis.yaml").write_text(
        "schedule:\n"
        "  working_hours:\n"
        f"    start: {work_start}\n"
        f"    end: {work_end}\n"
        "thresholds:\n"
        "  nudge_max_per_day: 2\n"
        "  free_block_min_minutes: 30\n",
        encoding="utf-8",
    )


def _calendar_with_free_block(mem_dir: Path) -> None:
    """A calendar whose only event is late tonight, so 'now' is inside a long
    free block ending at the next event (the next-event branch of the
    detector — robust regardless of the current time)."""
    hot = mem_dir / "hot"
    hot.mkdir(parents=True, exist_ok=True)
    (hot / "calendar_today.md").write_text(
        "# Calendar Today\n- 23:30-23:59 Late event\n", encoding="utf-8"
    )


def _calendar_busy_now(mem_dir: Path) -> None:
    """A calendar that keeps 'now' busy all day → no free block at all."""
    hot = mem_dir / "hot"
    hot.mkdir(parents=True, exist_ok=True)
    (hot / "calendar_today.md").write_text(
        "# Calendar Today\n- 00:00-23:59 All-day workshop\n", encoding="utf-8"
    )


def _add_watchlater(mem_dir: Path, *, status: str = "pending") -> None:
    sysd = mem_dir / "system"
    sysd.mkdir(parents=True, exist_ok=True)
    (sysd / "watchlater.jsonl").write_text(
        json.dumps(
            {
                "title": "Test Video",
                "url": "https://example.com/v",
                "status": status,
                "source": "test",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _run_free_time(tmp_path: Path) -> subprocess.CompletedProcess:
    jarvis = tmp_path / "jarvis"
    mem = tmp_path / "mem"
    jarvis.mkdir(parents=True, exist_ok=True)
    mem.mkdir(parents=True, exist_ok=True)
    if not (jarvis / "jarvis.yaml").exists():
        _write_yaml(jarvis)
    env = {
        **os.environ,
        "JARVIS_DIR": str(jarvis),
        "MEMORY_DIR": str(mem),
    }
    return subprocess.run(
        ["bash", str(FREE_TIME_PRE)],
        capture_output=True, text=True, env=env,
    )


def _run_content(tmp_path: Path, push: str | None = None) -> subprocess.CompletedProcess:
    jarvis = tmp_path / "jarvis"
    mem = tmp_path / "mem"
    jarvis.mkdir(parents=True, exist_ok=True)
    mem.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "JARVIS_DIR": str(jarvis),
        "MEMORY_DIR": str(mem),
    }
    if push is not None:
        env["CONTENT_RECOMMEND_PUSH"] = push
    else:
        env.pop("CONTENT_RECOMMEND_PUSH", None)
    return subprocess.run(
        ["bash", str(CONTENT_PRE)],
        capture_output=True, text=True, env=env,
    )


def _fake_yt_dlp(tmp_path: Path) -> Path:
    fake = tmp_path / "yt-dlp"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == ytsearch8:* ]]; then\n"
        "  echo 'Deep Philosophy Lecture ||| https://example.com/video ||| 50000 ||| 42:00'\n"
        "else\n"
        "  echo '{\"title\":\"Bili Deep Talk\",\"webpage_url\":\"https://www.bilibili.com/video/BV1\"}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_content_enabled(tmp_path: Path) -> subprocess.CompletedProcess:
    jarvis = tmp_path / "jarvis"
    mem = tmp_path / "mem"
    jarvis.mkdir(parents=True, exist_ok=True)
    mem.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "JARVIS_DIR": str(jarvis),
        "MEMORY_DIR": str(mem),
        "CONTENT_RECOMMEND_PUSH": "1",
        "CONTENT_RECOMMEND_TEST_HOUR": "15",
        "YT_DLP": str(_fake_yt_dlp(tmp_path)),
    }
    return subprocess.run(
        ["bash", str(CONTENT_PRE)],
        capture_output=True, text=True, env=env,
    )


# ──────────────────────────────────────────────────────────────────────────
# free-time-nudge: event gate (empty unless genuine content)
# ──────────────────────────────────────────────────────────────────────────

def test_free_time_empty_when_no_watchlater_or_todo(tmp_path):
    """Free block exists but NOTHING to offer → empty output (heartbeat skips).
    This is the core REQ-75 fix: the bare '你有空' nudge is gone."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    _calendar_with_free_block(mem)
    # no watchlater, no todos
    result = _run_free_time(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == "", f"expected silence, got: {result.stdout!r}"


def test_free_time_ignores_already_watched_items(tmp_path):
    """A non-pending (already watched) item is NOT genuine content → silence."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    _calendar_with_free_block(mem)
    _add_watchlater(mem, status="watched")
    result = _run_free_time(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_free_time_empty_when_no_free_block(tmp_path):
    """Busy all day → no free block → empty regardless of content."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    _calendar_busy_now(mem)
    _add_watchlater(mem)  # content exists, but there is no block to offer it in
    result = _run_free_time(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_free_time_empty_when_no_calendar(tmp_path):
    """No calendar file at all → exit clean, empty."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    _add_watchlater(mem)
    result = _run_free_time(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ──────────────────────────────────────────────────────────────────────────
# free-time-nudge: daily rate limit
# ──────────────────────────────────────────────────────────────────────────

def test_free_time_respects_daily_rate_limit(tmp_path):
    """If today's count already hit NUDGE_MAX, stay silent even with content."""
    mem = tmp_path / "mem"
    mem.mkdir(parents=True, exist_ok=True)
    _calendar_with_free_block(mem)
    _add_watchlater(mem)

    jarvis = tmp_path / "jarvis"
    jarvis.mkdir(parents=True, exist_ok=True)
    _write_yaml(jarvis)
    today = subprocess.run(
        ["date", "+%Y-%m-%d"], capture_output=True, text=True
    ).stdout.strip()
    # count == NUDGE_MAX (2) → over the limit
    (jarvis / ".free_time_nudge_state").write_text(f"{today}\n2\n", encoding="utf-8")

    result = _run_free_time(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ──────────────────────────────────────────────────────────────────────────
# content-recommend: deferred standalone push
# ──────────────────────────────────────────────────────────────────────────

def test_content_recommend_defers_by_default(tmp_path):
    """0%-engagement standalone push is gated OFF by default → empty output
    (heartbeat skips). Discovery is meant to be batched into the daily digest."""
    result = _run_content(tmp_path, push=None)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_content_recommend_defers_when_flag_zero(tmp_path):
    """Explicit CONTENT_RECOMMEND_PUSH=0 also defers."""
    result = _run_content(tmp_path, push="0")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_content_recommend_pre_syntax():
    """The content-recommend pre-script must stay valid bash."""
    result = subprocess.run(
        ["bash", "-n", str(CONTENT_PRE)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_content_recommend_enabled_reads_safe_taste_memory(tmp_path):
    """When explicitly enabled, curation context comes from safe memory files,
    while private/secret buffers are excluded from the outbound prompt."""
    warm = tmp_path / "mem" / "warm"
    warm.mkdir(parents=True, exist_ok=True)
    (warm / "interests.md").write_text(
        "喜欢严肃哲学和技术深潜\n偏好长讲座，不要浅层鸡汤\n",
        encoding="utf-8",
    )
    (warm / "secret_taste.md").write_text("SHOULD_NOT_LEAK\n", encoding="utf-8")
    (warm / "inbox_content_feedback.md").write_text(
        "ALSO_SHOULD_NOT_LEAK\n", encoding="utf-8"
    )

    result = _run_content_enabled(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Memory-derived taste context:" in result.stdout
    assert "喜欢严肃哲学和技术深潜" in result.stdout
    assert "偏好长讲座" in result.stdout
    assert "SHOULD_NOT_LEAK" not in result.stdout
    assert "ALSO_SHOULD_NOT_LEAK" not in result.stdout
    assert "Candidate videos:" in result.stdout
    assert (
        "Deep Philosophy Lecture" in result.stdout
        or "Bili Deep Talk" in result.stdout
    )


def test_content_recommend_enabled_uses_fallback_without_taste_memory(tmp_path):
    """No profile memory should still produce candidates with an explicit
    fallback cue for the curator."""
    result = _run_content_enabled(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "(none found; use the fallback curation criteria in the task prompt)" in result.stdout
    assert "Candidate videos:" in result.stdout


def test_free_time_pre_syntax():
    """The free-time pre-script must stay valid bash."""
    result = subprocess.run(
        ["bash", "-n", str(FREE_TIME_PRE)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
