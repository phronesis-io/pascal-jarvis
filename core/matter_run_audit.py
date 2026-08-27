"""Read-only Phase-0 truth metrics for Matter execution residue."""

from __future__ import annotations

import time
from typing import Any


def audit_matter_runs(*, now: float | None = None) -> dict[str, Any]:
    epoch = float(time.time() if now is None else now)
    from core.db import get_db
    db = get_db()
    counts = {
        row["status"]: int(row["count"])
        for row in db.execute(
            "SELECT status,COUNT(*) AS count FROM matter_runs GROUP BY status"
        ).fetchall()
    }
    stale_active = int(db.execute(
        "SELECT COUNT(*) FROM matter_runs WHERE status IN ('acquired','running') "
        "AND lease_expires_epoch <= ?", (epoch,),
    ).fetchone()[0])
    legacy = int(db.execute(
        "SELECT COUNT(*) FROM matter_events WHERE event_type='work_session_completed' "
        "AND json_extract(payload, '$.receipt_id') IS NULL"
    ).fetchone()[0])
    done_without_outcome = int(db.execute(
        "SELECT COUNT(*) FROM matters WHERE status='done' AND TRIM(outcome)=''"
    ).fetchone()[0])
    open_without_next = int(db.execute(
        "SELECT COUNT(*) FROM matters WHERE status IN ('active','waiting','blocked') "
        "AND TRIM(next_action)=''"
    ).fetchone()[0])
    unreceipted_terminal = int(db.execute(
        "SELECT COUNT(*) FROM matter_runs WHERE status IN ('released','failed') "
        "AND TRIM(result_digest)=''"
    ).fetchone()[0])
    return {
        "schema": "jarvis.matter-run-audit.v1",
        "counts": counts,
        "stale_active_leases": stale_active,
        "legacy_unreceipted_session_events": legacy,
        "done_matters_without_outcome": done_without_outcome,
        "open_matters_without_next_action": open_without_next,
        "terminal_runs_without_receipt": unreceipted_terminal,
        "healthy": not (
            stale_active or unreceipted_terminal or done_without_outcome
        ),
    }
