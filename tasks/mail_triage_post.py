#!/usr/bin/env python3
"""Post-hook: apply the mail-triage decision.

Reads Claude's JSON reply (per-email decisions + an optional surfaced message),
records EVERY email shown this cycle as triaged (so it's never re-read), and
emits one memorial card per surface-worthy email. Quiet-hour deferral belongs
to heartbeat_loop's intact-card queue; this hook never aggregates mail into a
long morning blob.

Input (stdin): Claude's reply, e.g.
  {"triage":[{"event_id":"...","decision":"push|silent","reason":"..."}],
   "user_messages":[{"event_id":"...","title":"...","body":"..."}],
   "urgent":false}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.card import build_card  # noqa: E402
from core.jsonl import read_jsonl, write_jsonl  # noqa: E402
from core.safety import parse_json_response  # noqa: E402
from mail_triage_lib import pending_path, triaged_path  # noqa: E402

TRIAGED_KEEP = 3000


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
    from core.timeutil import now_local_str
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

    # Reply drafts, keyed by the email they answer. One draft per email: a
    # second card for the same message is nagging, not help.
    drafts = {}
    for item in data.get("drafts", []) or []:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("event_id", "")).strip()
        body = str(item.get("body", "")).strip()
        if eid and body:
            drafts[eid] = item

    surface_items = []
    for item in data.get("user_messages", []) or []:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or item.get("message") or "").strip()
        if body:
            surface_items.append({
                "title": str(item.get("title") or "邮件").strip() or "邮件",
                "body": body,
                "event_id": str(item.get("event_id", "")),
                "draft": drafts.get(str(item.get("event_id", ""))),
            })
    # Backward compatibility while the model prompt rolls over.
    if not surface_items and msg:
        surface_items.append({"title": "邮件", "body": msg, "event_id": ""})
    if not surface_items:
        return 0
    # Non-urgent pushed mail is still worth keeping, but it belongs in the web
    # notice stream instead of becoming another Lark decision. Urgent mail is a
    # sparse alert: it uses heartbeat_loop's reliable CARD route and bypasses
    # quiet hours without inflating the pending-decision count.
    try:
        from core import memorial
        if urgent:
            # Bypass heartbeat_loop's own quiet-hours queue too; this item has
            # already passed the mail task's explicit urgent gate.
            try:
                Path(os.environ.get(
                    "JARVIS_DIR", Path(__file__).resolve().parent.parent
                )).joinpath(".urgent_send").touch()
            except OSError:
                pass
        from core import mail_draft
        for item in surface_items:
            body, options, preset, draft_id = item["body"], None, "fyi", ""
            draft = item.get("draft")
            if draft and not mail_draft.has_draft_for(item["event_id"]):
                try:
                    draft_id = mail_draft.save_draft(
                        event_id=item["event_id"],
                        to_name=str(draft.get("to", "")),
                        subject=str(draft.get("subject", "")),
                        body=str(draft.get("body", "")),
                        rationale=str(draft.get("why", "")))
                    body += mail_draft.card_section(draft_id,
                                                    str(draft.get("body", "")))
                    options, preset = mail_draft.options_for(draft_id), None
                except Exception as exc:
                    # A draft that fails to persist must not take the email
                    # card down with it — the mail itself still matters.
                    print(f"[mail-triage] draft save failed: {exc}",
                          file=sys.stderr)
            mem_id, _ = memorial.create(
                source="mail", title=item["title"], body=body,
                preset=preset, options=options, send=False,
                # A draft turns the card into an ask, so it earns a decision
                # lane; a plain surfaced email stays a notice.
                attention=("alert" if urgent
                           else ("decision" if draft_id else "notice")),
                context=(f"mail event_id={item['event_id']}"
                         if item["event_id"] else ""),
            )
            if urgent:
                print(memorial.card_json(mem_id))
    except Exception as e:
        print(f"[mail-triage] memorial failed: {e}",
              file=sys.stderr)
        if urgent:
            for item in surface_items:
                print(build_card(header=f"📬 {item['title']}", body=item["body"],
                                 source="mail-triage"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
