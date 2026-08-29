"""Tests for core.safety — error pattern detection."""

import json
import os
import stat
from pathlib import Path

import pytest

from core.safety import (
    ERROR_PATTERNS,
    ERROR_SUBSTRINGS,
    atomic_write,
    extract_json,
    is_idle_reply,
    looks_like_error,
    parse_json_response,
    salvage_field,
    salvage_task_ids,
    sanitize_for_user,
    strip_task_framing,
    summarize,
)


def test_empty_is_error():
    assert looks_like_error("") is True
    assert looks_like_error("   ") is True


def test_whitespace_only_is_error():
    assert looks_like_error("   ") is True
    assert looks_like_error("\n\t") is True


def test_short_replies_are_safe():
    """Short legitimate replies must NOT be blocked."""
    assert looks_like_error("OK") is False
    assert looks_like_error("好的") is False
    assert looks_like_error("记住了。") is False


def test_no_response_requested_is_noop_error():
    assert looks_like_error("No response requested.") is True
    assert looks_like_error("Continue from where you left off. No response requested.") is True


def test_long_debugging_text_can_discuss_noop_phrase():
    text = (
        "日志里的 No response requested 是 Claude Code 在失败 resume 后产生的空转文本；"
        "修复方式是把它识别为 no-op provider output，而不是把这句话发给用户。"
    )
    assert looks_like_error(text) is False


def test_normal_text_is_safe():
    assert looks_like_error("Here's what I found in your portfolio today...") is False


def test_traceback_is_error():
    assert looks_like_error("Traceback (most recent call last):\n ...") is True


def test_auth_error_detected():
    assert looks_like_error("Not logged in. Please run /login.") is True


def test_each_pattern_triggers_at_line_start():
    # looks_like_error now requires the pattern at the start of a line
    # (after optional whitespace) to avoid false positives on legitimate
    # content that mentions error terms mid-sentence.
    for p in ERROR_PATTERNS:
        assert looks_like_error(f"{p}\nmore detail follows" + "x" * 20) is True, f"missed line-start: {p}"
        assert looks_like_error(f"  {p} indented") is True, f"missed indented: {p}"


def test_mid_line_mention_is_safe():
    # The user can legitimately discuss errors — these should NOT trigger.
    assert looks_like_error("The API Error was caused by a network blip" + "x" * 50) is False
    assert looks_like_error("I encountered Traceback handling in my code" + "x" * 50) is False


def test_json_auth_error_caught():
    """Regression: API errors embedded in JSON were leaking to users.
    e.g. 'Failed to authenticate. API Error: 401 {"type":"error",...}'
    """
    err = ('Failed to authenticate. API Error: 401 '
           '{"type":"error","error":{"type":"authentication_error",'
           '"message":"Invalid authentication credentials"}}')
    assert looks_like_error(err) is True


def test_json_error_type_caught():
    """JSON responses with "type":"error" should be caught."""
    err = '{"type":"error","error":{"type":"rate_limit","message":"too fast"}}'
    assert looks_like_error(err) is True


def test_claude_spend_limit_caught_even_under_card_header():
    text = "**🏛️ Phronesis**\n\nYou've hit your monthly spend limit · raise it at claude.ai/settings/usage"
    assert looks_like_error(text, proactive=True) is True


def test_claude_session_limit_caught_even_under_card_header():
    text = "**Intent**\n\nYou've hit your session limit · resets 6pm (Asia/Shanghai)"
    assert looks_like_error(text, proactive=True) is True


def test_claude_weekly_limit_caught_in_interactive_reply():
    text = "You've hit your weekly limit · resets Aug 15 at 3am (Asia/Shanghai)"
    assert looks_like_error(text) is True


def test_claude_weekly_limit_caught_even_under_card_header():
    text = "**Intent**\n\nYou've hit your weekly limit · resets Aug 15 at 3am"
    assert looks_like_error(text, proactive=True) is True


def test_account_limit_variants_share_provider_classifier():
    assert looks_like_error("You have reached your weekly limit") is True
    assert looks_like_error("Weekly usage limit reached") is True
    assert looks_like_error(
        "**Intent**\n\nWeekly usage limit reached", proactive=True
    ) is True
    assert looks_like_error(
        "I verified that you've hit your weekly limit and fallback is active."
    ) is False


def test_substring_patterns_in_json():
    for p in ERROR_SUBSTRINGS:
        text = f'some prefix {p} and more text' + "x" * 50
        assert looks_like_error(text) is True, f"missed substring: {p}"


def test_extract_json_code_fence():
    raw = '```json\n{"key": "value"}\n```'
    assert extract_json(raw) == '{"key": "value"}'


def test_strip_task_framing_removes_heartbeat_batch_header():
    raw = "HEARTBEAT — 1 card.\n\nTITLE: 今天想听听你怎么样\n最近还好吗？"
    assert strip_task_framing(raw).startswith("TITLE: 今天想听听你怎么样")


def test_strip_task_framing_keeps_heartbeat_words_inside_body():
    raw = "TITLE: 系统说明\n正文里提到 HEARTBEAT — 1 card. 不应被删除"
    assert strip_task_framing(raw) == raw


def test_extract_json_trailing_text():
    """Regression: Claude returns JSON in code fence + trailing explanation."""
    raw = '```json\n{"user_message": "test"}\n```\n\nHere is the explanation.'
    result = extract_json(raw)
    import json
    data = json.loads(result)
    assert data["user_message"] == "test"


def test_extract_json_trailing_text_with_braces():
    raw = '{"key": "value"}\n\nNote: use {"example": "not part of payload"}'
    result = extract_json(raw)
    import json
    assert json.loads(result) == {"key": "value"}


def test_extract_json_no_fence():
    raw = '{"plain": "json"}'
    assert extract_json(raw) == '{"plain": "json"}'


def test_extract_json_preamble_text():
    raw = 'Here is the result:\n{"key": "value"}\nDone.'
    result = extract_json(raw)
    import json
    assert json.loads(result)["key"] == "value"


def test_sanitize_returns_fallback_for_errors():
    assert sanitize_for_user("Traceback\n" + "x" * 20, fallback="fallback") == "fallback"


def test_sanitize_returns_original_for_safe():
    safe = "Real reply from Claude."
    assert sanitize_for_user(safe) == safe


# --- salvage from broken JSON (unescaped inner quotes) -----------------------

# The real-world failure: model put bare ASCII quotes inside the value, so
# json.loads raises and the old code dumped the whole object to the user.
BROKEN = (
    '{\n"user_message": "三个 PGC 项可以归档了——不是"放下"，是"已经做完了"。",\n'
    '"auto_decay": [\n'
    '{"task_id": "t_20260531_001", "reason": "上线完成"},\n'
    '{"task_id": "t_20260531_002", "reason": "分支已并 main"}\n]\n}'
)


def test_broken_json_is_unparseable():
    import json
    try:
        json.loads(extract_json(BROKEN))
        assert False, "expected JSONDecodeError"
    except json.JSONDecodeError:
        pass


def test_salvage_field_recovers_message_with_inner_quotes():
    msg = salvage_field(BROKEN, "user_message")
    assert msg.startswith("三个 PGC")
    assert '"放下"' in msg  # inner quotes preserved
    assert "已经做完了" in msg
    assert "auto_decay" not in msg  # stops before the next key


def test_salvage_task_ids_recovers_all_ids():
    assert salvage_task_ids(BROKEN) == ["t_20260531_001", "t_20260531_002"]


def test_salvage_field_missing_returns_none():
    assert salvage_field('{"other": "x"}', "user_message") is None


def test_salvage_field_handles_value_at_end():
    raw = '{"user_message": "结束语带"引号"。"}'
    assert salvage_field(raw, "user_message") == '结束语带"引号"。'


# --- parse_json_response (the shared extract+loads boundary) ------------------


def test_parse_json_response_plain_object():
    assert parse_json_response('{"user_message": "hi"}') == {"user_message": "hi"}


def test_parse_json_response_code_fence_and_trailing():
    raw = '```json\n{"a": 1}\n```\n\nHere is why.'
    assert parse_json_response(raw) == {"a": 1}


def test_parse_json_response_preamble():
    assert parse_json_response('Sure!\n{"a": 1}\nDone.') == {"a": 1}


def test_parse_json_response_ignores_braced_trailer():
    raw = '```json\n{"tasks": {"a": "ok"}, "user_message": ""}\n```\nNote: {"debug": true}'
    assert parse_json_response(raw) == {"tasks": {"a": "ok"}, "user_message": ""}


def test_parse_json_response_empty_is_none():
    assert parse_json_response("") is None
    assert parse_json_response("   \n ") is None


def test_parse_json_response_broken_is_none():
    # Unescaped inner quotes — callers fall back to salvage_field on None.
    assert parse_json_response(BROKEN) is None


def test_parse_json_response_non_object_is_none():
    # A bare list/string/number is valid JSON but not the expected envelope;
    # returning None keeps callers' `.get(...)` from blowing up on a non-dict.
    assert parse_json_response('[1, 2, 3]') is None
    assert parse_json_response('"just a string"') is None
    assert parse_json_response('42') is None


def test_parse_json_response_plain_text_is_none():
    assert parse_json_response("好的，已经记下了。") is None


# --- summarize (the shared card-summary truncation) --------------------------


def test_summarize_short_text_unchanged():
    assert summarize("line1\nline2") == "line1\nline2"


def test_summarize_exactly_max_lines_no_ellipsis():
    text = "\n".join(f"l{i}" for i in range(4))
    assert summarize(text) == text
    assert not summarize(text).endswith("...")


def test_summarize_truncates_with_ellipsis():
    text = "\n".join(f"l{i}" for i in range(10))
    result = summarize(text)
    assert result == "l0\nl1\nl2\nl3\n..."


def test_summarize_respects_max_lines_arg():
    text = "\n".join(f"l{i}" for i in range(5))
    assert summarize(text, max_lines=2) == "l0\nl1\n..."


def test_summarize_strips_surrounding_whitespace():
    assert summarize("\n\n  hello  \n\n") == "hello"


# ── strip_task_framing (REQ-104) ─────────────────────────────────────────

def test_strip_task_framing_removes_headers():
    from core.safety import strip_task_framing
    assert strip_task_framing(
        "=== TASK: checkin ===\n今天不错。") == "今天不错。"
    assert strip_task_framing(
        "[CHECKIN]\n\n昨晚你聊到很晚。") == "昨晚你聊到很晚。"
    assert strip_task_framing(
        "[2026-07-19 09:16] checkin\n\n正文在这。") == "正文在这。"
    # stacked headers all go
    assert strip_task_framing(
        "=== TASK: checkin ===\n[CHECKIN]\n正文。") == "正文。"


def test_strip_task_framing_keeps_content():
    from core.safety import strip_task_framing
    # bracketed tokens mid-prose are content
    s = "他说 [CHECKIN] 这个词其实是内部黑话。"
    assert strip_task_framing(s) == s
    # a Chinese/lowercase bracket line is not framing
    s2 = "[今天的重点]\n锻炼。"
    assert strip_task_framing(s2) == s2
    # all-framing input degrades to empty, caller suppresses the card
    assert strip_task_framing("[CHECKIN]") == ""
    assert strip_task_framing("") == ""


def test_strip_task_framing_keeps_cjk_timeline_lines():
    """Red-team 7/20 #1: '[ts] 中文' timeline lines are CONTENT (checkin
    evidence quotes them); only ASCII task tokens after a timestamp are
    framing."""
    from core.safety import strip_task_framing
    s = "[2026-07-19 03:00] cron失败\n详情：任务连续三次超时。"
    assert strip_task_framing(s) == s
    timeline = ("[2026-07-19 07:30] 起床锻炼\n"
                "[2026-07-19 10:00] 产品评审会\n"
                "[2026-07-19 14:00] 试讲")
    assert strip_task_framing(timeline) == timeline
    # ASCII task token after ts is still framing
    assert strip_task_framing(
        "[2026-07-19 09:16] checkin\n\n正文。") == "正文。"


def test_atomic_write_is_private_from_directory_to_final_inode(tmp_path):
    parent = tmp_path / "private-state"
    parent.mkdir(mode=0o755)
    target = parent / "state.json"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(parent.glob(f".{target.name}.*.tmp")) == []


def test_atomic_write_replace_failure_keeps_old_file_and_cleans_temp(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


# --- is_idle_reply: the idle token inside a JSON payload is data, not idle ---

def test_idle_reply_bare_token_and_empty():
    assert is_idle_reply("HEARTBEAT_OK")
    assert is_idle_reply("")
    assert is_idle_reply("   \n")


def test_idle_reply_token_leaked_in_prose_is_idle():
    # "prose + trailing token" and inline leaks stay silent (fail-safe for cards)
    assert is_idle_reply("nothing noteworthy today.\n\nHEARTBEAT_OK")
    assert is_idle_reply("🌿 关怀 / HEARTBEAT_OK + internal reasoning")
    assert is_idle_reply("回复: HEARTBEAT_OK")


def test_idle_reply_plain_content_is_not_idle():
    assert not is_idle_reply("今天的日程有三件事。")
    assert not is_idle_reply('{"user_message": "提醒：明早九点开会"}')


def test_idle_reply_token_quoted_inside_json_object_is_content():
    # 2026-08-28/29: memory-compiler envelopes quoting Jarvis transcripts
    # carried the token in a source quote and were dropped as idle for 28h.
    envelope = json.dumps({
        "schema": "jarvis.memory-candidates.v1",
        "batch_id": "mcb_x",
        "claims": [{
            "source_ref": "session_turn:abc",
            "quote": 'post 用 "HEARTBEAT_OK" in raw 判空把整批丢了',
            "kind": "fact", "claim_key": "k", "content": "c",
        }],
        "ignored_source_refs": [],
    }, ensure_ascii=False)
    assert not is_idle_reply(envelope)
    assert not is_idle_reply("```json\n" + envelope + "\n```")


def test_no_post_hook_uses_bare_substring_idle_check():
    """Every post hook must route idle detection through is_idle_reply."""
    import re
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "tasks").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r'"HEARTBEAT_OK"\s+in\s', code):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], offenders
