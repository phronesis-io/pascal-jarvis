"""Attention ROI governor: it may quiet a lane, never hide a card.

The dangerous failure modes for a thing that rewires where Pascal's attention
goes are all about acting on too little, acting invisibly, or acting on its own
output. Each has a test here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.db as db_module  # noqa: E402
from core import attention_roi  # noqa: E402
from core.memorial import (ATTENTION_ALERT, ATTENTION_DECISION,  # noqa: E402
                           ATTENTION_NOTICE)


@pytest.fixture()
def roi_db(tmp_path, monkeypatch):
    db_module.DB_PATH = tmp_path / "roi.db"
    db_module._connection = None
    attention_roi._initialized = False
    attention_roi._invalidate()
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    yield tmp_path
    if db_module._connection:
        db_module._connection.close()
        db_module._connection = None
    db_module.DB_PATH = db_module._DEFAULT_DB_PATH
    attention_roi._initialized = False
    attention_roi._invalidate()


def _stats(**rows):
    """{'source|attention': (n, engaged)} → the compute_stats shape."""
    out = {}
    for key, (n, engaged) in rows.items():
        source, attention = key.split("|")
        out[(source, attention)] = {"n": n, "engaged": engaged,
                                    "rate": engaged / n if n else 0.0}
    return out


class TestPolicy:
    def test_demotes_an_ignored_decision_source(self, roi_db):
        d = attention_roi.evaluate(_stats(**{f"noisy|{ATTENTION_DECISION}": (20, 2)}))
        assert "noisy" in d["demote"]
        assert d["demote"]["noisy"]["to"] == ATTENTION_NOTICE

    def test_thin_evidence_is_not_enough_to_demote(self, roi_db):
        """0-for-5 is a bad week, not a verdict."""
        d = attention_roi.evaluate(_stats(**{f"thin|{ATTENTION_DECISION}": (5, 0)}))
        assert d["demote"] == {}

    def test_answered_source_is_left_alone(self, roi_db):
        d = attention_roi.evaluate(_stats(**{f"good|{ATTENTION_DECISION}": (20, 15)}))
        assert d["demote"] == {}

    def test_protected_source_is_never_demoted(self, roi_db):
        d = attention_roi.evaluate(
            _stats(**{f"calendar-sync|{ATTENTION_DECISION}": (50, 0)}))
        assert d["demote"] == {}

    def test_noisy_notice_is_reported_not_demoted(self, roi_db):
        """Nothing below notice exists; "demoting" it would mean hiding it."""
        d = attention_roi.evaluate(_stats(**{f"chatty|{ATTENTION_NOTICE}": (40, 2)}))
        assert d["demote"] == {}
        assert [x["source"] for x in d["noisy_notices"]] == ["chatty"]


class TestHysteresis:
    def _demote(self, source="noisy"):
        attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"{source}|{ATTENTION_DECISION}": (20, 2)})))

    def test_demotion_persists_and_is_applied(self, roi_db):
        self._demote()
        assert attention_roi.class_for("noisy", ATTENTION_DECISION) == ATTENTION_NOTICE

    def test_demoted_source_does_not_flip_back_next_cycle(self, roi_db):
        """The bug this guards: after demotion the source stops producing
        decision-lane rows, so judging promotion on that lane finds nothing and
        promotes it back every six hours."""
        self._demote()
        # Next window: it now emits notices, still ignored.
        d = attention_roi.evaluate(_stats(**{f"noisy|{ATTENTION_NOTICE}": (20, 1)}))
        assert d["promote"] == []
        attention_roi.apply(d)
        assert attention_roi.class_for("noisy", ATTENTION_DECISION) == ATTENTION_NOTICE

    def test_promotes_back_when_engagement_recovers(self, roi_db):
        self._demote()
        d = attention_roi.evaluate(_stats(**{f"noisy|{ATTENTION_NOTICE}": (20, 15)}))
        assert [x["source"] for x in d["promote"]] == ["noisy"]
        attention_roi.apply(d)
        assert attention_roi.class_for("noisy", ATTENTION_DECISION) == ATTENTION_DECISION

    def test_held_when_evidence_is_too_thin_to_judge_either_way(self, roi_db):
        self._demote()
        d = attention_roi.evaluate(_stats(**{f"noisy|{ATTENTION_NOTICE}": (3, 3)}))
        assert d["promote"] == []

    def test_repeat_apply_is_not_reported_as_a_change(self, roi_db):
        self._demote()
        again = attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"noisy|{ATTENTION_DECISION}": (20, 2)})))
        assert again == []      # no card, no churn


class TestBlastRadius:
    def test_never_touches_an_alert(self, roi_db):
        attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"noisy|{ATTENTION_DECISION}": (20, 0)})))
        assert attention_roi.class_for("noisy", ATTENTION_ALERT) == ATTENTION_ALERT

    def test_never_raises_a_class(self, roi_db):
        assert attention_roi.class_for("whatever", ATTENTION_NOTICE) == ATTENTION_NOTICE

    def test_unreadable_table_fails_open_to_the_natural_class(
            self, roi_db, monkeypatch):
        monkeypatch.setattr(attention_roi, "_get_db",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        attention_roi._invalidate()
        assert attention_roi.class_for("x", ATTENTION_DECISION) == ATTENTION_DECISION

    def test_governor_is_bypassed_when_measuring(self, roi_db):
        """compute_stats must read the natural class, never the governed one,
        or a demotion becomes self-justifying evidence."""
        import core.memorial as memorial
        attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"noisy|{ATTENTION_DECISION}": (20, 0)})))
        opts = [{"key": "yes", "label": "同意"}]
        assert memorial._default_attention("noisy", opts, []) == ATTENTION_NOTICE
        assert memorial.natural_attention("noisy", opts, []) == ATTENTION_DECISION


class TestAnnouncement:
    def test_change_is_announced_on_a_card(self, roi_db, monkeypatch):
        sent = []
        import core.memorial as memorial
        monkeypatch.setattr(memorial, "create",
                            lambda **kw: (sent.append(kw), ("mem_1", True))[1])
        monkeypatch.setattr(attention_roi, "evaluate",
                            lambda stats=None: {
                                "demote": {"noisy": {"source": "noisy",
                                                     "to": ATTENTION_NOTICE,
                                                     "n": 20, "rate": 0.1,
                                                     "reason": "问了 20 次回了 2 次"}},
                                "promote": [], "noisy_notices": []})
        changes = attention_roi.refresh()
        assert changes and len(sent) == 1
        assert "noisy" in sent[0]["body"]
        assert "不会消失" in sent[0]["body"]   # says where it went

    def test_no_change_means_no_card(self, roi_db, monkeypatch):
        sent = []
        import core.memorial as memorial
        monkeypatch.setattr(memorial, "create",
                            lambda **kw: (sent.append(kw), ("m", True))[1])
        monkeypatch.setattr(attention_roi, "evaluate",
                            lambda stats=None: {"demote": {}, "promote": [],
                                                "noisy_notices": []})
        assert attention_roi.refresh() == []
        assert sent == []


class TestCacheFreshness:
    """Caught by CI, not locally: the TTL check must not depend on where
    time.monotonic()'s arbitrary origin happens to sit.

    Invalidation used to set _cache_at = 0.0 and rely on
    `monotonic() - 0 > TTL` to force a reload. True on a host up for days;
    false in a fresh container and for the first five minutes after a reboot,
    where the empty cache read as fresh and the governor applied nothing.
    """

    def test_overrides_load_when_monotonic_is_near_zero(self, roi_db, monkeypatch):
        attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"noisy|{ATTENTION_DECISION}": (20, 1)})))
        attention_roi._invalidate()
        monkeypatch.setattr(attention_roi.time, "monotonic", lambda: 12.0)
        assert attention_roi.overrides() == {"noisy": ATTENTION_NOTICE}

    def test_class_for_applies_overrides_right_after_a_reboot(
            self, roi_db, monkeypatch):
        monkeypatch.setattr(attention_roi.time, "monotonic", lambda: 3.0)
        attention_roi.apply(attention_roi.evaluate(
            _stats(**{f"noisy|{ATTENTION_DECISION}": (20, 1)})))
        assert attention_roi.class_for("noisy", ATTENTION_DECISION) == ATTENTION_NOTICE

    def test_cache_is_still_reused_within_the_ttl(self, roi_db, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(attention_roi.time, "monotonic", lambda: clock["t"])
        attention_roi._invalidate()
        attention_roi.overrides()                       # populates at t=1000
        calls = {"n": 0}
        real = attention_roi._get_db

        def counting():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(attention_roi, "_get_db", counting)
        clock["t"] = 1000.0 + attention_roi.CACHE_TTL_S - 1
        attention_roi.overrides()
        assert calls["n"] == 0, "cache should still be warm inside the TTL"
        clock["t"] = 1000.0 + attention_roi.CACHE_TTL_S + 1
        attention_roi.overrides()
        assert calls["n"] > 0, "cache should reload past the TTL"
