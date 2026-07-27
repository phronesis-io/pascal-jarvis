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


# ── REQ-100/101: group chat prompt — privacy boundary ──────────────────


def _group_prompt(tmp_path, **kw):
    return build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="s",
        conv_key="oc_group1",
        now_ts="2026-07-14 16:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
        chat_type="group",
        **kw,
    )


def test_group_prompt_never_loads_personal_memory(tmp_path):
    mem = tmp_path / "memory"
    for tier in ("hot", "warm", "system", "timeline"):
        (mem / tier).mkdir(parents=True)
    (mem / "hot" / "profile.md").write_text("SECRET_HEALTH_MARKER 腰伤复健中")
    (mem / "warm" / "contacts.md").write_text("SECRET_CONTACT_MARKER 洪某某 VC")
    (mem / "system" / "todos.md").write_text("SECRET_TODO_MARKER 婚礼红包")

    prompt = _group_prompt(tmp_path)
    assert "SECRET_HEALTH_MARKER" not in prompt
    assert "SECRET_CONTACT_MARKER" not in prompt
    assert "SECRET_TODO_MARKER" not in prompt
    assert "隐私边界" in prompt          # group etiquette present
    # The ACTIONS_DOC (which teaches marker syntax) must be absent — the
    # etiquette text mentioning [ACTION:...] as forbidden is fine.
    assert "Available Actions" not in prompt


def test_group_prompt_uses_curated_group_context(tmp_path):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    (mem / "group_context.md").write_text("主人是 EigenFlux 创始人")
    prompt = _group_prompt(tmp_path)
    assert "EigenFlux 创始人" in prompt


def test_group_prompt_without_context_file_uses_fallback(tmp_path):
    (tmp_path / "memory").mkdir()
    prompt = _group_prompt(tmp_path)
    assert "未配置 group_context.md" in prompt


def test_group_prompt_owner_name_from_env_not_hardcoded(tmp_path, monkeypatch):
    (tmp_path / "memory").mkdir()
    monkeypatch.setenv("OWNER_NAME", "小王")
    prompt = _group_prompt(tmp_path)
    assert "小王 的 AI 助手" in prompt
    monkeypatch.delenv("OWNER_NAME")
    prompt = _group_prompt(tmp_path)
    assert "主人 的 AI 助手" in prompt   # neutral default, no hardcoded name


def test_p2p_prompt_unchanged_by_group_param(tmp_path):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("User is a developer")
    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path), memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path), session_id="s", conv_key="k",
        now_ts="t", tracker_path=str(tmp_path / "tracker.json"),
        chat_type="p2p",
    )
    assert "User is a developer" in prompt and "ACTION:" in prompt


def test_external_p2p_uses_shared_context_not_personal_memory(tmp_path):
    memory = tmp_path / "memory"
    (memory / "hot").mkdir(parents=True)
    (memory / "hot" / "profile.md").write_text(
        "PRIVATE_OWNER_MEMORY", encoding="utf-8")
    (memory / "hot" / "group_context.md").write_text(
        "PUBLIC_COMPANY_CONTEXT", encoding="utf-8")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(memory),
        session_dir=str(tmp_path),
        session_id="external",
        conv_key="external-user",
        now_ts="2026-07-27 12:00",
        tracker_path=str(tmp_path / "tracker.json"),
        chat_type="external_p2p",
    )

    assert "PUBLIC_COMPANY_CONTEXT" in prompt
    assert "PRIVATE_OWNER_MEMORY" not in prompt
    assert "Available Actions" not in prompt
