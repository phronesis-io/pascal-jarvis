from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from core.components import check_components, load_manifest


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "components.yaml"
    path.write_text(
        "components:\n"
        "  - name: model-runtime\n"
        "    check: model_runtime\n"
        "    path: data/jarvis.db\n"
        "    failure_streak: 3\n"
        "    stale_after_seconds: 1800\n",
        encoding="utf-8",
    )
    return path


def _db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "data" / "jarvis.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE model_runtime_calls (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            effect_authority TEXT NOT NULL,
            status TEXT NOT NULL,
            terminal_reason TEXT NOT NULL DEFAULT '',
            executor_pid INTEGER NOT NULL DEFAULT 0,
            started_epoch REAL NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE model_runtime_attempts (
            call_id TEXT NOT NULL,
            attempt INTEGER NOT NULL
        );
        """
    )
    return db


def _insert_call(
    db: sqlite3.Connection,
    call_id: str,
    *,
    status: str,
    age: float,
    effect_authority: str = "none",
    attempts: int = 0,
) -> None:
    db.execute(
        "INSERT INTO model_runtime_calls "
        "(id,task_id,effect_authority,status,started_epoch,attempt_count) "
        "VALUES (?,?,?,?,?,?)",
        (call_id, f"task:{call_id}", effect_authority, status,
         time.time() - age, attempts),
    )
    db.executemany(
        "INSERT INTO model_runtime_attempts (call_id,attempt) VALUES (?,?)",
        [(call_id, index + 1) for index in range(attempts)],
    )


def _check(tmp_path: Path) -> dict:
    (result,) = check_components(
        manifest_path=_manifest(tmp_path), root=tmp_path,
    )
    return result


def test_model_runtime_component_detects_stale_running_call(tmp_path):
    db = _db(tmp_path)
    _insert_call(db, "stale", status="running", age=3600)
    db.commit()
    db.close()

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "stalled" in result["detail"]


def test_model_runtime_component_detects_recent_failure_streak(tmp_path):
    db = _db(tmp_path)
    for index in range(3):
        _insert_call(
            db, f"failed-{index}", status="failed", age=index + 1,
        )
    db.commit()
    db.close()

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "3 consecutive failed" in result["detail"]


def test_model_runtime_component_success_breaks_failure_streak(tmp_path):
    db = _db(tmp_path)
    _insert_call(db, "older-failure", status="failed", age=30)
    _insert_call(db, "success", status="succeeded", age=20)
    _insert_call(db, "newer-failure", status="failed", age=10)
    db.commit()
    db.close()

    result = _check(tmp_path)

    assert result["ok"] is True
    assert "failure streak 1" in result["detail"]


def test_model_runtime_component_detects_ambiguous_external_effect(tmp_path):
    db = _db(tmp_path)
    _insert_call(
        db, "ambiguous", status="ambiguous", age=30,
        effect_authority="external", attempts=1,
    )
    db.commit()
    db.close()

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "ambiguous write/external" in result["detail"]


def test_model_runtime_component_detects_receipt_mismatch(tmp_path):
    db = _db(tmp_path)
    _insert_call(db, "mismatch", status="failed", age=30, attempts=0)
    db.execute(
        "UPDATE model_runtime_calls SET attempt_count=2 WHERE id='mismatch'"
    )
    db.commit()
    db.close()

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "receipt mismatch" in result["detail"]


def test_model_runtime_component_requires_receipt_schema(tmp_path):
    path = tmp_path / "data" / "jarvis.db"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()

    result = _check(tmp_path)

    assert result["ok"] is False
    assert "schema is not initialized" in result["detail"]


def test_shipped_manifest_arms_model_runtime_check():
    armed = [c for c in load_manifest() if c.get("check") == "model_runtime"]
    assert len(armed) == 1
    assert armed[0]["name"] == "model-runtime"
    assert armed[0]["critical"] is False
