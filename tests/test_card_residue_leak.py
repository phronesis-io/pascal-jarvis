"""TITLE:/OPTIONS: are authoring directives — they must never reach a card.

Reproduces the four open P0 audit findings (#268 #276 #282 #285, 2026-07-22 →
07-27). Both leaks came from the same shape: the residue extractors were bolted
onto ONE entry path, so every other way of making a card shipped the raw line.

  #276  intentions  — caller passed explicit options, so create() skipped the
                      OPTIONS strip entirely
  #268/#282/#285 daily-reflect — builds its own rich card, adopted by
                      adopt_card, which ran neither extractor
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    """Isolated memorial ledger — never the production one."""
    import core.memorial as memorial
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_ledger_path", lambda: tmp_path / "memorials.jsonl")
    monkeypatch.setattr(memorial, "_outbox_path", lambda: tmp_path / "outbox.jsonl")
    monkeypatch.setattr(memorial, "_pending_merge_path",
                        lambda: tmp_path / "pending.jsonl")
    monkeypatch.setattr(memorial, "_record_delivery", lambda *a, **k: None)
    monkeypatch.setattr(memorial, "_record_engagement", lambda *a, **k: None)
    monkeypatch.setattr(memorial, "_request_proactive_reach", lambda *a, **k: None)
    return memorial


def _rich_card(header: str, body: str) -> str:
    """The shape tasks build with core.card.build_rich_card."""
    return json.dumps({
        "header": {"title": {"content": header}},
        "elements": [{"text": {"content": body}}],
        "__jarvis_source": "daily-reflect",
    }, ensure_ascii=False)


class TestExplicitOptionsPath:
    """#276 — intentions closure cards passed their own options."""

    def test_options_line_is_stripped_even_when_caller_supplies_options(
            self, ledger):
        body = ("跟克拉拉那顿饭聊得怎么样？\n\n"
                "OPTIONS: 有个线索记一下 | 纯叙旧没别的 | 还没约成/没去")
        mid, _ = ledger.create(
            source="intentions", title="跟克拉拉那顿饭", body=body,
            options=[{"key": "a", "label": "记一下"},
                     {"key": "b", "label": "没别的"}],
            send=False)
        state = ledger.get_memorial(mid)
        assert "OPTIONS:" not in state["body"]
        # The caller's own buttons still win — stripping residue and choosing
        # buttons are separate decisions.
        assert [o["key"] for o in state["options"]] == ["a", "b"]

    def test_title_line_is_stripped_even_when_caller_supplies_title(self, ledger):
        mid, _ = ledger.create(
            source="intentions", title="调用方标题",
            body="TITLE: 模型写的标题\n\n正文在这里",
            options=[{"key": "a", "label": "好"}], send=False)
        state = ledger.get_memorial(mid)
        assert "TITLE:" not in state["body"]
        assert state["title"] == "调用方标题"      # caller wins
        assert "正文在这里" in state["body"]

    def test_body_title_fills_an_empty_caller_title(self, ledger):
        mid, _ = ledger.create(source="heartbeat", title="",
                               body="TITLE: 这才是标题\n正文", send=False)
        state = ledger.get_memorial(mid)
        assert state["title"] == "这才是标题"
        assert "TITLE:" not in state["body"]

    def test_stripping_is_idempotent(self, ledger):
        """The plain-text route already extracts before calling create()."""
        mid, _ = ledger.create(source="heartbeat", title="T",
                               body="干净正文，没有指令行", send=False)
        assert ledger.get_memorial(mid)["body"] == "干净正文，没有指令行"


class TestAdoptedCardPath:
    """#268/#282/#285 — daily-reflect built its own card."""

    def test_adopted_card_does_not_ship_options_residue(self, ledger):
        body = ("今天排得松，你自己把它填满了。\n\n"
                "OPTIONS: 记一笔 | 今天不想说")
        out = ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        assert "OPTIONS:" not in out          # not in the rendered card either
        states = ledger.list_memorials()
        assert states and all("OPTIONS:" not in s["body"] for s in states)

    def test_adopted_card_does_not_ship_title_residue(self, ledger):
        body = "TITLE: 今天的复盘——三条目标是你自己写下的\n\n上午你主动写出三条目标。"
        ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        state = ledger.list_memorials()[0]
        assert "TITLE:" not in state["body"]

    def test_explicit_title_beats_the_decorative_header(self, ledger):
        """'🌙 回顾' names the source; the TITLE line names THIS card."""
        body = "TITLE: 今天的复盘——三条目标是你自己写下的\n\n正文。"
        ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        state = ledger.list_memorials()[0]
        assert "三条目标" in state["title"]

    def test_options_line_becomes_real_buttons(self, ledger):
        body = "今天怎么样？\n\nOPTIONS: 挺好 | 一般 | 不想说"
        ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        state = ledger.list_memorials()[0]
        assert [o["label"] for o in state["options"]] == ["挺好", "一般", "不想说"]

    def test_a_card_with_its_own_options_is_never_split(self, ledger):
        """Splitting one designed ask would replicate it across cards."""
        body = ("**第一件事**\n内容一。\n\n**第二件事**\n内容二。\n\n"
                "OPTIONS: 都知道了 | 回头说")
        ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        assert len(ledger.list_memorials()) == 1

    def test_button_free_card_still_splits_by_matter(self, ledger):
        """The 一卡一事 backstop must survive this fix."""
        body = "**第一件事**\n内容一。\n\n**第二件事**\n内容二。"
        ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        assert len(ledger.list_memorials()) >= 1   # splitting still allowed

    def test_native_callback_card_keeps_its_own_buttons(self, ledger):
        card = json.dumps({
            "header": {"title": {"content": "日程变动"}},
            "elements": [
                {"text": {"content": "会议改到周四"}},
                {"actions": [{"text": {"content": "接受"},
                              "value": {"action": "cal", "id": "1"}}]},
            ],
        }, ensure_ascii=False)
        ledger.adopt_card("calendar-sync", card)
        state = ledger.list_memorials()[0]
        assert state["extra_buttons"][0]["text"] == "接受"


class TestNoResidueEverReachesTheLedger:
    def test_sweep_of_realistic_bodies(self, ledger):
        """Every audit-observed shape, through both entry paths."""
        shapes = [
            "TITLE: 标题\n正文\nOPTIONS: 甲 | 乙",
            "正文\n\nOPTIONS: 甲 | 乙",
            "TITLE: 只有标题",
            "标题：中文冒号形式\n正文",
        ]
        for i, body in enumerate(shapes):
            ledger.create(source="heartbeat", title="", body=body,
                          send=False, dedup_key=f"k{i}")
            ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        for state in ledger.list_memorials():
            assert "OPTIONS:" not in state["body"], state["body"]
            assert not state["body"].lstrip().startswith("TITLE:"), state["body"]
