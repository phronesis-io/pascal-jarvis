"""format_change_lines — the 日程变动 card body (2026-07-14 card-quality fix)
+ one-card-per-change (2026-07-21 一张卡一件事 regression, REQ-117)."""

import io
import json
from datetime import datetime

import pytest

import tasks.calendar_sync_post as csp
from tasks.calendar_sync_post import change_card_bodies, format_change_lines


@pytest.fixture(autouse=True)
def _calendar_clock(monkeypatch):
    """Keep formatting tests independent from the wall-clock month."""
    import core.timeutil

    monkeypatch.setattr(
        core.timeutil, "now_local", lambda: datetime(2026, 7, 14, 12, 0)
    )


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


def test_long_change_batch_merges_into_one_card():
    """2026-08-07: a 15:14 sync pushed 7 日程变动 cards in one beat; Pascal
    tapped none and asked to merge. Past 3 changes the batch is ONE matter."""
    lines = [f"新增：8/1{i}(周三) 1{i}:00 会议{i}" for i in range(5)]
    bodies = change_card_bodies(lines)
    assert len(bodies) == 1
    assert all(ln in bodies[0] for ln in lines)  # nothing dropped


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


def test_elapsed_events_are_not_reported_as_cancelled(monkeypatch):
    """Regression for 2026-08-19 13:04: after a quiet stretch the resync
    diffed a stale snapshot against a fresh window and fired two cards —
    '取消：8/17 羽毛球' and '取消：8/18 白皮书 + Vic 讨论' — for events Pascal
    had already attended. Pascal: 也不叫取消吧，做完了？

    Anything dated before today is dropped; a same-day removal is a genuine
    cancellation and must survive.
    """
    import core.timeutil
    monkeypatch.setattr(core.timeutil, "now_local", lambda: datetime(2026, 8, 19, 13, 4))

    elapsed = {"08/17|17:00|羽毛球", "08/18|16:00|白皮书 + Vic 讨论"}
    assert csp.format_change_lines(set(), elapsed) == []

    today_cancel = csp.format_change_lines(set(), {"08/19|21:00|邹"})
    assert today_cancel == ["取消：8/19(周三) 21:00 邹"]

    # A reschedule out of an elapsed slot into a future one is still news.
    moved = csp.format_change_lines({"08/22|15:00|周会"}, {"08/18|15:00|周会"})
    assert moved == ["改期：周会 — 8/18(周二) 15:00 → 8/22(周六) 15:00"]


def test_elapsed_events_resolve_across_month_and_year_boundaries(monkeypatch):
    import core.timeutil

    monkeypatch.setattr(
        core.timeutil, "now_local", lambda: datetime(2026, 8, 2, 9, 0)
    )
    assert csp.format_change_lines(set(), {"07/31|17:00|月末复盘"}) == []

    monkeypatch.setattr(
        core.timeutil, "now_local", lambda: datetime(2027, 1, 2, 9, 0)
    )
    assert csp.format_change_lines(set(), {"12/31|17:00|年末复盘"}) == []
    assert csp.format_change_lines({"01/03|10:00|新年计划"}, set()) == [
        "新增：1/3(周日) 10:00 新年计划"
    ]


# ---- 2026-08-31: only a near cancellation/reschedule is an alert ----------

def test_added_event_is_a_notice_not_an_alert(monkeypatch):
    from tasks.calendar_sync_post import change_attention
    assert change_attention("新增：9/6(周日) 10:30 dr stretch") == "notice"


def test_cancellation_within_two_days_is_an_alert(monkeypatch):
    import datetime as dt
    from core import timeutil
    from tasks import calendar_sync_post as post

    class _Now:
        @staticmethod
        def date():
            return dt.date(2026, 8, 31)

    monkeypatch.setattr(timeutil, "now_local", lambda: _Now)
    assert post.change_attention("取消：9/1(周二) 14:00 emma") == "alert"
    assert post.change_attention(
        "改期：评审 — 8/31(周一) 15:00 → 9/2(周三) 16:00") == "alert"
    assert post.change_attention("取消：9/20(周日) 10:00 瑜伽") == "notice"


def test_change_card_declares_its_attention_and_marker_never_ships(tmp_path,
                                                                  monkeypatch):
    import json
    from core import memorial
    from core.card import build_card

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_owner")
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_cal")
    card = build_card("📅 日程变动", "新增：9/6(周日) 10:30 dr stretch",
                      source="calendar-sync", work_receipt="比对日历",
                      attention="notice")
    assert json.loads(card)["__jarvis_attention"] == "notice"
    rendered = memorial.memorialize_output(card, "calendar-sync")
    state = memorial.list_memorials()[-1]
    assert state["attention"] == "notice"
    assert state["delivery_status"] == "ledger_only"
    assert "__jarvis_attention" not in rendered
