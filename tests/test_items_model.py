"""Memorial-first item model used by the desktop archive."""

from __future__ import annotations

import pytest

pytest.importorskip("nicegui", exc_type=ImportError)

from dashboard.pages.items import _item_source_label, enrich_items, filter_items


def _state(mid: str, epoch: float, *, matter_id: str = "",
           attention: str = "decision", review: str = "lark",
           status: str = "pending", options: list[dict] | None = None) -> dict:
    return {
        "id": mid,
        "epoch": epoch,
        "ts": "2026-07-23 12:00",
        "source": "mail",
        "title": mid,
        "body": "body",
        "matter_id": matter_id,
        "attention": attention,
        "review_surface": review if attention == "decision" else "none",
        "delivery_status": "delivered",
        "status": status,
        "options": options or (
            [{"key": "approve", "label": "同意", "action": None}]
            if attention == "decision"
            else [{"key": "read", "label": "已阅", "action": None}]
        ),
        "extra_buttons": [],
    }


def test_enriches_matter_as_topic_and_intent_as_timer():
    state = _state("mem_1", 100, matter_id="mat_1")
    items = enrich_items(
        [state],
        matters=[{"id": "mat_1", "title": "移动端统一"}],
        intents=[{"id": "int_1", "status": "pending",
                  "closure_status": "none"}],
        intent_topics={"int_1": "mat_1"},
    )
    assert items[0]["_topic_label"] == "移动端统一"
    assert items[0]["_has_timer"] is True
    assert items[0]["_timer_ids"] == ["int_1"]


def test_closure_button_exposes_timer_without_showing_intent_entity():
    state = _state(
        "mem_2", 100,
        options=[{
            "key": "done", "label": "做了",
            "action": {"type": "intent_close",
                       "params": {"id": "int_parent"}},
        }],
    )
    item = enrich_items(
        [state],
        intents=[{"id": "int_parent", "status": "executed",
                  "closure_status": "awaiting"}],
    )[0]
    assert item["_has_timer"] is True
    assert item["_timer_ids"] == ["int_parent"]


def test_filters_status_topic_time_and_attention_surface():
    now = 1_000_000.0
    items = enrich_items([
        _state("phone", now - 60, matter_id="mat_a", review="phone"),
        _state("lark", now - 60, matter_id="mat_b", review="lark"),
        _state("notice", now - 60, attention="notice"),
        _state("old", now - 40 * 86400, matter_id="mat_a"),
        _state("done", now - 60, status="decided"),
    ])
    assert [item["id"] for item in filter_items(
        items, mode="pending", topic_id="mat_a",
        time_window="30d", surface="lark", now=now)] == ["phone"]
    assert [item["id"] for item in filter_items(
        items, mode="notice", time_window="24h", now=now)] == ["notice"]
    assert [item["id"] for item in filter_items(
        items, mode="decided", time_window="24h", now=now)] == ["done"]


def test_intent_is_not_a_user_facing_topic_name():
    assert _item_source_label("intention-check") == "提醒"
