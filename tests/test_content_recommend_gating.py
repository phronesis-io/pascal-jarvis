"""Coverage for the still-active content recommendation pre-hook."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTENT_PRE = ROOT / "tasks" / "content_recommend_pre.sh"


def _run_content(tmp_path: Path, push: str | None = None):
    jarvis = tmp_path / "jarvis"
    memory = tmp_path / "memory"
    jarvis.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "JARVIS_DIR": str(jarvis),
        "MEMORY_DIR": str(memory),
    }
    if push is not None:
        env["CONTENT_RECOMMEND_PUSH"] = push
    else:
        env.pop("CONTENT_RECOMMEND_PUSH", None)
    return subprocess.run(
        ["bash", str(CONTENT_PRE)],
        capture_output=True,
        text=True,
        env=env,
    )


def _fake_yt_dlp(tmp_path: Path) -> Path:
    fake = tmp_path / "yt-dlp"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == ytsearch8:* ]]; then\n"
        "  echo 'Deep Philosophy Lecture ||| https://example.com/video "
        "||| 50000 ||| 42:00'\n"
        "else\n"
        "  echo '{\"title\":\"Bili Deep Talk\","
        "\"webpage_url\":\"https://www.bilibili.com/video/BV1\"}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _run_enabled(tmp_path: Path):
    jarvis = tmp_path / "jarvis"
    memory = tmp_path / "memory"
    jarvis.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        ["bash", str(CONTENT_PRE)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JARVIS_DIR": str(jarvis),
            "MEMORY_DIR": str(memory),
            "CONTENT_RECOMMEND_PUSH": "1",
            "CONTENT_RECOMMEND_TEST_HOUR": "15",
            "YT_DLP": str(_fake_yt_dlp(tmp_path)),
        },
    )


def test_content_recommend_defers_by_default(tmp_path):
    result = _run_content(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_content_recommend_defers_when_flag_zero(tmp_path):
    result = _run_content(tmp_path, push="0")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_content_recommend_pre_syntax():
    result = subprocess.run(
        ["bash", "-n", str(CONTENT_PRE)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_content_recommend_enabled_reads_only_safe_taste_memory(tmp_path):
    warm = tmp_path / "memory" / "warm"
    warm.mkdir(parents=True, exist_ok=True)
    (warm / "interests.md").write_text(
        "喜欢严肃哲学和技术深潜\n偏好长讲座，不要浅层鸡汤\n",
        encoding="utf-8",
    )
    (warm / "secret_taste.md").write_text(
        "SHOULD_NOT_LEAK\n", encoding="utf-8")
    (warm / "inbox_content_feedback.md").write_text(
        "ALSO_SHOULD_NOT_LEAK\n", encoding="utf-8")

    result = _run_enabled(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "喜欢严肃哲学和技术深潜" in result.stdout
    assert "SHOULD_NOT_LEAK" not in result.stdout
    assert "ALSO_SHOULD_NOT_LEAK" not in result.stdout
    assert "Candidate videos:" in result.stdout


def test_content_recommend_enabled_has_fallback_without_taste_memory(tmp_path):
    result = _run_enabled(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "(none found; use the fallback curation criteria" in result.stdout
    assert "Candidate videos:" in result.stdout
