"""Named, verified SQLite migrations shared by Jarvis domain stores.

The dashboard owns the ordered base schema. Domain modules can still start
independently, so their additive compatibility migrations live here instead
of importing the dashboard runtime. Each batch is atomic: schema changes and
their durable markers commit together or roll back together.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime, timezone


class MigrationError(RuntimeError):
    """The migration ledger and the physical schema disagree."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REGISTRY = "_domain_migrations"
_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.05
_ColumnShape = tuple[str, int, str | None]


def _identifier(value: str, label: str) -> str:
    value = str(value or "")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid SQLite {label}: {value!r}")
    return value


def _normalize_type(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def _normalize_default(value: object) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).strip())


def _table_shapes(
    db: sqlite3.Connection,
    table: str,
) -> dict[str, _ColumnShape]:
    return {
        str(row[1]): (
            _normalize_type(row[2]),
            int(row[3]),
            _normalize_default(row[4]),
        )
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _expected_shapes(
    columns: Sequence[tuple[str, str]],
) -> dict[str, _ColumnShape]:
    definition = ", ".join(
        f"{column} {ddl}" for column, ddl in columns
    )
    with sqlite3.connect(":memory:") as probe:
        probe.execute(f"CREATE TABLE migration_shape ({definition})")
        return _table_shapes(probe, "migration_shape")


def _verify_registry(db: sqlite3.Connection) -> None:
    rows = {
        str(row[1]): (
            _normalize_type(row[2]),
            int(row[3]),
            _normalize_default(row[4]),
            int(row[5]),
        )
        for row in db.execute(f"PRAGMA table_info({_REGISTRY})").fetchall()
    }
    expected = {
        "namespace": ("TEXT", 1, None, 1),
        "name": ("TEXT", 1, None, 2),
        "applied_at": ("TEXT", 1, None, 0),
    }
    if rows != expected:
        raise MigrationError(
            f"migration registry shape mismatch: expected={expected!r} "
            f"actual={rows!r}"
        )


def _verify_shape(
    *,
    namespace: str,
    table: str,
    column: str,
    actual: _ColumnShape,
    expected: _ColumnShape,
) -> None:
    if actual != expected:
        raise MigrationError(
            "migration column shape mismatch: "
            f"{namespace}/{table}.add_column.{column}; "
            f"expected={expected!r} actual={actual!r}"
        )


def _registry_exists(db: sqlite3.Connection) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (_REGISTRY,),
    ).fetchone() is not None


def _applied_names(db: sqlite3.Connection, namespace: str) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            f"SELECT name FROM {_REGISTRY} WHERE namespace=?",
            (namespace,),
        ).fetchall()
    }


def _batch_is_applied(
    db: sqlite3.Connection,
    *,
    namespace: str,
    table: str,
    columns: Sequence[tuple[str, str]],
    expected_shapes: dict[str, _ColumnShape],
) -> bool:
    have = _table_shapes(db, table)
    if not have:
        raise MigrationError(f"migration target table does not exist: {table}")
    if not _registry_exists(db):
        return False
    _verify_registry(db)
    applied = _applied_names(db, namespace)
    complete = True
    for column, _definition in columns:
        name = f"{table}.add_column.{column}"
        if name not in applied:
            complete = False
            continue
        if column not in have:
            raise MigrationError(
                "migration marker exists but column is missing: "
                f"{namespace}/{name}"
            )
        _verify_shape(
            namespace=namespace,
            table=table,
            column=column,
            actual=have[column],
            expected=expected_shapes[column],
        )
    return complete


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def ensure_additive_columns(
    db: sqlite3.Connection,
    *,
    namespace: str,
    table: str,
    columns: Sequence[tuple[str, str]],
    applied_at: str | None = None,
) -> None:
    """Apply and record one atomic batch of ``ADD COLUMN`` migrations.

    A pre-existing column without a marker is adopted and marked. A marker
    without its physical column is schema drift and fails closed. Pending work
    owns an ``IMMEDIATE`` transaction so concurrent launchd/CLI initializers
    serialize before reading migration state; lock contention retries the
    entire transaction from a fresh snapshot. Existing and newly created
    columns must match SQLite's parsed type, nullability, and default metadata
    before a marker is accepted.
    """
    namespace = _identifier(namespace, "migration namespace")
    table = _identifier(table, "table")
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for column, definition in columns:
        column = _identifier(column, "column")
        definition = str(definition or "").strip()
        if not definition or ";" in definition:
            raise ValueError(f"invalid SQLite definition for {column!r}")
        if column in seen:
            raise ValueError(f"duplicate migration column: {column}")
        seen.add(column)
        normalized.append((column, definition))
    if not normalized:
        return
    expected_shapes = _expected_shapes(normalized)

    stamp = applied_at or datetime.now(timezone.utc).isoformat()
    for attempt in range(_MAX_ATTEMPTS):
        transaction_started = False
        try:
            if _batch_is_applied(
                db,
                namespace=namespace,
                table=table,
                columns=normalized,
                expected_shapes=expected_shapes,
            ):
                return
            if db.in_transaction:
                raise MigrationError(
                    "pending migration requires an autonomous connection: "
                    f"{namespace}/{table}"
                )

            db.execute("BEGIN IMMEDIATE")
            transaction_started = True
            db.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_REGISTRY} (
                    namespace TEXT NOT NULL,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    PRIMARY KEY(namespace, name)
                )
                """
            )
            _verify_registry(db)
            applied = _applied_names(db, namespace)
            have = _table_shapes(db, table)
            if not have:
                raise MigrationError(
                    f"migration target table does not exist: {table}"
                )

            for column, definition in normalized:
                name = f"{table}.add_column.{column}"
                if name in applied:
                    if column not in have:
                        raise MigrationError(
                            "migration marker exists but column is missing: "
                            f"{namespace}/{name}"
                        )
                elif column not in have:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
                    have = _table_shapes(db, table)
                if column not in have:
                    raise MigrationError(
                        f"migration did not create column: {namespace}/{name}"
                    )
                _verify_shape(
                    namespace=namespace,
                    table=table,
                    column=column,
                    actual=have[column],
                    expected=expected_shapes[column],
                )
                if name not in applied:
                    db.execute(
                        f"INSERT INTO {_REGISTRY}(namespace,name,applied_at) "
                        "VALUES (?,?,?)",
                        (namespace, name, stamp),
                    )
            db.commit()
            return
        except sqlite3.OperationalError as exc:
            if transaction_started:
                db.rollback()
            if not _is_lock_contention(exc) or attempt + 1 >= _MAX_ATTEMPTS:
                raise
            time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
        except Exception:
            if transaction_started:
                db.rollback()
            raise
