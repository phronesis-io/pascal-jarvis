"""REQ-122 账本口径合一 — one accounting, mechanically shared by every reporter.

The incident (2026-08-11): a ledger query counted 106 张未闭环 while the same
morning's escrow docket announced 「待批 14 件」 — a 7x split, because the
docket only counted overdue decisions. That docket is also one of only two
cards Pascal ever tapped 「看不懂」 on. These tests pin both fixes:

  1. ledger_accounting() is the single closed-loop arithmetic
     (pending + decided + lapsed == created, asserted);
  2. the docket card's numbers come from that same function, and its face
     speaks 人话, never bookkeeping jargon;
  3. the one-shot ghost backfill (scripts/backfill_ghost_lapse.py) only ever
     appends normal lapse events, is idempotent, and never touches a row a
     human actually saw.

Every test injects a fixed clock (NOW) — nothing here may read the wall
clock, or CI in UTC turns red at midnight (learned 2026-08-11).
All sends are mocked and JARVIS_DIR is redirected at tmp_path.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import core.memorial as memorial
from scripts.backfill_ghost_lapse import REASON as BACKFILL_REASON
from scripts.backfill_ghost_lapse import run as backfill_run


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
    # Deterministic routing: the real seam reads live pairing state. raising=
    # False keeps this harmless once REQ-119 deletes the desk seam entirely.
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False,
                        raising=False)
    return rec


NOW = datetime(2026, 8, 11, 9, 0)

_SEQ = iter(range(1, 10_000))


def _make(source, attention, age_h, title=""):
    """Create a memorial and backdate it by rewriting its create event ts.

    Bodies are unique: create()'s 6h content dedup is a real production guard
    and identical fixtures would silently collapse into one row.
    """
    n = next(_SEQ)
    mid, _ = memorial.create(
        source=source, title=title or f"{source} card {n}", body=f"body {n}",
        preset="decision" if attention == memorial.ATTENTION_DECISION else "fyi",
        attention=attention, send=False)
    stamp = (NOW - timedelta(hours=age_h)).strftime("%Y-%m-%d %H:%M")
    path = memorial._ledger_path()
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("id") == mid and e.get("ev") == "create":
            e["ts"] = stamp
        lines.append(json.dumps(e, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mid


# ── the accounting itself ────────────────────────────────────────────────


def test_three_buckets_sum_to_created(env):
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=2)
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=5)
    decided = _make("intention-check", memorial.ATTENTION_DECISION, age_h=30)
    resolved = _make("eigenflux-friends", memorial.ATTENTION_DECISION, age_h=40)
    stale = _make("metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    memorial.decide(decided, "approve", owner_authenticated=True)
    memorial.resolve(resolved, "已在上游处理")
    memorial.lapse(stale, "未读满 8 天")

    acct = memorial.ledger_accounting(now=NOW)
    assert acct["created"] == 5
    assert acct["pending"] == 2
    assert acct["decided"] == 2  # 批红 and upstream resolve are both 已办
    assert acct["lapsed"] == 1
    assert acct["pending"] + acct["decided"] + acct["lapsed"] == acct["created"]
    assert acct["pending_decision"] == 1
    assert acct["pending_notice"] == 1


def test_window_filters_by_creation_time(env):
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=24 * 30)
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=24 * 2)
    assert memorial.ledger_accounting(window_days=14, now=NOW)["created"] == 1
    assert memorial.ledger_accounting(now=NOW)["created"] == 2


def test_unparsable_ts_is_excluded_from_every_bucket(env):
    """Same contract as escrow_scan: never guess an age — and never let a
    garbage row break the identity."""
    states = [{"id": "x", "status": "pending", "ts": "garbage",
               "attention": memorial.ATTENTION_NOTICE, "source": "s"}]
    acct = memorial.ledger_accounting(states=states, now=NOW)
    assert acct["created"] == 0 and acct["pending"] == 0


def test_an_unknown_folded_status_fails_loudly(env):
    """口径分裂 starts with a status quietly missing from the arithmetic —
    a real ValueError naming the status, alive even under python -O."""
    states = [{"id": "x", "status": "half_done", "ts": "2026-08-10 09:00",
               "attention": memorial.ATTENTION_NOTICE, "source": "s"}]
    with pytest.raises(ValueError, match="half_done"):
        memorial.ledger_accounting(states=states, now=NOW)


def test_accounting_excludes_the_dockets_own_cards(env):
    """Same counts_in_ledger predicate as escrow_scan: the docket reports the
    ledger, it is not an entry in it."""
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=2)
    _make(memorial.ESCROW_DIGEST_SOURCE, memorial.ATTENTION_DECISION, age_h=24)
    acct = memorial.ledger_accounting(now=NOW)
    assert acct["created"] == 1 and acct["pending_decision"] == 1


def test_cli_accounting_reports_the_same_numbers(env, capsys, monkeypatch):
    """巡检复算入口: python3 -m core.memorial accounting. The default window
    is the whole ledger — the same 口径 the docket card counts."""
    monkeypatch.setattr(memorial, "now_local", lambda: NOW)
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=2)
    stale = _make("metrics-digest", memorial.ATTENTION_NOTICE, age_h=24 * 8)
    memorial.lapse(stale, "未读满 8 天")
    assert memorial.main(["accounting"]) == 0
    acct = json.loads(capsys.readouterr().out.strip())
    assert acct["window_days"] is None
    assert acct["created"] == 2
    assert acct["pending"] == 1 and acct["lapsed"] == 1
    assert acct["pending"] + acct["decided"] + acct["lapsed"] == acct["created"]


def test_cli_accounting_rejects_a_negative_window(env, capsys):
    """A typo'd --days must error, never silently widen to all-time."""
    assert memorial.main(["accounting", "--days", "-3"]) == 2
    assert "--days" in capsys.readouterr().err


# ── the docket card uses the SAME arithmetic ─────────────────────────────


def test_docket_headline_number_is_the_accounting_number(env):
    for age in (24 * 6, 72, 60, 50):
        _make("intention-check", memorial.ATTENTION_DECISION, age_h=age)
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=3)
    _make("eigenflux-feed-triage", memorial.ATTENTION_NOTICE, age_h=5)
    answered = _make("intention-check", memorial.ATTENTION_DECISION, age_h=90)
    memorial.decide(answered, "approve", owner_authenticated=True)

    states = memorial.list_memorials()
    acct = memorial.ledger_accounting(states=states, now=NOW)
    title, body = memorial.escrow_docket(states, now=NOW)
    assert title == f"{acct['pending_decision']} 件事等你拍板"
    others = acct["pending"] - acct["pending_decision"]
    assert f"另有 {others} 条" in body


def test_docket_never_counts_its_own_prior_cards(env):
    """Yesterday's unanswered docket must not inflate today's by one/day."""
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=72)
    _make(memorial.ESCROW_DIGEST_SOURCE, memorial.ATTENTION_DECISION, age_h=24)
    title, _ = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    assert title == "1 件事等你拍板"


def test_docket_opens_with_the_conclusion_and_names_the_most_urgent(env):
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=24 * 6,
          title="要不要续费代理")
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=30,
          title="周报口径选哪个")
    _, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    first = body.splitlines()[0]
    assert first.startswith("有 2 件事等你拍板，最急的是「要不要续费代理」")
    assert "等了 6 天" in first
    assert "周报口径选哪个" in body


def test_docket_face_speaks_no_jargon(env):
    """The 8/11 「待批 14 件，最久 7 天」 docket earned a 看不懂. Bookkeeping
    vocabulary never reaches the card face again."""
    for age in (24 * 6, 72, 60, 50):
        _make("intention-check", memorial.ATTENTION_DECISION, age_h=age)
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=24 * 2)
    _make("calendar-sync", memorial.ATTENTION_ALERT, age_h=4)
    for i in range(6):
        _make(memorial.SIGNAL_SOURCE, memorial.ATTENTION_NOTICE, age_h=6 + i)
    face = "\n".join(
        memorial.escrow_docket(memorial.list_memorials(), now=NOW))
    assert "信号攒了 6 条" in face  # every line kind is present in this face
    for banned in ("escrow", "lapse", "pending", "docket", "memorial",
                   "待批", "留中", "逾期"):
        assert banned not in face.lower(), banned


def test_docket_with_nothing_waiting_says_so(env):
    """A card that needs nothing must SAY so — never leave him guessing."""
    title, body = memorial.escrow_docket([], now=NOW)
    assert title == "没有等你拍板的事"
    assert "知道就行" in body


def test_docket_with_only_notices_still_reports_them(env):
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=3)
    _make(memorial.SIGNAL_SOURCE, memorial.ATTENTION_NOTICE, age_h=5)
    title, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    assert title == "没有等你拍板的事"
    # Below the 📡 threshold the signal rows stay in the plain-notice line.
    assert "另有 2 条" in body and "自动归档" in body


def test_docket_alerts_get_their_own_line_never_the_hands_off_bucket(env):
    """Review #3: a pending alert described as 「不用动手」 would be the card
    telling him to ignore an alarm."""
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=72)
    _make("calendar-sync", memorial.ATTENTION_ALERT, age_h=30,
          title="日历授权快过期了")
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=3)
    _, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    assert "⚠️ 1 条告警还挂着：「日历授权快过期了」" in body
    # The hands-off bucket holds ONLY the plain notice, never the alert.
    assert "另有 1 条只是说给你听的" in body


def test_docket_counts_each_signal_row_exactly_once(env):
    """Review #4: rows the 📡 line already counts are deducted from 另有 N 条
    — every pending card lands on exactly one line of the face."""
    _make("intention-check", memorial.ATTENTION_DECISION, age_h=72)
    _make("checkin", memorial.ATTENTION_NOTICE, age_h=3)
    for i in range(5):
        _make(memorial.SIGNAL_SOURCE, memorial.ATTENTION_NOTICE, age_h=6 + i)
    _, body = memorial.escrow_docket(memorial.list_memorials(), now=NOW)
    assert "📡 信号攒了 5 条" in body
    assert "另有 1 条" in body  # the checkin notice — signals not re-counted


# ── the ghost backfill ───────────────────────────────────────────────────


def _ghost(age_h, delivery_status,
           attention=memorial.ATTENTION_NOTICE):
    mid = _make("old-web-source", attention, age_h=age_h)
    if delivery_status:
        memorial._record_delivery(mid, delivery_status)
    return mid


def test_backfill_dry_run_writes_nothing(env):
    _ghost(24 * 10, "web_only")
    summary = backfill_run(now=NOW, apply=False)
    assert summary["candidates"] == 1 and summary["lapsed"] == 0
    assert memorial.list_memorials()[0]["status"] == "pending"


def test_backfill_lapses_only_rows_no_human_ever_saw(env):
    ghost_web = _ghost(24 * 10, "web_only")
    ghost_phone = _ghost(24 * 9, "phone_ready")
    ghost_unsent_old = _ghost(24 * 3, "")           # not_sent, old
    kept_fresh = _ghost(3, "")                      # not_sent, mid-flight
    kept_delivered = _ghost(24 * 10, "delivered")   # seen, just unanswered
    kept_queued = _ghost(24 * 10, "queued")
    kept_decided = _ghost(24 * 10, "web_only",
                          attention=memorial.ATTENTION_DECISION)
    memorial.decide(kept_decided, "approve", owner_authenticated=True)

    summary = backfill_run(now=NOW, apply=True)
    assert summary["lapsed"] == 3
    assert summary["by_source"] == {"old-web-source": 3}

    by_id = {st["id"]: st for st in memorial.list_memorials()}
    for mid in (ghost_web, ghost_phone, ghost_unsent_old):
        assert by_id[mid]["status"] == memorial.STATUS_LAPSED
        # The ledger event stream carries the audit trail for the sweep.
        assert by_id[mid]["lapse_reason"] == BACKFILL_REASON
    for mid in (kept_fresh, kept_delivered, kept_queued):
        assert by_id[mid]["status"] == "pending"
    assert by_id[kept_decided]["status"] == "decided"


def test_backfill_is_idempotent(env):
    _ghost(24 * 10, "web_only")
    first = backfill_run(now=NOW, apply=True)
    second = backfill_run(now=NOW, apply=True)
    assert first["lapsed"] == 1
    assert second == {"candidates": 0, "lapsed": 0, "apply": True,
                      "by_source": {}}


def test_backfill_never_claims_a_decision_happened(env):
    mid = _ghost(24 * 10, "web_only", attention=memorial.ATTENTION_DECISION)
    backfill_run(now=NOW, apply=True)
    st = memorial.get_memorial(mid)
    assert st["decided_opt"] == "" and st["decided_ts"] == ""
    # And a revived tap still works exactly like any other 留中 row.
    memorial.decide(mid, "approve", owner_authenticated=True)
    assert memorial.get_memorial(mid)["status"] == "decided"
