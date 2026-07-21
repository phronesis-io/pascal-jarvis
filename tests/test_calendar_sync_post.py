"""format_change_lines — the 日程变动 card body (2026-07-14 card-quality fix)
+ one-card-per-change (2026-07-21 一张卡一件事 regression, REQ-117)."""

import io
import json
from datetime import datetime

import tasks.calendar_sync_post as csp
from tasks.calendar_sync_post import change_card_bodies, format_change_lines


def test_move_is_one_line_not_add_cancel_pair():
    removed = {"07/15|15:00|Agent Card 评审"}
    added = {"07/15|16:00|Agent Card 评审"}
    lines = format_change_lines(added, removed)
    assert len(lines) == 1
    assert lines[0].startswith("改期：Agent Card 评审")
    assert "15:00" in lines[0] and "16:00" in lines[0]
    assert "7/15" in lines[0]  # date always present


def test_added_line_carries_date_and_weekday():
    lines = format_change_lines({"07/15|15:00|评审会"}, set())
    assert len(lines) == 1
    assert lines[0].startswith("新增：7/15(周")
    assert "15:00 评审会" in lines[0]


def test_removed_line_carries_date():
    lines = format_change_lines(set(), {"07/16|14:00|瑜伽课"})
    assert len(lines) == 1
    assert lines[0].startswith("取消：7/16(周")


def test_overflow_is_counted_not_dropped():
    added = {f"07/2{i % 8}|1{i}:00|会议{i}" for i in range(8)}
    lines = format_change_lines(added, set(), cap=5)
    assert len(lines) == 6
    assert lines[-1] == "…另有 3 项变动（详见日历）"


def test_cosmetic_empty_titles_yield_nothing():
    assert format_change_lines({"07/15|15:00| "}, set()) == []


# ── one card per change (一张卡一件事, REQ-117) ──────────────────────────


def test_three_changes_become_three_card_bodies():
    lines = ["改期：交易下一步计划 — 7/21(周二) 14:00 → 7/21(周二) 14:30",
             "改期：聊下近期的一些用户反馈 — 7/21(周二) 15:00 → 7/21(周二) 15:30",
             "改期：白皮书 + Vic 讨论 — 7/21(周二) 16:00 → 7/21(周二) 16:30"]
    bodies = change_card_bodies(lines)
    assert bodies == lines  # one line = one matter = one card


def test_single_change_is_single_card_body():
    assert change_card_bodies(["新增：7/22(周三) 15:00 评审会"]) == [
        "新增：7/22(周三) 15:00 评审会"]


def test_overflow_counter_rides_the_last_card_not_its_own():
    lines = ["改期：A — 7/21 14:00 → 15:00", "取消：7/22(周三) 10:00 B",
             "…另有 3 项变动（详见日历）"]
    bodies = change_card_bodies(lines)
    assert len(bodies) == 2
    assert bodies[0] == lines[0]
    assert bodies[1] == lines[1] + "\n" + lines[2]


def test_empty_lines_yield_no_bodies():
    assert change_card_bodies([]) == []


def _fixed_now():
    return datetime(2026, 7, 21, 14, 0)


def test_main_emits_one_card_per_reschedule(tmp_path, monkeypatch, capsys):
    """Regression for the 2026-07-21 13:46 card: three meetings shifted by 30
    minutes must produce THREE cards, not one merged 日程变动 card."""
    monkeypatch.setattr(csp, "CALENDAR_FILE", tmp_path / "hot" / "calendar_today.md")
    monkeypatch.setattr(csp, "HASH_FILE", tmp_path / "system" / ".calendar_hash")
    monkeypatch.setattr(csp, "EVENTS_FILE", tmp_path / "system" / ".calendar_events.json")
    monkeypatch.setattr(csp, "RAW_CACHE", tmp_path / "system" / ".calendar_raw_output.txt")
    import core.timeutil
    monkeypatch.setattr(core.timeutil, "now_local", _fixed_now)

    csp.EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    csp.EVENTS_FILE.write_text(json.dumps([
        "07/21|14:00|交易下一步计划",
        "07/21|15:00|聊下近期的一些用户反馈",
        "07/21|16:00|白皮书 + Vic 讨论",
    ], ensure_ascii=False))
    schedule = ("Today (2026-07-21 Tuesday):\n"
                "  14:30-15:00  交易下一步计划\n"
                "  15:30-16:00  聊下近期的一些用户反馈\n"
                "  16:30-17:00  白皮书 + Vic 讨论\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(schedule))

    assert csp.main() == 0
    cards = [json.loads(line) for line in capsys.readouterr().out.splitlines()
             if line.startswith("{")]
    assert len(cards) == 3
    for card in cards:
        assert card["header"]["title"]["content"] == "📅 日程变动"
        body = card["elements"][0]["text"]["content"]
        assert body.count("改期") == 1  # exactly one matter per card
