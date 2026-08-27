"""Human-reviewed acceptance gate for the Codex frontstage migration."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from core.matter_runs import get_run


CONNECTOR_VERSION = "0.1.0"
SURFACES = {"desktop", "mobile"}
TARGET_PER_SURFACE = 20


def _db():
    from core.db import get_db

    return get_db()


def _boolean(value: Any, field: str) -> int:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be boolean")
    return int(value)


def _receipt_valid(run: dict[str, Any]) -> bool:
    receipt = run.get("receipt") or {}
    return bool(
        run.get("status") in {"released", "failed"}
        and receipt.get("receipt_id")
        and receipt.get("digest")
        and receipt.get("digest") == run.get("result_digest")
    )


def record_acceptance(
    *,
    run_id: str,
    surface: str,
    matter_discovered_correct: bool,
    context_packet_correct: bool,
    task_completed: bool,
    duplicate_effect: bool,
    reexplanation_required: bool,
    reviewer: str,
    notes: str = "",
    connector_version: str = CONNECTOR_VERSION,
    now: float | None = None,
) -> dict[str, Any]:
    """Record one explicit review; never infer it from executor prose."""
    run = get_run(run_id)
    if run is None:
        raise KeyError(f"matter run not found: {run_id}")
    surface = str(surface or "").strip().lower()
    if surface not in SURFACES:
        raise ValueError("surface must be desktop or mobile")
    reviewer = str(reviewer or "").strip()
    if not reviewer:
        raise ValueError("reviewer is required")
    if str(run.get("surface") or "") not in {"", surface}:
        raise ValueError("review surface conflicts with the recorded run surface")
    if task_completed and not (
        run.get("status") == "released" and int(run.get("exit_code") or 0) == 0
    ):
        raise ValueError("task completion conflicts with the run receipt")
    values = {
        "matter_discovered_correct": _boolean(
            matter_discovered_correct, "matter_discovered_correct"
        ),
        "context_packet_correct": _boolean(
            context_packet_correct, "context_packet_correct"
        ),
        "task_completed": _boolean(task_completed, "task_completed"),
        "duplicate_effect": _boolean(duplicate_effect, "duplicate_effect"),
        "reexplanation_required": _boolean(
            reexplanation_required, "reexplanation_required"
        ),
    }
    receipt_valid = int(_receipt_valid(run))
    reviewed_epoch = float(time.time() if now is None else now)
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """INSERT INTO frontstage_acceptance(
                   run_id,connector_version,surface,matter_discovered_correct,
                   context_packet_correct,task_completed,receipt_valid,
                   duplicate_effect,reexplanation_required,reviewer,notes,
                   reviewed_epoch
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                   connector_version=excluded.connector_version,
                   surface=excluded.surface,
                   matter_discovered_correct=excluded.matter_discovered_correct,
                   context_packet_correct=excluded.context_packet_correct,
                   task_completed=excluded.task_completed,
                   receipt_valid=excluded.receipt_valid,
                   duplicate_effect=excluded.duplicate_effect,
                   reexplanation_required=excluded.reexplanation_required,
                   reviewer=excluded.reviewer,
                   notes=excluded.notes,
                   reviewed_epoch=excluded.reviewed_epoch""",
            (
                run_id,
                str(connector_version),
                surface,
                values["matter_discovered_correct"],
                values["context_packet_correct"],
                values["task_completed"],
                receipt_valid,
                values["duplicate_effect"],
                values["reexplanation_required"],
                reviewer[:120],
                str(notes or "")[:2000],
                reviewed_epoch,
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute(
        "SELECT * FROM frontstage_acceptance WHERE run_id=?", (run_id,)
    ).fetchone()
    return dict(row)


def acceptance_report(
    *, connector_version: str = CONNECTOR_VERSION
) -> dict[str, Any]:
    rows = _db().execute(
        "SELECT * FROM frontstage_acceptance WHERE connector_version=? "
        "ORDER BY reviewed_epoch",
        (str(connector_version),),
    ).fetchall()
    surfaces: dict[str, dict[str, Any]] = {}
    for surface in sorted(SURFACES):
        samples = [dict(row) for row in rows if row["surface"] == surface]
        reviewed = len(samples)

        def rate(field: str) -> float:
            return (
                sum(int(sample[field]) for sample in samples) / reviewed
                if reviewed
                else 0.0
            )

        critical_failures = sum(
            (not bool(sample["receipt_valid"]))
            or bool(sample["duplicate_effect"])
            for sample in samples
        )
        metrics = {
            "reviewed": reviewed,
            "target": TARGET_PER_SURFACE,
            "matter_discovery_rate": rate("matter_discovered_correct"),
            "context_packet_rate": rate("context_packet_correct"),
            "task_completion_rate": rate("task_completed"),
            "reexplanation_rate": rate("reexplanation_required"),
            "critical_failures": critical_failures,
        }
        metrics["ready"] = bool(
            reviewed >= TARGET_PER_SURFACE
            and metrics["matter_discovery_rate"] >= 0.95
            and metrics["context_packet_rate"] >= 0.95
            and metrics["task_completion_rate"] >= 0.95
            and metrics["reexplanation_rate"] <= 0.10
            and critical_failures == 0
        )
        surfaces[surface] = metrics
    return {
        "schema": "jarvis.frontstage-acceptance.v1",
        "connector_version": connector_version,
        "surfaces": surfaces,
        "ready": all(item["ready"] for item in surfaces.values()),
        "retirement_boundary": (
            "no_lark_path_retires_until_ready_and_owner_review"
        ),
    }


def _parse_bool(value: str) -> bool:
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y"}:
        return True
    if lowered in {"0", "false", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report")
    record = sub.add_parser("record")
    record.add_argument("--run-id", required=True)
    record.add_argument("--surface", choices=sorted(SURFACES), required=True)
    record.add_argument("--matter-correct", type=_parse_bool, required=True)
    record.add_argument("--packet-correct", type=_parse_bool, required=True)
    record.add_argument("--task-completed", type=_parse_bool, required=True)
    record.add_argument("--duplicate-effect", type=_parse_bool, required=True)
    record.add_argument("--reexplained", type=_parse_bool, required=True)
    record.add_argument("--reviewer", required=True)
    record.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    if args.command == "report":
        result = acceptance_report()
    else:
        result = record_acceptance(
            run_id=args.run_id,
            surface=args.surface,
            matter_discovered_correct=args.matter_correct,
            context_packet_correct=args.packet_correct,
            task_completed=args.task_completed,
            duplicate_effect=args.duplicate_effect,
            reexplanation_required=args.reexplained,
            reviewer=args.reviewer,
            notes=args.notes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
