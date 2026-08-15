"""memory_tidy_post 的同步/瘦身机制（2026-07-21 记忆瘦身 PRD R1/R2/R5）。"""

import os
import subprocess
import time
from pathlib import Path

import tasks.memory_tidy_post as tidy


def _twin_dirs(tmp_path, monkeypatch):
    auto = tmp_path / "auto"
    hb = tmp_path / "hb"
    (auto / "warm").mkdir(parents=True)
    (hb / "warm").mkdir(parents=True)
    monkeypatch.setattr(tidy, "AUTO_MEMORY", auto)
    monkeypatch.setattr(tidy, "HEARTBEAT_MEMORY", hb)
    return auto, hb


def test_verify_index_drops_dead_entries_and_appends_omitted(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "alive.md").write_text("x")
    (warm / "omitted.md").write_text("y")
    llm_index = ("# Warm Memory Index\n"
                 "## 分区\n"
                 "- alive.md — 还在的文件\n"
                 "- ghost.md — 已归档但 LLM 还记得\n")
    out = tidy._verify_index(llm_index, warm)
    assert "alive.md" in out
    assert "ghost.md" not in out          # dead entry dropped
    assert "omitted.md" in out            # existing file auto-appended
    assert "未分类" in out


def test_verify_index_clean_input_passes_through(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "a.md").write_text("x")
    idx = "# Index\n- a.md — 唯一文件\n"
    out = tidy._verify_index(idx, warm)
    assert "a.md" in out and "未分类" not in out


def test_process_writes_the_loader_visible_warm_index(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    warm = memory / "warm"
    warm.mkdir(parents=True)
    (warm / "profile.md").write_text("profile")
    monkeypatch.setattr(tidy, "MEMORY_DIR", memory)
    monkeypatch.setattr(tidy, "INDEX_FILE", warm / "_index.md")

    raw = ('{"index_update":"# Warm Memory Index\\n'
           '- profile.md — stable user profile and current preferences"}')
    assert tidy._process(raw) == 0
    assert (warm / "_index.md").exists()
    assert not (memory / "_index.md").exists()


def test_pre_hook_reports_the_loader_visible_warm_index(tmp_path):
    memory = tmp_path / "memory"
    for tier in ("hot", "warm", "system", "timeline"):
        (memory / tier).mkdir(parents=True)
    (memory / "_index.md").write_text("wrong root index")
    (memory / "warm" / "_index.md").write_text("correct warm index")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [str(root / "tasks" / "memory_tidy_pre.sh")],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "MEMORY_DIR": str(memory)},
    )
    assert "Current warm/_index.md contents:" in result.stdout
    assert "correct warm index" in result.stdout
    assert "wrong root index" not in result.stdout


def test_stray_repo_warm_is_verified_into_memory_archive_before_removal(
        tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    stray = tmp_path / "repo" / "warm"
    stray.mkdir(parents=True)
    note = stray / "nightwork.md"
    note.write_text("precious independent review")
    monkeypatch.setattr(tidy, "MEMORY_DIR", memory)
    monkeypatch.setattr(tidy, "STRAY_WARM_DIR", stray)

    tidy._recover_stray_repo_warm()

    recovered = memory / "archive" / "recovered_repo_warm" / "nightwork.md"
    assert recovered.read_text() == "precious independent review"
    assert not note.exists()
    assert not stray.exists()


def test_stray_repo_warm_collision_keeps_both_versions(tmp_path, monkeypatch):
    memory = tmp_path / "memory"
    archive = memory / "archive" / "recovered_repo_warm"
    archive.mkdir(parents=True)
    (archive / "_index.md").write_text("older recovered index")
    stray = tmp_path / "repo" / "warm"
    stray.mkdir(parents=True)
    (stray / "_index.md").write_text("new stray index")
    monkeypatch.setattr(tidy, "MEMORY_DIR", memory)
    monkeypatch.setattr(tidy, "STRAY_WARM_DIR", stray)

    tidy._recover_stray_repo_warm()

    assert (archive / "_index.md").read_text() == "older recovered index"
    assert (archive / "_index.1.md").read_text() == "new stray index"


def test_sync_preserves_source_mtime(tmp_path, monkeypatch):
    auto, hb = _twin_dirs(tmp_path, monkeypatch)
    src = auto / "warm" / "feedback_x.md"
    src.write_text("rule")
    old = time.time() - 60 * 86400
    os.utime(src, (old, old))

    tidy._sync_warm_auto_to_heartbeat()

    dst = hb / "warm" / "feedback_x.md"
    assert dst.exists()
    # 副本 mtime = 源 mtime，不是同步时刻——newest-first 排序和 demote
    # 的陈旧判断都建立在这上面。
    assert abs(dst.stat().st_mtime - old) < 2


def test_mirror_archival_requires_deletion_evidence(tmp_path, monkeypatch):
    """红队修正：replica-only ≠ auto 删过。只有 auto warm/archive/ 里有
    同名副本（真实的降级证据）才镜像归档；心跳会话直接写在副本侧的活
    档案（健康/能量/投资画像）必须原地保留。"""
    auto, hb = _twin_dirs(tmp_path, monkeypatch)
    (auto / "warm" / "alive.md").write_text("still canonical")
    (hb / "warm" / "alive.md").write_text("still canonical")
    # auto 侧确实降级过的文件：archive 里有证据
    (auto / "warm" / "archive").mkdir()
    (auto / "warm" / "archive" / "demoted_prep.md").write_text("archived canon")
    (hb / "warm" / "demoted_prep.md").write_text("replica copy of demoted")
    # 副本自有的活档案：auto 侧从来没有过 → 没有证据 → 不动
    (hb / "warm" / "health_profile.md").write_text("live replica-owned profile")
    (hb / "warm" / "_index.md").write_text("index is replica-owned")

    tidy._mirror_warm_deletions()

    assert (hb / "warm" / "alive.md").exists()
    assert (hb / "warm" / "_index.md").exists()
    assert (hb / "warm" / "health_profile.md").exists()  # 无证据不归档
    assert not (hb / "warm" / "demoted_prep.md").exists()
    assert (hb / "warm" / "archive" / "demoted_prep.md").exists()


def test_mirror_deletions_refuses_without_evidence(tmp_path, monkeypatch):
    """auto 侧空目录/无 archive：什么都不动。"""
    auto, hb = _twin_dirs(tmp_path, monkeypatch)
    (hb / "warm" / "precious.md").write_text("only copy in sight")

    tidy._mirror_warm_deletions()

    assert (hb / "warm" / "precious.md").exists()
    assert not (hb / "warm" / "archive").exists()


def test_mirror_deletions_suffixes_archive_collision(tmp_path, monkeypatch):
    auto, hb = _twin_dirs(tmp_path, monkeypatch)
    (auto / "warm" / "alive.md").write_text("canon")
    (auto / "warm" / "archive").mkdir()
    (auto / "warm" / "archive" / "zombie.md").write_text("auto archived it")
    (hb / "warm" / "zombie.md").write_text("new zombie")
    archive = hb / "warm" / "archive"
    archive.mkdir()
    (archive / "zombie.md").write_text("older archived copy")

    tidy._mirror_warm_deletions()

    assert (archive / "zombie.md").read_text() == "older archived copy"
    assert (archive / "zombie.1.md").read_text() == "new zombie"


def test_demote_wiring_end_to_end(tmp_path, monkeypatch):
    """demote(auto) → sync → mirror：陈旧备料文档从两侧的装配集里消失，
    feedback_* 两侧都保留。"""
    from core.memory import WARM_STALE_DAYS

    auto, hb = _twin_dirs(tmp_path, monkeypatch)
    old = time.time() - (WARM_STALE_DAYS + 5) * 86400
    stale = auto / "warm" / "trip_prep.md"
    stale.write_text("stale prep")
    guidance = auto / "warm" / "feedback_rule.md"
    guidance.write_text("timeless")
    for f in (stale, guidance):
        os.utime(f, (old, old))
    # 副本此前已同步过两份
    (hb / "warm" / "trip_prep.md").write_text("stale prep")
    (hb / "warm" / "feedback_rule.md").write_text("timeless")

    tidy._demote_stale_auto_warm()
    tidy._sync_warm_auto_to_heartbeat()
    tidy._mirror_warm_deletions()

    # auto 侧：备料进 archive，准则原地不动
    assert not stale.exists()
    assert (auto / "warm" / "archive" / "trip_prep.md").exists()
    assert guidance.exists()
    # 副本侧：镜像归档备料，准则保留
    assert not (hb / "warm" / "trip_prep.md").exists()
    assert (hb / "warm" / "archive" / "trip_prep.md").exists()
    assert (hb / "warm" / "feedback_rule.md").exists()
