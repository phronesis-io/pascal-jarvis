"""Durability and privacy tests for the watch-later task tool."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import core.db as shared_db
import tasks.watchlater_save as watchlater

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tasks" / "watchlater_save.py"
REAL_SQLITE_SAVE = watchlater._save_to_sqlite


def _configure(tmp_path, monkeypatch):
    store = tmp_path / "memory" / "system" / "watchlater.jsonl"
    monkeypatch.setattr(watchlater, "STORE_FILE", store)
    monkeypatch.setattr(watchlater, "now_local_str", lambda _fmt: "2026-08-26 12:00")
    monkeypatch.setattr(watchlater, "_save_to_sqlite", lambda *_args: True)
    return store


def test_argument_save_is_atomic_private_and_deduplicated(tmp_path, monkeypatch, capsys):
    store = _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        watchlater.sys,
        "argv",
        ["watchlater_save.py", "Article", "https://example.com/a", "button"],
    )

    assert watchlater.main() == 0
    assert "已收藏" in capsys.readouterr().out
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.with_suffix(".jsonl.lock").stat().st_mode) == 0o600
    assert watchlater.load_entries() == [{
        "ts": "2026-08-26 12:00",
        "title": "Article",
        "url": "https://example.com/a",
        "status": "pending",
        "source": "button",
    }]

    assert watchlater.main() == 0
    assert "已在收藏列表中" in capsys.readouterr().out
    assert len(watchlater.load_entries()) == 1


def test_json_input_validation_and_malformed_rows(tmp_path, monkeypatch, capsys):
    store = _configure(tmp_path, monkeypatch)
    store.parent.mkdir(parents=True)
    store.write_text('{"url":"https://old"}\nnot-json\n[1,2]\n', encoding="utf-8")
    monkeypatch.setattr(watchlater.sys, "argv", ["watchlater_save.py"])
    monkeypatch.setattr(
        watchlater.sys,
        "stdin",
        io.StringIO(json.dumps({"title": "New", "url": "https://example.com/new"})),
    )

    assert watchlater.main() == 0
    assert len(watchlater.load_entries()) == 2
    assert "已收藏" in capsys.readouterr().out

    monkeypatch.setattr(watchlater.sys, "stdin", io.StringIO("[]"))
    assert watchlater.main() == 1
    assert "must be an object" in capsys.readouterr().err


def test_store_is_capped_to_newest_fifty(tmp_path, monkeypatch):
    store = _configure(tmp_path, monkeypatch)
    watchlater.save_entries([
        {"url": f"https://example.com/{index}"}
        for index in range(watchlater.MAX_ENTRIES)
    ])
    monkeypatch.setattr(
        watchlater.sys,
        "argv",
        ["watchlater_save.py", "Newest", "https://example.com/newest"],
    )

    assert watchlater.main() == 0

    rows = watchlater.load_entries()
    assert len(rows) == watchlater.MAX_ENTRIES
    assert rows[0]["url"] == "https://example.com/1"
    assert rows[-1]["url"] == "https://example.com/newest"


def test_jsonl_failure_is_not_reported_as_success(tmp_path, monkeypatch, capsys):
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        watchlater.sys,
        "argv",
        ["watchlater_save.py", "Article", "https://example.com/fail"],
    )
    monkeypatch.setattr(
        watchlater,
        "save_entries",
        lambda _entries: (_ for _ in ()).throw(OSError("private detail")),
    )
    events = []
    monkeypatch.setattr(
        watchlater,
        "_structured_log",
        lambda component, message, **fields: events.append((component, message, fields)),
    )

    assert watchlater.main() == 1

    output = capsys.readouterr()
    assert "写入失败" in output.err
    assert "已收藏" not in output.out
    assert events == [(
        "watchlater-save",
        "jsonl_write_failed",
        {"level": "error", "error_type": "OSError"},
    )]
    assert "private detail" not in json.dumps(events)


def test_sqlite_failure_is_visible_without_undoing_jsonl(tmp_path, monkeypatch, capsys):
    store = _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(watchlater, "_save_to_sqlite", REAL_SQLITE_SAVE)
    monkeypatch.setattr(
        watchlater.sys,
        "argv",
        ["watchlater_save.py", "Article", "https://example.com/sqlite"],
    )
    events = []
    monkeypatch.setattr(
        watchlater,
        "_structured_log",
        lambda component, message, **fields: events.append((component, message, fields)),
    )
    monkeypatch.setattr(
        shared_db,
        "bookmark_add",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    assert watchlater.main() == 0
    assert store.exists()
    assert "已收藏" in capsys.readouterr().out
    assert events == [(
        "watchlater-save",
        "sqlite_dual_write_failed",
        {"level": "warn", "error_type": "RuntimeError"},
    )]
    assert "private detail" not in json.dumps(events)


def test_concurrent_cli_writers_preserve_every_distinct_url(tmp_path):
    env = os.environ.copy()
    env["MEMORY_DIR"] = str(tmp_path / "memory")
    env["JARVIS_DIR"] = str(tmp_path / "runtime")
    processes = [
        subprocess.Popen(
            [sys.executable, str(SCRIPT), f"Item {index}", f"https://example.com/{index}"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(12)
    ]
    results = [process.communicate(timeout=20) for process in processes]

    assert [process.returncode for process in processes] == [0] * len(processes), results
    store = tmp_path / "memory" / "system" / "watchlater.jsonl"
    rows = [json.loads(line) for line in store.read_text(encoding="utf-8").splitlines()]
    assert {row["url"] for row in rows} == {
        f"https://example.com/{index}" for index in range(12)
    }
