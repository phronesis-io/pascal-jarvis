"""Compile cross-product transcripts into traceable, lifecycle-aware memory.

Provider transcripts and Lark turns remain audit sources. A model may propose
bounded claims, but this module validates exact source quotes and owns every
state transition. Assistant prose is a candidate; owner-authored statements
may become active memory. No claim is completion evidence for external work.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from core.memory_compiler_common import (
    AUTO_SUPERSEDE_KINDS,
    CONTEXT_SCHEMA,
    DEFAULT_BATCH_SIZE,
    MAX_CLAIMS_PER_SOURCE,
    MAX_CONTEXT_CHARS,
    MAX_CONTEXT_CLAIMS,
    OUTPUT_SCHEMA,
    VALID_KINDS,
    VALID_STATUSES,
    MemoryCompilerError,
    claim_key as _claim_key,
    db as _db,
    decode as _decode,
    flat as _flat,
    normalized as _normalized,
    now as _now,
)
from core.memory_compiler_sources import prepare_batch
from core.safety import parse_json_response


def _envelope(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        result = dict(value)
    else:
        result = parse_json_response(str(value or ""))
    if not isinstance(result, dict):
        raise MemoryCompilerError("memory compiler output is not a JSON object")
    if result.get("schema") != OUTPUT_SCHEMA:
        raise MemoryCompilerError(f"schema must be {OUTPUT_SCHEMA}")
    return result


def _source_map(batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("source_ref") or ""): dict(item)
        for item in batch.get("sources", [])
        if isinstance(item, dict) and item.get("source_ref")
    }


def _validate_claim(
    claim: Any, sources: dict[str, dict[str, Any]], counts: dict[str, int],
) -> dict[str, Any]:
    if not isinstance(claim, dict):
        raise MemoryCompilerError("each claim must be an object")
    source_ref = str(claim.get("source_ref") or "").strip()
    source = sources.get(source_ref)
    if source is None:
        raise MemoryCompilerError(f"unknown source_ref: {source_ref}")
    counts[source_ref] = counts.get(source_ref, 0) + 1
    if counts[source_ref] > MAX_CLAIMS_PER_SOURCE:
        raise MemoryCompilerError(f"too many claims for {source_ref}")
    kind = str(claim.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        raise MemoryCompilerError(f"invalid claim kind: {kind}")
    quote = _flat(claim.get("quote"), limit=1000)
    if not quote or _normalized(quote) not in _normalized(source["text"]):
        raise MemoryCompilerError(f"quote is not grounded in {source_ref}")
    content = _flat(claim.get("content"), limit=1000)
    if not content:
        raise MemoryCompilerError("claim content is required")
    requested_matter = str(claim.get("matter_id") or "").strip()
    source_matter = str(source.get("matter_id") or "").strip()
    if requested_matter and requested_matter != source_matter:
        raise MemoryCompilerError(
            f"claim cannot infer a Matter for {source_ref}"
        )
    return {
        "source_ref": source_ref,
        "kind": kind,
        "claim_key": _claim_key(claim.get("claim_key")),
        "content": content,
        "normalized_content": _normalized(content),
        "quote": quote,
        "matter_id": source_matter,
        "role": source["role"],
        "occurred_epoch": _occurred_epoch(source.get("occurred_at")),
    }


def _occurred_epoch(value: Any) -> float | None:
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _scope_clause(matter_id: str) -> tuple[str, tuple[Any, ...]]:
    if matter_id:
        return "matter_id=?", (matter_id,)
    return "matter_id IS NULL", ()


def _claim_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    sources = _db().execute(
        """SELECT source_ref,source_quote FROM memory_claim_sources
           WHERE claim_id=? ORDER BY source_ref""",
        (result["id"],),
    ).fetchall()
    result["sources"] = [dict(item) for item in sources]
    return result


def _insert_claim(
    db, claim: dict[str, Any], epoch: float,
) -> tuple[str, str, str | None]:
    matter_id = claim["matter_id"]
    scope_sql, scope_params = _scope_clause(matter_id)
    existing_rows = db.execute(
        f"""SELECT * FROM memory_claims
              WHERE {scope_sql} AND kind=? AND claim_key=?
                AND status IN ('candidate','active','conflicted')
              ORDER BY updated_epoch DESC""",
        (*scope_params, claim["kind"], claim["claim_key"]),
    ).fetchall()
    for row in existing_rows:
        if str(row["normalized_content"]) == claim["normalized_content"]:
            claim_id = str(row["id"])
            db.execute(
                """INSERT OR IGNORE INTO memory_claim_sources
                   (claim_id,source_ref,source_quote) VALUES (?,?,?)""",
                (claim_id, claim["source_ref"], claim["quote"]),
            )
            db.execute(
                "UPDATE memory_claims SET updated_epoch=? WHERE id=?",
                (epoch, claim_id),
            )
            return claim_id, "reinforced", None

    authority = (
        "owner_asserted" if claim["role"] == "user"
        else "assistant_candidate"
    )
    status = "active" if authority == "owner_asserted" else "candidate"
    claim_id = f"mcl_{uuid.uuid4().hex[:20]}"
    db.execute(
        """INSERT INTO memory_claims
           (id,kind,claim_key,content,normalized_content,status,authority,
            matter_id,valid_from_epoch,created_epoch,updated_epoch)
           VALUES (?,?,?,?,?,?,?,NULLIF(?,''),?,?,?)""",
        (
            claim_id, claim["kind"], claim["claim_key"], claim["content"],
            claim["normalized_content"], status, authority, matter_id,
            claim["occurred_epoch"], epoch, epoch,
        ),
    )
    db.execute(
        """INSERT INTO memory_claim_sources
           (claim_id,source_ref,source_quote) VALUES (?,?,?)""",
        (claim_id, claim["source_ref"], claim["quote"]),
    )
    active = [row for row in existing_rows if row["status"] in {"active", "conflicted"}]
    if status != "active" or not active:
        return claim_id, status, None
    if claim["kind"] in AUTO_SUPERSEDE_KINDS:
        for row in active:
            db.execute(
                """UPDATE memory_claims SET status='superseded',
                   superseded_by=?,updated_epoch=? WHERE id=?""",
                (claim_id, epoch, row["id"]),
            )
            db.execute(
                """UPDATE memory_conflicts SET status='resolved',
                   resolution='superseded_by_new_owner_statement',
                   resolved_by='memory_compiler',resolved_epoch=?
                   WHERE status='open' AND (prior_claim_id=? OR incoming_claim_id=?)""",
                (epoch, row["id"], row["id"]),
            )
        return claim_id, "superseded_previous", None

    prior = active[0]
    conflict_id = f"mcf_{uuid.uuid4().hex[:20]}"
    db.execute(
        "UPDATE memory_claims SET status='conflicted',updated_epoch=? WHERE id IN (?,?)",
        (epoch, prior["id"], claim_id),
    )
    db.execute(
        """INSERT OR IGNORE INTO memory_conflicts
           (id,matter_scope,claim_key,prior_claim_id,incoming_claim_id,
            status,created_epoch) VALUES (?,?,?,?,?,'open',?)""",
        (conflict_id, matter_id, claim["claim_key"], prior["id"], claim_id, epoch),
    )
    return claim_id, "conflicted", conflict_id


def apply_compile_result(
    value: str | dict[str, Any], *, now: float | None = None,
) -> dict[str, Any]:
    """Validate a model proposal against its pending batch and reconcile it."""
    envelope = _envelope(value)
    batch_id = str(envelope.get("batch_id") or "").strip()
    row = _db().execute(
        "SELECT * FROM memory_compile_batches WHERE id=? AND status='pending'",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise MemoryCompilerError("pending compile batch not found")
    batch = _decode(row["payload"], {})
    sources = _source_map(batch)
    claims_raw = envelope.get("claims", [])
    ignored_raw = envelope.get("ignored_source_refs", [])
    if not isinstance(claims_raw, list) or not isinstance(ignored_raw, list):
        raise MemoryCompilerError("claims and ignored_source_refs must be lists")
    counts: dict[str, int] = {}
    claims = [_validate_claim(item, sources, counts) for item in claims_raw]
    ignored = [str(item or "").strip() for item in ignored_raw]
    if len(set(ignored)) != len(ignored) or any(item not in sources for item in ignored):
        raise MemoryCompilerError("ignored_source_refs contains an invalid reference")
    covered = set(counts).union(ignored)
    if covered != set(sources):
        missing = sorted(set(sources) - covered)
        raise MemoryCompilerError("compile output omitted sources: " + ",".join(missing))
    if set(counts).intersection(ignored):
        raise MemoryCompilerError("a source cannot be both claimed and ignored")

    epoch = _now(now)
    db = _db()
    outcomes: list[dict[str, Any]] = []
    conflicts: list[str] = []
    try:
        db.execute("BEGIN IMMEDIATE")
        for claim in claims:
            claim_id, outcome, conflict_id = _insert_claim(db, claim, epoch)
            outcomes.append({
                "claim_id": claim_id,
                "source_ref": claim["source_ref"],
                "outcome": outcome,
            })
            if conflict_id:
                conflicts.append(conflict_id)
        if counts:
            db.executemany(
                """UPDATE memory_compile_sources SET status='compiled',
                   processed_epoch=? WHERE source_ref=? AND batch_id=?""",
                ((epoch, source_ref, batch_id) for source_ref in counts),
            )
        if ignored:
            db.executemany(
                """UPDATE memory_compile_sources SET status='ignored',
                   processed_epoch=? WHERE source_ref=? AND batch_id=?""",
                ((epoch, source_ref, batch_id) for source_ref in ignored),
            )
        db.execute(
            """UPDATE memory_compile_batches SET status='applied',payload='{}',
               completed_epoch=?,last_error='' WHERE id=?""",
            (epoch, batch_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "schema": "jarvis.memory-compile-receipt.v1",
        "batch_id": batch_id,
        "source_count": len(sources),
        "claim_count": len(claims),
        "ignored_count": len(ignored),
        "outcomes": outcomes,
        "new_conflict_ids": conflicts,
        "needs_review": bool(conflicts),
    }


def _query_claims(
    *, statuses: Iterable[str], matter_id: str | None = None,
    query: str = "", limit: int = 100,
) -> list[dict[str, Any]]:
    selected = [str(item) for item in statuses if str(item) in VALID_STATUSES]
    if not selected:
        return []
    where = ["status IN (%s)" % ",".join("?" for _ in selected)]
    params: list[Any] = list(selected)
    if matter_id is not None:
        if matter_id:
            where.append("matter_id=?")
            params.append(matter_id)
        else:
            where.append("matter_id IS NULL")
    terms: list[str] = []
    normalized_query = _normalized(query)
    for token in re.findall(r"[a-z0-9_]{3,}", normalized_query):
        if token not in terms:
            terms.append(token)
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", normalized_query):
        if run not in terms:
            terms.append(run)
        for index in range(len(run) - 1):
            token = run[index:index + 2]
            if token not in terms:
                terms.append(token)
    terms = terms[:24]
    if terms:
        where.append("(" + " OR ".join(
            "claim_key LIKE ? OR normalized_content LIKE ?" for _ in terms
        ) + ")")
        for term in terms:
            params.extend((f"%{term}%", f"%{term}%"))
    params.append(max(1, min(int(limit), 500)))
    rows = _db().execute(
        "SELECT * FROM memory_claims WHERE " + " AND ".join(where)
        + " ORDER BY updated_epoch DESC LIMIT ?",
        params,
    ).fetchall()
    return [_claim_row(row) for row in rows]


def search_compiled_memory(
    query: str = "", *, matter_id: str | None = None,
    include_candidates: bool = False, limit: int = 20,
) -> dict[str, Any]:
    statuses = ["active", "conflicted"]
    if include_candidates:
        statuses.append("candidate")
    claims = _query_claims(
        statuses=statuses, matter_id=matter_id, query=query, limit=limit,
    )
    public_claims = []
    for item in claims:
        public = dict(item)
        public["source_refs"] = [
            source["source_ref"] for source in public.pop("sources", [])
        ]
        public_claims.append(public)
    return {
        "schema": CONTEXT_SCHEMA,
        "query": query,
        "matter_id": matter_id,
        "count": len(public_claims),
        "claims": public_claims,
        "raw_transcripts_included": False,
    }


def compiled_context(
    query: str = "", *, matter_id: str | None = None,
    max_chars: int = MAX_CONTEXT_CHARS,
) -> str:
    """Render only active, source-traceable claims for one attention scope."""
    claims = _query_claims(
        statuses=["active"], matter_id=matter_id, query=query,
        limit=MAX_CONTEXT_CLAIMS,
    )
    if not claims:
        return ""
    lines = [
        "## Compiled Cross-Product Memory",
        "Only current, source-traceable claims are shown. Raw transcripts and "
        "assistant-only candidates are excluded.",
    ]
    for claim in claims:
        refs = ", ".join(item["source_ref"] for item in claim["sources"][:3])
        line = (
            f"- [{claim['kind']}] {claim['content']} "
            f"(claim `{claim['id']}`; source `{refs}`)"
        )
        if len("\n".join([*lines, line])) > max(200, int(max_chars)):
            break
        lines.append(line)
    return "\n".join(lines) if len(lines) > 2 else ""


def context_records(matter_id: str) -> dict[str, Any]:
    """Return prompt-safe claim records for one exact Matter boundary."""
    claims = _query_claims(
        statuses=["active"], matter_id=str(matter_id), query="",
        limit=MAX_CONTEXT_CLAIMS,
    )
    return {
        "claims": [
            {
                "id": item["id"],
                "kind": item["kind"],
                "content": item["content"],
                "authority": item["authority"],
                "source_refs": [
                    source["source_ref"] for source in item["sources"][:3]
                ],
            }
            for item in claims
        ],
        "conflicts": [
            {
                "id": item["id"],
                "claim_key": item["claim_key"],
                "prior_claim_id": item["prior_claim_id"],
                "incoming_claim_id": item["incoming_claim_id"],
            }
            for item in open_conflicts(matter_id=str(matter_id))
        ],
    }


def open_conflicts(*, matter_id: str | None = None, limit: int = 20) -> list[dict]:
    where = ["c.status='open'"]
    params: list[Any] = []
    if matter_id is not None:
        where.append("c.matter_scope=?")
        params.append(matter_id)
    params.append(max(1, min(int(limit), 100)))
    rows = _db().execute(
        """SELECT c.*,p.content AS prior_content,i.content AS incoming_content
             FROM memory_conflicts c
             JOIN memory_claims p ON p.id=c.prior_claim_id
             JOIN memory_claims i ON i.id=c.incoming_claim_id
            WHERE """ + " AND ".join(where)
        + " ORDER BY c.created_epoch DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def resolve_claim(
    claim_id: str, *, action: str, reviewer: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Apply an explicit human review; models cannot call this implicitly."""
    action = str(action or "").strip().lower()
    reviewer = _flat(reviewer, limit=120)
    if action not in {"confirm", "choose", "reject"}:
        raise MemoryCompilerError("action must be confirm, choose, or reject")
    if not reviewer:
        raise MemoryCompilerError("reviewer is required")
    db = _db()
    row = db.execute("SELECT * FROM memory_claims WHERE id=?", (claim_id,)).fetchone()
    if row is None:
        raise KeyError(f"memory claim not found: {claim_id}")
    claim = dict(row)
    epoch = _now(now)
    scope_sql, scope_params = _scope_clause(str(claim.get("matter_id") or ""))
    try:
        db.execute("BEGIN IMMEDIATE")
        if action == "reject":
            counterpart_rows = db.execute(
                """SELECT CASE WHEN prior_claim_id=? THEN incoming_claim_id
                                ELSE prior_claim_id END AS other_id
                     FROM memory_conflicts
                    WHERE status='open'
                      AND (prior_claim_id=? OR incoming_claim_id=?)""",
                (claim_id, claim_id, claim_id),
            ).fetchall()
            db.execute(
                """UPDATE memory_claims SET status='rejected',confirmed_by=?,
                   updated_epoch=? WHERE id=?""",
                (reviewer, epoch, claim_id),
            )
            db.execute(
                """UPDATE memory_conflicts SET status='resolved',
                   resolution='claim_rejected',resolved_by=?,resolved_epoch=?
                   WHERE status='open' AND (prior_claim_id=? OR incoming_claim_id=?)""",
                (reviewer, epoch, claim_id, claim_id),
            )
            for counterpart in counterpart_rows:
                other_id = str(counterpart["other_id"])
                still_open = db.execute(
                    """SELECT 1 FROM memory_conflicts WHERE status='open'
                       AND (prior_claim_id=? OR incoming_claim_id=?) LIMIT 1""",
                    (other_id, other_id),
                ).fetchone()
                if still_open is None:
                    db.execute(
                        """UPDATE memory_claims SET status='active',updated_epoch=?
                           WHERE id=? AND status='conflicted'""",
                        (epoch, other_id),
                    )
        elif action == "choose":
            others = db.execute(
                f"""SELECT id FROM memory_claims WHERE {scope_sql}
                     AND kind=? AND claim_key=? AND id<>?
                     AND status IN ('active','conflicted')""",
                (*scope_params, claim["kind"], claim["claim_key"], claim_id),
            ).fetchall()
            db.executemany(
                """UPDATE memory_claims SET status='superseded',
                   superseded_by=?,updated_epoch=? WHERE id=?""",
                ((claim_id, epoch, item["id"]) for item in others),
            )
            db.execute(
                """UPDATE memory_claims SET status='active',
                   authority='human_confirmed',confirmed_by=?,updated_epoch=?
                   WHERE id=?""",
                (reviewer, epoch, claim_id),
            )
            db.execute(
                """UPDATE memory_conflicts SET status='resolved',
                   resolution='claim_chosen',resolved_by=?,resolved_epoch=?
                   WHERE status='open' AND claim_key=? AND matter_scope=?""",
                (reviewer, epoch, claim["claim_key"], str(claim.get("matter_id") or "")),
            )
        else:
            others = db.execute(
                f"""SELECT id FROM memory_claims WHERE {scope_sql}
                     AND kind=? AND claim_key=? AND id<>?
                     AND status IN ('active','conflicted')""",
                (*scope_params, claim["kind"], claim["claim_key"], claim_id),
            ).fetchall()
            if others:
                db.execute(
                    """UPDATE memory_claims SET status='conflicted',
                       authority='human_confirmed',confirmed_by=?,updated_epoch=?
                       WHERE id=?""",
                    (reviewer, epoch, claim_id),
                )
                for other in others:
                    db.execute(
                        """UPDATE memory_claims SET status='conflicted',
                           updated_epoch=? WHERE id=?""",
                        (epoch, other["id"]),
                    )
                    db.execute(
                        """INSERT OR IGNORE INTO memory_conflicts
                           (id,matter_scope,claim_key,prior_claim_id,
                            incoming_claim_id,status,created_epoch)
                           VALUES (?,?,?,?,?,'open',?)""",
                        (
                            f"mcf_{uuid.uuid4().hex[:20]}",
                            str(claim.get("matter_id") or ""), claim["claim_key"],
                            other["id"], claim_id, epoch,
                        ),
                    )
            else:
                db.execute(
                    """UPDATE memory_claims SET status='active',
                       authority='human_confirmed',confirmed_by=?,updated_epoch=?
                       WHERE id=? AND status='candidate'""",
                    (reviewer, epoch, claim_id),
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    updated = db.execute("SELECT * FROM memory_claims WHERE id=?", (claim_id,)).fetchone()
    return {
        "schema": "jarvis.memory-review.v1",
        "action": action,
        "claim": _claim_row(updated),
        "open_conflicts": open_conflicts(
            matter_id=str(claim.get("matter_id") or "")
        ),
    }


def compiler_status() -> dict[str, Any]:
    db = _db()
    sources = {
        str(row[0]): int(row[1]) for row in db.execute(
            "SELECT status,COUNT(*) FROM memory_compile_sources GROUP BY status"
        )
    }
    claims = {
        str(row[0]): int(row[1]) for row in db.execute(
            "SELECT status,COUNT(*) FROM memory_claims GROUP BY status"
        )
    }
    audit = audit_compiled_memory()
    return {
        "schema": "jarvis.memory-compiler-health.v1",
        "healthy": not audit["findings"],
        "sources": sources,
        "claims": claims,
        "pending_batches": int(db.execute(
            "SELECT COUNT(*) FROM memory_compile_batches WHERE status='pending'"
        ).fetchone()[0]),
        "open_conflicts": int(db.execute(
            "SELECT COUNT(*) FROM memory_conflicts WHERE status='open'"
        ).fetchone()[0]),
        "audit": audit,
    }


def audit_compiled_memory() -> dict[str, Any]:
    """Check invariants that prose-based self-assessment cannot satisfy."""
    db = _db()
    findings: list[dict[str, Any]] = []
    active = int(db.execute(
        "SELECT COUNT(*) FROM memory_claims WHERE status='active'"
    ).fetchone()[0])
    traced = int(db.execute(
        """SELECT COUNT(DISTINCT c.id) FROM memory_claims c
             JOIN memory_claim_sources s ON s.claim_id=c.id
            WHERE c.status='active'"""
    ).fetchone()[0])
    assistant_active = int(db.execute(
        """SELECT COUNT(*) FROM memory_claims
            WHERE status='active' AND authority='assistant_candidate'"""
    ).fetchone()[0])
    if assistant_active:
        findings.append({
            "code": "assistant_candidate_active",
            "count": assistant_active,
        })
    untraced = max(0, active - traced)
    if untraced:
        findings.append({"code": "active_claim_without_source", "count": untraced})
    duplicate_rows = db.execute(
        """SELECT COALESCE(matter_id,''),kind,claim_key,COUNT(*) AS n
             FROM memory_claims WHERE status='active'
            GROUP BY COALESCE(matter_id,''),kind,claim_key HAVING COUNT(*)>1"""
    ).fetchall()
    if duplicate_rows:
        findings.append({
            "code": "multiple_active_values",
            "count": len(duplicate_rows),
        })
    retained_payloads = int(db.execute(
        """SELECT COUNT(*) FROM memory_compile_batches
            WHERE status='applied' AND payload NOT IN ('','{}')"""
    ).fetchone()[0])
    if retained_payloads:
        findings.append({
            "code": "applied_batch_retains_transcript",
            "count": retained_payloads,
        })
    return {
        "active_claims": active,
        "traceable_active_claims": traced,
        "traceability_rate": round(traced / active, 4) if active else 1.0,
        "assistant_candidate_active": assistant_active,
        "multiple_active_values": len(duplicate_rows),
        "applied_payloads_retained": retained_payloads,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.memory_compiler")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--input", default="-")
    search = sub.add_parser("search")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--matter-id")
    search.add_argument("--include-candidates", action="store_true")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("claim_id")
    resolve.add_argument("action", choices=("confirm", "choose", "reject"))
    resolve.add_argument("--reviewer", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result: Any = prepare_batch(batch_size=args.batch_size)
        if result:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "apply":
        import sys
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text()
        result = apply_compile_result(raw)
    elif args.command == "search":
        result = search_compiled_memory(
            args.query, matter_id=args.matter_id,
            include_candidates=args.include_candidates,
        )
    elif args.command == "resolve":
        result = resolve_claim(
            args.claim_id, action=args.action, reviewer=args.reviewer,
        )
    else:
        result = compiler_status()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
