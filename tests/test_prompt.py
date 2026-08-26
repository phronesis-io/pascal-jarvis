"""Tests for core.prompt — system prompt builder."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

from core.prompt import (
    ACTIONS_DOC,
    PROMPT_SNAPSHOT_MAX_AGE_SECONDS,
    _prune_prompt_snapshots,
    build_cached_system_prompt,
    build_system_prompt,
    load_ef_skills,
)


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
    assert "2026-05-25 10:00 Monday" not in prompt
    assert "Current time:" not in prompt
    assert "User is a developer" in prompt
    assert "ACTION:" in prompt


def test_owner_prompt_uses_current_message_to_focus_memory(tmp_path):
    warm = tmp_path / "memory" / "warm"
    warm.mkdir(parents=True)
    (warm / "feedback_rules.md").write_text("GENERAL\n" + "g" * 12_000)
    (warm / "insurance.md").write_text("董责险关键结论\n" + "i" * 3000)

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-08-14 10:00",
        tracker_path=str(tmp_path / "tracker.json"),
        max_memory_chars=5000,
        focus_text="董责险",
    )

    assert "董责险关键结论" in prompt


def test_owner_prompt_uses_runtime_warm_index_mode(tmp_path, monkeypatch):
    """bot.sh's exported index mode must reach the live owner prompt."""
    warm = tmp_path / "memory" / "warm"
    warm.mkdir(parents=True)
    (warm / "feedback_rules.md").write_text("INLINE_GUIDANCE")
    (warm / "project_large.md").write_text(
        "---\ndescription: 大项目资料\n---\n" + "REFERENCE_BODY" * 100)
    monkeypatch.setenv("JARVIS_WARM_MEMORY_MODE", "index")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-08-25 13:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "INLINE_GUIDANCE" in prompt
    assert "REFERENCE_BODY" not in prompt
    assert "project_large.md" in prompt


def test_owner_prompt_explicit_full_mode_overrides_runtime_index(
        tmp_path, monkeypatch):
    warm = tmp_path / "memory" / "warm"
    warm.mkdir(parents=True)
    (warm / "project_large.md").write_text("REFERENCE_BODY")
    monkeypatch.setenv("JARVIS_WARM_MEMORY_MODE", "index")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-08-25 13:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
        warm_mode="full",
    )

    assert "REFERENCE_BODY" in prompt


def test_owner_system_prompt_is_clock_free(tmp_path):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    (mem / "profile.md").write_text("MEMORY_SENTINEL")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="test-key",
        now_ts="2026-08-25 13:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "Current time:" not in prompt
    assert "2026-08-25 13:00 Tuesday" not in prompt
    assert "MEMORY_SENTINEL" in prompt


def test_cached_system_prompt_is_exact_per_session_and_private(tmp_path):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    profile = mem / "profile.md"
    profile.write_text("FIRST_MEMORY", encoding="utf-8")
    cache_dir = tmp_path / "data" / "session_prompt_cache"

    common = {
        "cache_dir": cache_dir,
        "jarvis_dir": str(tmp_path),
        "memory_dir": str(tmp_path / "memory"),
        "session_dir": str(tmp_path),
        "session_id": "session-one",
        "conv_key": "owner",
        "now_ts": "2026-08-25 13:00 Tuesday",
        "tracker_path": str(tmp_path / "tracker.json"),
    }
    first = build_cached_system_prompt(**common)
    profile.write_text("SECOND_MEMORY", encoding="utf-8")
    second = build_cached_system_prompt(
        **{**common, "now_ts": "2026-08-25 13:10 Tuesday"})

    assert second == first
    assert "FIRST_MEMORY" in second
    assert "SECOND_MEMORY" not in second
    assert "Current time:" not in second

    third = build_cached_system_prompt(
        **{**common, "session_id": "session-two"})
    assert "SECOND_MEMORY" in third
    assert third != first
    assert cache_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600
               for path in cache_dir.iterdir())

    snapshot = next(
        path for path in cache_dir.glob("*.txt")
        if path.read_text(encoding="utf-8") == first
    )
    snapshot.chmod(0o644)
    assert build_cached_system_prompt(**common) == first
    assert snapshot.stat().st_mode & 0o777 == 0o600


def test_cached_system_prompt_isolated_by_release_and_expires_private_data(
        tmp_path, monkeypatch):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    profile = mem / "profile.md"
    profile.write_text("FIRST_RELEASE", encoding="utf-8")
    cache_dir = tmp_path / "data" / "session_prompt_cache"
    common = {
        "cache_dir": cache_dir,
        "jarvis_dir": str(tmp_path),
        "memory_dir": str(tmp_path / "memory"),
        "session_dir": str(tmp_path),
        "session_id": "session-one",
        "conv_key": "owner",
        "now_ts": "2026-08-25 13:00 Tuesday",
        "tracker_path": str(tmp_path / "tracker.json"),
    }

    monkeypatch.setenv("JARVIS_RUNTIME_GIT_HEAD", "a" * 40)
    first = build_cached_system_prompt(**common)
    profile.write_text("SECOND_RELEASE", encoding="utf-8")
    monkeypatch.setenv("JARVIS_RUNTIME_GIT_HEAD", "b" * 40)
    second = build_cached_system_prompt(**common)

    assert "FIRST_RELEASE" in first
    assert "SECOND_RELEASE" in second
    assert second != first

    snapshot = max(cache_dir.glob("*.txt"), key=lambda path: path.stat().st_mtime)
    old = time.time() - PROMPT_SNAPSHOT_MAX_AGE_SECONDS - 60
    os.utime(snapshot, (old, old))
    profile.write_text("AFTER_RETENTION", encoding="utf-8")
    third = build_cached_system_prompt(**common)
    assert "AFTER_RETENTION" in third
    assert "SECOND_RELEASE" not in third
    assert {path.name for path in cache_dir.glob("*.lock")} == {".cache.lock"}


def test_prompt_snapshot_pruning_enforces_count_for_fresh_files(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    snapshots = []
    for index in range(4):
        path = cache_dir / f"{index}.txt"
        path.write_text(str(index), encoding="utf-8")
        os.utime(path, (time.time() + index, time.time() + index))
        snapshots.append(path)

    _prune_prompt_snapshots(cache_dir, keep=2, max_age_seconds=3600)

    assert {path.name for path in cache_dir.glob("*.txt")} == {
        snapshots[2].name,
        snapshots[3].name,
    }


def test_unrelated_prompt_cache_misses_build_concurrently(tmp_path, monkeypatch):
    """A slow cross-session/memory read for one new chat must not hold the
    directory lock and stall another new chat."""
    import core.prompt as prompt_module

    cache_dir = tmp_path / "cache"
    started = {"one": threading.Event(), "two": threading.Event()}
    release = threading.Event()

    def slow_build(**kwargs):
        session_id = kwargs["session_id"]
        started[session_id].set()
        assert release.wait(timeout=3)
        return f"prompt:{session_id}"

    monkeypatch.setattr(prompt_module, "build_system_prompt", slow_build)
    common = {
        "cache_dir": cache_dir,
        "jarvis_dir": str(tmp_path),
        "memory_dir": str(tmp_path / "memory"),
        "session_dir": str(tmp_path),
        "conv_key": "owner",
        "now_ts": "ignored",
        "tracker_path": str(tmp_path / "tracker.json"),
    }
    results = {}

    def worker(session_id):
        results[session_id] = build_cached_system_prompt(
            **common, session_id=session_id)

    first = threading.Thread(target=worker, args=("one",))
    second = threading.Thread(target=worker, args=("two",))
    first.start()
    assert started["one"].wait(timeout=1)
    second.start()
    assert started["two"].wait(timeout=1), (
        "second prompt build was serialized behind unrelated session work"
    )
    release.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results == {"one": "prompt:one", "two": "prompt:two"}


def test_same_prompt_cache_miss_publishes_one_exact_snapshot(
        tmp_path, monkeypatch):
    """Concurrent cold calls for one session must converge on one snapshot."""
    import core.prompt as prompt_module

    cache_dir = tmp_path / "cache"
    both_started = threading.Barrier(2)
    build_number = iter((1, 2))

    def concurrent_build(**_kwargs):
        number = next(build_number)
        both_started.wait(timeout=3)
        return f"prompt:{number}"

    monkeypatch.setattr(prompt_module, "build_system_prompt", concurrent_build)
    kwargs = {
        "cache_dir": cache_dir,
        "jarvis_dir": str(tmp_path),
        "memory_dir": str(tmp_path / "memory"),
        "session_dir": str(tmp_path),
        "conv_key": "owner",
        "session_id": "same-session",
        "now_ts": "ignored",
        "tracker_path": str(tmp_path / "tracker.json"),
    }
    results = []

    def worker():
        results.append(build_cached_system_prompt(**kwargs))

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(results) == 2
    assert results[0] == results[1]
    assert results[0] in {"prompt:1", "prompt:2"}
    snapshots = list(cache_dir.glob("*.txt"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == results[0]


def test_cached_system_prompt_fails_open_without_sharing_empty_session(
        tmp_path):
    mem = tmp_path / "memory" / "hot"
    mem.mkdir(parents=True)
    profile = mem / "profile.md"
    profile.write_text("FIRST", encoding="utf-8")
    cache_dir = tmp_path / "data" / "session_prompt_cache"
    kwargs = {
        "cache_dir": cache_dir,
        "jarvis_dir": str(tmp_path),
        "memory_dir": str(tmp_path / "memory"),
        "session_dir": str(tmp_path),
        "session_id": "",
        "conv_key": "owner",
        "now_ts": "2026-08-25 13:00 Tuesday",
        "tracker_path": str(tmp_path / "tracker.json"),
    }

    first = build_cached_system_prompt(**kwargs)
    profile.write_text("SECOND", encoding="utf-8")
    second = build_cached_system_prompt(**kwargs)

    assert "FIRST" in first
    assert "SECOND" in second
    assert not cache_dir.exists()


def test_resumed_prompt_omits_transcript_context_and_focus_reordering(
        tmp_path, monkeypatch):
    (tmp_path / "memory").mkdir()
    seen = []
    monkeypatch.setattr(
        "core.prompt.load_tiered_memory",
        lambda _path, **kwargs: seen.append(kwargs) or "STABLE_MEMORY",
    )
    monkeypatch.setattr(
        "core.prompt.build_recent_turns", lambda *_a, **_kw: "RECENT_TURNS"
    )
    monkeypatch.setattr(
        "core.prompt.read_compact", lambda *_a, **_kw: "SESSION_COMPACT"
    )
    monkeypatch.setattr(
        "core.prompt._external_work_context",
        lambda *_a, **_kw: "EXTERNAL_CONTEXT",
    )

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="existing-session",
        conv_key="owner",
        now_ts="2026-08-25 14:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
        focus_text="CURRENT_MESSAGE",
        resume_existing=True,
    )

    assert seen[0]["focus_text"] == ""
    assert "RECENT_TURNS" not in prompt
    assert "SESSION_COMPACT" not in prompt
    assert "EXTERNAL_CONTEXT" not in prompt


def test_every_bot_prompt_rebuild_preserves_resume_detection():
    script = (Path(__file__).parents[1] / "bot.sh").read_text(encoding="utf-8")

    assert script.count('JV_RESUME_EXISTING="$_resume_existing"') == 3
    assert script.count(
        "resume_existing=os.environ.get('JV_RESUME_EXISTING') == '1'"
    ) == 3



def test_named_matter_does_not_receive_global_external_session_history(
    tmp_path, monkeypatch,
):
    (tmp_path / "memory").mkdir()
    monkeypatch.setattr(
        "core.prompt._external_work_context",
        lambda *_args, **_kwargs: "PRIVATE_GLOBAL_HISTORY",
    )

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="owner-key",
        now_ts="2026-08-14 10:00",
        tracker_path=str(tmp_path / "tracker.json"),
        matter_id="matter-one",
        focus_text="模型控制",
    )

    assert "PRIVATE_GLOBAL_HISTORY" not in prompt


def test_owner_prompt_includes_id_free_known_people_context(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "data").mkdir()
    registry_path = tmp_path / "data" / "person_registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "people": [{
            "person_id": "partner",
            "name": "Partner Name",
            "aliases": ["my partner"],
            "relationships": ["spouse"],
            "channels": {"lark": {
                "open_id": "ou_partner_verified",
                "verified_at": "2026-08-13",
            }},
        }],
    }))
    registry_path.chmod(0o600)

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="test-session",
        conv_key="owner-key",
        now_ts="2026-08-13 16:00",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "Known People" in prompt
    assert "Partner Name" in prompt and "my partner" in prompt
    assert "ou_partner_verified" not in prompt


@pytest.mark.parametrize("chat_type", ["group", "external_p2p"])
def test_untrusted_prompt_never_includes_private_people_registry(tmp_path, chat_type):
    (tmp_path / "memory" / "hot").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    registry_path = tmp_path / "data" / "person_registry.json"
    registry_path.write_text(json.dumps({
        "version": 1,
        "people": [{
            "person_id": "private_person",
            "name": "Private Person",
            "aliases": ["private relationship"],
            "channels": {"lark": {
                "open_id": "ou_private_verified",
                "verified_at": "2026-08-13",
            }},
        }],
    }))
    registry_path.chmod(0o600)

    prompt = _group_prompt(tmp_path, chat_type=chat_type)

    assert "Known People" not in prompt
    assert "Private Person" not in prompt
    assert "private relationship" not in prompt


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


def test_owner_prompt_includes_bounded_cross_provider_context(tmp_path):
    from core.matter_bridge import record_turn

    (tmp_path / "memory").mkdir()
    record_turn(
        "owner-key", "assistant", "Codex just finished the implementation",
        message_id="om_codex", provider="Codex", model="gpt-test")

    prompt = build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="claude-session",
        conv_key="owner-key",
        now_ts="2026-08-11 20:00",
        tracker_path=str(tmp_path / "tracker.json"),
    )

    assert "Recent Cross-Provider Turns" in prompt
    assert "Codex just finished the implementation" in prompt
    assert "untrusted conversation history" in prompt


def test_actions_doc_complete():
    """All action types should be documented."""
    actions = [
        "feed_search", "watchlater", "bg", "jobs", "job_cancel", "job_output",
        "heartbeat", "calendar_create", "calendar_update", "calendar_attendees",
        "calendar_delete",
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


def _group_prompt(tmp_path, *, chat_type="group", **kw):
    return build_system_prompt(
        jarvis_dir=str(tmp_path),
        memory_dir=str(tmp_path / "memory"),
        session_dir=str(tmp_path),
        session_id="s",
        conv_key="oc_group1",
        now_ts="2026-07-14 16:00 Tuesday",
        tracker_path=str(tmp_path / "tracker.json"),
        chat_type=chat_type,
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


def test_group_prompt_does_not_load_provider_neutral_private_projection(tmp_path):
    from core.matter_bridge import record_turn

    (tmp_path / "memory").mkdir()
    record_turn(
        "oc_group1", "assistant", "PRIVATE_PROVIDER_TURN",
        message_id="om_private", provider="Codex", model="gpt-test")

    assert "PRIVATE_PROVIDER_TURN" not in _group_prompt(tmp_path)


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
