#!/usr/bin/env python3
"""Post-hook: apply the mail-triage decision.

Reads Claude's JSON reply (per-email decisions + an optional surfaced message),
records EVERY email shown this cycle as triaged (so it's never re-read), and
emits one Lark card for the surface-worthy ones. Night-held into a backlog that
drains on the first morning cycle, same gate EigenFlux uses.

Input (stdin): Claude's reply, e.g.
  {"triage":[{"event_id":"...","decision":"push|silent","reason":"..."}],
   "user_message":"<markdown or empty>","urgent":false}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.card import build_card  # noqa: E402
from core.jsonl import read_jsonl, write_jsonl  # noqa: E402
from core.safety import parse_json_response  # noqa: E402
from core.timeutil import now_local_str  # noqa: E402
import _ef_delivery as efd  # noqa: E402
from mail_triage_lib import pending_path, triaged_path, _mail_dir  # noqa: E402

TRIAGED_KEEP = 3000


def _mail_backlog() -> Path:
    return _mail_dir() / "mail_backlog.jsonl"


def _hold(message: str) -> None:
    rows = read_jsonl(_mail_backlog())
    rows.append({"ts": now_local_str(), "message": message})
    write_jsonl(_mail_backlog(), rows)


def _drain() -> list[str]:
    p = _mail_backlog()
    rows = read_jsonl(p)
    if p.exists():
        p.unlink()
    return [r.get("message", "") for r in rows if r.get("message")]


def _record_triaged(decisions: dict) -> None:
    """Mark every pending email as triaged (union of the batch + decisions),
    so nothing gets re-read even if Claude omitted some."""
    pp = pending_path()
    pending = []
    if pp.exists():
        try:
            pending = json.loads(pp.read_text())
        except (json.JSONDecodeError, ValueError):
            pending = []
    rows = read_jsonl(triaged_path())
    have = {r.get("event_id") for r in rows}
    ts = now_local_str()
    for item in pending:
        eid = item.get("event_id")
        if not eid or eid in have:
            continue
        rows.append({"event_id": eid, "ts": ts,
                     "decision": decisions.get(eid, "silent"),
                     "subject": item.get("subject", "")})
        have.add(eid)
    if rows:
        write_jsonl(triaged_path(), rows[-TRIAGED_KEEP:])
    if pp.exists():
        pp.unlink()


def main() -> int:
    raw = sys.stdin.read().strip()
    data = parse_json_response(raw)
    if data is None:
        # Parse failed — do NOT record triaged, so the batch is retried next
        # cycle rather than silently swallowed.
        if raw and raw != "HEARTBEAT_OK":
            print("[mail-triage] JSON parse failed", file=sys.stderr)
        return 0

    decisions = {}
    for d in data.get("triage", []) or []:
        eid = str(d.get("event_id", "")).strip()
        if eid:
            decisions[eid] = str(d.get("decision", "silent")).strip()

    # Record dedup state for the whole shown batch.
    _record_triaged(decisions)

    msg = str(data.get("user_message", "")).strip()
    urgent = bool(data.get("urgent", False))

    # Morning flush: prepend anything held overnight onto today's first card.
    held = []
    if not efd.in_quiet_hours():
        held = _drain()

    if efd.in_quiet_hours() and not urgent:
        if msg:
            _hold(msg)
        return 0

    parts = held + ([msg] if msg else [])
    if not parts:
        return 0
    body = "\n\n———\n\n".join(parts)
    print(build_card(header="📬 邮件", body=body, source="mail-triage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
