"""事由 / 票拟 / 通政司 — the three defects that made a docket hard to read.

Each test reproduces a failure observed in the production ledger on 7/29:

  事由  79 cards shipped headed literally 「Intent」 while their own TITLE:
        line sat unused in the body, because _title_for_chunk measured the RAW
        first line — the 7-character 「TITLE: 」 prefix either leaked into the
        header or pushed a good headline past MAX_TITLE_CHARS.
  票拟  decision cards listed options with no stated recommendation, leaving
        Pascal to do the Grand Secretariat's drafting himself.
  通政司 attention_roi._engaged() scored eigenflux-friends at 13/13 = 100%
        engaged on decision cards Pascal had tapped zero times, because
        upstream `__external__` resolutions counted as his attention. That
        inversion is why the worst decision source was never demoted.
"""

from types import SimpleNamespace

import pytest

import core.memorial as memorial


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rec = SimpleNamespace(dir=tmp_path, cards=[])
    monkeypatch.setattr(memorial, "_send_card",
                        lambda card, chat_id="": (rec.cards.append(card)
                                                  or "om_test"))
    monkeypatch.setattr(memorial, "_send_text", lambda text, chat_id="": "om_test")
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    return rec


# ── 事由 ─────────────────────────────────────────────────────────────────


DECLARED_LONG = "TITLE: 缓存层昨晚回源了两万次，先把我能确定的部分说清楚，再说哪些只是猜测而已"
DECLARED_SHORT = "TITLE: 备了3条候选方案，下午会前你挑一条"


def test_declared_title_survives_being_longer_than_the_header_budget(env):
    """Synthetic, but sized like the bodies that shipped as 「Intent」: the
    7-char 「TITLE: 」 prefix is what pushed the raw line past the budget, so
    the old code discarded a perfectly good 事由 and used the module label."""
    assert len(DECLARED_LONG) > memorial.MAX_TITLE_CHARS  # the trigger
    title, rest = memorial._title_for_chunk(
        DECLARED_LONG + "\n昨晚 02:10 起回源率从 3% 涨到 61%。", "intention-check")
    assert title != "Intent"
    assert title.startswith("缓存层昨晚回源了两万次")
    assert "TITLE" not in rest.upper()


def test_short_declared_title_does_not_leak_the_directive(env):
    """Under the budget the old code returned the line WITH 「TITLE: 」 in it."""
    assert len(DECLARED_SHORT) <= memorial.MAX_TITLE_CHARS
    title, rest = memorial._title_for_chunk(
        DECLARED_SHORT + "\n三个候选各配一个真问题。", "intention-check")
    assert title == "备了3条候选方案，下午会前你挑一条"
    assert not title.upper().startswith("TITLE")
    assert "TITLE" not in rest.upper()


def test_prose_only_card_gets_a_headline_not_a_module_label(env):
    body = ("上线漏斗那三条建议挂了 13 天没批，我替你追一次。"
            "背景：验证通过后约三分之一的人从来没填过自我介绍。")
    title, _ = memorial._title_for_chunk(body, "intention-check")
    assert title != "Intent"
    assert title.startswith("上线漏斗那三条建议挂了")
    assert len(title) <= memorial.MAX_TITLE_CHARS


def test_headline_is_never_cut_mid_thought(env):
    """A fragment that says less than the generic label is worse than it."""
    assert memorial._headline_from_prose("短") == ""
    # No clause break in the back half → refuse rather than truncate mid-word.
    assert memorial._headline_from_prose("A" * 120) == ""


def test_a_real_heading_card_still_works(env):
    title, body = memorial._title_for_chunk("**磁盘满了**\n根分区 98%。", "selfmon")
    assert title == "磁盘满了"
    assert body.strip() == "根分区 98%。"


# ── 票拟 ─────────────────────────────────────────────────────────────────


def test_recommendation_renders_and_makes_its_option_primary(env):
    mid, _ = memorial.create(
        source="eigenflux-publish", title="广播待确认",
        body=("这条广播已核对三处来源。\n"
              "OPTIONS: 同意 | 不采纳\n"
              "RECOMMEND: 同意 — 三个信源已复现，撤回成本一条命令"),
        attention=memorial.ATTENTION_DECISION,
        authoring_protocol=True, send=False)
    state = memorial.get_memorial(mid)
    assert state["recommend"]["label"] == "同意"
    assert state["recommend"]["why"] == "三个信源已复现，撤回成本一条命令"
    # The directive is never content.
    assert "RECOMMEND" not in state["body"].upper()

    import json
    card = json.loads(memorial.card_json(mid))
    text = json.dumps(card, ensure_ascii=False)
    assert "建议：同意" in text
    buttons = [a for el in card["elements"] for a in el.get("actions", [])]
    primary = [b for b in buttons if b.get("type") == "primary"]
    assert len(primary) == 1
    assert primary[0]["text"]["content"] == "同意"


def test_recommendation_without_a_reason_is_dropped(env):
    """An unexplained recommendation is an order, not advice."""
    mid, _ = memorial.create(
        source="eigenflux-publish", title="t",
        body="正文\nOPTIONS: 同意 | 不采纳\nRECOMMEND: 同意",
        attention=memorial.ATTENTION_DECISION,
        authoring_protocol=True, send=False)
    state = memorial.get_memorial(mid)
    assert state["recommend"] is None
    assert "RECOMMEND" not in state["body"].upper()


def test_recommendation_naming_a_missing_option_is_dropped(env):
    mid, _ = memorial.create(
        source="eigenflux-publish", title="t",
        body="正文\nOPTIONS: 同意 | 不采纳\nRECOMMEND: 稍后再说 — 理由充分",
        attention=memorial.ATTENTION_DECISION,
        authoring_protocol=True, send=False)
    assert memorial.get_memorial(mid)["recommend"] is None


def test_trailing_recommendation_does_not_cost_the_card_its_buttons(env):
    """RECOMMEND after OPTIONS used to hide OPTIONS from its own parser."""
    mid, _ = memorial.create(
        source="eigenflux-publish", title="t",
        body="正文\nOPTIONS: 同意 | 不采纳\nRECOMMEND: 同意 — 因为证据齐了",
        attention=memorial.ATTENTION_DECISION,
        authoring_protocol=True, send=False)
    labels = [o["label"] for o in memorial.get_memorial(mid)["options"]]
    assert labels == ["同意", "不采纳"]


def test_recommendation_disappears_after_the_decision(env):
    mid, _ = memorial.create(
        source="eigenflux-publish", title="t",
        body="正文\nOPTIONS: 同意 | 不采纳\nRECOMMEND: 同意 — 因为证据齐了",
        attention=memorial.ATTENTION_DECISION,
        authoring_protocol=True, send=False)
    memorial.decide(mid, "r1", owner_authenticated=True)
    import json
    card = memorial._decided_card(memorial.get_memorial(mid))
    assert "建议：" not in json.dumps(card, ensure_ascii=False)


def test_no_recommendation_keeps_the_first_option_primary(env):
    import json
    mid, _ = memorial.create(source="s", title="t", body="b",
                             preset="decision", send=False)
    card = json.loads(memorial.card_json(mid))
    buttons = [a for el in card["elements"] for a in el.get("actions", [])]
    primary = [b for b in buttons if b.get("type") == "primary"]
    assert len(primary) == 1
    assert primary[0]["text"]["content"] == "同意"


# ── 通政司 ───────────────────────────────────────────────────────────────


def test_upstream_resolution_is_not_pascals_attention(env):
    """The eigenflux-friends inversion: 13/13 "engaged", 0 taps."""
    from core import attention_roi
    mid, _ = memorial.create(source="eigenflux-friends", title="好友申请",
                             body="某人申请加好友", preset="decision",
                             attention=memorial.ATTENTION_DECISION, send=False)
    memorial.resolve(mid, "已在 EigenFlux 侧处理")
    state = memorial.get_memorial(mid)
    assert state["status"] == "decided"
    assert attention_roi._engaged(state) is False


def test_a_real_tap_is_engagement(env):
    from core import attention_roi
    mid, _ = memorial.create(source="s", title="t", body="b",
                             preset="decision", send=False)
    memorial.decide(mid, "approve", owner_authenticated=True)
    assert attention_roi._engaged(memorial.get_memorial(mid)) is True


def test_chat_counts_as_engagement(env):
    """_engaged read `chat_started_at`, which no writer has ever produced."""
    from core import attention_roi
    mid, _ = memorial.create(source="s", title="t", body="b",
                             preset="decision", send=False)
    memorial.chat(mid)
    state = memorial.get_memorial(mid)
    assert state["chat_ts"]
    assert attention_roi._engaged(state) is True
