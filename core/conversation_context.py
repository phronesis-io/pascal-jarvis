"""Logical conversation context shared by Lark, Claude, and Codex.

Matter is the durable user-facing context. Provider sessions are disposable
execution windows and must never be used as the product-level session ID.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core.jsonl import append_jsonl_locked, rewrite_jsonl_locked


def _db():
    from core.db import get_db
    return get_db()


def logical_context_key(conv_key: str, matter_id: str = "") -> str:
    matter = str(matter_id or "").strip()
    if matter:
        return f"matter:{matter}"
    key = str(conv_key or "").strip()
    if not key:
        raise ValueError("conv_key is required")
    return f"conversation:{key}"


_GENERATION_RE = re.compile(r"@g([0-9]+)$")


def base_context_key(context_key: str) -> str:
    """Return the durable logical identity without its reset generation."""
    return _GENERATION_RE.sub("", str(context_key or "").strip())


def context_generation_from_key(context_key: str) -> int:
    match = _GENERATION_RE.search(str(context_key or "").strip())
    return int(match.group(1)) if match else 0


def versioned_context_key(context_key: str, generation: int) -> str:
    base = base_context_key(context_key)
    if not base:
        raise ValueError("context_key is required")
    value = max(0, int(generation))
    return base if value == 0 else f"{base}@g{value}"


def current_context_generation(context_key: str) -> int:
    base = base_context_key(context_key)
    if not base:
        raise ValueError("context_key is required")
    row = _db().execute(
        "SELECT generation FROM logical_context_states WHERE context_key = ?",
        (base,),
    ).fetchone()
    return max(0, int(row["generation"])) if row else 0


def current_context_key(context_key: str) -> str:
    base = base_context_key(context_key)
    return versioned_context_key(base, current_context_generation(base))


def matter_id_from_context_key(context_key: str) -> str:
    value = base_context_key(context_key)
    if not value.startswith("matter:"):
        return ""
    return value.split(":", 1)[1].strip()


def compact_key_from_context_key(context_key: str) -> str:
    value = str(context_key or "").strip()
    if value.startswith("conversation:"):
        return value.split(":", 1)[1]
    return value


def context_snapshot(conv_key: str, matter_id: str | None = None,
                     *, allow_binding: bool = True) -> dict:
    """Capture the logical context used for one provider dispatch.

    Supplying ``matter_id`` is important for delayed receipts: it preserves the
    context selected when the turn started even if the live binding changes.
    """
    key = str(conv_key or "").strip()
    if not key:
        raise ValueError("conv_key is required")
    if matter_id is None and allow_binding:
        row = _db().execute(
            "SELECT matter_id FROM matter_bindings WHERE conv_key = ?", (key,)
        ).fetchone()
        selected = str(row["matter_id"] or "") if row else ""
    elif matter_id is not None:
        selected = str(matter_id or "").strip()
    else:
        selected = ""
    logical_key = logical_context_key(key, selected)
    generation = current_context_generation(logical_key)
    context_key = versioned_context_key(logical_key, generation)
    # Preserve pre-session-lifecycle unbound compact filenames. Bound Matters
    # get their own path and therefore can never inherit the conversation-wide
    # compact that predates this feature.
    compact_key = context_key if selected else compact_key_from_context_key(context_key)
    return {
        "conv_key": key,
        "matter_id": selected,
        "logical_context_key": logical_key,
        "context_generation": generation,
        "context_key": context_key,
        "compact_key": compact_key,
    }


def _like_prefix(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clear_derived_context(context_key: str, jarvis_dir: str | Path) -> dict:
    """Advance one reset generation and clear rebuildable projections.

    The generation bump and database cleanup are one transaction.  Old raw
    provider transcripts and Matter events remain as audit history, while a
    late writer carrying the old scope cannot become current again.
    """
    base = base_context_key(context_key)
    if not base:
        raise ValueError("context_key is required")
    db = _db()
    pattern = _like_prefix(base) + "@g%"
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT generation FROM logical_context_states WHERE context_key = ?",
            (base,),
        ).fetchone()
        old_generation = max(0, int(row["generation"])) if row else 0
        old_scope = versioned_context_key(base, old_generation)
        new_generation = old_generation + 1
        turns = db.execute(
            """DELETE FROM conversation_turns
                 WHERE context_key = ? OR context_key LIKE ? ESCAPE '\\'""",
            (base, pattern),
        ).rowcount
        codex = db.execute(
            """DELETE FROM codex_conversation_sessions
                 WHERE conv_key = ? OR conv_key LIKE ? ESCAPE '\\'""",
            (base, pattern),
        ).rowcount
        db.execute(
            """INSERT INTO logical_context_states(context_key, generation, reset_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(context_key) DO UPDATE SET
                 generation=excluded.generation, reset_at=excluded.reset_at""",
            (base, new_generation),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    compact_key = compact_key_from_context_key(old_scope)
    from core.compact import get_compact_path
    compact = get_compact_path(jarvis_dir, compact_key)
    removed_compact = int(compact.exists())
    try:
        compact.unlink(missing_ok=True)
    except OSError:
        # The new generation has a distinct compact path, so failed cleanup of
        # the old derived file cannot reintroduce it into the active prompt.
        removed_compact = 0
    return {
        "turns": max(0, int(turns)),
        "codex_sessions": max(0, int(codex)),
        "compacts": removed_compact,
        "generation": new_generation,
        "context_key": versioned_context_key(base, new_generation),
    }


def queue_pending_context(
    path: str | Path,
    *,
    conv_key: str,
    context_key: str,
    job_id: str,
    timestamp: str,
    summary: str,
) -> None:
    """Append one context-scoped deferred result under the shared file lock."""
    key = str(conv_key or "").strip()
    logical = str(context_key or "").strip() or logical_context_key(key)
    append_jsonl_locked(path, {
        "conv_key": key,
        "context_key": logical,
        "job_id": str(job_id or ""),
        "ts": str(timestamp or ""),
        "summary": str(summary or "")[:4000],
    })


def claim_pending_context(
    path: str | Path, *, conv_key: str, context_key: str,
) -> list[dict]:
    """Atomically claim rows for exactly one logical context.

    Legacy rows had no context key. They belong only to the unbound transport
    conversation and therefore cannot leak into a named Matter.
    """
    transport = str(conv_key or "").strip()
    logical = str(context_key or "").strip() or logical_context_key(transport)
    claimed: list[dict] = []

    def transform(rows: list[dict]) -> list[dict]:
        keep = []
        for row in rows:
            row_transport = str(row.get("conv_key") or "")
            row_context = str(row.get("context_key") or "")
            if not row_context:
                row_context = logical_context_key(row_transport)
            if row_transport == transport and row_context == logical:
                claimed.append(row)
            elif (row_transport == transport
                  and base_context_key(row_context) == base_context_key(logical)):
                # A reset made this deferred result stale.  Its completion card
                # and job output remain available, but it must neither enter the
                # new generation nor accumulate forever in this one-shot queue.
                continue
            else:
                keep.append(row)
        return keep

    rewrite_jsonl_locked(path, transform)
    return claimed


def apply_runtime_transition(
    *,
    conv_key: str,
    context_key: str,
    tracker_path: str | Path,
    session_dir: str | Path,
    jarvis_dir: str | Path,
    reset: bool = False,
) -> dict:
    """Rotate provider state after a committed logical-session transition."""
    key = str(conv_key or "").strip()
    target = base_context_key(context_key)
    if not key or not target:
        raise ValueError("conv_key and context_key are required")
    snapshot = context_snapshot(key)
    if base_context_key(snapshot["context_key"]) != target:
        raise RuntimeError(
            "logical context changed before provider rotation could commit"
        )
    cleared = clear_derived_context(target, jarvis_dir) if reset else {}
    runtime_target = str(cleared.get("context_key") or snapshot["context_key"])
    from core.session import SessionManager
    manager = SessionManager(tracker_path, session_dir)
    session_id = ""
    deferred_rotation = False
    try:
        session_id = manager.force_rotate(
            key, context_key=runtime_target, preserve_previous=not reset,
            reason="reset" if reset else "logical_transition",
        )
    except OSError:
        # The binding/generation is already durable.  get_session() compares the
        # target scope and will rotate before the next provider dispatch.
        deferred_rotation = True
    db = _db()
    try:
        db.execute("DELETE FROM conversation_runtime WHERE conv_key = ?", (key,))
        db.commit()
    except Exception:
        db.rollback()
    return {
        "session_id": session_id,
        "context_key": runtime_target,
        "reset": bool(reset),
        "cleared": cleared,
        "deferred_rotation": deferred_rotation,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.conversation_context")
    sub = parser.add_subparsers(dest="cmd", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--conv-key", required=True)
    snapshot.add_argument("--ignore-binding", action="store_true")
    apply = sub.add_parser("apply")
    apply.add_argument("--conv-key", required=True)
    apply.add_argument("--context-key", required=True)
    apply.add_argument("--tracker", required=True)
    apply.add_argument("--session-dir", required=True)
    apply.add_argument("--jarvis-dir", required=True)
    apply.add_argument("--reset", action="store_true")
    claim = sub.add_parser("claim-pending")
    claim.add_argument("--path", required=True)
    claim.add_argument("--conv-key", required=True)
    claim.add_argument("--context-key", required=True)
    queue = sub.add_parser("queue-pending")
    queue.add_argument("--path", required=True)
    queue.add_argument("--conv-key", required=True)
    queue.add_argument("--context-key", required=True)
    queue.add_argument("--job-id", required=True)
    queue.add_argument("--timestamp", default="")
    queue.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        result = context_snapshot(
            args.conv_key, allow_binding=not args.ignore_binding)
    elif args.cmd == "apply":
        result = apply_runtime_transition(
            conv_key=args.conv_key,
            context_key=args.context_key,
            tracker_path=args.tracker,
            session_dir=args.session_dir,
            jarvis_dir=args.jarvis_dir,
            reset=args.reset,
        )
    elif args.cmd == "claim-pending":
        result = claim_pending_context(
            args.path, conv_key=args.conv_key, context_key=args.context_key)
    else:
        queue_pending_context(
            args.path, conv_key=args.conv_key, context_key=args.context_key,
            job_id=args.job_id, timestamp=args.timestamp, summary=args.summary,
        )
        result = {"queued": True}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
