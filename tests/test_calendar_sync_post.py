"""format_change_lines — the 日程变动 card body (2026-07-14 card-quality fix)."""

from tasks.calendar_sync_post import format_change_lines


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
