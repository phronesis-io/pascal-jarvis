"""Tests for 缴回制度 — the escrow sweep that gives every pending Item an end.

The incident: nothing ever swept the memorial ledger, so a card sent once and
never tapped stayed `pending` forever. On 7/29 that was 314 of 600 memorials,
110 older than a week, 47 decision-class — real asks lost among dead notices.

All sends are mocked and JARVIS_DIR is redirected at tmp_path; no test here
touches the production ledger.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import core.memorial as memorial
from tasks import memorial_escrow


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated JARVIS_DIR + mocked send channel."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rec = SimpleNamespace(dir=tmp_path, cards=[])
    monkeypatch.setattr(memorial, "_send_card",
                        lambda card, chat_id="": (rec.cards.append(card)
                                                  or "om_test"))
    monkeypatch.setattr(memorial, "_send_text",
                        lambda text, chat_id="": "om_test")
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    return rec


NOW = datetime(2026, 7, 29, 9, 0)


_SEQ = iter(range(1, 10_000))


def _make(env, source, attention, age_h, *, delivered=True, **kw):
    """Create a memorial and backdate it by rewriting its create event ts.

    Bodies are made unique: create()'s 6h content dedup is a real production
    guard, and identical fixtures would silently collapse into one row.
    """
    n = next(_SEQ)
    mid, _ = memorial.create(
        source=source, title=f"{source} card {n}", body=f"body {n}",
        preset="decision" if attention == memorial.ATTENTION_DECISION else "fyi",
        attention=attention, send=False, **kw)
    if delivered:
        memorial._record_delivery(mid, "delivered")
    stamp = (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%d %H:%M")
    path = memorial._ledger_path()
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        import json as _json
        e = _json.loads(line)
        if e.get("id") == mid and e.get("ev") == "create":
            e["ts"] = stamp
        lines.append(_json.dumps(e, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mid


# ── deadlines ────────────────────────────────────────────────────────────


def test_fresh_cards_are_left_alone(env):
    _make(env, "checkin", memorial.ATTENTION_NOTICE, age_h=2)
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=2)
    _make(env, "calendar-sync", memorial.ATTENTION_ALERT, age_h=2)
    scan = memorial.escrow_scan(now=NOW)
    assert scan["lapse"] == [] and scan["overdue"] == []


def test_stale_notice_and_alert_are_archived_but_decision_is_not(env):
    notice = _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    alert = _make(env, "calendar-sync", memorial.ATTENTION_ALERT, age_h=30)
    decision = _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    scan = memorial.escrow_scan(now=NOW)
    assert {s["id"] for s, _ in scan["lapse"]} == {notice, alert}
    # A live decision is re-surfaced, never silently archived.
    assert [s["id"] for s in scan["overdue"]] == [decision]


def test_notice_older_than_one_day_leaves_the_live_attention_queue(env):
    """An informational card is not work Pascal owes the system."""
    _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 4)
    assert len(memorial.escrow_scan(now=NOW)["lapse"]) == 1


def test_decision_past_hard_ceiling_stops_nagging(env):
    old = _make(env, "eigenflux-publish", memorial.ATTENTION_DECISION, age_h=24 * 15)
    scan = memorial.escrow_scan(now=NOW)
    assert [s["id"] for s, _ in scan["lapse"]] == [old]
    assert scan["overdue"] == []


def test_never_delivered_decision_never_becomes_owner_backlog(env):
    unseen = _make(
        env, "intention-check", memorial.ATTENTION_DECISION,
        age_h=72, delivered=False,
    )

    scan = memorial.escrow_scan(now=NOW)
    title, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)

    assert unseen not in [state["id"] for state in scan["overdue"]]
    assert title == "没有等你拍板的事"
    assert "知道就行" in body


def test_terminal_linked_intent_retires_its_old_decision(env, monkeypatch):
    decision = _make(
        env, "intention-check", memorial.ATTENTION_DECISION, age_h=72,
    )
    monkeypatch.setattr(
        memorial,
        "_linked_intents_are_terminal",
        lambda state: state.get("id") == decision,
    )

    scan = memorial.escrow_scan(now=NOW)

    assert [(state["id"], reason) for state, reason in scan["lapse"]] == [
        (decision, "关联事项已经闭环")
    ]
    assert scan["overdue"] == []


def test_unparsable_timestamp_is_skipped_not_archived(env):
    state = {"id": "x", "status": "pending", "ts": "garbage",
             "attention": memorial.ATTENTION_NOTICE, "source": "s"}
    scan = memorial.escrow_scan(now=NOW, states=[state])
    assert scan["lapse"] == [] and scan["overdue"] == []


# ── terminal state honesty ───────────────────────────────────────────────


def test_lapse_is_terminal_but_never_claims_a_decision(env):
    mid = _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    assert memorial.lapse(mid, "未读满 8 天") is True
    st = memorial.get_memorial(mid)
    assert st["status"] == memorial.STATUS_LAPSED
    assert st["lapse_reason"] == "未读满 8 天"
    # The ledger must not read as if Pascal answered it.
    assert st["decided_opt"] == "" and st["decided_ts"] == ""


def test_lapse_is_idempotent_and_never_overrides_a_real_decision(env):
    mid = _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=24 * 20)
    memorial.decide(mid, "approve", owner_authenticated=True)
    assert memorial.lapse(mid, "逾期") is False
    assert memorial.get_memorial(mid)["status"] == "decided"


def test_a_lapsed_card_is_still_tappable(env):
    """Scrolling back in Lark and answering an archived ask must revive it."""
    mid = _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=24 * 20)
    memorial.lapse(mid, "逾期")
    memorial.decide(mid, "approve", owner_authenticated=True)
    st = memorial.get_memorial(mid)
    assert st["status"] == "decided"
    assert st["decided_opt"] == "approve"
    assert st["lapse_reason"] == ""


def test_lapsed_cards_do_not_count_as_engagement(env, monkeypatch):
    """The ROI governor demotes noisy sources by engagement rate. Counting an
    auto-archived card as engaged would push noise to ~100% and freeze it."""
    from core import attention_roi
    mid = _make(env, "eigenflux-feed-triage", memorial.ATTENTION_NOTICE,
                age_h=24 * 9)
    memorial.lapse(mid, "未读满 9 天")
    assert attention_roi._engaged(memorial.get_memorial(mid)) is False


# ── the docket ───────────────────────────────────────────────────────────


def test_docket_names_the_urgent_few_and_sums_the_rest(env):
    """奏折铁律 rewrite (REQ-122): conclusion first, 2-3 named asks, one line
    for the remainder — not a per-source accounting table."""
    for _ in range(14):
        _make(env, "eigenflux-publish", memorial.ATTENTION_DECISION, age_h=24 * 7)
    _make(env, "pgc-improvement", memorial.ATTENTION_DECISION, age_h=72)
    title, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    assert title == "15 件事等你拍板"
    first = body.splitlines()[0]
    assert first.startswith("有 15 件事等你拍板，最急的是")
    assert "等了 7 天" in first
    # Named bullets stay few; the other 12 are one honest line, not a pile.
    assert body.count("·") == 2
    assert "其余 12 件" in body


def test_run_archives_and_sends_one_docket(env):
    _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    summary = memorial_escrow.run(now=NOW, send=True)
    assert summary["lapsed"] == 1
    assert summary["overdue"] == 1
    assert summary["docket_id"]


def test_only_one_docket_per_day(env):
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    first = memorial_escrow.run(now=NOW, send=True)
    assert first["docket_id"]
    cards_after_first = len(env.cards)
    memorial_escrow.run(now=NOW.replace(hour=11), send=True)
    # The dedup gate may hand back the existing docket id, but a same-day
    # second sweep must neither mint a second docket nor send a second card.
    dockets = [s for s in memorial.list_memorials()
               if s["source"] == memorial.ESCROW_DIGEST_SOURCE]
    assert len(dockets) == 1
    assert len(env.cards) == cards_after_first


def test_new_docket_supersedes_yesterdays_unanswered_one(env):
    """Review #1: the docket is excluded from every sweep and count by
    design, so nothing else can ever close it — an unanswered docket must be
    resolved by its successor, not accumulate as immortal pending rows."""
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    first = memorial_escrow.run(now=NOW, send=True)
    second = memorial_escrow.run(now=NOW + timedelta(days=1), send=True)
    assert first["docket_id"] and second["docket_id"]
    old = memorial.get_memorial(first["docket_id"])
    assert old["status"] == "decided"
    assert old["action_result"] == "superseded_by_next_docket"
    # The new docket stays pending — it is today's card, nothing replaced it.
    assert memorial.get_memorial(second["docket_id"])["status"] == "pending"


def test_no_docket_outside_the_morning_window(env):
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    summary = memorial_escrow.run(now=NOW.replace(hour=23), send=False)
    assert summary["overdue"] and not summary["docket_id"]


def test_sweep_still_archives_outside_the_morning_window(env):
    """Only the card waits for morning; bookkeeping runs on every pass."""
    _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    summary = memorial_escrow.run(now=NOW.replace(hour=23), send=False)
    assert summary["lapsed"] == 1


def test_silent_when_there_is_no_backlog(env):
    _make(env, "checkin", memorial.ATTENTION_NOTICE, age_h=1)
    summary = memorial_escrow.run(now=NOW, send=False)
    assert summary == {"lapsed": 0, "overdue": 0,
                       "docket_sent": False, "docket_id": ""}


def test_lapsed_rows_record_their_reason_and_leave_the_pending_queue(env):
    """留中 rows must stay archived — a lapsed row that still reads as a live
    pending ask would resurface hundreds of closed matters as open work."""
    mid = _make(env, "metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    memorial.lapse(mid, "未读满 8 天")
    state = memorial.get_memorial(mid)
    assert state.get("status") == "lapsed"
    assert state.get("lapse_reason") == "未读满 8 天"


def test_docket_never_sweeps_itself_into_the_next_docket(env):
    """Otherwise the backlog grows by exactly one card every single day."""
    _make(env, "intention-check", memorial.ATTENTION_DECISION, age_h=72)
    memorial_escrow.run(now=NOW, send=False)
    later = NOW + timedelta(days=9)
    scan = memorial.escrow_scan(now=later)
    assert all(s.get("source") != memorial.ESCROW_DIGEST_SOURCE
               for s in scan["overdue"])
    assert all(s.get("source") != memorial.ESCROW_DIGEST_SOURCE
               for s, _ in scan["lapse"])


def test_a_week_old_decision_no_longer_rides_the_docket(env):
    """Obsolete asks must leave the docket before the old 14-day ceiling."""
    dead = _make(env, "intention-check", memorial.ATTENTION_DECISION,
                 age_h=24 * 7)
    live = _make(env, "intention-check", memorial.ATTENTION_DECISION,
                 age_h=24 * 3)
    scan = memorial.escrow_scan(now=NOW)
    assert [s["id"] for s, _ in scan["lapse"]] == [dead]
    assert [s["id"] for s in scan["overdue"]] == [live]
