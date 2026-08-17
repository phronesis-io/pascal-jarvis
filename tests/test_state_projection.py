import json
import sqlite3

from core.state_projection import breach_overview, delivery_overview


def _seed(tmp_path):
    path = tmp_path / "data" / "jarvis.db"
    path.parent.mkdir()
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE delivery_envelopes (
            id TEXT, source TEXT, kind TEXT, route_channel TEXT,
            state TEXT, last_error TEXT, created_epoch REAL,
            next_attempt_epoch REAL, attempts INTEGER
        );
        CREATE TABLE delivery_dead_letters (
            id INTEGER, notified_epoch REAL
        );
        CREATE TABLE intent_breaches (
            id TEXT, payload TEXT, notify_attempts INTEGER,
            created_epoch REAL, retired_epoch REAL
        );
    """)
    db.execute(
        "INSERT INTO delivery_envelopes VALUES "
        "('dlv_1','heartbeat','text','lark','queued','offline',1,2,3)")
    db.execute(
        "INSERT INTO delivery_envelopes VALUES "
        "('dlv_2','heartbeat','text','lark','failed','offline',1,NULL,9)")
    db.execute("INSERT INTO delivery_dead_letters VALUES (1,NULL)")
    db.execute(
        "INSERT INTO intent_breaches VALUES (?,?,?,?,NULL)",
        ("int_1", json.dumps({"id": "int_1", "name": "test"}), 0, 1),
    )
    db.commit()
    db.close()


def test_sqlite_operational_projections(tmp_path):
    _seed(tmp_path)
    delivery = delivery_overview(tmp_path)
    assert delivery["source"] == "sqlite"
    assert delivery["queued"] == 1
    assert delivery["failed"] == 1
    assert delivery["dead_letters"] == 1
    assert delivery["queued_items"][0]["id"] == "dlv_1"
    assert breach_overview(tmp_path)[0]["name"] == "test"


def test_projection_is_none_before_migration(tmp_path):
    assert delivery_overview(tmp_path) is None
    assert breach_overview(tmp_path) is None


def test_delivery_health_aggregation_is_not_limited_to_display_rows(tmp_path):
    _seed(tmp_path)
    path = tmp_path / "data" / "jarvis.db"
    db = sqlite3.connect(path)
    db.execute("DELETE FROM delivery_envelopes")
    for idx in range(20):
        db.execute(
            "INSERT INTO delivery_envelopes VALUES (?,?,?,?,?,?,?,?,?)",
            (f"future_{idx}", "heartbeat", "text", "lark", "queued",
             "global_daily_cap", idx + 1, 2_000, 0),
        )
    # Newer than the 20 operator-display rows, but genuinely overdue.
    db.execute(
        "INSERT INTO delivery_envelopes VALUES (?,?,?,?,?,?,?,?,?)",
        ("overdue_21", "heartbeat", "text", "lark", "queued",
         "transport_retry", 21, 100, 1),
    )
    db.commit()
    db.close()

    delivery = delivery_overview(
        tmp_path, now=1_000, queue_overdue_grace_seconds=100)

    assert delivery["queued"] == 21
    assert len(delivery["queued_items"]) == 20
    assert delivery["queued_overdue"] == 1
    assert delivery["next_queued_epoch"] == 2_000
