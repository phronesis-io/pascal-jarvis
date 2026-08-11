"""Card delivery closure (docs/prd_card_delivery_closure.md).

The incidents these guard, all found in the 2026-08-03 audit:

- 7/24: Lark fell from ~60 cards/day to 1-7 because the 7/23 routing change
  parked decisions on a phone desk NO PHONE HAD EVER PAIRED WITH, and notices
  on a web archive behind device auth + a custom CA. Cards were delivered to a
  surface that does not ring and often does not open.
- 「Intent not found or already closed」was toasted as 「已批：✓」five times.
- The 7/24 broadcast approval died on `[Errno 2] ... 'eigenflux'` (launchd
  PATH), promised 「内容仍保留待重试」, and aged out unsent — nothing retried.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import memorial  # noqa: E402
from core.eigenflux_publish import (  # noqa: E402
    APPROVED_MAX_ATTEMPTS,
    mark_approved_failure,
    reconcile_pending_drafts,
)


# ── C1 (REQ-119): Lark is the ONLY delivery surface ──────────────────────────
# 14d to 8/11: Lark cards read 95.7% (235), web cards 1.8% (170) — the web
# desk was a dead surface whose transport faked success. New contract: a card
# either goes to Lark or stays ledger-only (ambient exhaust, batched into the
# morning anchor digest). No routing decision consults desk reachability.


def test_every_decision_reviews_on_lark():
    assert memorial._infer_review_surface(
        "eigenflux-publish", memorial.ATTENTION_DECISION, [],
    ) == memorial.REVIEW_LARK
    # Neither urgency nor native buttons nor a live chat change the answer —
    # there is no other surface to send a decision to.
    assert memorial._infer_review_surface(
        "task-triage", memorial.ATTENTION_DECISION,
        [{"text": "打开", "value": {"action": "open"}}],
        urgent=True, chat_id="oc_x",
    ) == memorial.REVIEW_LARK


def test_notice_pushes_to_lark():
    """The product's own voice (checkin) goes to the chat, unconditionally."""
    st = {"source": "checkin", "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is True


@pytest.mark.parametrize("source", sorted(memorial.AMBIENT_SOURCES))
def test_ambient_sources_are_ledger_only_never_web(source, tmp_path,
                                                   monkeypatch):
    """Monitoring exhaust never pushes AND never grows a delivery envelope —
    pushing it would recreate the 7/22 card storm; a web envelope would
    resurrect the fake-delivered ledger. The morning digest carries it."""
    st = {"source": source, "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is False

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "jarvis.db"))
    mid, accepted = memorial.create(
        source=source, title="监控尾气", body=f"exhaust from {source}")
    assert accepted is True
    assert memorial.get_memorial(mid)["delivery_status"] == "ledger_only"

    from core.delivery import DeliveryPipeline
    rows = DeliveryPipeline(tmp_path, db_path=tmp_path / "jarvis.db").list()
    assert rows == [], "ledger-only cards must not enter the delivery pipeline"


def test_ambient_alert_still_pages():
    """An ANOMALY from an ambient source is an alert, not exhaust — the
    attention class outranks the source's ledger-only default."""
    st = {"source": "metrics-digest", "attention": memorial.ATTENTION_ALERT,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is True


# ── C2: a no-op action must not toast success ────────────────────────────────


def test_noop_action_result_is_not_a_success():
    for result in ("Intent not found or already closed",
                   "没有找到这条待广播内容（可能已经处理过了）",
                   "广播失败，已进重试队列（自动重试，最多 5 次）：offline",
                   "FAILED: whatever"):
        assert memorial._action_result_is_noop(result), result
    for result in ("✅ 已广播", "Closure recorded", "Intent cancelled", ""):
        assert not memorial._action_result_is_noop(result), result


def test_decide_toast_is_honest_about_a_noop(tmp_path, monkeypatch):
    """The five 「已批：✓」toasts on 「Intent not found」, reproduced."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_test")
    monkeypatch.setattr(
        memorial, "_execute_action",
        lambda action, **kw: "Intent not found or already closed")

    mid, _ = memorial.create(
        source="intention-check", title="t", body="b",
        options=[{"key": "done", "label": "做了",
                  "action": {"type": "intent_close", "params": {"id": "x"}}}],
        send=False,
    )
    out = memorial.decide(mid, "done")
    assert out["toast"]["type"] != "success", (
        "a no-op was toasted as success — the user is told 办好了 about "
        "something that did not happen"
    )
    assert "没有执行" in out["toast"]["content"]


def test_decide_toast_still_celebrates_real_success(tmp_path, monkeypatch):
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_test")
    monkeypatch.setattr(
        memorial, "_execute_action", lambda action, **kw: "Closure recorded")
    mid, _ = memorial.create(
        source="intention-check", title="t2", body="b2",
        options=[{"key": "done", "label": "做了",
                  "action": {"type": "intent_close", "params": {"id": "x"}}}],
        send=False,
    )
    out = memorial.decide(mid, "done")
    assert out["toast"]["type"] == "success"


# ── C3: an approval is durable until executed or explicitly dead ─────────────


def _draft(tmp_path, name="1_1.json", **extra) -> Path:
    pending = tmp_path / "eigenflux" / "pending_publish"
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / name
    path.write_text(json.dumps(
        {"id": name[:-5], "content": "内容", "notes": {"summary": "s"},
         **extra}, ensure_ascii=False))
    return path


def test_approved_draft_is_retried_and_published(tmp_path):
    path = _draft(tmp_path)
    mark_approved_failure(path, json.loads(path.read_text()), "offline")

    calls = []
    result = reconcile_pending_drafts(
        tmp_path, publisher=lambda d, cwd: calls.append(d) or (True, ""))
    assert result["retried"] == 1 and result["published"] == 1
    assert calls and calls[0]["content"] == "内容"
    assert not path.exists(), "published draft must leave the queue"
    state = json.loads(
        (tmp_path / "eigenflux" / "publish_state.json").read_text())
    assert state["last_publish_epoch"] > 0


def test_failed_retry_increments_attempts_and_keeps_the_draft(tmp_path):
    path = _draft(tmp_path)
    mark_approved_failure(path, json.loads(path.read_text()), "offline")

    result = reconcile_pending_drafts(
        tmp_path, publisher=lambda d, cwd: (False, "still offline"))
    assert result["retried"] == 1 and result["published"] == 0
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["attempts"] == 2
    assert "still offline" in data["last_error"]


def test_exhausted_approval_expires_loudly_not_silently(tmp_path):
    path = _draft(tmp_path, attempts=APPROVED_MAX_ATTEMPTS,
                  approved_epoch=int(time.time()), last_error="offline")
    result = reconcile_pending_drafts(
        tmp_path,
        publisher=lambda d, cwd: pytest.fail("exhausted draft must not retry"))
    assert result["expired"] == 1
    assert (tmp_path / "eigenflux" / "pending_publish" / "expired"
            / path.name).exists()


def test_approved_draft_does_not_age_out_while_attempts_remain(tmp_path):
    """The 7/24 failure mode: approval at hour 47.9, then the 48h mtime expiry
    swallowed it. Approval must grant its own window."""
    path = _draft(tmp_path)
    mark_approved_failure(path, json.loads(path.read_text()), "offline")
    # Far past the unapproved max age:
    result = reconcile_pending_drafts(
        tmp_path, now=time.time() + 100 * 3600,
        publisher=lambda d, cwd: (False, "still offline"))
    assert result["retried"] == 1
    assert path.exists(), "an approved draft with attempts left was expired"


def test_unapproved_draft_still_expires_at_48h(tmp_path):
    path = _draft(tmp_path)
    result = reconcile_pending_drafts(tmp_path, now=time.time() + 49 * 3600)
    assert result["expired"] == 1
    assert not path.exists()


def test_resolve_eigenflux_bin_searches_the_shared_launchd_path(monkeypatch):
    """The resolver must consult PATH_ENV — the family-wide augmented PATH
    (system PATH + ~/.local/bin) that the sibling eigenflux callers already
    share — so all eigenflux subprocess sites agree on where the binary is."""
    from core import eigenflux_publish as ep
    from core.eigenflux_friends import PATH_ENV

    seen = {}

    def fake_which(name, path=None):
        seen["name"], seen["path"] = name, path
        return "/resolved/eigenflux"

    monkeypatch.setattr(ep.shutil, "which", fake_which)
    assert ep.resolve_eigenflux_bin() == "/resolved/eigenflux"
    assert seen["name"] == "eigenflux"
    assert seen["path"] == PATH_ENV
    assert "/.local/bin" in seen["path"]


def test_retry_success_converges_the_card_via_memorial_resolve(tmp_path, monkeypatch):
    """On the live root, a successful retry must go through memorial.resolve()
    — which re-renders the delivered Lark copies and completes handoffs — not
    a bare ledger append that flips state while the user keeps seeing
    「广播失败」on the card (the said-one-thing-did-another gap C2 closes)."""
    from core import memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)

    mid, _ = memorial.create(
        source="eigenflux-publish", title="广播批准", body="内容",
        context="pending_publish id=1_1", send=False,
    )
    resolved = []
    real_resolve = memorial.resolve
    monkeypatch.setattr(
        memorial, "resolve",
        lambda *a, **k: resolved.append((a, k)) or real_resolve(*a, **k))

    path = _draft(tmp_path)
    mark_approved_failure(path, json.loads(path.read_text()), "offline")
    result = reconcile_pending_drafts(
        tmp_path, publisher=lambda d, cwd: (True, ""))

    assert result["published"] == 1
    assert resolved, (
        "live-root retry success bypassed memorial.resolve() — the ledger "
        "flips but the delivered card still shows 广播失败"
    )
    st = memorial.get_memorial(mid)
    assert st["status"] == "decided"
    assert "已广播" in st.get("resolved_label", "")


def test_curated_signals_reach_lark(monkeypatch):
    """eigenflux-feed-triage is not ambient (2026-08-03): ~2 curated,
    pre-contextualized signal briefs a day earn the chat; monitoring
    exhaust stays out."""
    st = {"source": "eigenflux-feed-triage",
          "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is True
    assert "eigenflux-feed-triage" not in memorial.AMBIENT_SOURCES
    assert "cross-session-sync" in memorial.AMBIENT_SOURCES


# ── 8/3 card clarity: every card opens by saying what it wants ───────────────


def test_notice_card_opens_with_zhidaojiuxing(tmp_path, monkeypatch):
    """Owner: 「每一个东西我不知道怎么办」— a card that needs nothing must SAY
    so, first line, so no card ever leaves him guessing."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    mid, _ = memorial.create(
        source="eigenflux-feed-triage", title="信号", body="一条简报",
        preset="fyi", send=False)
    card = json.loads(memorial.card_json(mid))
    assert card["elements"][0]["text"]["content"].startswith("ℹ️ 知道就行")


def test_docket_mentions_accumulated_unread_signals():
    """Owner: 「信号…攒的比较多，你可以提醒我去看一眼」— one line in the
    morning docket, threshold 5 so one unread brief doesn't nag."""
    overdue = [{"source": "intention-check", "title": "t",
                "ts": "2026-08-01 09:00", "epoch": 1785600000}]
    _, body_quiet = memorial.escrow_docket(overdue, unread_signals=2)
    assert "信号攒了" not in body_quiet
    _, body_loud = memorial.escrow_docket(overdue, unread_signals=7)
    assert "信号攒了 7 条" in body_loud
