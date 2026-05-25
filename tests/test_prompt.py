"""Tests for core.prompt — system prompt builder."""

import json
from pathlib import Path

from core.prompt import build_system_prompt, load_ef_skills, ACTIONS_DOC


def test_build_system_prompt_basic(tmp_path):
    """Prompt should contain base instructions + memory."""
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("User is a developer")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-05-25 10:00 Monday",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "personal assistant" in prompt
    assert "2026-05-25 10:00 Monday" in prompt
    assert "User is a developer" in prompt
    assert "ACTION:" in prompt


def test_build_system_prompt_with_compact(tmp_path):
    """If session compact exists, it should be included."""
    mem = tmp_path / "memory"
    mem.mkdir(parents=True)
    compact_dir = tmp_path / "session_compacts"
    compact_dir.mkdir()
    (compact_dir / "test-key.md").write_text("Last session we discussed X")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(mem),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-05-25 10:00",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "Previous Session Summary" in prompt
    assert "discussed X" in prompt


def test_actions_doc_complete():
    """All action types should be documented."""
    actions = [
        "feed_search", "watchlater", "bg", "jobs", "job_cancel", "job_output",
        "heartbeat", "calendar_create", "calendar_update", "calendar_delete",
        "task_create", "task_complete", "task_capture", "task_commit",
        "task_done", "task_reject", "task_defer",
        "praxis_done", "praxis_add", "praxis_remove",
        "intent_create", "intent_cancel", "intent_list",
    ]
    for action in actions:
        assert f"ACTION:{action}" in ACTIONS_DOC, f"Missing action: {action}"


def test_load_ef_skills_empty(tmp_path):
    assert load_ef_skills(tmp_path) == ""


def test_load_ef_skills(tmp_path):
    skill_dir = tmp_path / "plugins" / "eigenflux" / "skills" / "ef-test"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: test\n---\nSkill content here")

    result = load_ef_skills(tmp_path)
    assert "Skill content here" in result
    assert "---" not in result  # frontmatter stripped
