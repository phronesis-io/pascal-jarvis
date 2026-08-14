"""No affordance may lead nowhere.

A dead end is any place the product names a destination or spends the user's
tap without getting them there. Pascal's words: "死路让我用不了这个产品了".
Four were shipped and are pinned here:

  1. The escrow docket's 「去看看」 was a record-only option — it marked the
     docket 已批 and moved the user nowhere.
  2. 「飞书入口暂不可用」 on three surfaces, reported AFTER memorial.chat()
     had already injected the context and sent the opener: a completed action
     announced as a failure, with no next step offered.
  3. A Lark fallback card told the user to look in 「事项」 while carrying no
     way to reach it — on a phone the web desk cannot be reached by typing.
  4. 「可在「事项」里翻回」 shown to someone standing on the items page.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.memorial as memorial
from tasks import memorial_escrow


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


def _buttons(card: dict) -> list[dict]:
    return [a for el in card.get("elements", []) for a in el.get("actions", [])]


# ── 1. every button either acts or navigates ─────────────────────────────


def test_docket_never_renders_a_desk_button(env):
    """The web desk is retired (REQ-120): any URL button pointing at it would
    be a dead end, so the docket ships with no navigation buttons at all —
    an absent button beats a button that spends a tap and does nothing."""
    from datetime import datetime, timedelta
    now = datetime(2026, 7, 29, 9, 0)
    mid, _ = memorial.create(source="intention-check", title="要拍板的事",
                             body="正文", preset="decision",
                             attention=memorial.ATTENTION_DECISION, send=False)
    path = memorial._ledger_path()
    stamp = (now - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M")
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("id") == mid and e.get("ev") == "create":
            e["ts"] = stamp
        lines.append(json.dumps(e, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = memorial_escrow.run(now=now, send=True)
    docket = memorial.get_memorial(summary["docket_id"])
    assert docket["extra_buttons"] == []
    assert [o["key"] for o in docket["options"]] == ["lapse_all"]


# ── 2. a completed action is never reported as a failure ─────────────────


def test_chat_notice_reports_success_not_an_unavailable_entrance():
    from dashboard.uiutil import chat_started_notice
    payload = {"toast": {"type": "success", "content": "已加载背景——回对话窗回复我即可"},
               "deep_link": ""}
    notice = chat_started_notice(payload)
    assert notice == "已加载背景——回对话窗回复我即可"
    assert "不可用" not in notice


def test_chat_notice_still_says_something_useful_without_a_toast():
    from dashboard.uiutil import chat_started_notice
    notice = chat_started_notice({})
    assert notice and "不可用" not in notice


def test_clipped_memorial_continue_promise_is_wired_for_owner_lark_chat():
    root = Path(__file__).resolve().parent.parent
    bot = (root / "bot.sh").read_text(encoding="utf-8")
    assert "python3 -m core.memorial continue" in bot
    assert '[ "$_inline_cmd_ok" -eq 1 ]' in bot
    assert '--lookup-key "$chat_id"' in bot
    assert '--memorial-id "${_mem_id:-}"' in bot
    assert 'delivery_reply_reliable "$message_id" "$_continue_reply"' in bot
    assert "python3 -m core.memorial continue-commit" in bot
    for phrase in ("继续发", "继续发送", "发剩下的"):
        assert f'[ "$content" = "{phrase}" ]' in bot


def test_no_surface_reports_the_lark_entrance_as_unavailable():
    """All three pages used to strand the user with this exact string."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("pages/items.py", "pages/home.py"):
        text = (root / "dashboard" / name).read_text(encoding="utf-8")
        assert "飞书入口暂不可用" not in text, name


# ── 3. a card never names a destination it cannot reach ──────────────────


def test_fallback_card_has_no_retired_phone_web_link(env):
    mid, _ = memorial.create(source="s", title="t", body="b",
                             preset="fyi", send=False)
    card = memorial._replacement_card("", memorial.get_memorial(mid))
    text = json.dumps(card, ensure_ascii=False)
    assert not [b for b in _buttons(card) if b.get("url")]
    # Must not send the user somewhere the card cannot take them.
    assert "请在 Jarvis「事项」中查看" not in text


# ── 4. never point someone at the page they are standing on ──────────────


def test_lapse_all_notice_names_a_filter_not_a_destination(env, monkeypatch):
    from core.actions import ActionProcessor
    # A fresh pending decision — NOT overdue: since REQ-122 the button
    # archives the same set the docket headline counts (all pending
    # decisions), recomputed at tap time.
    memorial.create(source="intention-check", title="t", body="b",
                    preset="decision",
                    attention=memorial.ATTENTION_DECISION, send=False)
    ap = ActionProcessor(jarvis_dir=env.dir, memory_dir=str(env.dir),
                         jobs_dir=str(env.dir), log_file="",
                         owner_authenticated=True)
    message = ap._do_memorial_lapse_all("")
    assert "已收起 1 件" in message
    # 「去事项看」 is a dead end for whoever is already on the items page.
    assert "「事项」里翻回" not in message
    assert "「全部」" in message
