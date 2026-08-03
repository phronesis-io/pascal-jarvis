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


# ── C1: no card routes to an unreachable surface ─────────────────────────────


def test_decision_degrades_to_lark_when_desk_unreachable(monkeypatch):
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False)
    assert memorial._infer_review_surface(
        "eigenflux-publish", memorial.ATTENTION_DECISION, [],
    ) == memorial.REVIEW_LARK


def test_decision_stays_on_desk_when_reachable(monkeypatch):
    """The 7/23 design comes back, unchanged, the moment a phone pairs."""
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: True)
    assert memorial._infer_review_surface(
        "eigenflux-publish", memorial.ATTENTION_DECISION, [],
    ) == memorial.REVIEW_PHONE


def test_notice_pushes_to_lark_when_desk_unreachable(monkeypatch):
    """The product's own voice (checkin) must not be archived into silence."""
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False)
    st = {"source": "checkin", "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is True


def test_notice_stays_web_when_desk_reachable(monkeypatch):
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: True)
    st = {"source": "checkin", "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is False


@pytest.mark.parametrize("source", sorted(memorial.AMBIENT_SOURCES))
def test_ambient_sources_never_degrade_to_lark(monkeypatch, source):
    """Monitoring exhaust stays web-first even with the desk down — pushing it
    would recreate the 7/22 card storm; the escrow docket carries the tail."""
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False)
    st = {"source": source, "attention": memorial.ATTENTION_NOTICE,
          "options": [], "extra_buttons": []}
    assert memorial.should_push_to_lark(st) is False


def test_desk_reachability_failure_fails_toward_lark(monkeypatch):
    """If the reachability check itself dies, the safe wrong answer is the
    chat the user reads — not the desk that may not exist."""
    import core.proactive as proactive
    monkeypatch.setattr(proactive, "_desk_cache", None)
    monkeypatch.setattr(
        proactive, "_has_paired_phone_subscription",
        lambda root=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert proactive.desk_reachable(root="x") is False


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
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False)
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
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: False)
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


def test_resolve_eigenflux_bin_falls_back_to_local_bin(monkeypatch, tmp_path):
    from core import eigenflux_publish as ep
    monkeypatch.setattr(ep.shutil, "which", lambda name: None)
    fake_home = tmp_path / "home"
    binary = fake_home / ".local" / "bin" / "eigenflux"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(ep.Path, "home", staticmethod(lambda: fake_home))
    assert ep.resolve_eigenflux_bin() == str(binary)
