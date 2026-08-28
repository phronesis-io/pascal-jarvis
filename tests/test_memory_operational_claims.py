from __future__ import annotations

from core.memory_operational_claims import reconcile_operational_claims


def test_known_obsolete_runtime_claims_are_reconciled_without_touching_history(tmp_path):
    for subdir in ("system", "timeline", "hot"):
        (tmp_path / subdir).mkdir()
    (tmp_path / "system" / "todos.md").write_text(
        "- history stays\n- 【warm 记忆索引模式】**但生产仍是关的**：旧说明\n",
        encoding="utf-8",
    )
    (tmp_path / "system" / "open_threads.md").write_text(
        "  另：PR #100 的按需索引模式仍是关的，旧说明。\n",
        encoding="utf-8",
    )
    (tmp_path / "timeline" / "longterm_digest.md").write_text(
        "- 记忆索引模式已做好，开关要写进 bot.sh 并重启——旧说明。\n",
        encoding="utf-8",
    )
    (tmp_path / "hot" / "behavioral_rules.md").write_text(
        "- 投资：详见 warm/interests.md「投资内容边界」（2026-06-07）\n",
        encoding="utf-8",
    )
    (tmp_path / "hot" / "feedback_rules.md").write_text(
        "# Rules\n\n## 重启 Jarvis 的正确方式\n**永远使用 `./restart.sh`**\n"
        "- `./restart.sh --full`\n\n## Next\nkeep\n",
        encoding="utf-8",
    )

    changed = reconcile_operational_claims(tmp_path)

    assert set(changed) == {
        "system/todos.md",
        "system/open_threads.md",
        "timeline/longterm_digest.md",
        "hot/behavioral_rules.md",
        "hot/feedback_rules.md",
    }
    all_text = "\n".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.md")
    )
    assert "生产仍是关的" not in all_text
    assert "索引模式仍是关的" not in all_text
    assert "开关要写进 bot.sh" not in all_text
    assert "warm/interests.md" not in all_text
    assert "restart.sh --full" in all_text
    assert "core.deploy verify" in all_text
    assert "不要手动\n`pkill` 或直接运行 `launchctl kickstart`" in all_text
    assert "history stays" in all_text and "## Next\nkeep" in all_text


def test_reconciliation_is_idempotent(tmp_path):
    (tmp_path / "hot").mkdir()
    target = tmp_path / "hot" / "feedback_rules.md"
    target.write_text(
        "## 重启 Jarvis 的正确方式\n**永远使用 `./restart.sh`**\n",
        encoding="utf-8",
    )
    assert reconcile_operational_claims(tmp_path) == ["hot/feedback_rules.md"]
    assert reconcile_operational_claims(tmp_path) == []
