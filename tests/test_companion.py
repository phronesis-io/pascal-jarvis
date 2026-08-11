"""Companion checkin: the gradient, the budget, the floor, and the silence.

The incident these guard is 2026-08-02: checkin — the product itself — had
been silent for 10 days (last card 7/23) while reporting `last_status: ok`
across 708 runs, because the 7/21 prompt declared silence the expected outcome
and nothing anywhere counted it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import companion  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    yield


def _card(kind: str, *, chat: bool = False, opt: str = "",
          status: str = "decided", ts: str = "2026-08-02 12:00") -> dict:
    return {
        "source": "checkin",
        "ts": ts,
        "context": json.dumps({"kind": kind}),
        "chat_ts": "2026-08-02 12:30" if chat else "",
        "decided_opt": opt,
        "status": status,
    }


# ── the gradient ─────────────────────────────────────────────────────────────


def test_negative_tap_is_distinguishable_from_an_ack():
    """「这类不必」and「知道了」must not collapse into one number.

    They did before: the ledger offered only 「已阅／标为重点」, 22 of 23 cards
    ever sent were "engaged", and that number was worthless because the same
    tap meant both "good" and "go away". Pascal's only channel for the second
    was to complain out of band, which he did four times.
    """
    states = [_card("notice", opt="ack") for _ in range(6)]
    good = companion.kind_stats(states=states)["notice"]

    states = [_card("notice", opt=companion.NEGATIVE_OPT) for _ in range(6)]
    bad = companion.kind_stats(states=states)["notice"]

    assert good["ack"] == 6 and good["negative"] == 0
    assert bad["negative"] == 6 and bad["ack"] == 0
    assert good["score"] > bad["score"], (
        "an ack and a rejection produced the same score — the gradient is "
        "not being measured and nothing can be learned from it"
    )


def test_chat_outweighs_a_tap():
    chatted = companion.kind_stats(
        states=[_card("notice", chat=True) for _ in range(6)])["notice"]
    tapped = companion.kind_stats(
        states=[_card("notice", opt="ack") for _ in range(6)])["notice"]
    assert chatted["score"] > tapped["score"]


def test_kind_is_read_back_from_card_context():
    states = [_card("guide", opt="ack"), _card("standing", opt="ack")]
    stats = companion.kind_stats(states=states)
    assert stats["guide"]["n"] == 1
    assert stats["standing"]["n"] == 1
    assert stats["notice"]["n"] == 0


def test_card_without_a_declared_kind_falls_back_not_crashes():
    states = [{"source": "checkin", "ts": "2026-08-02 12:00",
               "context": "", "decided_opt": "ack"}]
    stats = companion.kind_stats(states=states)
    assert stats[companion.DEFAULT_KIND]["n"] == 1


def test_other_sources_are_not_counted():
    states = [{"source": "metrics-digest", "ts": "2026-08-02 12:00",
               "context": json.dumps({"kind": "notice"}), "decided_opt": "ack"}]
    assert companion.kind_stats(states=states)["notice"]["n"] == 0


# ── the budget ───────────────────────────────────────────────────────────────


def test_rejected_kind_decays_but_never_to_zero():
    """A kind at zero can never earn its way back.

    Demotion is a hypothesis, not a sentence — the same principle
    core.attention_roi states for lanes. A muted kind stops producing
    evidence, so the mute becomes permanent by construction.
    """
    states = [_card("guide", opt=companion.NEGATIVE_OPT) for _ in range(12)]
    allow = companion.allowances(companion.kind_stats(states=states))
    assert allow["guide"] == companion.ALLOWANCE_FLOOR
    assert allow["guide"] >= 1


def test_well_received_kind_earns_more_room():
    states = [_card("notice", chat=True) for _ in range(12)]
    allow = companion.allowances(companion.kind_stats(states=states))
    assert allow["notice"] > companion.ALLOWANCE_BASE
    assert allow["notice"] <= companion.ALLOWANCE_CEILING


def test_small_sample_keeps_base_allowance():
    """Under MIN_SAMPLE a rate is noise. Starving a kind on 2 cards would stop
    it ever gathering the evidence needed to judge it."""
    states = [_card("guide", opt=companion.NEGATIVE_OPT) for _ in range(2)]
    allow = companion.allowances(companion.kind_stats(states=states))
    assert allow["guide"] == companion.ALLOWANCE_BASE


def test_daily_ceiling_caps_every_kind():
    for _ in range(companion.DAILY_CEILING):
        companion.record_spoke("notice")
    state = companion.plan()
    assert state["day_remaining"] == 0
    assert all(v == 0 for v in state["remaining"].values())
    assert state["owed"] == "", "a spent day must not also owe a card"


# ── the floor: silence stops being free ──────────────────────────────────────


def test_long_silence_owes_a_card():
    """The 10-day outage, in miniature."""
    state = companion.plan(silent_hours=companion.FLOOR_HOURS + 1)
    assert state["owed"], (
        "after the floor window the system must owe a card; this is exactly "
        "the state that went unnoticed for 10 days"
    )


def test_recent_speech_owes_nothing():
    state = companion.plan(silent_hours=1.0)
    assert state["owed"] == ""


def test_owed_card_goes_to_the_best_scoring_kind():
    states = ([_card("notice", chat=True) for _ in range(8)]
              + [_card("guide", opt=companion.NEGATIVE_OPT) for _ in range(8)])
    state = companion.plan(stats=companion.kind_stats(states=states),
                           silent_hours=companion.FLOOR_HOURS + 1)
    assert state["owed"] == "notice"


def test_brief_survives_never_having_spoken():
    """Regression: formatting None hours crashed the brief, which would have
    made the silence alarm itself fail silently."""
    text = companion.brief(companion.plan(silent_hours=None))
    assert "还从没说过话" in text
    assert companion.KIND_NOTICE in text


def test_brief_tells_the_model_when_it_owes_a_card():
    text = companion.brief(companion.plan(silent_hours=companion.FLOOR_HOURS + 5))
    assert "HEARTBEAT_OK" in text and "欠一张" in text


# ── silence is a recorded decision ───────────────────────────────────────────


def test_silence_is_written_to_the_ledger():
    companion.record_silence("no anchor found")
    rows = companion.read_voice_log()
    assert [r["ev"] for r in rows] == ["silent"]
    assert rows[0]["reason"] == "no anchor found"


def test_speaking_stamps_the_freshness_file_components_watches():
    assert not companion.last_spoke_path().exists()
    companion.record_spoke("notice", "topic")
    assert companion.last_spoke_path().exists(), (
        "components.yaml companion-voice watches this file; without the stamp "
        "a mute checkin stays invisible to supervision"
    )


def test_checkin_post_records_silence_on_heartbeat_ok(tmp_path, monkeypatch):
    """The end-to-end version: HEARTBEAT_OK must leave a trace.

    Before 2026-08-02 this path was a bare `return 0`, the heartbeat scored
    the run a success, and 10 days of muteness looked identical to 10 days of
    healthy quiet.
    """
    env = {
        **dict(__import__("os").environ),
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "memory"),
        "PYTHONPATH": str(ROOT),
    }
    (tmp_path / "memory" / "system").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "checkin_post.py")],
        input="HEARTBEAT_OK", capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "HEARTBEAT_OK must not emit a card"

    log = tmp_path / "data" / "companion_voice.jsonl"
    assert log.exists(), "the decision to stay silent left no trace"
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert rows and rows[-1]["ev"] == "silent"
    assert "HEARTBEAT_OK" in rows[-1]["reason"]


def test_checkin_post_strips_kind_and_carries_it_into_the_card(tmp_path):
    """KIND must reach the ledger and must NOT reach Pascal's card."""
    env = {
        **dict(__import__("os").environ),
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "memory"),
        "PYTHONPATH": str(ROOT),
    }
    (tmp_path / "memory" / "system").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "checkin_post.py")],
        input="昨晚那条你自己留的口子，今天还开着。\nKIND: followup\nTHEMES: 测试主题",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    card = json.loads(proc.stdout.strip())

    body = json.dumps(card, ensure_ascii=False)
    assert "KIND" not in body, "the KIND contract line leaked onto the card"
    assert "THEMES" not in body, "the THEMES contract line leaked onto the card"
    assert json.loads(card["__jarvis_context"])["kind"] == "followup"

    rows = [json.loads(line) for line in
            (tmp_path / "data" / "companion_voice.jsonl").read_text().splitlines()
            if line.strip()]
    assert rows[-1]["ev"] == "spoke" and rows[-1]["kind"] == "followup"


def test_checkin_preset_offers_the_negative_option():
    from core.memorial import PRESETS, SOURCE_DEFAULT_PRESET
    assert SOURCE_DEFAULT_PRESET["checkin"] == "companion"
    keys = {opt["key"] for opt in PRESETS["companion"]}
    assert companion.NEGATIVE_OPT in keys, (
        "checkin must offer an in-band way to say 「这类不必」; without it the "
        "only channel for 乱联系 is complaining to a developer"
    )


def test_checkin_is_a_notice_whatever_buttons_it_carries():
    """8/3 09:17: the model imitated historical cards, emitted its own
    OPTIONS line, and the r1/r2 keys flipped the checkin to decision-class
    (48h escrow, decision ROI lane, phone review surface). A companion's
    voice must not be able to promote itself into a demand for a decision."""
    from core.memorial import (ATTENTION_NOTICE, _default_attention,
                               natural_attention)
    weird = [{"key": "r1", "label": "说说这个", "action": None, "reply": True},
             {"key": "r2", "label": "知道了", "action": None, "reply": True}]
    assert _default_attention("checkin", weird, []) == ATTENTION_NOTICE
    assert natural_attention("checkin", weird, []) == ATTENTION_NOTICE


def test_checkin_post_strips_model_authored_options_line(tmp_path):
    """The companion preset is the contract; an ad-hoc OPTIONS line would
    displace「这类不必」— the signal the learning loop depends on."""
    env = {
        **dict(__import__("os").environ),
        "JARVIS_DIR": str(tmp_path),
        "MEMORY_DIR": str(tmp_path / "memory"),
        "PYTHONPATH": str(ROOT),
    }
    (tmp_path / "memory" / "system").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tasks" / "checkin_post.py")],
        input="昨晚那条线你自己收了尾。\nOPTIONS: 说说这个 | 知道了\nKIND: notice\nTHEMES: 收尾",
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0
    card = json.dumps(json.loads(proc.stdout.strip()), ensure_ascii=False)
    assert "OPTIONS" not in card and "说说这个" not in card
    assert "收了尾" in card


def test_preset_lock_is_enforced_at_create_for_every_entry_path(tmp_path, monkeypatch):
    """The 8/3 button failure, fixed at the chokepoint: even a caller passing
    explicit options for a preset-locked source gets the companion preset.
    The task-script strip only covers checkin's own stdout; create() covers
    adopt_card, the heartbeat prose route, and any future emitter."""
    from core import memorial
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)

    mid, _ = memorial.create(
        source="checkin", title="t", body="正文",
        options=[{"key": "r1", "label": "说说这个", "action": None},
                 {"key": "r2", "label": "知道了", "action": None}],
        send=False,
    )
    st = memorial.get_memorial(mid)
    keys = {o["key"] for o in st["options"]}
    assert keys == {"ack", "not_this_kind"}, (
        "model/caller-authored options displaced the companion preset — "
        "the「这类不必」signal is gone again"
    )
    assert st["attention"] == memorial.ATTENTION_NOTICE
