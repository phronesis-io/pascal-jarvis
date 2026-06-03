"""Tests for core.jsonl — shared rolling-JSONL store helpers."""

from core.jsonl import append_jsonl, read_jsonl, write_jsonl


def test_read_missing_file_is_empty(tmp_path):
    assert read_jsonl(tmp_path / "nope.jsonl") == []


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "log.jsonl"
    rows = [{"a": 1}, {"b": "中文"}]
    write_jsonl(p, rows)
    assert read_jsonl(p) == rows


def test_write_is_utf8_not_escaped(tmp_path):
    p = tmp_path / "log.jsonl"
    write_jsonl(p, [{"x": "放下"}])
    assert "放下" in p.read_text(encoding="utf-8")  # ensure_ascii=False


def test_read_skips_blank_and_malformed_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text('{"ok": 1}\n\nnot json\n{"ok": 2}\n', encoding="utf-8")
    assert read_jsonl(p) == [{"ok": 1}, {"ok": 2}]


def test_write_creates_parent_dirs(tmp_path):
    p = tmp_path / "deep" / "nested" / "log.jsonl"
    write_jsonl(p, [{"a": 1}])
    assert read_jsonl(p) == [{"a": 1}]


def test_write_empty_list_yields_empty_file(tmp_path):
    p = tmp_path / "log.jsonl"
    write_jsonl(p, [])
    assert read_jsonl(p) == []


def test_append_adds_one_row(tmp_path):
    p = tmp_path / "log.jsonl"
    append_jsonl(p, {"n": 1})
    append_jsonl(p, {"n": 2})
    assert read_jsonl(p) == [{"n": 1}, {"n": 2}]


def test_append_keep_last_trims_oldest(tmp_path):
    p = tmp_path / "log.jsonl"
    for i in range(5):
        append_jsonl(p, {"n": i}, keep_last=3)
    assert read_jsonl(p) == [{"n": 2}, {"n": 3}, {"n": 4}]


def test_write_is_atomic_no_tmp_left_behind(tmp_path):
    p = tmp_path / "log.jsonl"
    write_jsonl(p, [{"a": 1}])
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != "log.jsonl"]
    assert leftovers == []
