"""JSON CLI for workers that execute a Verified Delegation.

Mutations read a JSON object from stdin so private summaries and evidence never
need to appear in process arguments.  Read-only commands accept stable IDs.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from core.delegations import DelegationError, DelegationStore


def _input() -> dict:
    try:
        value = json.load(sys.stdin)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise DelegationError(f"stdin must be a JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise DelegationError("stdin must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verified Delegation worker CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create")
    get_cmd = sub.add_parser("get")
    get_cmd.add_argument("id")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status", default="")
    list_cmd.add_argument("--matter-id", default="")
    list_cmd.add_argument("--needs-attention", action="store_true")
    list_cmd.add_argument("--include-shadow", action="store_true")
    list_cmd.add_argument("--limit", type=int, default=100)
    for command in (
        "bind",
        "revise",
        "add-step",
        "claim",
        "renew",
        "attempt",
        "evidence",
        "wait",
        "resume",
        "confirm",
        "retry",
        "terminal",
        "link",
    ):
        cmd = sub.add_parser(command)
        cmd.add_argument("id")
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("id")
    reconcile = sub.add_parser("release-expired")
    reconcile.add_argument("--limit", type=int, default=100)
    sub.add_parser("metrics")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = DelegationStore()
    try:
        if args.command == "create":
            row, created = store.create(**_input())
            result = {"created": created, "delegation": row}
        elif args.command == "get":
            result = store.get(args.id)
        elif args.command == "list":
            result = {
                "items": store.list(
                    status=args.status,
                    matter_id=args.matter_id,
                    needs_attention=args.needs_attention,
                    include_shadow=args.include_shadow,
                    limit=args.limit,
                )
            }
        elif args.command == "bind":
            result = store.bind(args.id, **_input())
        elif args.command == "revise":
            result = store.revise_contract(args.id, **_input())
        elif args.command == "add-step":
            result = store.add_step(args.id, **_input())
        elif args.command == "claim":
            result = asdict(store.claim_step(args.id, **_input()))
        elif args.command == "renew":
            result = asdict(store.renew_claim(args.id, **_input()))
        elif args.command == "attempt":
            result = store.record_attempt(args.id, **_input())
        elif args.command == "evidence":
            data = _input()
            if set(data) != {"step_id"}:
                raise DelegationError(
                    "evidence accepts only step_id; authority, strength, and "
                    "matching come from the registered verifier"
                )
            from core.delegation_verify import verify_step

            result = verify_step(
                args.id,
                str(data["step_id"]),
                store=store,
            )
        elif args.command == "wait":
            result = store.mark_waiting(args.id, **_input())
        elif args.command == "resume":
            result = store.resume_external(args.id, **_input())
        elif args.command == "confirm":
            result = store.confirm(args.id, **_input())
        elif args.command == "retry":
            result = store.retry(args.id, **_input())
        elif args.command == "terminal":
            result = store.terminal(args.id, **_input())
        elif args.command == "link":
            data = _input()
            store.link(args.id, **data)
            result = {"status": "ok"}
        elif args.command == "evaluate":
            result = store.evaluate_completion(args.id)
        elif args.command == "release-expired":
            result = {"released": store.release_expired_leases(limit=args.limit)}
        else:
            result = store.metrics()
    except DelegationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
