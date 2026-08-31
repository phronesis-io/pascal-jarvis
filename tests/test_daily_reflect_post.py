"""Behavior tests for the daily reflection post-hook."""

from __future__ import annotations

import io
import json

import pytest

import tasks.daily_reflect_post as reflect
from core.jsonl import read_jsonl, write_jsonl


@pytest.fixture(autouse=True)
def retained(monkeypatch):
    monkeypatch.setattr(
        reflect, "retained_rhythm_enabled", lambda _name: True
    )


def _fixed_time(fmt: str) -> str:
    return "2026-08-26 21:30" if "%H" in fmt else "2026-08-26"


def _install_card_spy(monkeypatch):
    captured = {}

    def fake_build_rich_card(**kwargs):
        captured.update(kwargs)
        return "CARD"

    monkeypatch.setattr(reflect, "build_rich_card", fake_build_rich_card)
    return captured


def test_parsed_reflection_persists_bounded_patterns_journal_and_stamp(
    tmp_path, monkeypatch, capsys
):
    patterns = tmp_path / "memory" / "system" / "patterns.jsonl"
    write_jsonl(
        patterns,
        [{"date": "2026-08-01", "pattern": f"old-{index}"} for index in range(49)],
    )
    monkeypatch.setattr(reflect, "PATTERNS_FILE", patterns)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(reflect, "now_local_str", _fixed_time)
    journaled = []
    monkeypatch.setattr(reflect, "append_entry", journaled.append)
    card = _install_card_spy(monkeypatch)
    payload = {
        "user_message": " 今天完成了重要收尾。 ",
        "patterns_noted": ["先验证再发布", "", 42],
    }
    monkeypatch.setattr(reflect.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert reflect.main() == 0

    assert capsys.readouterr().out.strip() == "CARD"
    assert journaled == ["今天完成了重要收尾。"]
    rows = read_jsonl(patterns)
    assert len(rows) == 50
    assert rows[-1] == {"date": "2026-08-26", "pattern": "先验证再发布"}
    assert card["meta"] == {"source": "daily_reflect", "date": "2026-08-26"}
    assert card["work_receipt"] == "汇总当日记录、提炼模式并写入纵向日志"
    assert (tmp_path / "runtime" / "data" / ".daily_reflect_stamp").read_text() == "2026-08-26"


def test_persistence_failures_are_observable_but_do_not_hide_the_card(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(reflect, "PATTERNS_FILE", tmp_path / "patterns.jsonl")
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(reflect, "now_local_str", _fixed_time)
    monkeypatch.setattr(
        reflect,
        "write_jsonl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private path")),
    )
    monkeypatch.setattr(
        reflect,
        "append_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private body")),
    )
    events = []
    monkeypatch.setattr(
        reflect,
        "_structured_log",
        lambda component, message, **fields: events.append((component, message, fields)),
    )
    _install_card_spy(monkeypatch)
    monkeypatch.setattr(
        reflect.sys,
        "stdin",
        io.StringIO(json.dumps({
            "user_message": "reflection body must not enter logs",
            "patterns_noted": ["private pattern"],
        })),
    )

    assert reflect.main() == 0

    assert capsys.readouterr().out.strip() == "CARD"
    assert [event[1] for event in events] == [
        "pattern_store_failed",
        "journal_append_failed",
    ]
    assert {event[2]["error_type"] for event in events} == {"OSError", "RuntimeError"}
    assert "private" not in json.dumps(events)


def test_raw_fallback_is_shown_but_never_journaled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setattr(reflect, "now_local_str", _fixed_time)
    journaled = []
    monkeypatch.setattr(reflect, "append_entry", journaled.append)
    card = _install_card_spy(monkeypatch)
    monkeypatch.setattr(reflect.sys, "stdin", io.StringIO("a clean plain-text reflection"))

    assert reflect.main() == 0

    assert capsys.readouterr().out.strip() == "CARD"
    assert journaled == []
    assert card["sections"][0]["content"] == "a clean plain-text reflection"


def test_empty_ok_and_error_outputs_are_silent(monkeypatch, capsys):
    monkeypatch.setattr(reflect.sys, "stdin", io.StringIO("HEARTBEAT_OK"))
    assert reflect.main() == 0
    assert capsys.readouterr().out == ""

    monkeypatch.setattr(reflect.sys, "stdin", io.StringIO("provider exploded"))
    monkeypatch.setattr(reflect, "looks_like_error", lambda _raw: True)
    assert reflect.main() == 0
    assert capsys.readouterr().out == ""


def test_non_object_json_envelope_is_rejected_without_leaking(monkeypatch, capsys):
    events = []
    monkeypatch.setattr(reflect.sys, "stdin", io.StringIO("[1, 2]"))
    monkeypatch.setattr(reflect, "parse_json_response", lambda _raw: [1, 2])
    monkeypatch.setattr(
        reflect,
        "_structured_log",
        lambda component, message, **fields: events.append((component, message, fields)),
    )

    assert reflect.main() == 0

    assert capsys.readouterr().out == ""
    assert events == [(
        "daily-reflect",
        "invalid_response_envelope",
        {"level": "error", "response_type": "list"},
    )]


def test_disabled_rhythm_does_not_persist_or_emit(monkeypatch, capsys):
    monkeypatch.setattr(
        reflect, "retained_rhythm_enabled", lambda _name: False
    )
    monkeypatch.setattr(
        reflect.sys, "stdin", io.StringIO('{"user_message":"private"}')
    )
    monkeypatch.setattr(
        reflect, "append_entry", lambda _text: pytest.fail("must stay quiet")
    )
    assert reflect.main() == 0
    assert capsys.readouterr().out == ""
