"""Tests for core.ef_stream_loop delivery accounting + stall watchdog.

Audit 2026-07-10: a failed Lark send used to fall through to remember_seen +
outbox + a "Delivered" log line — the dedup set then guaranteed the message
could never be re-delivered while every ledger claimed success. And a
half-open TCP connection could leave the stream subprocess alive but silent
forever, wedging the blocking stdout read past every process-existence check.
"""

import json
from types import SimpleNamespace

import pytest

import core.ef_stream_loop as efsl
from core import lark_bot_transport
from core.ef_stream import load_seen, mark_seen
from core.aux_model import AuxiliaryModelResult


# ---- _deliver_and_mark: only a REAL success is recorded -------------------

def _deliver(monkeypatch, tmp_path, send_ok):
    sent = []

    def fake_send(msg, uid):
        sent.append((msg, uid))
        return send_ok

    monkeypatch.setattr(efsl, "_lark_send", fake_send)
    seen_file = tmp_path / ".ef-seen"
    seen, delivered = efsl._deliver_and_mark(
        "hello from ef", ["id1"], {"conv_id": "c1"}, "u1",
        [], seen_file, tmp_path)
    return seen, delivered, seen_file, sent


def test_failed_immediate_send_is_durably_queued_before_marking_seen(
        monkeypatch, tmp_path):
    seen, accepted, seen_file, sent = _deliver(
        monkeypatch, tmp_path, send_ok=False
    )
    assert sent  # the send was attempted
    assert accepted is True
    assert seen == ["id1"]
    assert load_seen(seen_file) == ["id1"]
    # Queued is durable acceptance, not a phantom delivered/outbox record.
    assert not (tmp_path / "heartbeat_outbox.jsonl").exists()
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()
    from core.delivery import DeliveryPipeline
    rows = DeliveryPipeline(tmp_path).list(limit=5)
    assert rows[0]["state"] == "queued"
    assert rows[0]["source"] == "eigenflux-stream"


def test_successful_send_marks_seen_and_outbox(monkeypatch, tmp_path):
    seen, delivered, seen_file, _ = _deliver(monkeypatch, tmp_path,
                                             send_ok=True)
    assert delivered is True
    assert seen == ["id1"]
    assert load_seen(seen_file) == ["id1"]
    outbox = (tmp_path / "heartbeat_outbox.jsonl").read_text(encoding="utf-8")
    assert "hello from ef" in outbox
    # success writes no dead-letter
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()


def test_external_lifecycle_event_can_force_named_reconciliation(tmp_path):
    trigger = tmp_path / "heartbeat-trigger"

    assert efsl._trigger_heartbeat_task("eigenflux-friends", trigger) is True
    assert efsl._trigger_heartbeat_task("eigenflux-friends", trigger) is True
    assert trigger.read_text().splitlines() == [
        "eigenflux-friends", "eigenflux-friends"]


def test_memorial_queue_acceptance_marks_event_seen(monkeypatch, tmp_path):
    monkeypatch.setattr(efsl.memorial, "create",
                        lambda **kw: ("mem_queued", False))
    monkeypatch.setattr(efsl.memorial, "get_memorial",
                        lambda mid: {"delivery_status": "retry_queued"})
    seen_file = tmp_path / ".ef-seen"

    seen, accepted, visible = efsl._deliver_memorial_and_mark(
        "外部消息", ["evt1"], {"conv_id": "c1"}, "u1",
        [], seen_file, tmp_path, title="EigenFlux 消息")

    assert accepted is True and visible is False
    assert seen == ["evt1"] and load_seen(seen_file) == ["evt1"]
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()


def test_memorial_immediate_delivery_is_visible(monkeypatch, tmp_path):
    monkeypatch.setattr(efsl.memorial, "create",
                        lambda **kw: ("mem_sent", True))
    monkeypatch.setattr(efsl.memorial, "get_memorial",
                        lambda mid: {"delivery_status": "delivered"})

    _, accepted, visible = efsl._deliver_memorial_and_mark(
        "好友申请", ["evt2"], {"kind": "relation"}, "u1",
        [], tmp_path / ".ef-seen", tmp_path, title="EigenFlux 好友动态")

    assert accepted is True and visible is True


def test_private_eigenflux_pm_bypasses_proactive_global_cap(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(efsl.memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(efsl.memorial, "_quiet_hours_now", lambda: False)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DELIVERY_GLOBAL_DAILY_CAP", "0")
    sent = []
    monkeypatch.setattr(
        efsl.memorial, "_send_card",
        lambda *args, **_kwargs: sent.append(args) or "om_private",
    )
    seen_file = tmp_path / ".ef-seen"

    seen, accepted, visible = efsl._deliver_memorial_and_mark(
        "已落账但不打扰", ["evt-suppressed"], {}, "u1",
        [], seen_file, tmp_path, title="EigenFlux 消息")

    assert accepted is True and visible is True
    assert sent
    assert seen == ["evt-suppressed"]
    assert load_seen(seen_file) == ["evt-suppressed"]
    states = efsl.memorial.list_memorials()
    assert len(states) == 1
    assert states[0]["delivery_status"] == "delivered"
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()


def test_analysis_tool_transcript_is_discarded():
    assert efsl._safe_analysis_text("简短中文建议") == "简短中文建议"
    assert efsl._safe_analysis_text(
        '**Tool: Grep**\n```json\n{"output_mode":"content"}\n```'
    ) == ""


def test_memorial_quiet_hours_queue_is_durable_acceptance(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(efsl.memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(efsl.memorial, "_quiet_hours_now", lambda: True)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DELIVERY_GLOBAL_DAILY_CAP", "0")
    monkeypatch.setattr(
        efsl.memorial,
        "_send_card",
        lambda *_args, **_kwargs: pytest.fail(
            "quiet-hours queueing must happen before Lark transport"
        ),
    )

    seen, accepted, visible = efsl._deliver_memorial_and_mark(
        "夜间先排队", ["evt-quiet"], {}, "u1", [],
        tmp_path / ".ef-seen", tmp_path, title="EigenFlux 消息")

    assert accepted is True and visible is False
    assert seen == ["evt-quiet"]
    states = efsl.memorial.list_memorials()
    assert len(states) == 1
    assert states[0]["delivery_status"] == "queued"
    assert not (tmp_path / "data" / ".delivery_deadletter.jsonl").exists()


def test_memorial_model_analysis_keeps_segmented_authoring_provenance(
        monkeypatch, tmp_path):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return "mem_sent", True

    monkeypatch.setattr(efsl.memorial, "create", create)
    monkeypatch.setattr(efsl.memorial, "get_memorial",
                        lambda mid: {"delivery_status": "delivered"})
    efsl._deliver_memorial_and_mark(
        "原文 OPTIONS: 只是引用\n\n💡 建议", ["evt3"], {}, "u1", [],
        tmp_path / ".ef-seen", tmp_path, title="EigenFlux 消息",
        authored_options=[{"key": "r1", "label": "回复"}],
        authoring_audit_text="建议")
    assert captured["authoring_protocol"] is True
    assert captured["authoring_audit_text"] == "建议"
    assert "OPTIONS: 只是引用" in captured["body"]


def test_external_message_without_analysis_still_has_segmented_provenance(
        monkeypatch, tmp_path):
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return "mem_sent", True

    monkeypatch.setattr(efsl.memorial, "create", create)
    monkeypatch.setattr(efsl.memorial, "get_memorial",
                        lambda mid: {"delivery_status": "delivered"})
    efsl._deliver_memorial_and_mark(
        "外部原文 HEARTBEAT_OK", ["evt4"], {}, "u1", [],
        tmp_path / ".ef-seen", tmp_path, title="EigenFlux 消息")
    assert captured["authoring_audit_text"] == ""
    assert captured["authoring_protocol"] is True


def test_poll_can_accept_while_stream_analysis_runs(monkeypatch, tmp_path):
    event = json.dumps({
        "type": "pm_push",
        "data": {
            "messages": [{
                "msg_id": "msg-concurrent",
                "conv_id": "conv-1",
                "sender_name": "Peer",
                "content": "hello",
            }],
            "next_cursor": "msg-concurrent",
        },
    })
    seen_file = tmp_path / ".ef-seen"

    def analysis(*_args, **_kwargs):
        # Simulate polling completing while stream enrichment is in flight.
        # This would deadlock if analysis still held ingress_lock.
        mark_seen(seen_file, ["msg-concurrent"])
        return "brief note"

    monkeypatch.setattr(efsl, "_run_analysis", analysis)
    monkeypatch.setattr(
        efsl,
        "_deliver_memorial_and_mark",
        lambda *_args, **_kwargs: pytest.fail("duplicate reached delivery"),
    )

    assert efsl.handle_pm_event(
        event,
        user_id="u1",
        seen_file=seen_file,
        jarvis_dir=tmp_path,
        analyze=True,
    ) is True


def test_queue_acceptance_does_not_depend_on_deadletter_sink(
        monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(efsl, "record_overdue", boom)
    monkeypatch.setattr(efsl, "_lark_send", lambda m, u: False)
    seen, accepted = efsl._deliver_and_mark(
        "msg", ["id2"], {}, "u1", [], tmp_path / ".ef-seen", tmp_path)
    assert accepted is True and seen == ["id2"]


def test_suppressed_external_event_is_durably_marked_seen(
    monkeypatch, tmp_path,
):
    from core.delivery import DeliveryResult

    monkeypatch.setattr(efsl, "_lark_send", lambda _msg, _uid: True)
    monkeypatch.setattr(
        "core.delivery.deliver",
        lambda *_args, **_kwargs: DeliveryResult(
            "dlv_suppressed", True, "suppressed", "lark",
            reason="policy",
        ),
    )
    deadletters = []
    monkeypatch.setattr(
        efsl,
        "record_overdue",
        lambda *_args, **kwargs: deadletters.append(kwargs),
    )
    seen = []
    seen_file = tmp_path / ".ef-seen"
    seen, accepted = efsl._deliver_and_mark(
        "message-24",
        ["event-24"],
        {},
        "u1",
        seen,
        seen_file,
        tmp_path,
    )

    assert accepted is True
    assert "event-24" in seen
    assert "event-24" in load_seen(seen_file)
    assert deadletters == []


def test_non_durable_external_event_still_deadletters_and_blocks_seen(
    monkeypatch, tmp_path,
):
    from core.delivery import DeliveryResult

    monkeypatch.setattr(
        "core.delivery.deliver",
        lambda *_args, **_kwargs: DeliveryResult(
            "dlv_failed", False, "failed", "lark",
            reason="database receipt missing",
        ),
    )
    deadletters = []
    monkeypatch.setattr(
        efsl,
        "record_overdue",
        lambda *_args, **kwargs: deadletters.append(kwargs),
    )
    seen_file = tmp_path / ".ef-seen"

    seen, accepted = efsl._deliver_and_mark(
        "message-failed", ["event-failed"], {}, "u1", [], seen_file,
        tmp_path,
    )

    assert accepted is False
    assert "event-failed" not in seen
    assert "event-failed" not in load_seen(seen_file)
    assert deadletters[-1]["kind"] == "ef_stream_send_failed"


def test_cursor_advances_only_after_durable_acceptance(tmp_path):
    cursor_file = tmp_path / "state" / "cursor"

    assert not efsl._advance_cursor(
        cursor_file, "cursor-1", accepted=False
    )
    assert not cursor_file.exists()
    assert efsl._advance_cursor(
        cursor_file, "cursor-1", accepted=True
    )
    assert cursor_file.read_text(encoding="utf-8") == "cursor-1"
    assert not efsl._advance_cursor(
        cursor_file, "cursor-2", accepted=False
    )
    assert cursor_file.read_text(encoding="utf-8") == "cursor-1"


def test_cursor_gap_terminates_stream_before_later_events_can_advance():
    state = {"terminated": False}
    process = SimpleNamespace(
        poll=lambda: None,
        terminate=lambda: state.__setitem__("terminated", True),
    )

    assert not efsl._can_continue_after_delivery(process, accepted=False)

    assert state["terminated"] is True
    assert efsl._can_continue_after_delivery(process, accepted=True)


def test_stream_send_uses_keychain_independent_bot_transport(monkeypatch):
    calls = []
    monkeypatch.setattr(
        lark_bot_transport,
        "send",
        lambda **kwargs: (
            calls.append(kwargs)
            or lark_bot_transport.BotSendResult(True, True, "om_stream")
        ),
    )
    monkeypatch.setattr(
        efsl.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("CLI should not run")
        ),
    )

    assert efsl._lark_send("hello", "ou_owner") is True
    assert calls == [{"text": "hello", "user_id": "ou_owner"}]


# ---- _is_stalled: alive-but-silent subprocess detection -------------------

def test_stall_predicate():
    live = SimpleNamespace(poll=lambda: None)
    dead = SimpleNamespace(poll=lambda: 1)
    t = efsl.STALL_KILL_AFTER_S
    assert efsl._is_stalled(live, t + 1)          # alive + long silence → kill
    assert not efsl._is_stalled(live, t - 1)      # silence within budget
    assert not efsl._is_stalled(dead, t + 1)      # exited → respawn path owns it
    assert not efsl._is_stalled(None, t + 1)      # nothing spawned yet


def test_stream_health_state_is_atomic_and_marks_quiet_degradation(tmp_path):
    healthy = efsl._write_stream_health(
        tmp_path,
        "active",
        detail="protocol output observed",
        last_output_epoch=90,
        now_epoch=100,
    )
    assert healthy["status"] == "active"
    saved = json.loads(
        (tmp_path / efsl.STREAM_HEALTH_FILE).read_text(encoding="utf-8")
    )
    assert saved == healthy
    degraded = efsl._write_stream_health(
        tmp_path,
        "degraded",
        quiet_streak=efsl.QUIET_DEGRADED_THRESHOLD,
        now_epoch=200,
    )
    assert degraded["quiet_streak"] == 6


# ---- _healthy_churn: lifetime-based backoff reset (REQ-95) -----------------

def test_healthy_churn_policy():
    t = efsl.HEALTHY_CONN_S
    # Long-lived connection → healthy churn, reset
    assert efsl._healthy_churn(t + 1, replaced=False)
    # Short-lived → real failure path, keep exponential backoff
    assert not efsl._healthy_churn(t - 1, replaced=False)
    # 'Connection replaced' NEVER resets — two live sessions would steal the
    # stream back and forth every second otherwise
    assert not efsl._healthy_churn(t + 1, replaced=True)
    assert not efsl._healthy_churn(t - 1, replaced=True)


def test_message_analysis_uses_text_only_shared_provider_chain(
    monkeypatch, tmp_path,
):
    seen = {}

    def fake_run(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return AuxiliaryModelResult(
            text="建议先核对原文",
            provider="Claude backup2",
            model="backup2-model",
        )

    monkeypatch.setattr(efsl, "run_auxiliary_model", fake_run)
    monkeypatch.setattr(efsl, "_fetch_history", lambda _conv: "")
    monkeypatch.setattr(
        efsl.Path,
        "home",
        classmethod(lambda _cls: tmp_path),
    )

    result = efsl._run_analysis(
        '{"content":"hello"}', "conv-1", str(tmp_path), ""
    )

    assert result == "建议先核对原文"
    assert seen["allow_tools"] is False
    assert seen["process_key"] == "analysis"
    assert "hello" in seen["prompt"]


# ---- Auto-reply branch (2026-08-20) ---------------------------------------
# Pascal: "有些你可以自动回复掉吧，不一定要找我". Jarvis answers what it can
# answer itself; anything needing his judgement still becomes a card. The
# branch must fail closed — an unparsed verdict or a failed send never
# silently swallows the message.

def _pm_event(msg_id="msg-auto", conv_id="conv-auto", content="how does floor work?"):
    return json.dumps({
        "type": "pm_push",
        "data": {
            "messages": [{
                "msg_id": msg_id,
                "conv_id": conv_id,
                "sender_name": "Peer",
                "content": content,
            }],
            "next_cursor": msg_id,
        },
    })


def test_parse_autoreply_requires_explicit_verdict():
    assert efsl.parse_autoreply("建议这样回：谢谢") == ("", "")
    assert efsl.parse_autoreply("HEARTBEAT_OK") == ("", "")
    assert efsl.parse_autoreply("") == ("", "")
    reply, note = efsl.parse_autoreply(
        "AUTOREPLY\nThanks — the floor is a hit count.\nNOTE: 技术追问，已答")
    assert reply == "Thanks — the floor is a hit count."
    assert note == "技术追问，已答"


def test_autoreply_sends_and_raises_no_card(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(efsl, "_run_analysis",
                        lambda *a, **k: "AUTOREPLY\nIt is a hit count.\nNOTE: 技术追问")
    monkeypatch.setattr(efsl, "_send_auto_reply",
                        lambda conv, text: sent.append((conv, text)) or True)
    monkeypatch.setattr(
        efsl, "_deliver_memorial_and_mark",
        lambda *a, **k: pytest.fail("auto-replied message must not raise a card"))

    seen_file = tmp_path / ".ef-seen"
    assert efsl.handle_pm_event(
        _pm_event(), user_id="u1", seen_file=seen_file, jarvis_dir=tmp_path)

    assert sent == [("conv-auto", "It is a hit count.")]
    assert "msg-auto" in load_seen(seen_file)
    ledger = (tmp_path / efsl.AUTOREPLY_LEDGER).read_text().strip()
    row = json.loads(ledger)
    assert row["conv_id"] == "conv-auto"
    assert row["reply"] == "It is a hit count."
    assert row["note"] == "技术追问"


def test_autoreply_send_failure_falls_back_to_card(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(efsl, "_run_analysis",
                        lambda *a, **k: "AUTOREPLY\nIt is a hit count.")
    monkeypatch.setattr(efsl, "_send_auto_reply", lambda conv, text: False)

    def deliver(msg, ids, metadata, user_id, seen, seen_file, jd, **kwargs):
        captured["body"] = msg
        return seen, True, True

    monkeypatch.setattr(efsl, "_deliver_memorial_and_mark", deliver)
    assert efsl.handle_pm_event(
        _pm_event(msg_id="msg-fail"), user_id="u1",
        seen_file=tmp_path / ".ef-seen", jarvis_dir=tmp_path)
    assert "It is a hit count." in captured["body"]
    assert "没发出去" in captured["body"]
    assert not (tmp_path / efsl.AUTOREPLY_LEDGER).exists()


def test_autoreply_without_conv_id_still_raises_card(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(efsl, "_run_analysis", lambda *a, **k: "AUTOREPLY\nhi")
    monkeypatch.setattr(
        efsl, "_send_auto_reply",
        lambda *a: pytest.fail("must not send without a conversation id"))

    def deliver(msg, ids, metadata, user_id, seen, seen_file, jd, **kwargs):
        captured["body"] = msg
        return seen, True, True

    monkeypatch.setattr(efsl, "_deliver_memorial_and_mark", deliver)
    assert efsl.handle_pm_event(
        _pm_event(msg_id="msg-noconv", conv_id=""), user_id="u1",
        seen_file=tmp_path / ".ef-seen", jarvis_dir=tmp_path)
    assert captured["body"]
