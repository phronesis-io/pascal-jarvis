"""Human-reviewed acceptance gate for the Codex frontstage migration."""

from __future__ import annotations

import argparse
import json
import re
import time
from typing import Any

from core.matter_runs import get_run


CONNECTOR_VERSION = "0.3.1"
SURFACES = {"desktop", "mobile"}
TARGET_PER_SURFACE = 20
FEEDBACK_PROMPT = (
    "这次接续顺吗？回「顺」；有问题可回「找错事项 / 背景不对 / 没做完 / "
    "有重复动作 / 需要重讲」。"
)
_ISSUE_LABELS = {
    "找错事项": "matter_discovered_correct",
    "背景不对": "context_packet_correct",
    "没做完": "task_completed",
    "有重复动作": "duplicate_effect",
    "需要重讲": "reexplanation_required",
}
_FEEDBACK_SPLIT_RE = re.compile(r"\s*(?:、|，|,|/|\+|；|;)\s*")


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


def parse_owner_feedback(feedback: str) -> dict[str, Any]:
    """Map only the published owner labels to deterministic acceptance facts."""
    exact = str(feedback or "").strip()
    if not exact:
        raise ValueError("feedback is required")
    if exact == "顺":
        labels = ["顺"]
    else:
        labels = [part for part in _FEEDBACK_SPLIT_RE.split(exact) if part]
        if not labels or "顺" in labels:
            raise ValueError("顺 cannot be combined with issue labels")
        unknown = sorted(set(labels) - set(_ISSUE_LABELS))
        if unknown:
            raise ValueError("unsupported feedback label: " + ", ".join(unknown))
        labels = list(dict.fromkeys(labels))

    values = {
        "matter_discovered_correct": True,
        "context_packet_correct": True,
        "task_completed": True,
        "duplicate_effect": False,
        "reexplanation_required": False,
    }
    for label in labels:
        field = _ISSUE_LABELS.get(label)
        if field in {
            "matter_discovered_correct",
            "context_packet_correct",
            "task_completed",
        }:
            values[field] = False
        elif field:
            values[field] = True
    return {
        "owner_confirmation": exact,
        "labels": labels,
        **values,
    }


def _decode_receipt(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def claim_acceptance_prompt(
    run_id: str, *, now: float | None = None,
) -> dict[str, Any]:
    """Claim the one optional feedback prompt for an eligible released run."""
    epoch = float(time.time() if now is None else now)
    db = _db()
    should_ask = False
    reason = "ineligible"
    surface = ""
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM matter_runs WHERE id=?", (str(run_id),)
        ).fetchone()
        if row is None:
            raise KeyError(f"matter run not found: {run_id}")
        run = dict(row)
        run["receipt"] = _decode_receipt(run.pop("receipt_json", "{}"))
        surface = str(run.get("surface") or "")
        if surface not in SURFACES:
            reason = "surface_not_recorded"
        elif not (
            run.get("status") == "released"
            and int(run.get("exit_code") or 0) == 0
            and _receipt_valid(run)
        ):
            reason = "run_not_successfully_released"
        elif db.execute(
            "SELECT 1 FROM frontstage_acceptance WHERE run_id=?", (str(run_id),)
        ).fetchone() is not None:
            reason = "feedback_already_recorded"
        elif int(db.execute(
            "SELECT COUNT(*) FROM frontstage_acceptance "
            "WHERE connector_version=? AND surface=?",
            (CONNECTOR_VERSION, surface),
        ).fetchone()[0]) >= TARGET_PER_SURFACE:
            reason = "surface_target_reached"
        elif run.get("acceptance_prompted_epoch") is not None:
            reason = "prompt_already_claimed"
        else:
            updated = db.execute(
                "UPDATE matter_runs SET acceptance_prompted_epoch=?,"
                "acceptance_prompt_version=? "
                "WHERE id=? AND acceptance_prompted_epoch IS NULL",
                (epoch, CONNECTOR_VERSION, str(run_id)),
            )
            should_ask = updated.rowcount == 1
            reason = "prompt_claimed" if should_ask else "prompt_already_claimed"
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "schema": "jarvis.frontstage-feedback-prompt.v1",
        "run_id": str(run_id),
        "surface": surface,
        "connector_version": CONNECTOR_VERSION,
        "should_ask": should_ask,
        "reason": reason,
        "prompt": FEEDBACK_PROMPT if should_ask else "",
    }


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
    owner_confirmation: str,
    notes: str = "",
    connector_version: str | None = None,
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
    if reviewer != "owner":
        raise ValueError("reviewer must be owner")
    owner_confirmation = str(owner_confirmation or "").strip()
    if not owner_confirmation:
        raise ValueError("owner_confirmation is required")
    if len(owner_confirmation) > 200:
        raise ValueError("owner_confirmation exceeds 200 characters")
    connector_version = str(connector_version or CONNECTOR_VERSION)
    if str(run.get("surface") or "") != surface:
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
    parsed_confirmation = parse_owner_feedback(owner_confirmation)
    for field, value in values.items():
        if bool(value) != bool(parsed_confirmation[field]):
            raise ValueError("owner_confirmation conflicts with acceptance fields")
    receipt_valid = int(_receipt_valid(run))
    reviewed_epoch = float(time.time() if now is None else now)
    db = _db()
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM frontstage_acceptance WHERE run_id=?", (run_id,)
        ).fetchone()
        expected = {
            "connector_version": str(connector_version),
            "surface": surface,
            **values,
            "receipt_valid": receipt_valid,
            "reviewer": reviewer[:120],
            "notes": str(notes or "")[:2000],
            "owner_confirmation": owner_confirmation,
        }
        if existing is not None:
            current = dict(existing)
            if all(current.get(key) == value for key, value in expected.items()):
                db.commit()
                return current
            raise ValueError("acceptance evidence is immutable once recorded")
        db.execute(
            """INSERT INTO frontstage_acceptance(
                   run_id,connector_version,surface,matter_discovered_correct,
                   context_packet_correct,task_completed,receipt_valid,
                   duplicate_effect,reexplanation_required,reviewer,notes,
                   reviewed_epoch,owner_confirmation
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                owner_confirmation,
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


def record_owner_feedback(
    run_id: str, feedback: str, *, now: float | None = None,
) -> dict[str, Any]:
    """Record Pascal's exact label response for one previously prompted run."""
    parsed = parse_owner_feedback(feedback)
    run = get_run(run_id)
    if run is None:
        raise KeyError(f"matter run not found: {run_id}")
    surface = str(run.get("surface") or "")
    if surface not in SURFACES:
        raise ValueError("run surface must be desktop or mobile")
    if run.get("acceptance_prompted_epoch") is None:
        raise ValueError("feedback prompt was not claimed for this run")
    connector_version = str(run.get("acceptance_prompt_version") or "")
    if not connector_version:
        raise ValueError("feedback prompt has no connector version")
    return record_acceptance(
        run_id=run_id,
        surface=surface,
        matter_discovered_correct=parsed["matter_discovered_correct"],
        context_packet_correct=parsed["context_packet_correct"],
        task_completed=parsed["task_completed"],
        duplicate_effect=parsed["duplicate_effect"],
        reexplanation_required=parsed["reexplanation_required"],
        reviewer="owner",
        owner_confirmation=parsed["owner_confirmation"],
        connector_version=connector_version,
        now=now,
    )


def acceptance_report(
    *, connector_version: str | None = None
) -> dict[str, Any]:
    connector_version = str(connector_version or CONNECTOR_VERSION)
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
    feedback = sub.add_parser("feedback")
    feedback.add_argument("--run-id", required=True)
    feedback.add_argument("--feedback", required=True)
    record = sub.add_parser("record")
    record.add_argument("--run-id", required=True)
    record.add_argument("--surface", choices=sorted(SURFACES), required=True)
    record.add_argument("--matter-correct", type=_parse_bool, required=True)
    record.add_argument("--packet-correct", type=_parse_bool, required=True)
    record.add_argument("--task-completed", type=_parse_bool, required=True)
    record.add_argument("--duplicate-effect", type=_parse_bool, required=True)
    record.add_argument("--reexplained", type=_parse_bool, required=True)
    record.add_argument("--reviewer", required=True)
    record.add_argument("--owner-confirmation", required=True)
    record.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    if args.command == "report":
        result = acceptance_report()
    elif args.command == "feedback":
        result = record_owner_feedback(args.run_id, args.feedback)
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
            owner_confirmation=args.owner_confirmation,
            notes=args.notes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
