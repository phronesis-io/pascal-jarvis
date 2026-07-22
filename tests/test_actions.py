"""Tests for core.actions — ACTION marker extraction and processing."""

import json
from pathlib import Path

import pytest

from core.actions import ActionProcessor, parse_params


def test_parse_params_basic():
    assert parse_params("title=hello|url=http://x") == {"title": "hello", "url": "http://x"}


def test_parse_params_empty():
    assert parse_params("") == {}


def test_parse_params_single():
    assert parse_params("query=test search") == {"query": "test search"}


def test_parse_params_value_with_equals():
    """Values containing '=' should not be split."""
    result = parse_params("desc=a=b|title=c")
    assert result["desc"] == "a=b"
    assert result["title"] == "c"


def _make_processor(tmp_path) -> ActionProcessor:
    memory_dir = tmp_path / "memory" / "system"
    memory_dir.mkdir(parents=True)
    return ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
        heartbeat_trigger_path=tmp_path / "heartbeat-trigger",
    )


def test_no_actions_passthrough(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Hello, this is a normal reply."
    assert ap.process(reply) == reply


def test_marker_stripping(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "I'll save that. [ACTION:heartbeat]"
    result = ap.process(reply)
    assert "[ACTION:" not in result
    assert "I'll save that." in result
    assert (tmp_path / "heartbeat-trigger").read_text() == "intention-check\n"


def test_task_capture_creates_task(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Got it. [ACTION:task_capture|title=写周报|type=poiesis|energy=m|est=30]"
    result = ap.process(reply)

    # Verify task was created
    tasks_file = tmp_path / "memory" / "system" / "tasks.jsonl"
    assert tasks_file.exists()
    tasks = [json.loads(line) for line in tasks_file.read_text().splitlines() if line.strip()]
    assert len(tasks) == 1
    assert tasks[0]["title"] == "写周报"
    assert tasks[0]["type"] == "poiesis"


def test_task_done(tmp_path):
    ap = _make_processor(tmp_path)
    # First capture a task
    ap.process("[ACTION:task_capture|title=test task]")
    tasks = [json.loads(l) for l in (tmp_path / "memory/system/tasks.jsonl").read_text().splitlines() if l.strip()]
    tid = tasks[0]["id"]

    # Mark it done
    ap.process(f"[ACTION:task_done|id={tid}]")
    tasks = [json.loads(l) for l in (tmp_path / "memory/system/tasks.jsonl").read_text().splitlines() if l.strip()]
    assert tasks[0]["status"] == "done"


def test_multiple_actions(tmp_path):
    ap = _make_processor(tmp_path)
    reply = "Done. [ACTION:task_capture|title=task1] Also [ACTION:task_capture|title=task2]"
    result = ap.process(reply)

    tasks_file = tmp_path / "memory" / "system" / "tasks.jsonl"
    tasks = [json.loads(l) for l in tasks_file.read_text().splitlines() if l.strip()]
    assert len(tasks) == 2


def _pending_broadcast(tmp_path, pending_id="123_456"):
    path = tmp_path / "eigenflux" / "pending_publish" / f"{pending_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "id": pending_id, "content": "need collaborators",
        "notes": {"type": "demand"}, "url": "https://example.com",
    }))
    return path


def test_eigenflux_publish_action_sends_selected_pending(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    pending = _pending_broadcast(tmp_path)
    calls = []

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr("core.actions.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd) or Result())
    result = ap._do_eigenflux_publish("id=123_456")
    assert result == "✅ 已广播"
    assert calls[0][:2] == ["eigenflux", "publish"]
    assert not pending.exists()


def test_eigenflux_publish_stamps_publish_state(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    _pending_broadcast(tmp_path)

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr("core.actions.subprocess.run", lambda *a, **kw: Result())
    ap._do_eigenflux_publish("id=123_456")
    state = json.loads((tmp_path / "eigenflux" / "publish_state.json").read_text())
    assert state["last_publish_epoch"] > 0
    assert state["recent"][-1]["content_preview"].startswith("need collaborators")


def test_eigenflux_publish_failure_keeps_pending(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    pending = _pending_broadcast(tmp_path)

    class Result:
        returncode = 1
        stdout = ""
        stderr = "offline"

    monkeypatch.setattr("core.actions.subprocess.run", lambda *a, **kw: Result())
    assert "仍保留" in ap._do_eigenflux_publish("id=123_456")
    assert pending.exists()


def test_eigenflux_cancel_publish_action(tmp_path):
    ap = _make_processor(tmp_path)
    pending = _pending_broadcast(tmp_path)
    assert ap._do_eigenflux_cancel_publish("id=123_456") == "已取消广播"
    assert not pending.exists()


def test_intent_create(tmp_path):
    """Intent create should not crash even without the DB (graceful error).

    create_intent writes to the real intentions DB (dashboard.db is path-fixed),
    so this test self-cleans the row it creates — otherwise every run leaves a
    junk 'test' intent in live data.
    """
    import re as _re
    from core import intentions as _mod
    ap = _make_processor(tmp_path)
    reply = "[ACTION:intent_create|name=test|when=2026-01-01T09:00:00|type=date|prompt=hello]"
    result = ap.process(reply)
    # Should produce either success or graceful error, not crash
    assert "Intent" in result or "❌" in result
    m = _re.search(r"id:\s*(int_\w+)", result)
    if m:
        _mod.delete_intent(m.group(1))


def test_auto_category():
    from core.actions import _auto_category
    assert _auto_category("health,rehab", "每日康复", "") == "healing"
    assert _auto_category("reading", "读 x402", "") == "healing"
    assert _auto_category("", "tushare token 到期", "续费") == "hard"
    assert _auto_category("social", "约学妹 review", "") == "external"
    assert _auto_category("", "每日日报", "夜工成果") == "autonomous"
    assert _auto_category("calendar-prep", "Prep: 周会", "") == "context"
    assert _auto_category("", "随便", "") == "none"


def test_intent_close_multiword_result(tmp_path):
    """The `do` CLI joins argv with '|'; _do_intent_close must reconstruct a
    multi-word result instead of truncating at the first space. Verifies the
    write path against the real intentions DB (record_closure round-trip)."""
    import sys
    from core import intentions as mod
    from core.intentions import create_intent, get_intent, mark_triggered, mark_executed
    ap = _make_processor(tmp_path)

    pid = create_intent(name="约学妹", trigger_type="date",
                        trigger_config={"datetime": "2026-06-11T10:00:00"},
                        category="external", closure_question="约上了吗？")
    mark_triggered(pid); mark_executed(pid)   # spawns awaiting follow-up

    # Simulate the `do` CLI path: argv joined with '|'
    raw = "|".join([f"id={pid}", "outcome=done", "result=招到", "候选人", "下周面"])
    out = ap._do_intent_close(raw)
    assert out == "Closure recorded"
    p = get_intent(pid)
    assert p["closure_status"] == "done"
    assert p["closure_result"] == "招到 候选人 下周面"   # full multi-word, not truncated
    # cleanup so we don't leave residue in the shared real DB
    mod.delete_intent(pid)
    mod.delete_intent(f"{pid}__fu")


def test_unknown_actions_preserved_for_bash(tmp_path):
    """Actions not handled by Python (bg, jobs, etc.) must keep their markers
    so the bash layer can process them."""
    ap = _make_processor(tmp_path)
    reply = "OK [ACTION:bg|prompt=research] and [ACTION:task_capture|title=test]"
    result = ap.process(reply)
    assert "[ACTION:bg|" in result, "bg marker must be preserved for bash"
    assert "[ACTION:task_capture|" not in result, "task_capture should be handled and stripped"


def test_praxis_lifecycle(tmp_path):
    ap = _make_processor(tmp_path)
    # Add
    ap.process("[ACTION:praxis_add|title=拉伸|freq=daily|time=08:30|dur=20]")
    praxis_file = tmp_path / "memory" / "system" / "praxis.jsonl"
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert len(items) == 1
    pid = items[0]["id"]

    # Done
    ap.process(f"[ACTION:praxis_done|id={pid}]")
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert items[0]["streak_current"] == 1

    # Remove
    ap.process(f"[ACTION:praxis_remove|id={pid}]")
    items = [json.loads(l) for l in praxis_file.read_text().splitlines() if l.strip()]
    assert len(items) == 0


def test_intent_close_via_passthrough(tmp_path, monkeypatch):
    """Memorial closure buttons pass via=button (placed BEFORE result=); the
    handler must forward it to record_closure so one-tap telemetry survives
    the memorial migration. Everything after result= still folds into the
    result text."""
    from core import intentions as mod
    ap = _make_processor(tmp_path)
    seen = {}

    def fake_record_closure(pid, outcome="done", result="", via="cli"):
        seen.update(pid=pid, outcome=outcome, result=result, via=via)
        return True

    monkeypatch.setattr(mod, "record_closure", fake_record_closure)
    raw = "id=int_x|outcome=done|via=button|result=做了（按钮记录）"
    assert ap._do_intent_close(raw) == "Closure recorded"
    assert seen["via"] == "button"
    assert seen["result"] == "做了（按钮记录）"


def test_process_execute_false_strips_all_markers_and_runs_nothing(tmp_path):
    """REQ-102: non-owner group replies must not execute ANY action —
    python-handled or bash-layer — and markers must not leak to the group."""
    from core.actions import ActionProcessor
    ap = ActionProcessor(str(tmp_path), str(tmp_path / "memory"), "jobs", "")
    reply = ("好的我来安排。[ACTION:calendar_create|title=测试|time=2026-07-15T10:00]"
             " 顺便跑个任务 [ACTION:bg|prompt=rm -rf /] 完成。")
    out = ap.process(reply, execute=False)
    assert "[ACTION:" not in out
    assert "动作类指令仅限主人触发" in out
    assert "好的我来安排。" in out
    # nothing was created on disk
    assert not (tmp_path / "jobs").exists() or not any((tmp_path / "jobs").iterdir())


def test_process_execute_false_no_markers_passthrough(tmp_path):
    from core.actions import ActionProcessor
    ap = ActionProcessor(str(tmp_path), str(tmp_path / "memory"), "jobs", "")
    assert ap.process("纯聊天回复", execute=False) == "纯聊天回复"
