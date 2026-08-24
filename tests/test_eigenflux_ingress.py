"""End-to-end regressions for EigenFlux stream + polling reconciliation."""

from __future__ import annotations

import json
import sqlite3
import subprocess

from core import memorial
from core.ef_stream import load_seen
from core.eigenflux_ingress import reconcile_once
from core.runtime_paths import database_path


def test_eigenflux_inbox_reconcile_task_wires_the_ingress_pre_hook():
    from pathlib import Path

    heartbeat = (Path(__file__).parent.parent / "HEARTBEAT.md").read_text()
    section = heartbeat.split(
        "### eigenflux-inbox-reconcile", 1
    )[1].split("### ", 1)[0]
    assert "tasks/eigenflux_ingress_pre.sh" in section


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    sent = []
    monkeypatch.setattr(
        memorial,
        "_send_card",
        lambda *_args, **_kwargs: (
            sent.append(True) or f"om_{len(sent)}"
        ),
    )
    return sent


def _cache(home, message):
    (home / "servers" / "eigenflux").mkdir(parents=True, exist_ok=True)
    (home / "servers" / "eigenflux" / "profile.json").write_text(
        json.dumps({"agent_id": "agent-owner"})
    )
    day = home / "servers" / "eigenflux" / "data" / "messages" / "20990101"
    day.mkdir(parents=True, exist_ok=True)
    (day / f"agent-{message['sender_id']}.json").write_text(
        json.dumps([message], ensure_ascii=False)
    )


def _message(msg_id="msg-1", content="需要你知道的一件事"):
    return {
        "msg_id": msg_id,
        "conv_id": "conv-1",
        "sender_id": "agent-peer",
        "receiver_id": "agent-owner",
        "sender_name": "Peer",
        "receiver_name": "Jarvis",
        "content": content,
        "created_at": 4_070_908_800_000,
    }


def _empty_fetch(command, **_kwargs):
    assert command[1:3] == ["msg", "fetch"]
    return subprocess.CompletedProcess(
        command, 0, stdout=json.dumps({"messages": []}), stderr=""
    )


def _installed_cli(monkeypatch):
    monkeypatch.setattr(
        "core.eigenflux_publish.resolve_eigenflux_bin",
        lambda: "/usr/local/bin/eigenflux",
    )


def test_cache_reconciliation_delivers_raw_pm_and_records_msg_id(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    _cache(home, _message())

    result = reconcile_once(
        tmp_path,
        runner=_empty_fetch,
        eigenflux_home=home,
        now_epoch=4_070_908_800,
    )

    assert result.status == "ok"
    assert result.accepted == 1
    assert sent == [True]
    assert "msg-1" in load_seen(tmp_path / "eigenflux" / ".ef-seen")
    cards = memorial.list_memorials()
    assert len(cards) == 1
    assert cards[0]["dedup_key"] == "eigenflux:msg-1"
    assert "需要你知道" in cards[0]["body"]


def test_polling_and_stream_receipts_do_not_double_deliver(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    _cache(home, _message())

    first = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_908_800,
    )
    second = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_908_860,
    )

    assert first.accepted == 1
    assert second.accepted == 0
    assert second.already_receipted == 1
    assert sent == [True]
    assert len(memorial.list_memorials()) == 1


def test_proven_keychain_no_send_is_redelivered_after_transport_recovers(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    _cache(home, _message())
    reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_908_800,
    )
    with sqlite3.connect(database_path(tmp_path)) as db:
        db.execute(
            "UPDATE delivery_envelopes SET state='failed',attempts=9,"
            "delivered_epoch=NULL,last_error='keychain Get failed: "
            "keychain access blocked'"
        )
    (tmp_path / "eigenflux" / ".ef-seen").write_text("[]")

    recovered = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_909_000,
    )

    assert recovered.recovered == 1
    assert recovered.status == "ok"
    assert sent == [True, True]
    with sqlite3.connect(database_path(tmp_path)) as db:
        states = [row[0] for row in db.execute(
            "SELECT state FROM delivery_envelopes ORDER BY created_epoch"
        )]
    # The generic recovery reconciler retires the proven no-send envelope
    # once its replacement is confirmed. Keeping it as `failed` after a
    # successful replay would leave a permanent false incident in ops.
    assert states == ["suppressed", "delivered"]


def test_closed_memorial_is_never_resurrected(monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    _cache(home, _message())
    reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_908_800,
    )
    state = memorial.list_memorials()[0]
    memorial.lapse(state["id"], "用户已处理")
    with sqlite3.connect(database_path(tmp_path)) as db:
        db.execute(
            "UPDATE delivery_envelopes SET state='failed',attempts=9,"
            "delivered_epoch=NULL,last_error='keychain access blocked'"
        )
    (tmp_path / "eigenflux" / ".ef-seen").write_text("[]")

    result = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_909_000,
    )

    assert result.recovered == 0
    assert result.already_receipted == 1
    assert sent == [True]


def test_tool_transcript_card_is_retired_before_safe_raw_replacement(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    message = _message(content="只传递这段安全原文")
    _cache(home, message)
    mid, _ = memorial.create(
        source="eigenflux",
        title="Peer 来信",
        body="只传递这段安全原文\n\n**Tool: Grep**\n{\"output_mode\":\"content\"}",
        preset="fyi",
        dedup_key="eigenflux:msg-1",
        context=json.dumps({"conv_id": "conv-1", "sender_id": "agent-peer"}),
    )
    with sqlite3.connect(database_path(tmp_path)) as db:
        db.execute(
            "UPDATE delivery_envelopes SET state='failed',attempts=9,"
            "delivered_epoch=NULL,last_error='keychain access blocked'"
        )

    result = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_909_000,
    )

    assert result.recovered == 1
    states = {item["id"]: item for item in memorial.list_memorials()}
    assert states[mid]["status"] == "lapsed"
    replacements = [
        item for item in states.values()
        if item["id"] != mid and item.get("dedup_key") == "eigenflux:msg-1"
    ]
    assert len(replacements) == 1
    assert replacements[0]["status"] == "pending"
    assert replacements[0]["body"].endswith("只传递这段安全原文\n\n📡 Powered by EigenFlux")
    assert "Tool:" not in replacements[0]["body"]
    assert sent == [True, True]


def test_reconcile_bounds_delivery_attempts_and_reports_remaining_gap(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    sent = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "ef-home"
    for index in range(5):
        message = _message(msg_id=f"msg-{index}", content=f"message {index}")
        message["sender_id"] = f"agent-peer-{index}"
        _cache(home, message)

    result = reconcile_once(
        tmp_path, runner=_empty_fetch, eigenflux_home=home,
        now_epoch=4_070_908_800,
    )

    assert result.status == "degraded"
    assert result.accepted == 3
    assert result.unresolved_failures == 2
    assert len(sent) == 3


def test_fetch_failure_preserves_error_health_without_user_output(
        monkeypatch, tmp_path):
    _installed_cli(monkeypatch)
    _runtime(monkeypatch, tmp_path)

    def failed(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="network unavailable"
        )

    result = reconcile_once(
        tmp_path, runner=failed, eigenflux_home=tmp_path / "none",
        now_epoch=2_000,
    )

    assert result.status == "error"
    health = json.loads(
        (tmp_path / "data" / "ef_ingress_health.json").read_text()
    )
    assert health["last_success_epoch"] == 0
    assert "network unavailable" in health["detail"]


def test_cli_exits_nonzero_when_ingress_probe_fails(monkeypatch, tmp_path):
    import core.eigenflux_ingress as eigenflux_ingress

    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(
        eigenflux_ingress,
        "reconcile_once",
        lambda *_args, **_kwargs: eigenflux_ingress.ReconcileResult(
            status="error", detail="offline"
        ),
    )

    assert eigenflux_ingress.main() == 1
