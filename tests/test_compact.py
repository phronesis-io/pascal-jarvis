"""Tests for core.compact — session compaction (summary generation)."""

import json
import uuid
from unittest.mock import patch

from core.aux_model import AuxiliaryModelResult
from core.compact import (generate_compact, get_compact_path,
                          get_old_session_id, read_compact)


def test_get_compact_path(tmp_path):
    path = get_compact_path(tmp_path, "user123")
    assert "user123" in str(path)
    assert path.suffix == ".md"


def test_read_compact_exists(tmp_path):
    path = get_compact_path(tmp_path, "user123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Previous session summary content")
    result = read_compact(tmp_path, "user123")
    assert "Previous session summary" in result


def test_read_compact_missing(tmp_path):
    result = read_compact(tmp_path, "nonexistent")
    assert result == ""


def test_get_old_session_id_deterministic():
    """Same inputs must always produce the same session ID."""
    id1 = get_old_session_id("user123", 5)
    id2 = get_old_session_id("user123", 5)
    assert id1 == id2
    # Must be a valid UUID
    uuid.UUID(id1)


def test_get_old_session_id_different_counters():
    id1 = get_old_session_id("user123", 5)
    id2 = get_old_session_id("user123", 6)
    assert id1 != id2


# ── generate_compact: rc!=0 discard + provider-gate env (2026-07-09 [9]) ──

_SPEND_LIMIT_LINE = ("You've hit your monthly spend limit · "
                     "raise it at claude.ai/settings/usage")

_OK_SUMMARY = ("## 摘要\n- 足够长的正常摘要内容，为满足五十字符下限再补一句：\n"
               "- 部署完成后记得验证守护进程稳定运行，无重复拉起。")


def _fake_session(tmp_path):
    """A session JSONL with enough turn content (>200 chars) to compact."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "old-session"
    lines = []
    for i in range(4):
        lines.append(json.dumps({"type": "user", "message": {
            "role": "user", "content": f"第{i}条用户消息，" + "聊部署的事。" * 15}}))
        lines.append(json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": f"第{i}条回复，" + "已经处理好。" * 15}}))
    (session_dir / f"{sid}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return session_dir, sid


def _run_generate(tmp_path, text, capture=None):
    session_dir, sid = _fake_session(tmp_path)

    def fake_run(prompt, **kwargs):
        if capture is not None:
            capture["prompt"] = prompt
            capture["kwargs"] = kwargs
        return AuxiliaryModelResult(text=text)

    with patch("core.compact.run_auxiliary_model", side_effect=fake_run):
        return generate_compact(tmp_path, session_dir, sid, "user123")


def test_generate_compact_nonzero_rc_discards_stdout(tmp_path):
    """The live failure: the CLI prints the spend-limit error on STDOUT with
    rc=1; it is 76 chars so the old len>=50 guard passed it and the error line
    became the saved 'previous session summary'."""
    out = _run_generate(tmp_path, text="")

    assert out == ""
    assert not get_compact_path(tmp_path, "user123").exists()
    assert read_compact(tmp_path, "user123") == ""


def test_generate_compact_success_saves(tmp_path):
    summary = ("## 摘要\n- 用户在部署新版本，重点是守护进程与机器人重启顺序\n"
               "- 决定先修守护进程，再启动机器人，避免全线沉默\n"
               "- 待办：明早验证 launchd 任务稳定，无重复拉起")
    out = _run_generate(tmp_path, text=summary)

    assert out == summary
    assert get_compact_path(tmp_path, "user123").read_text(encoding="utf-8") == summary


def test_generate_compact_uses_text_only_shared_provider_chain(tmp_path):
    captured = {}

    out = _run_generate(tmp_path, text=_OK_SUMMARY, capture=captured)

    assert out.startswith("## 摘要")
    assert captured["kwargs"]["allow_tools"] is False
    assert captured["kwargs"]["timeout"] == 180
    assert "对话记录" in captured["prompt"]
