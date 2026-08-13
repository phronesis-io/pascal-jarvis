"""Contracts for named, transactional domain schema migrations."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from core.sqlite_migrations import MigrationError, ensure_additive_columns


def _db(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(tmp_path / "migrations.db")
    db.execute("CREATE TABLE things (id INTEGER PRIMARY KEY)")
    db.commit()
    return db


def test_additive_migration_is_named_verified_and_idempotent(tmp_path):
    db = _db(tmp_path)
    try:
        ensure_additive_columns(
            db,
            namespace="things",
            table="things",
            columns=(("label", "TEXT NOT NULL DEFAULT ''"),),
            applied_at="2026-08-13T00:00:00+00:00",
        )
        ensure_additive_columns(
            db,
            namespace="things",
            table="things",
            columns=(("label", "TEXT NOT NULL DEFAULT ''"),),
        )
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(things)")
        }
        marker = db.execute(
            "SELECT name,applied_at FROM _domain_migrations "
            "WHERE namespace='things'"
        ).fetchall()
    finally:
        db.close()

    assert columns == {"id", "label"}
    assert marker == [(
        "things.add_column.label",
        "2026-08-13T00:00:00+00:00",
    )]


def test_pending_migration_rejects_callers_active_transaction(tmp_path):
    db = _db(tmp_path)
    try:
        db.execute("BEGIN")
        with pytest.raises(MigrationError, match="autonomous connection"):
            ensure_additive_columns(
                db,
                namespace="things",
                table="things",
                columns=(("label", "TEXT"),),
            )
        assert db.in_transaction is True
        db.rollback()
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(things)")
        }
        registry = db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='_domain_migrations'"
        ).fetchone()
    finally:
        db.close()

    assert columns == {"id"}
    assert registry is None


def test_concurrent_initializers_converge_on_one_marker(tmp_path):
    path = tmp_path / "concurrent.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE things (id INTEGER PRIMARY KEY)")
        db.execute(
            "CREATE TABLE _domain_migrations ("
            "namespace TEXT NOT NULL,name TEXT NOT NULL,applied_at TEXT NOT NULL,"
            "PRIMARY KEY(namespace,name))"
        )
        db.execute("PRAGMA journal_mode=WAL")

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def migrate() -> None:
        try:
            with sqlite3.connect(path, timeout=5) as db:
                db.execute("PRAGMA busy_timeout=5000")
                barrier.wait(timeout=5)
                ensure_additive_columns(
                    db,
                    namespace="things",
                    table="things",
                    columns=(("label", "TEXT NOT NULL DEFAULT ''"),),
                )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=migrate) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    with sqlite3.connect(path) as db:
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(things)")
        }
        markers = db.execute(
            "SELECT name FROM _domain_migrations "
            "WHERE namespace='things'"
        ).fetchall()
    assert columns == {"id", "label"}
    assert markers == [("things.add_column.label",)]


def test_lock_contention_retries_entire_migration(tmp_path):
    real = _db(tmp_path)

    class LockedFirstBegin:
        def __init__(self, connection):
            self.connection = connection
            self.begin_attempts = 0

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                self.begin_attempts += 1
                if self.begin_attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
            return self.connection.execute(sql, *args)

        def commit(self):
            return self.connection.commit()

        def rollback(self):
            return self.connection.rollback()

    db = LockedFirstBegin(real)
    try:
        ensure_additive_columns(
            db,
            namespace="things",
            table="things",
            columns=(("label", "TEXT"),),
        )
        marker = real.execute(
            "SELECT name FROM _domain_migrations WHERE namespace='things'"
        ).fetchall()
    finally:
        real.close()

    assert db.begin_attempts == 2
    assert marker == [("things.add_column.label",)]


def test_preexisting_column_is_adopted_without_rewrite(tmp_path):
    db = _db(tmp_path)
    try:
        db.execute(
            "ALTER TABLE things "
            "ADD COLUMN label TEXT NOT NULL DEFAULT ''"
        )
        db.execute("INSERT INTO things(label) VALUES ('kept')")
        db.commit()

        ensure_additive_columns(
            db,
            namespace="things",
            table="things",
            columns=(("label", "TEXT NOT NULL DEFAULT ''"),),
        )
        value = db.execute("SELECT label FROM things").fetchone()[0]
        marker = db.execute(
            "SELECT COUNT(*) FROM _domain_migrations "
            "WHERE namespace='things'"
        ).fetchone()[0]
    finally:
        db.close()

    assert value == "kept"
    assert marker == 1


def test_incompatible_preexisting_column_fails_closed(tmp_path):
    db = _db(tmp_path)
    try:
        db.execute("ALTER TABLE things ADD COLUMN label TEXT DEFAULT 'legacy'")
        db.commit()

        with pytest.raises(MigrationError, match="shape mismatch"):
            ensure_additive_columns(
                db,
                namespace="things",
                table="things",
                columns=(("label", "TEXT NOT NULL DEFAULT ''"),),
            )
        registry = db.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='_domain_migrations'"
        ).fetchone()
    finally:
        db.close()

    assert registry is None


def test_incompatible_registry_fails_closed(tmp_path):
    db = _db(tmp_path)
    try:
        db.execute(
            "CREATE TABLE _domain_migrations ("
            "namespace TEXT,name TEXT,applied_at TEXT)"
        )
        db.commit()

        with pytest.raises(MigrationError, match="registry shape mismatch"):
            ensure_additive_columns(
                db,
                namespace="things",
                table="things",
                columns=(("label", "TEXT"),),
            )
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(things)")
        }
    finally:
        db.close()

    assert columns == {"id"}


def test_marker_without_physical_column_fails_closed(tmp_path):
    db = _db(tmp_path)
    try:
        db.execute(
            "CREATE TABLE _domain_migrations ("
            "namespace TEXT NOT NULL,name TEXT NOT NULL,applied_at TEXT NOT NULL,"
            "PRIMARY KEY(namespace,name))"
        )
        db.execute(
            "INSERT INTO _domain_migrations VALUES (?,?,?)",
            ("things", "things.add_column.label", "earlier"),
        )
        db.commit()

        with pytest.raises(MigrationError, match="column is missing"):
            ensure_additive_columns(
                db,
                namespace="things",
                table="things",
                columns=(("label", "TEXT"),),
            )
    finally:
        db.close()


def test_failed_batch_rolls_back_columns_and_markers(tmp_path):
    real = _db(tmp_path)

    class FailSecondAlter:
        def __init__(self, connection):
            self.connection = connection

        @property
        def in_transaction(self):
            return self.connection.in_transaction

        def execute(self, sql, *args):
            if "ADD COLUMN second" in sql:
                raise sqlite3.DatabaseError("synthetic migration failure")
            return self.connection.execute(sql, *args)

        def commit(self):
            return self.connection.commit()

        def rollback(self):
            return self.connection.rollback()

    db = FailSecondAlter(real)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="synthetic migration"):
            ensure_additive_columns(
                db,
                namespace="things",
                table="things",
                columns=(("first", "TEXT"), ("second", "TEXT")),
            )
        columns = {
            row[1] for row in real.execute("PRAGMA table_info(things)")
        }
        registry = real.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='_domain_migrations'"
        ).fetchone()
    finally:
        real.close()

    assert columns == {"id"}
    assert registry is None


@pytest.mark.parametrize(
    ("namespace", "table", "columns"),
    [
        ("bad-name", "things", (("label", "TEXT"),)),
        ("things", "bad table", (("label", "TEXT"),)),
        ("things", "things", (("bad column", "TEXT"),)),
        ("things", "things", (("label", "TEXT; DROP TABLE things"),)),
    ],
)
def test_migration_metadata_rejects_unsafe_identifiers(
    tmp_path, namespace, table, columns,
):
    db = _db(tmp_path)
    try:
        with pytest.raises(ValueError):
            ensure_additive_columns(
                db,
                namespace=namespace,
                table=table,
                columns=columns,
            )
    finally:
        db.close()
