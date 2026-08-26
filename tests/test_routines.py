"""User-authored Routines: definition, autonomy enforcement, and audit.

The invariants under test are the ones that make a routine trustworthy enough
to run unattended:

  - a definition that cannot fire is rejected at creation, not at 3am;
  - the autonomy level is enforced by code over the *stored* value, so a model
    cannot promote itself by asking;
  - `observe` reaches nobody, ever;
  - every claimed run reaches a terminal audit row, including the ones the
    model forgot about;
  - two processes racing one occurrence produce one run, not two cards;
  - evidence providers refuse to read outside their roots or leak credentials.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.db as db_module  # noqa: E402
from core import routine_evidence, routines  # noqa: E402
from core.timeutil import now_local  # noqa: E402

WORK_RECEIPT = "读取声明证据并完成结果核对"


@pytest.fixture()
def routine_db(tmp_path, monkeypatch):
    """Isolated DB + JARVIS_DIR. Never touches the production ledger."""
    dbfile = tmp_path / "test.db"
    db_module.DB_PATH = dbfile
    db_module._connection = None
    routines._initialized = False
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "memory" / "hot").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    if db_module._connection:
        db_module._connection.close()
        db_module._connection = None
    db_module.DB_PATH = db_module._DEFAULT_DB_PATH
    routines._initialized = False


def _mk(routine_db, **over):
    kwargs = dict(name="周报", trigger_type="cron", trigger_expr="0 17 * * 5",
                  instruction="把这周的提交汇总成一段话")
    kwargs.update(over)
    return routines.create_routine(**kwargs)


# ── definition validation ────────────────────────────────────────────────


class TestDefinition:
    def test_creates_with_next_fire(self, routine_db):
        r = _mk(routine_db)
        assert r["id"].startswith("rt_")
        assert r["autonomy"] == routines.AUTONOMY_PROPOSE  # safe default
        assert r["next_fire_at"]

    def test_rejects_malformed_cron_at_creation(self, routine_db):
        with pytest.raises(routines.RoutineError, match="5 段"):
            _mk(routine_db, trigger_expr="0 17 * *")

    def test_rejects_cron_that_never_fires(self, routine_db):
        # Feb 30th: syntactically fine, will never come.
        with pytest.raises(routines.RoutineError, match="不会触发"):
            _mk(routine_db, trigger_expr="0 12 30 2 *")

    def test_rejects_interval_below_floor(self, routine_db):
        with pytest.raises(routines.RoutineError, match="300 秒"):
            _mk(routine_db, trigger_type="interval", trigger_expr="30")

    def test_rejects_unknown_evidence_source(self, routine_db):
        with pytest.raises(routines.RoutineError, match="未知证据源"):
            _mk(routine_db, evidence="crystal_ball")

    def test_rejects_unknown_autonomy(self, routine_db):
        with pytest.raises(routines.RoutineError, match="autonomy"):
            _mk(routine_db, autonomy="yolo")

    def test_rejects_duplicate_name(self, routine_db):
        _mk(routine_db)
        with pytest.raises(routines.RoutineError, match="已经有一个"):
            _mk(routine_db)

    def test_empty_instruction_rejected(self, routine_db):
        with pytest.raises(routines.RoutineError, match="产出"):
            _mk(routine_db, instruction="   ")

    def test_edit_rejects_unknown_field(self, routine_db):
        r = _mk(routine_db)
        with pytest.raises(routines.RoutineError, match="不认识的字段"):
            routines.update_routine(r["id"], colour="blue")

    def test_resume_rearms_from_now_not_backlog(self, routine_db):
        """A week paused must not dump a week of cards on resume."""
        r = _mk(routine_db, trigger_type="interval", trigger_expr="3600")
        routines.set_status(r["id"], routines.STATUS_PAUSED)
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = ? WHERE id = ?",
                   ("2020-01-01 00:00", r["id"]))
        db.commit()
        resumed = routines.set_status(r["id"], routines.STATUS_ACTIVE)
        assert resumed["next_fire_at"] > now_local().strftime("%Y-%m-%d %H:%M")
        assert routines.claim_due() == []

    def test_default_list_reports_paused_routines_instead_of_data_loss(
            self, routine_db, capsys):
        row = _mk(routine_db)
        routines.set_status(row["id"], routines.STATUS_PAUSED)

        assert routines.main(["list"]) == 0

        output = capsys.readouterr().out
        assert "当前没有运行中的例程" in output
        assert "另有 1 条已暂停" in output
        assert "list --all" in output
        assert "还没有例程" not in output


# ── firing and claim safety ──────────────────────────────────────────────


class TestClaim:
    def test_due_routine_is_claimed_once(self, routine_db):
        r = _mk(routine_db, trigger_type="interval", trigger_expr="300")
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = ? WHERE id = ?",
                   ("2020-01-01 00:00", r["id"]))
        db.commit()

        first = routines.claim_due()
        assert [x["id"] for x in first] == [r["id"]]
        # The watermark advanced, so an immediate second pass finds nothing.
        assert routines.claim_due() == []

    def test_claim_opens_exactly_one_running_run(self, routine_db):
        r = _mk(routine_db, trigger_type="interval", trigger_expr="300")
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()
        routines.claim_due()
        runs = routines.list_runs(r["id"])
        assert len(runs) == 1 and runs[0]["status"] == "running"

    def test_paused_routine_never_claimed(self, routine_db):
        r = _mk(routine_db, trigger_type="interval", trigger_expr="300")
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()
        routines.set_status(r["id"], routines.STATUS_PAUSED)
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()
        assert routines.claim_due() == []

    def test_stuck_run_is_swept_not_left_running(self, routine_db):
        r = _mk(routine_db)
        db = db_module.get_db()
        old = (now_local() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO routine_runs (id, routine_id, started_at, status)"
            " VALUES ('rr_stuck', ?, ?, 'running')", (r["id"], old))
        db.commit()
        assert routines.sweep_stuck_runs() == 1
        assert routines.list_runs(r["id"])[0]["status"] == "failed"


# ── autonomy contract ────────────────────────────────────────────────────


class TestAutonomy:
    def test_propose_refuses_every_action(self, routine_db):
        permitted, refused = routines.authorize_actions(
            routines.AUTONOMY_PROPOSE, [{"type": "add_task", "title": "x"}])
        assert permitted == []
        assert refused and "点头" in refused[0]

    def test_observe_refuses_every_action(self, routine_db):
        permitted, refused = routines.authorize_actions(
            routines.AUTONOMY_OBSERVE, [{"type": "note", "text": "x"}])
        assert permitted == [] and refused

    def test_act_permits_only_allowlisted_types(self, routine_db):
        permitted, refused = routines.authorize_actions(
            routines.AUTONOMY_ACT,
            [{"type": "add_task", "title": "ok"},
             {"type": "send_email", "to": "someone@example.com"}])
        assert [a["type"] for a in permitted] == ["add_task"]
        assert any("send_email" in r for r in refused)

    def test_act_caps_action_count(self, routine_db):
        many = [{"type": "note", "text": str(i)} for i in range(12)]
        permitted, refused = routines.authorize_actions(
            routines.AUTONOMY_ACT, many)
        assert len(permitted) == routines.MAX_ACTIONS_PER_RUN
        assert any("上限" in r for r in refused)

    def test_model_cannot_promote_its_own_level(self, routine_db, monkeypatch):
        """A model claiming autonomy in its payload changes nothing.

        Enforcement reads the stored routine, so the only lever is the record
        the user created.
        """
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db, autonomy=routines.AUTONOMY_PROPOSE)
        run_id = _claim_one(r)
        routines.apply_run_result({"routines": {run_id: {
            "title": "t", "body": "b", "work_receipt": WORK_RECEIPT,
            "autonomy": "act",
            "actions": [{"type": "note", "text": "sneaky"}]}}})
        run = routines.list_runs(r["id"])[0]
        assert run["actions"] == []          # nothing executed
        assert "未执行（超出授权）" in sent[0]["body"]


# ── delivery and audit closure ───────────────────────────────────────────


def _stub_memorial(monkeypatch, sink):
    """Capture memorial.create instead of touching the production ledger."""
    import core.memorial as memorial

    def fake_create(**kwargs):
        sink.append(kwargs)
        return f"mem_{len(sink)}", True

    monkeypatch.setattr(memorial, "create",
                        lambda **kw: fake_create(**kw))
    return sink


def _claim_one(routine: dict) -> str:
    db = db_module.get_db()
    db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
               "WHERE id = ?", (routine["id"],))
    db.commit()
    claimed = routines.claim_due()
    assert claimed, "expected the routine to be due"
    # emit_due_block writes the in-flight file the post-hook reads; claim_due
    # alone does not, so mirror it here.
    routines._inflight_path().parent.mkdir(parents=True, exist_ok=True)
    routines._inflight_path().write_text(json.dumps(
        [{"run_id": c["run_id"], "routine_id": c["id"], "name": c["name"],
          "autonomy": c["autonomy"]} for c in claimed]), encoding="utf-8")
    return claimed[0]["run_id"]


class TestCardButtons:
    def test_pause_button_is_in_the_shape_the_executor_dispatches(self, routine_db):
        """Regression: written first as the CLI's 'type:k=v' spec string, which
        _execute_action cannot parse — the button would tap and do nothing."""
        from core.actions import ActionProcessor
        r = _mk(routine_db)
        action = [o for o in routines._card_options(r) if o["key"] == "pause"][0]["action"]
        assert isinstance(action, dict) and isinstance(action["params"], dict)
        assert action["params"]["id"] == r["id"]
        assert hasattr(ActionProcessor, f"_do_{action['type']}")

    def test_pause_button_actually_pauses(self, routine_db):
        from core.actions import ActionProcessor
        r = _mk(routine_db)
        ap = ActionProcessor(jarvis_dir=str(routine_db),
                             memory_dir=str(routine_db / "memory"),
                             jobs_dir=str(routine_db / "jobs"), log_file="")
        assert ap._do_routine_pause(f"id={r['id']}").startswith("✅")
        assert routines.get_routine(r["id"])["status"] == routines.STATUS_PAUSED

    def test_create_action_reports_a_bad_definition_instead_of_raising(
            self, routine_db):
        from core.actions import ActionProcessor
        ap = ActionProcessor(jarvis_dir=str(routine_db),
                             memory_dir=str(routine_db / "memory"),
                             jobs_dir=str(routine_db / "jobs"), log_file="")
        out = ap._do_routine_create("name=x|type=cron|expr=nope|instruction=y")
        assert out.startswith("❌") and "5 段" in out


class TestApply:
    def test_propose_delivers_one_card(self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db)
        run_id = _claim_one(r)
        out = routines.apply_run_result(
            {"routines": {run_id: {
                "title": "本周 12 次提交", "body": "正文",
                "work_receipt": WORK_RECEIPT,
            }}})
        assert out[0]["status"] == "delivered"
        assert len(sent) == 1
        assert sent[0]["title"] == "本周 12 次提交"
        assert sent[0]["work_receipt"] == WORK_RECEIPT
        assert routines.list_runs(r["id"])[0]["memorial_id"] == "mem_1"

    def test_observe_delivers_nothing(self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db, autonomy=routines.AUTONOMY_OBSERVE)
        run_id = _claim_one(r)
        out = routines.apply_run_result(
            {"routines": {run_id: {"title": "t", "body": "有内容"}}})
        assert out[0]["status"] == "observed"
        assert sent == []                                  # nobody was told
        assert routines.list_runs(r["id"])[0]["output"] == "有内容"  # but recorded

    def test_missing_run_in_envelope_is_closed_not_left_running(
            self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db)
        run_id = _claim_one(r)
        routines.apply_run_result({"routines": {}})
        run = routines.list_runs(r["id"])[0]
        assert run["id"] == run_id
        assert run["status"] == "no_output"
        assert run["finished_at"]

    def test_replayed_hook_does_not_double_deliver(self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db)
        run_id = _claim_one(r)
        payload = {"routines": {run_id: {
            "title": "t", "body": "b", "work_receipt": WORK_RECEIPT,
        }}}
        routines.apply_run_result(payload)
        routines.apply_run_result(payload)   # same batch replayed
        assert len(sent) == 1

    def test_card_failure_is_recorded_as_failed_not_delivered(
            self, routine_db, monkeypatch):
        import core.memorial as memorial
        monkeypatch.setattr(memorial, "create", lambda **kw: (_ for _ in ()).throw(
            RuntimeError("lark down")))
        r = _mk(routine_db)
        run_id = _claim_one(r)
        out = routines.apply_run_result(
            {"routines": {run_id: {
                "title": "t", "body": "b",
                "work_receipt": WORK_RECEIPT,
            }}})
        assert out[0]["status"] == "failed"
        run = routines.list_runs(r["id"])[0]
        assert run["status"] == "failed" and "发卡失败" in run["error"]
        assert run["output"] == "b"   # the work is not lost

    def test_act_executes_allowlisted_action_and_reports_it(
            self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db, autonomy=routines.AUTONOMY_ACT)
        run_id = _claim_one(r)
        routines.apply_run_result({"routines": {run_id: {
            "title": "t", "body": "正文", "work_receipt": WORK_RECEIPT,
            "actions": [{"type": "note", "text": "记一笔"}]}}})
        run = routines.list_runs(r["id"])[0]
        assert run["actions"][0]["type"] == "note"
        assert run["actions"][0]["ok"] is True
        assert "已自动执行" in sent[0]["body"]
        note = Path(os.environ["MEMORY_DIR"]) / "system" / "routine_notes.md"
        assert "记一笔" in note.read_text(encoding="utf-8")

    def test_failed_action_is_reported_not_swallowed(self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db, autonomy=routines.AUTONOMY_ACT)
        run_id = _claim_one(r)
        routines.apply_run_result({"routines": {run_id: {
            "title": "t", "body": "正文", "work_receipt": WORK_RECEIPT,
            "actions": [{"type": "add_task"}]}}})   # missing title
        run = routines.list_runs(r["id"])[0]
        assert run["actions"][0]["ok"] is False
        assert "✗" in sent[0]["body"]

    def test_missing_work_receipt_withholds_card_and_actions(
            self, routine_db, monkeypatch):
        sent = []
        _stub_memorial(monkeypatch, sent)
        r = _mk(routine_db, autonomy=routines.AUTONOMY_ACT)
        run_id = _claim_one(r)

        out = routines.apply_run_result({"routines": {run_id: {
            "title": "t", "body": "看起来该记一笔",
            "actions": [{"type": "note", "text": "不能执行"}],
        }}})

        assert out == [{"run_id": run_id, "status": "withheld"}]
        run = routines.list_runs(r["id"])[0]
        assert run["status"] == "withheld"
        assert "work_receipt" in run["error"]
        assert run["actions"] == []
        assert sent == []


# ── evidence providers ───────────────────────────────────────────────────


class TestEvidence:
    def test_reads_a_memory_file(self, routine_db, monkeypatch):
        target = Path(os.environ["MEMORY_DIR"]) / "hot" / "calendar_today.md"
        target.write_text("今天 15:00 和 X 开会", encoding="utf-8")
        text, gathered = routine_evidence.collect(["calendar"])
        assert "15:00" in text and gathered == ["calendar"]

    def test_path_escape_is_refused(self, routine_db):
        text, gathered = routine_evidence.collect(["memory:../../../etc/passwd"])
        assert "unavailable" in text and gathered == []

    def test_credential_file_is_refused_even_inside_root(self, routine_db):
        (Path(os.environ["JARVIS_DIR"]) / "jarvis.yaml").write_text(
            "token: s3cret", encoding="utf-8")
        text, gathered = routine_evidence.collect(["file:jarvis.yaml"])
        assert "s3cret" not in text
        assert "凭证" in text and gathered == []

    def test_unavailable_source_is_visible_not_silent(self, routine_db):
        text, gathered = routine_evidence.collect(["memory:nope.md"])
        assert "unavailable" in text
        assert gathered == []          # audit can tell unread from empty

    def test_one_broken_source_does_not_kill_the_others(self, routine_db):
        target = Path(os.environ["MEMORY_DIR"]) / "hot" / "calendar_today.md"
        target.write_text("有日程", encoding="utf-8")
        text, gathered = routine_evidence.collect(["memory:nope.md", "calendar"])
        assert "有日程" in text and gathered == ["calendar"]

    def test_oversized_file_is_clipped_with_notice(self, routine_db):
        big = Path(os.environ["MEMORY_DIR"]) / "hot" / "calendar_today.md"
        big.write_text("x" * 50000, encoding="utf-8")
        text, _ = routine_evidence.collect(["calendar"])
        assert "已截断" in text
        assert len(text) < routine_evidence.PROVIDER_MAX_CHARS + 200

    def test_git_provider_rejects_path_traversal(self, routine_db):
        text, gathered = routine_evidence.collect(["git:../../etc"])
        assert "unavailable" in text and gathered == []

    def test_intent_card_task_and_mail_providers_return_bounded_facts(
            self, routine_db, monkeypatch):
        monkeypatch.setattr(
            "core.intentions.list_intents",
            lambda status="pending": [{
                "name": "确认白皮书",
                "trigger_type": "date",
                "trigger_config": {"datetime": "2099-01-02T10:00:00+08:00"},
            }] if status == "pending" else [],
        )
        monkeypatch.setattr(
            "core.memorial.list_memorials",
            lambda: [{
                "ts": "2099-01-01 12:00",
                "status": "acted",
                "source": "test",
                "title": "已批方案",
            }, {
                "ts": "2099-01-01 13:00",
                "status": "pending",
                "source": "test",
                "title": "待批方案",
            }],
        )
        monkeypatch.setattr(
            "core.timeutil.now_local",
            lambda: now_local().replace(year=2099, month=1, day=2),
        )

        class Tasks:
            def __init__(self, _memory_dir):
                pass

            def active(self):
                return [{"content": "收尾发布", "status": "active"}]

        monkeypatch.setattr("core.tasks.TaskManager", Tasks)
        mail_dir = routine_db / "mail"
        mail_dir.mkdir()
        (mail_dir / "triaged.jsonl").write_text(
            json.dumps({
                "ts": "2099-01-02T08:00:00+08:00",
                "decision": "reply",
                "subject": "需要回复的邮件",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        text, gathered = routine_evidence.collect([
            "intents", "cards:7", "tasks", "mail:3",
        ])

        assert gathered == ["intents", "cards:7", "tasks", "mail:3"]
        assert "确认白皮书" in text
        assert "2 张卡，其中 1 张被批过" in text
        assert "收尾发布" in text
        assert "需要回复的邮件" in text

    def test_git_provider_records_success_and_transport_failure(
            self, routine_db, monkeypatch):
        repo = routine_db.parent / "eigenflux-pgc"
        (repo / ".git").mkdir(parents=True)

        class Result:
            returncode = 0
            stdout = "abc1234 2099-01-01 fix: bounded coordination"
            stderr = ""

        monkeypatch.setattr(
            routine_evidence.subprocess,
            "run",
            lambda *args, **kwargs: Result(),
        )
        text, gathered = routine_evidence.collect(["git:eigenflux-pgc"])
        assert gathered == ["git:eigenflux-pgc"]
        assert "bounded coordination" in text

        def fail(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("git", 20)

        monkeypatch.setattr(routine_evidence.subprocess, "run", fail)
        text, gathered = routine_evidence.collect(["git:eigenflux-pgc"])
        assert gathered == []
        assert "unavailable" in text and "执行失败" in text

    def test_evidence_validation_and_total_limit_are_explicit(
            self, routine_db, monkeypatch):
        assert routine_evidence.validate_spec(" Cards:7 ") == "cards:7"
        with pytest.raises(routine_evidence.EvidenceError, match="空的证据项"):
            routine_evidence.validate_spec(" ")
        with pytest.raises(routine_evidence.EvidenceError, match="需要参数"):
            routine_evidence.validate_spec("memory")
        with pytest.raises(routine_evidence.EvidenceError, match="超出范围"):
            routine_evidence._int_arg("91", default=7, lo=1, hi=90,
                                      label="cards")
        with pytest.raises(routine_evidence.EvidenceError, match="不是整数"):
            routine_evidence._int_arg("many", default=7, lo=1, hi=90,
                                      label="cards")

        monkeypatch.setattr(routine_evidence, "TOTAL_MAX_CHARS", 80)
        monkeypatch.setitem(routine_evidence.PROVIDERS, "large",
                            lambda _arg: "x" * 1000)
        text, gathered = routine_evidence.collect(["large", "unknown"])
        assert gathered == ["large"]
        assert "证据总量超限" in text


# ── pre-hook contract ────────────────────────────────────────────────────


class TestEmitBlock:
    def test_emits_nothing_when_no_routine_is_due(self, routine_db):
        _mk(routine_db)
        assert routines.emit_due_block() == ""

    def test_block_carries_run_id_autonomy_and_evidence(self, routine_db):
        target = Path(os.environ["MEMORY_DIR"]) / "hot" / "calendar_today.md"
        target.write_text("今天 15:00 开会", encoding="utf-8")
        r = _mk(routine_db, evidence="calendar",
                autonomy=routines.AUTONOMY_OBSERVE)
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()
        block = routines.emit_due_block()
        run_id = routines.list_runs(r["id"])[0]["id"]
        assert run_id in block
        assert "observe" in block
        assert "15:00 开会" in block
        assert "把这周的提交汇总成一段话" in block

    def test_evidence_sources_recorded_before_the_model_runs(self, routine_db):
        target = Path(os.environ["MEMORY_DIR"]) / "hot" / "calendar_today.md"
        target.write_text("x", encoding="utf-8")
        r = _mk(routine_db, evidence="calendar")
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()
        routines.emit_due_block()
        assert routines.list_runs(r["id"])[0]["evidence_sources"] == ["calendar"]

    def test_infrastructure_failure_rearms_occurrence_without_calling_it_no_output(
            self, routine_db):
        r = _mk(routine_db, trigger_type="interval", trigger_expr="3600")
        db = db_module.get_db()
        db.execute("UPDATE routines SET next_fire_at = '2020-01-01 00:00' "
                   "WHERE id = ?", (r["id"],))
        db.commit()

        routines.emit_due_block()
        original = routines.list_runs(r["id"])[0]
        result = routines.defer_inflight_infrastructure("provider timeout")

        deferred = routines.list_runs(r["id"])[0]
        assert result == {"deferred": [original["id"]]}
        assert deferred["status"] == "deferred"
        assert "provider timeout" in deferred["error"]
        assert routines.get_routine(r["id"])["last_status"] == "deferred"
        assert json.loads(routines._inflight_path().read_text()) == []

        retry_at = now_local() + timedelta(minutes=6)
        retried = routines.claim_due(now=retry_at)
        assert len(retried) == 1
        assert retried[0]["id"] == r["id"]
        assert retried[0]["run_id"] != original["id"]


def test_routine_post_distinguishes_call_failure_from_model_no_output(
        monkeypatch):
    import tasks.routine_run_post as post

    calls = []
    monkeypatch.setattr(post, "defer_inflight_infrastructure",
                        lambda reason="": calls.append(reason) or {"deferred": ["rr_x"]})
    monkeypatch.setattr(
        post, "apply_run_result",
        lambda *_args, **_kwargs: pytest.fail(
            "an infrastructure failure must not consume routine content"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("__CALL_FAILED__"))

    assert post.main() == 0
    assert calls == ["模型调用失败"]


def test_calendar_evidence_flags_a_stale_snapshot(tmp_path, monkeypatch):
    """2026-08-02: with MEMORY_DIR unset the calendar provider fell back to a
    legacy dir whose snapshot was four days old and served it as "today". A
    dated snapshot must announce its age; the model cannot discount staleness
    it cannot see."""
    import os
    import time as _time
    from core import routine_evidence

    memory = tmp_path / "memory"
    (memory / "hot").mkdir(parents=True)
    cal = memory / "hot" / "calendar_today.md"
    cal.write_text("# Calendar (synced long ago)\n08:00 旧日程\n")
    os.utime(cal, (_time.time() - 4 * 86400, _time.time() - 4 * 86400))
    monkeypatch.setenv("MEMORY_DIR", str(memory))

    text = routine_evidence._p_calendar("")
    assert "没有更新" in text and "4.0 天" in text

    os.utime(cal, None)  # fresh again
    assert "没有更新" not in routine_evidence._p_calendar("")
