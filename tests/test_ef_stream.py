"""Tests for core.ef_stream — NDJSON parsing + PM dedup helpers."""

import json

from core.ef_stream import (
    extract_detail,
    extract_item_ids,
    extract_metadata,
    extract_relation_ids,
    format_message,
    format_relation_event,
    is_duplicate_event,
    load_seen,
    parse_cursor,
    relation_event_kind,
    remember_seen,
    save_seen,
)


def _event(messages, next_cursor="c1"):
    return json.dumps({"data": {"messages": messages, "next_cursor": next_cursor}})


def test_parse_cursor():
    assert parse_cursor(_event([], "abc")) == "abc"
    assert parse_cursor("not json") == ""
    assert parse_cursor(json.dumps({"data": {}})) == ""


def test_format_message():
    ev = _event([{"sender_name": "Ada", "content": "hi"}])
    out = format_message(ev)
    assert "Ada" in out and "hi" in out
    assert "Powered by EigenFlux" in out
    assert format_message(_event([])) == ""


def test_extract_metadata_and_detail():
    ev = _event([{"sender_name": "Ada", "sender_id": "u1", "conv_id": "c9", "item_id": "42", "content": "yo"}])
    meta = extract_metadata(ev)
    assert meta["conv_id"] == "c9" and meta["sender_id"] == "u1"
    detail = extract_detail(ev)
    assert detail[0]["item_id"] == "42" and detail[0]["conv_id"] == "c9"


# ── PM dedup ─────────────────────────────────────────────────────────

def test_extract_item_ids_skips_empty():
    ev = _event([
        {"item_id": "1", "content": "a"},
        {"item_id": "", "content": "b"},   # no id → skipped
        {"content": "c"},                   # missing id → skipped
        {"item_id": 2, "content": "d"},     # numeric → stringified
    ])
    assert extract_item_ids(ev) == ["1", "2"]
    assert extract_item_ids("garbage") == []


def test_is_duplicate_event():
    seen = {"1", "2", "3"}
    assert is_duplicate_event(["1", "2"], seen) is True        # all seen
    assert is_duplicate_event(["1", "9"], seen) is False       # one new
    assert is_duplicate_event([], seen) is False               # no ids → deliver
    assert is_duplicate_event(["4"], seen) is False            # new


def test_remember_seen_dedups_and_trims():
    s = remember_seen([], ["1", "2", "2", "3"])
    assert s == ["1", "2", "3"]                                 # order + dedup
    s = remember_seen(s, ["3", "4"])                            # 3 already present
    assert s == ["1", "2", "3", "4"]
    big = remember_seen([], [str(i) for i in range(250)], cap=200)
    assert len(big) == 200
    assert big[0] == "50" and big[-1] == "249"                  # kept the last 200


def test_seen_roundtrip(tmp_path):
    p = tmp_path / "seen.json"
    assert load_seen(p) == []                                   # missing → []
    save_seen(p, ["1", "2"])
    assert load_seen(p) == ["1", "2"]
    p.write_text("not json")
    assert load_seen(p) == []                                   # corrupt → []


def test_dedup_end_to_end():
    """A re-delivered event is recognized as duplicate after it's remembered."""
    ev = _event([{"item_id": "100", "content": "x"}])
    ids = extract_item_ids(ev)
    seen = remember_seen([], ids)
    assert is_duplicate_event(ids, set(seen)) is True           # second delivery suppressed


# ── Friend-request / relation events ─────────────────────────────────

def _friend_request_event(reqs, messages=None, next_cursor="c1"):
    """A new friend request: pm_push with EMPTY messages + friend_requests."""
    return json.dumps({"type": "pm_push", "data": {
        "messages": messages or [], "friend_requests": reqs, "next_cursor": next_cursor}})


def test_friend_request_surfaces_and_pm_formatter_drops_it():
    """The exact bug: a friend request has empty messages, so the PM formatter
    returns "" — but the relation formatter must surface it."""
    ev = _friend_request_event([
        {"request_id": "123", "from_uid": "111", "from_name": "凯瑞", "greeting": "加个好友"}
    ])
    assert format_message(ev) == ""                             # PM path drops it (the bug)
    out = format_relation_event(ev)
    assert "凯瑞" in out and "加个好友" in out
    assert "好友申请" in out
    assert "Powered by EigenFlux" in out


def test_friend_request_without_greeting():
    ev = _friend_request_event([{"request_id": "1", "from_name": "Ada"}])
    out = format_relation_event(ev)
    assert "Ada" in out
    assert ">" not in out                                       # no blank greeting quote


def test_friend_accepted_event():
    ev = json.dumps({"type": "friend_accepted", "data": {"friend_uid": "111"}})
    out = format_relation_event(ev)
    assert "已通过" in out and "好友" in out
    assert format_message(ev) == ""                             # not a PM


def test_relation_event_kind_assigns_single_lifecycle_owner():
    request = _friend_request_event([{"request_id": "1", "from_name": "Ada"}])
    accepted = json.dumps(
        {"type": "friend_accepted", "data": {"friend_uid": "111"}})

    assert relation_event_kind(request) == "friend_request"
    assert relation_event_kind(accepted) == "friend_accepted"
    assert relation_event_kind(_event([])) == ""


def test_non_relation_event_returns_empty():
    pm = _event([{"sender_name": "Ada", "content": "hi"}])
    assert format_relation_event(pm) == ""                     # plain PM → not relation
    assert format_relation_event("garbage") == ""
    assert format_relation_event(json.dumps({"data": {}})) == ""


def test_extract_relation_ids():
    ev = _friend_request_event([
        {"request_id": "123", "from_name": "A"},
        {"request_id": "", "from_name": "B"},                  # no id → skipped
        {"request_id": 456, "from_name": "C"},                 # numeric → stringified
    ])
    assert extract_relation_ids(ev) == ["123", "456"]
    acc = json.dumps({"type": "friend_accepted", "data": {"friend_uid": "111"}})
    assert extract_relation_ids(acc) == ["accepted:111"]       # synthetic key
    assert extract_relation_ids(_event([{"item_id": "1"}])) == []   # PM → no relation ids


def test_relation_dedup_end_to_end():
    """A re-delivered friend request is suppressed after being remembered."""
    ev = _friend_request_event([{"request_id": "999", "from_name": "凯瑞"}])
    ids = extract_relation_ids(ev)
    seen = remember_seen([], ids)
    assert is_duplicate_event(ids, set(seen)) is True
