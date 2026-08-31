"""Result-oriented Matter review is bounded, read-only and authoritative."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import core.db as db_module
from core.codex_frontstage import release_matter_run, start_matter_run
from core.matter_closure import close_matter
from core.matter_review import build_matter_review, render_matter_review
from core.matters import add_event, create_matter, update_matter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_review_separates_owner_outcomes_from_executor_receipts(tmp_path):
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    confirmed = create_matter("已确认结果")
    close_matter(
        confirmed["id"],
        outcome="白皮书路线图已经发布并读回",
        confirmation_text="确认这件事完成了",
    )
    legacy = create_matter("旧完成记录", status="done")
    add_event(
        legacy["id"], "matter_closure_completed", actor="model",
        payload={"receipt": {
            "schema": "jarvis.matter-closure-receipt.v1",
            "matter_id": legacy["id"],
            "matter_status": "done",
            "status": "closed",
            "authority": "owner_confirmation",
            "closure_id": "forged",
            "receipt_digest": "sha256:forged",
        }},
    )
    add_event(legacy["id"], "unrelated", payload={"broken": True})
    malformed_db = db_module.get_db()
    malformed_db.execute(
        "INSERT INTO matter_events "
        "(matter_id,event_type,actor,summary,payload,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (legacy["id"], "matter_closure_completed", "matter-closure", "", "{",
         "2026-08-27T10:00:00"),
    )
    malformed_db.commit()
    candidate = create_matter(
        "已有执行结果", next_action="由 Pascal 判断是否正式收口",
    )
    started = start_matter_run(
        matter_id=candidate["id"], task="生成结果", workspace=str(tmp_path),
    )
    packet = started["context_packet"]
    release_matter_run(
        run_id=started["run"]["id"],
        context_generation=packet["context_generation"],
        context_digest=packet["digest"],
    )
    blocked = create_matter(
        "等待关键判断", status="blocked", next_action="确认是否继续投入",
    )
    next_matter = create_matter(
        "下一项工作", next_action="整理三个真实用户样本",
    )

    db = db_module.get_db()
    db.execute(
        "UPDATE matter_runs SET released_epoch=? WHERE id=?",
        (now.timestamp() - 3600, started["run"]["id"]),
    )
    db.commit()

    report = build_matter_review(now=now)

    assert [item["id"] for item in report["outcomes"]] == [confirmed["id"]]
    assert report["outcomes"][0]["outcome"] == "白皮书路线图已经发布并读回"
    assert [item["id"] for item in report["closure_candidates"]] == [candidate["id"]]
    assert blocked["id"] in {item["id"] for item in report["attention"]}
    assert next_matter["id"] in {item["id"] for item in report["next_actions"]}
    assert report["integrity"]["recent_closed_without_owner_receipt"] == 1
    assert report["authority"]["result_receipt_completes_matter"] is False
    assert report["authority"]["read_only"] is True

    rendered = render_matter_review(report)
    assert "本周形成的结果" in rendered
    assert "已有产出，尚未确认收口" in rendered
    assert "卡住或等待中" in rendered
    assert "接下来最值得推进" in rendered
    assert "旧完成记录" not in rendered


def test_review_does_not_reuse_an_old_receipt_after_reopen_and_direct_close():
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    matter = create_matter("曾经闭环的事项")
    close_matter(
        matter["id"], outcome="第一版结果", confirmation_text="确认第一版完成",
    )
    update_matter(matter["id"], status="active", actor="owner")
    update_matter(
        matter["id"], status="done", outcome="没有确认的第二版结果",
        actor="model",
    )
    db = db_module.get_db()
    db.execute(
        "UPDATE matters SET closed_at=? WHERE id=?",
        ("2026-08-27T11:00:00", matter["id"]),
    )
    db.commit()

    report = build_matter_review(now=now)

    assert matter["id"] not in {item["id"] for item in report["outcomes"]}
    assert report["integrity"]["recent_closed_without_owner_receipt"] == 1


def test_review_excludes_live_work_from_the_next_action_shortlist(tmp_path):
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    matter = create_matter("正在执行", next_action="继续当前执行窗口")
    start_matter_run(
        matter_id=matter["id"], task="still running", workspace=str(tmp_path),
    )
    other = create_matter("另一项正在执行", next_action="也继续当前执行窗口")
    start_matter_run(
        matter_id=other["id"], task="also running", workspace=str(tmp_path),
    )

    report = build_matter_review(now=now, limit=1)

    assert report["summary"]["active_runs"] == 2
    assert matter["id"] not in {item["id"] for item in report["next_actions"]}
    encoded = str(report)
    assert "raw_transcript" not in encoded
    assert "receipt_json" not in encoded


def test_review_treats_an_expired_running_lease_as_actionable(tmp_path):
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    matter = create_matter(
        "睡眠后恢复", next_action="重新接续这项工作",
    )
    started = start_matter_run(
        matter_id=matter["id"], task="before sleep", workspace=str(tmp_path),
    )
    db = db_module.get_db()
    db.execute(
        "UPDATE matter_runs SET lease_expires_epoch=? WHERE id=?",
        (now.timestamp() - 1, started["run"]["id"]),
    )
    db.commit()

    report = build_matter_review(now=now)

    assert report["summary"]["active_runs"] == 0
    assert matter["id"] in {item["id"] for item in report["next_actions"]}


def test_weekly_review_recovers_expired_runs_before_building_the_read_model():
    source = (
        Path(__file__).resolve().parents[1]
        / "tasks"
        / "weekly_review_pre.sh"
    ).read_text(encoding="utf-8")

    assert source.index("recover_expired_runs") < source.index(
        "-m core.matter_review"
    )


def test_review_does_not_call_an_unreceipted_released_run_an_output(tmp_path):
    now = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    matter = create_matter("缺收据的运行", next_action="重新执行并留下收据")
    started = start_matter_run(
        matter_id=matter["id"], task="broken release", workspace=str(tmp_path),
    )
    packet = started["context_packet"]
    release_matter_run(
        run_id=started["run"]["id"],
        context_generation=packet["context_generation"],
        context_digest=packet["digest"],
    )
    db = db_module.get_db()
    db.execute(
        "UPDATE matter_runs SET result_digest='', released_epoch=? WHERE id=?",
        (now.timestamp() - 60, started["run"]["id"]),
    )
    db.commit()

    report = build_matter_review(now=now)

    assert matter["id"] not in {
        item["id"] for item in report["closure_candidates"]
    }


def test_review_is_empty_when_no_matter_has_user_value():
    report = build_matter_review(
        now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    )

    assert report["material"] is False
    assert render_matter_review(report) == ""


def test_review_bounds_untrusted_display_fields():
    matter = create_matter(
        "题" * 500,
        summary="摘" * 1000,
        next_action="步" * 1000,
    )

    report = build_matter_review(
        now=datetime(2026, 8, 27, 12, tzinfo=timezone.utc),
    )
    item = next(row for row in report["next_actions"] if row["id"] == matter["id"])

    assert len(item["title"]) == 160
    assert len(item["summary"]) == 500
    assert len(item["next_action"]) == 500
    rendered = render_matter_review(report)
    assert len(rendered) < 500
    assert "题" * 81 not in rendered
    assert "步" * 241 not in rendered
