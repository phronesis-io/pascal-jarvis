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


def _make_processor(tmp_path, *, owner_authenticated=False) -> ActionProcessor:
    memory_dir = tmp_path / "memory" / "system"
    memory_dir.mkdir(parents=True)
    return ActionProcessor(
        jarvis_dir=tmp_path,
        memory_dir=tmp_path / "memory",
        jobs_dir=tmp_path / "jobs",
        heartbeat_trigger_path=tmp_path / "heartbeat-trigger",
        owner_authenticated=owner_authenticated,
    )


def test_failed_delegation_card_action_raises_instead_of_claiming_success(tmp_path):
    processor = _make_processor(tmp_path, owner_authenticated=True)

    with pytest.raises(RuntimeError, match="未生效"):
        processor._do_delegation_confirm(
            "id=missing|version=1|principal=owner"
        )


def test_failed_delegation_retry_raises_instead_of_claiming_recovery(tmp_path):
    processor = _make_processor(tmp_path, owner_authenticated=True)

    with pytest.raises(RuntimeError, match="没有恢复"):
        processor._do_delegation_retry("id=missing|version=1")


def test_failed_iteration_card_action_raises_instead_of_claiming_success(tmp_path):
    processor = _make_processor(tmp_path, owner_authenticated=True)

    with pytest.raises(RuntimeError, match="没有进入研发队列"):
        processor._do_iteration_approve("id=missing")


def test_model_marker_cannot_approve_owner_decisions(tmp_path):
    processor = _make_processor(tmp_path)

    result = processor.process(
        "已批准。[ACTION:delegation_confirm|id=dlg|version=1|principal=owner]"
    )

    assert "[ACTION:" not in result
    assert "只能通过已认证的卡片按钮或控制台" in result
    assert "已批准" not in result


def test_direct_owner_handler_requires_authenticated_callback(tmp_path):
    processor = _make_processor(tmp_path)

    with pytest.raises(RuntimeError, match="authenticated"):
        processor._do_iteration_reject("id=proposal")


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

    monkeypatch.setattr("core.eigenflux_publish.subprocess.run",
                        lambda cmd, **kw: calls.append(cmd) or Result())
    monkeypatch.setattr("core.eigenflux_publish.resolve_eigenflux_bin",
                        lambda: "/resolved/bin/eigenflux")
    result = ap._do_eigenflux_publish("id=123_456")
    assert result == "✅ 已广播"
    # ABSOLUTE path, not the bare name: the card callback runs under launchd
    # whose PATH lacks ~/.local/bin — bare "eigenflux" turned the 7/24
    # approval into `[Errno 2] No such file or directory: 'eigenflux'`.
    assert calls[0][:2] == ["/resolved/bin/eigenflux", "publish"]
    assert not pending.exists()


def test_eigenflux_publish_stamps_publish_state(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    _pending_broadcast(tmp_path)

    class Result:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr("core.eigenflux_publish.subprocess.run",
                        lambda *a, **kw: Result())
    monkeypatch.setattr("core.eigenflux_publish.resolve_eigenflux_bin",
                        lambda: "/resolved/bin/eigenflux")
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

    monkeypatch.setattr("core.eigenflux_publish.subprocess.run",
                        lambda *a, **kw: Result())
    monkeypatch.setattr("core.eigenflux_publish.resolve_eigenflux_bin",
                        lambda: "/resolved/bin/eigenflux")
    assert "重试" in ap._do_eigenflux_publish("id=123_456")
    assert pending.exists()
    # 「重试」is now a promise something keeps: the failure stamps the approval
    # onto the draft so reconcile_pending_drafts retries it deterministically.
    stamped = json.loads(pending.read_text())
    assert stamped["approved_epoch"] > 0
    assert stamped["attempts"] == 1
    assert "offline" in stamped["last_error"]


def test_eigenflux_cancel_publish_action(tmp_path):
    ap = _make_processor(tmp_path)
    pending = _pending_broadcast(tmp_path)
    assert ap._do_eigenflux_cancel_publish("id=123_456") == "已取消广播"
    assert not pending.exists()


def test_intent_create(tmp_path):
    """Intent creation uses the test-isolated runtime database."""
    ap = _make_processor(tmp_path)
    reply = "[ACTION:intent_create|name=test|when=2026-01-01T09:00:00|type=date|prompt=hello]"
    result = ap.process(reply)
    assert "Intent" in result
    assert "❌" not in result


class _CmdResult:
    """Fake subprocess.CompletedProcess for monkeypatching subprocess.run."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_primary_calendar(cmd, calendar_id="cal_primary_1"):
    """Return a success Result for a `calendars primary` lookup, else None."""
    if cmd[:4] == ["lark-cli", "calendar", "calendars", "primary"]:
        return _CmdResult(0, stdout=json.dumps({
            "data": {"calendars": [{"calendar": {"calendar_id": calendar_id}}]}
        }))
    return None


def test_run_cmd_reports_failure_on_nonzero_exit_with_empty_stdout(monkeypatch):
    """lark-cli's real failure shape: nonzero exit, empty stdout, error on
    stderr. A caller that only checked stdout for the string FAILED would
    treat this as success (this was the actual bug)."""
    from core.actions import _run_cmd

    monkeypatch.setattr(
        "core.actions.subprocess.run",
        lambda *a, **kw: _CmdResult(2, stdout="", stderr='{"error":"invalid_argument"}'),
    )
    result = _run_cmd(["lark-cli", "calendar", "events", "delete"])
    assert result.startswith("FAILED")
    assert "invalid_argument" in result


def test_run_cmd_success_ignores_returncode_zero():
    from core.actions import _run_cmd
    import core.actions as actions_mod

    result = actions_mod._run_cmd(["true"])
    assert not result.startswith("FAILED")


def test_calendar_delete_reports_failure_instead_of_false_success(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)

    def fake_run(cmd, **kw):
        r = _fake_primary_calendar(cmd)
        if r is not None:
            return r
        assert cmd[:4] == ["lark-cli", "calendar", "events", "delete"]
        return _CmdResult(2, stdout="", stderr='{"error":{"message":"invalid calendar_id"}}')

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    result = ap._do_calendar_delete("event_id=evt1|title=Standup")
    assert "❌" in result
    assert "已删除" not in result


def test_calendar_delete_passes_resolved_primary_calendar_id(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = _fake_primary_calendar(cmd)
        if r is not None:
            return r
        return _CmdResult(0, stdout="{}")

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    result = ap._do_calendar_delete("event_id=evt1|title=Standup")
    assert result == "✅ 已删除日程: Standup"
    delete_call = next(c for c in calls if "delete" in c)
    assert "--calendar-id" in delete_call
    assert delete_call[delete_call.index("--calendar-id") + 1] == "cal_primary_1"


def test_calendar_delete_honors_explicit_calendar_id_param(monkeypatch, tmp_path):
    """An optional calendar_id in the marker (e.g. a shared, non-primary
    calendar) should be used as-is without resolving the primary calendar."""
    ap = _make_processor(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _CmdResult(0, stdout="{}")

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    ap._do_calendar_delete("event_id=evt1|title=Standup|calendar_id=shared_cal_9")
    assert len(calls) == 1  # no primary-calendar lookup needed
    delete_call = calls[0]
    assert delete_call[delete_call.index("--calendar-id") + 1] == "shared_cal_9"


def test_calendar_update_reports_failure_instead_of_false_success(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)

    def fake_run(cmd, **kw):
        r = _fake_primary_calendar(cmd)
        if r is not None:
            return r
        assert cmd[:4] == ["lark-cli", "calendar", "events", "patch"]
        return _CmdResult(1, stdout="", stderr='{"error":"token expired"}')

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    result = ap._do_calendar_update("event_id=evt1|field=summary|value=新标题")
    assert "❌" in result
    assert "已更新" not in result


def test_calendar_update_passes_resolved_primary_calendar_id(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        r = _fake_primary_calendar(cmd)
        if r is not None:
            return r
        return _CmdResult(0, stdout="{}")

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    result = ap._do_calendar_update("event_id=evt1|field=summary|value=新标题")
    assert result == "✅ 已更新日程: summary → 新标题"
    patch_call = next(c for c in calls if "patch" in c)
    assert "--calendar-id" in patch_call
    assert patch_call[patch_call.index("--calendar-id") + 1] == "cal_primary_1"


def test_primary_calendar_id_resolved_once_per_processor(monkeypatch, tmp_path):
    """Two calendar actions in the same reply should only look up the
    primary calendar once (instance-level cache)."""
    ap = _make_processor(tmp_path)
    primary_lookups = []

    def fake_run(cmd, **kw):
        r = _fake_primary_calendar(cmd)
        if r is not None:
            primary_lookups.append(cmd)
            return r
        return _CmdResult(0, stdout="{}")

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    ap._do_calendar_update("event_id=evt1|field=summary|value=x")
    ap._do_calendar_delete("event_id=evt2|title=y")
    assert len(primary_lookups) == 1


def test_calendar_create_reports_failure_reason_instead_of_false_success(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)

    def fake_run(cmd, **kw):
        if cmd[:3] == ["lark-cli", "calendar", "+freebusy"]:
            return _CmdResult(0, stdout='{"busy": false}')
        assert cmd[:3] == ["lark-cli", "calendar", "+create"]
        return _CmdResult(3, stdout="", stderr='{"error":"quota exceeded"}')

    monkeypatch.setattr("core.actions.subprocess.run", fake_run)
    result = ap._do_calendar_create(
        "title=评审会|start=2026-08-01T10:00:00|end=2026-08-01T11:00:00"
    )
    assert result.startswith("❌ 日程创建失败")
    assert "quota exceeded" in result


def test_task_create_reports_failure_instead_of_false_success(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    monkeypatch.setattr(
        "core.actions.subprocess.run",
        lambda *a, **kw: _CmdResult(1, stdout="", stderr='{"error":"unauthorized"}'),
    )
    result = ap._do_task_create("title=买菜")
    assert result.startswith("❌ 任务创建失败")
    assert "已创建" not in result


def test_task_complete_reports_failure_instead_of_false_success(monkeypatch, tmp_path):
    ap = _make_processor(tmp_path)
    monkeypatch.setattr(
        "core.actions.subprocess.run",
        lambda *a, **kw: _CmdResult(1, stdout="", stderr='{"error":"not found"}'),
    )
    result = ap._do_task_complete("task_id=t1")
    assert result.startswith("❌ 任务完成标记失败")
    assert "已完成" not in result


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


def test_malformed_action_type_rejected(tmp_path):
    """Action types with dots, slashes, or uppercase are silently ignored —
    prevents getattr abuse via crafted markers. They pass through unhandled
    (same as any unknown action) rather than being dispatched to a handler."""
    ap = _make_processor(tmp_path)
    # __class__ starts with underscore, ../../etc has dots/slashes,
    # Normal_CaSe has uppercase — all fail the [a-z][a-z0-9_]{0,30} regex
    reply = "test [ACTION:__class__|x=1] and [ACTION:../../etc|y=2] and [ACTION:Normal_CaSe|z=3]"
    result = ap.process(reply)
    # All three markers survive (none was handled by a _do_X method)
    assert "[ACTION:__class__" in result
    assert "[ACTION:../../etc" in result
    assert "[ACTION:Normal_CaSe" in result
    # Crucially: no handler was invoked (no side effects on disk)
    assert not (tmp_path / "memory" / "system" / "tasks.jsonl").exists()
