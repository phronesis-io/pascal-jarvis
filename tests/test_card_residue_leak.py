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
            authoring_protocol=True, send=False)
        state = ledger.get_memorial(mid)
        assert "OPTIONS:" not in state["body"]
        # The caller's own buttons still win — stripping residue and choosing
        # buttons are separate decisions.
        assert [o["key"] for o in state["options"]] == ["a", "b"]

    def test_title_line_is_stripped_even_when_caller_supplies_title(self, ledger):
        mid, _ = ledger.create(
            source="intentions", title="调用方标题",
            body="TITLE: 模型写的标题\n\n正文在这里",
            options=[{"key": "a", "label": "好"}],
            authoring_protocol=True, send=False)
        state = ledger.get_memorial(mid)
        assert "TITLE:" not in state["body"]
        assert state["title"] == "调用方标题"      # caller wins
        assert "正文在这里" in state["body"]

    def test_body_title_fills_an_empty_caller_title(self, ledger):
        mid, _ = ledger.create(source="heartbeat", title="",
                               body="TITLE: 这才是标题\n正文",
                               authoring_protocol=True, send=False)
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

    def test_adopted_overlong_card_keeps_every_word_for_phone_reader(
            self, ledger):
        from core.card import build_card

        body = "\n".join(
            f"第{i:03d}段：" + ("完整内容" * 30) for i in range(100)
        )
        legacy = build_card("长文", body, source="daily-reflect")
        assert "__jarvis_full_body" in json.loads(legacy)

        rendered = ledger.adopt_card("daily-reflect", legacy)
        state = ledger.list_memorials()[0]

        assert state["body"] == body
        assert "__jarvis_full_body" not in rendered
        assert ledger.FULL_TEXT_BUTTON_LABEL in rendered

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
                          authoring_protocol=True, send=False,
                          dedup_key=f"k{i}")
            ledger.adopt_card("daily-reflect", _rich_card("🌙 回顾", body))
        for state in ledger.list_memorials():
            assert "OPTIONS:" not in state["body"], state["body"]
            assert not state["body"].lstrip().startswith("TITLE:"), state["body"]

    def test_concatenated_directive_cards_are_split_and_clean(self, ledger):
        """8/12: two valid drafts without `---` became one leaking card."""
        output = (
            "TITLE: 3点碰 ANP\n"
            "第一件事的完整背景。\n"
            "OPTIONS: 把全套备料给我 | 够了，进去聊\n\n"
            "TITLE: 4点白皮书\n"
            "第二件事的完整背景。\n"
            "OPTIONS: 把待办列给我 | 知道了"
        )

        rendered = ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()

        assert len(rendered.splitlines()) == 2
        assert [state["title"] for state in states] == ["3点碰 ANP", "4点白皮书"]
        assert [option["label"] for option in states[0]["options"]] == [
            "把全套备料给我", "够了，进去聊"]
        assert [option["label"] for option in states[1]["options"]] == [
            "把待办列给我", "知道了"]
        assert all("OPTIONS:" not in state["body"] for state in states)
        assert all("TITLE:" not in state["body"] for state in states)

    def test_generic_create_preserves_quoted_directive_like_content(self, ledger):
        mid, _ = ledger.create(
            source="mail", title="协议讨论",
            body=("对方原文：\nOPTIONS: 这是正文里的一句\n"
                  "TITLE: 也是引用内容\n后面还有话"),
            preset="fyi", send=False)
        state = ledger.get_memorial(mid)
        assert "OPTIONS: 这是正文里的一句" in state["body"]
        assert "TITLE: 也是引用内容" in state["body"]

    def test_generic_create_preserves_leading_and_trailing_directives(self, ledger):
        body = ("TITLE: 对方邮件标题\n正文\nOPTIONS: 这是原文末行\n"
                "RECOMMEND: 这也是引文 — 不应执行")
        mid, _ = ledger.create(
            source="mail", title="邮件转述", body=body,
            preset="fyi", send=False)
        state = ledger.get_memorial(mid)
        assert state["body"] == body
        assert state["authoring_protocol"] is False
        assert [option["key"] for option in state["options"]] == [
            "read", "watch"]

    def test_fenced_protocol_example_is_not_split_or_scrubbed(self, ledger):
        output = (
            "TITLE: 协议说明\n"
            "下面是配置例子：\n```text\n"
            "TITLE: 示例一\nOPTIONS: 甲 | 乙\n"
            "TITLE: 示例二\nOPTIONS: 丙 | 丁\n```\n"
            "OPTIONS: 看过了 | 稍后读"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert "TITLE: 示例一" in states[0]["body"]
        assert "OPTIONS: 甲 | 乙" in states[0]["body"]

    def test_quoted_protocol_example_is_not_split_or_scrubbed(self, ledger):
        output = (
            "TITLE: 引用说明\n"
            "> TITLE: 示例一\n> OPTIONS: 甲 | 乙\n"
            "> TITLE: 示例二\n> OPTIONS: 丙 | 丁\n"
            "OPTIONS: 看过了 | 稍后读"
        )
        ledger.memorialize_output(output, source="intention-check")
        state = ledger.list_memorials()[0]
        assert "> TITLE: 示例一" in state["body"]
        assert "> OPTIONS: 甲 | 乙" in state["body"]

    def test_preamble_does_not_hide_first_authored_title(self, ledger):
        output = (
            "下面是两张卡：\n"
            "TITLE: 第一件事\n正文一\nOPTIONS: 甲 | 乙\n"
            "TITLE: 第二件事\n正文二\nOPTIONS: 丙 | 丁"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert [state["title"] for state in states] == ["第一件事", "第二件事"]
        assert "下面是两张卡" in states[0]["body"]

    def test_empty_options_directive_is_scrubbed_from_model_output(self, ledger):
        ledger.memorialize_output(
            "TITLE: 一件事\n正文\nOPTIONS:", source="intention-check")
        state = ledger.list_memorials()[0]
        assert "OPTIONS:" not in state["body"]

    def test_notice_followed_by_decision_never_cross_binds_buttons(self, ledger):
        output = (
            "TITLE: 只是通知\n这是不需要拍板的变化。\n"
            "TITLE: 需要决定\n这是另一件事。\nOPTIONS: 同意 | 拒绝"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert [state["title"] for state in states] == ["只是通知", "需要决定"]
        assert [option["key"] for option in states[0]["options"]] == [
            "read", "watch"]
        assert [option["label"] for option in states[1]["options"]] == [
            "同意", "拒绝"]

    def test_empty_options_in_second_card_cannot_bind_to_first(self, ledger):
        ledger.memorialize_output(
            "TITLE: 第一张\n正文一\n"
            "TITLE: 第二张\n正文二\nOPTIONS:",
            source="intention-check")
        states = ledger.list_memorials()
        assert [state["title"] for state in states] == ["第一张", "第二张"]
        assert all("OPTIONS:" not in state["body"] for state in states)

    def test_indented_protocol_example_is_preserved(self, ledger):
        output = (
            "TITLE: 协议说明\n下面是缩进代码：\n"
            "    TITLE: 示例一\n    OPTIONS: 甲 | 乙\n"
            "OPTIONS: 看过了 | 稍后读"
        )
        ledger.memorialize_output(output, source="intention-check")
        state = ledger.list_memorials()[0]
        assert "    TITLE: 示例一" in state["body"]
        assert "    OPTIONS: 甲 | 乙" in state["body"]
        assert [option["label"] for option in state["options"]] == [
            "看过了", "稍后读"]

    def test_fence_with_suffix_is_not_treated_as_a_close(self, ledger):
        output = (
            "TITLE: 协议说明\n```text\n"
            "TITLE: 示例一\n```oops\n"
            "TITLE: 示例二\nOPTIONS: 甲 | 乙\n```\n"
            "OPTIONS: 看过了 | 稍后读"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert "TITLE: 示例二" in states[0]["body"]

    def test_unclosed_fence_options_never_become_buttons(self, ledger):
        output = "TITLE: 协议说明\n```text\nOPTIONS: 只是代码"
        ledger.memorialize_output(output, source="intention-check")
        state = ledger.list_memorials()[0]
        assert "OPTIONS: 只是代码" in state["body"]
        assert [option["key"] for option in state["options"]] == [
            "read", "watch"]

    def test_fenced_card_json_never_becomes_an_interactive_card(self, ledger):
        callback_card = json.dumps({
            "config": {"wide_screen_mode": True},
            "header": {"title": {"content": "示例卡"}},
            "elements": [{"actions": [{
                "text": {"content": "执行"},
                "value": {"action": "dangerous-example"},
            }]}],
        }, ensure_ascii=False)
        output = (
            "TITLE: 卡片协议示例\n```json\n"
            f"{callback_card}\n---\n```"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert not states[0]["extra_buttons"]
        assert "dangerous-example" in states[0]["body"]

    def test_list_fenced_card_json_never_becomes_interactive(self, ledger):
        callback_card = json.dumps({
            "config": {"wide_screen_mode": True},
            "header": {"title": {"content": "示例卡"}},
            "elements": [{"actions": [{
                "text": {"content": "执行"},
                "value": {"action": "dangerous-example"},
            }]}],
        }, ensure_ascii=False)
        output = (
            "TITLE: 列表内协议示例\n- ```json\n"
            f"  {callback_card}\n  ```\n正文结束"
        )
        ledger.memorialize_output(output, source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert not states[0]["extra_buttons"]
        assert "dangerous-example" in states[0]["body"]

    def test_lazy_blockquote_bare_json_never_becomes_interactive(self, ledger):
        callback_card = json.dumps({
            "config": {}, "header": {"title": {"content": "示例"}},
            "elements": [{"actions": [{
                "text": {"content": "执行"},
                "value": {"action": "dangerous-example"},
            }]}],
        }, ensure_ascii=False)
        ledger.memorialize_output(
            f"TITLE: 引用示例\n> 下面是旧协议卡：\n{callback_card}\n正文结束",
            source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert not states[0]["extra_buttons"]
        assert "dangerous-example" in states[0]["body"]

    def test_lazy_blockquote_card_envelope_never_becomes_interactive(self, ledger):
        callback_card = json.dumps({
            "config": {}, "header": {"title": {"content": "示例"}},
            "elements": [{"actions": [{
                "text": {"content": "执行"},
                "value": {"action": "dangerous-example"},
            }]}],
        }, ensure_ascii=False)
        ledger.memorialize_output(
            f"TITLE: 引用示例\n> 下面是 CARD 协议：\nCARD:{callback_card}\n正文结束",
            source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert not states[0]["extra_buttons"]
        assert "CARD:" in states[0]["body"]

    def test_malformed_card_envelope_is_dropped(self, ledger):
        rendered = ledger.memorialize_output(
            'CARD:{"config":', source="intention-check")
        assert rendered == ""
        assert ledger.list_memorials() == []

    def test_indented_standalone_json_never_becomes_interactive(self, ledger):
        callback_card = json.dumps({
            "config": {}, "header": {"title": {"content": "示例"}},
            "elements": [{"actions": [{
                "text": {"content": "执行"},
                "value": {"action": "dangerous-example"},
            }]}],
        }, ensure_ascii=False)
        rendered = ledger.memorialize_output(
            "    " + callback_card, source="intention-check")
        assert rendered == ""
        assert ledger.list_memorials() == []

    def test_adopted_concatenated_card_splits_without_callback_replication(
            self, ledger):
        body = ("TITLE: 第一张\n正文一\nOPTIONS: 甲 | 乙\n"
                "TITLE: 第二张\n正文二\nOPTIONS: 丙 | 丁")
        legacy = json.dumps({
            "config": {},
            "header": {"title": {"content": "旧卡"}},
            "elements": [
                {"text": {"content": body}},
                {"actions": [{
                    "text": {"content": "执行旧动作"},
                    "value": {"action": "legacy-action"},
                }]},
            ],
        }, ensure_ascii=False)
        rendered = ledger.adopt_card("heartbeat", legacy)
        states = ledger.list_memorials()
        assert len(rendered.splitlines()) == 2
        assert [state["title"] for state in states] == ["第一张", "第二张"]
        assert states[0]["extra_buttons"] == []
        assert states[1]["extra_buttons"] == []
        assert all("OPTIONS:" not in state["body"] for state in states)

    def test_adopted_elements_keep_callbacks_with_their_own_text(self, ledger):
        legacy = json.dumps({
            "config": {}, "header": {"title": {"content": "旧卡"}},
            "elements": [
                {"text": {"content": "TITLE: A\n正文 A"}},
                {"actions": [{"text": {"content": "动作 A"},
                              "value": {"action": "action-a"}}]},
                {"text": {"content": "TITLE: B\n正文 B"}},
                {"actions": [{"text": {"content": "动作 B"},
                              "value": {"action": "action-b"}}]},
            ],
        }, ensure_ascii=False)
        ledger.adopt_card("heartbeat", legacy)
        states = ledger.list_memorials()
        assert [state["title"] for state in states] == ["A", "B"]
        assert [state["extra_buttons"][0]["value"]["action"]
                for state in states] == ["action-a", "action-b"]

    def test_direct_authoring_create_keeps_only_first_card_contract(self, ledger):
        mid, _ = ledger.create(
            source="intentions", title="闭环事项",
            body=("TITLE: 第一张\n正文一\nOPTIONS: 甲 | 乙\n"
                  "TITLE: 第二张\n正文二\nOPTIONS: 丙 | 丁"),
            options=[{"key": "done", "label": "做完了"}],
            authoring_protocol=True, send=False)
        state = ledger.get_memorial(mid)
        assert state["body"] == "正文一"
        assert [option["key"] for option in state["options"]] == ["done"]
        assert "第二张" not in state["body"]

    def test_segmented_external_sentinel_renders_but_authored_sentinel_does_not(
            self, ledger):
        external_id, _ = ledger.create(
            source="eigenflux", title="外部来信",
            body="对方原文提到了 HEARTBEAT_OK",
            authoring_protocol=True, authoring_audit_text="本地分析正常",
            preset="fyi", send=False)
        assert ledger.card_json(external_id)
        rendered = json.loads(ledger.card_json(external_id))
        rendered_text = "\n".join(
            element.get("text", {}).get("content", "")
            for element in rendered["elements"])
        assert "HEARTBEAT\\_OK" in rendered_text

        internal_id, _ = ledger.create(
            source="eigenflux", title="外部来信", body="普通原文",
            authoring_protocol=True,
            authoring_audit_text="模型泄漏 HEARTBEAT_OK",
            preset="fyi", send=False)
        assert ledger.card_json(internal_id) == ""

    def test_multiline_malformed_card_envelope_drops_entire_block(self, ledger):
        rendered = ledger.memorialize_output(
            'CARD:{\n  "config": {},\n  "elements": []\n}\n泄漏尾巴',
            source="intention-check")
        assert rendered == ""
        assert ledger.list_memorials() == []

    def test_prose_before_malformed_card_drops_the_whole_envelope_tail(
            self, ledger):
        rendered = ledger.memorialize_output(
            '可见前言\nCARD:{\n  "config": {},\n  "elements": []\n}\n泄漏尾巴',
            source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert states[0]["body"] == "可见前言"
        assert "config" not in states[0]["body"]
        assert "泄漏尾巴" not in states[0]["body"]
        assert rendered

    def test_lazy_blockquote_directives_remain_quoted_content(self, ledger):
        ledger.memorialize_output(
            "> 对方原文：\nTITLE: 引用标题\nOPTIONS: 甲 | 乙",
            source="intention-check")
        state = ledger.list_memorials()[0]
        assert "TITLE: 引用标题" in state["body"]
        assert "OPTIONS: 甲 | 乙" in state["body"]
        assert [option["key"] for option in state["options"]] == [
            "read", "watch"]

    def test_indented_card_json_and_separator_remain_plain_content(self, ledger):
        card = json.dumps({
            "config": {}, "header": {"title": {"content": "示例"}},
            "elements": [{"text": {"content": "正文"}}],
        }, ensure_ascii=False)
        ledger.memorialize_output(
            f"TITLE: 示例说明\n    {card}\n    ---\n正文结束",
            source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 1
        assert "正文结束" in states[0]["body"]

    def test_space_tab_indented_directives_are_preserved(self, ledger):
        output = (
            "TITLE: 协议说明\n混合缩进代码：\n"
            " \tTITLE: 示例\n \tOPTIONS: 甲 | 乙\n"
            "OPTIONS: 看过了 | 稍后读"
        )
        ledger.memorialize_output(output, source="intention-check")
        state = ledger.list_memorials()[0]
        assert " \tTITLE: 示例" in state["body"]
        assert " \tOPTIONS: 甲 | 乙" in state["body"]

    def test_empty_title_is_a_hard_boundary(self, ledger):
        ledger.memorialize_output(
            "TITLE:\n第一张正文\n"
            "TITLE: 第二张\n第二张正文\nOPTIONS: 同意 | 拒绝",
            source="intention-check")
        states = ledger.list_memorials()
        assert len(states) == 2
        assert states[0]["title"] != "第二张"
        assert [option["label"] for option in states[1]["options"]] == [
            "同意", "拒绝"]

    def test_malformed_and_distant_recommendations_are_scrubbed(self, ledger):
        for index, body in enumerate((
                "TITLE: 空推荐\n正文\nRECOMMEND:",
                "TITLE: 远推荐\nRECOMMEND: 同意 — 因为证据齐了\n"
                "正文一\n正文二\n正文三\n正文四")):
            ledger.memorialize_output(body, source=f"heartbeat-{index}")
        assert all(
            "RECOMMEND:" not in state["body"]
            for state in ledger.list_memorials()
        )

    def test_empty_recommendation_keeps_authored_buttons(self, ledger):
        ledger.memorialize_output(
            "TITLE: 是否发布\n正文\nOPTIONS: 同意 | 不采纳\nRECOMMEND:",
            source="intention-check")
        state = ledger.list_memorials()[0]
        assert [option["label"] for option in state["options"]] == [
            "同意", "不采纳"]
        assert state["recommend"] is None

    def test_invalid_backtick_info_string_does_not_hide_directives(self, ledger):
        ledger.memorialize_output(
            "TITLE: 协议说明\n```lang`oops\nOPTIONS: 同意 | 拒绝",
            source="intention-check")
        state = ledger.list_memorials()[0]
        assert [option["label"] for option in state["options"]] == [
            "同意", "拒绝"]

    def test_trailing_recommendation_keeps_authored_buttons(self, ledger):
        ledger.memorialize_output(
            "TITLE: 是否发布\n正文\nOPTIONS: 同意 | 不采纳\n"
            "RECOMMEND: 同意 — 因为证据齐了",
            source="intention-check")
        state = ledger.list_memorials()[0]
        assert [option["label"] for option in state["options"]] == [
            "同意", "不采纳"]
        assert state["recommend"] == {
            "key": "r1", "label": "同意", "why": "因为证据齐了"}
        assert "OPTIONS:" not in state["body"]
        assert "RECOMMEND:" not in state["body"]

    def test_concatenated_cards_keep_their_own_recommendations(self, ledger):
        ledger.memorialize_output(
            "TITLE: 第一张\n正文一\nOPTIONS: 同意 | 拒绝\n"
            "RECOMMEND: 同意 — 第一张证据充分\n"
            "TITLE: 第二张\n正文二\nOPTIONS: 执行 | 暂缓\n"
            "RECOMMEND: 暂缓 — 第二张还缺数据",
            source="intention-check")
        states = ledger.list_memorials()
        assert [state["title"] for state in states] == ["第一张", "第二张"]
        assert [state["recommend"]["label"] for state in states] == [
            "同意", "暂缓"]
        assert [state["recommend"]["why"] for state in states] == [
            "第一张证据充分", "第二张还缺数据"]
