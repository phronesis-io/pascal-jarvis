#!/usr/bin/env python3
"""Post-hook: submit feedback to EigenFlux via CLI, output user message as Lark card."""
import json
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core.card import build_card
from core.safety import parse_json_response
import _ef_delivery as efd


def _source_url(data: dict, msg: str) -> str:
    """The public link to put behind '阅读原文'.

    NEVER a richview/localhost URL — Pascal reads Feishu on his phone, where
    127.0.0.1 is unreachable. Prefer a structured source_url; otherwise pull the
    first real link out of the message (markdown target, then bare URL).
    """
    src = str(data.get("source_url") or data.get("url") or "").strip()
    if src.startswith("http") and "127.0.0.1" not in src and "localhost" not in src:
        return src
    m = re.search(r'\]\((https?://[^\s)]+)\)', msg)
    if m:
        return m.group(1)
    m = re.search(r'https?://[^\s<>()\[\]]+', msg)
    return m.group(0).rstrip('.,;:!?，。、）)') if m else ""


def _distinct_links(msg: str) -> list[str]:
    """All distinct http(s) links in the body (markdown targets + bare URLs).

    Used to decide whether a single bottom "阅读原文" button makes sense. A
    multi-item digest (the FYI/知会 tier) carries one inline link per item, so a
    single footer button would point to only one of them and mislead.
    """
    found = re.findall(r'\]\((https?://[^\s)]+)\)', msg)
    found += re.findall(r'(?<![(\[])\bhttps?://[^\s<>()\[\]]+', msg)
    seen: list[str] = []
    for u in found:
        u = u.rstrip('.,;:!?，。、）)')
        if u not in seen:
            seen.append(u)
    return seen

LOG = open(os.environ.get("LOG_FILE", os.devnull), "a")
PATH = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")


def run_eigenflux(*args: str, stdin_data: str | None = None) -> dict:
    result = subprocess.run(
        ["eigenflux", *args, "-f", "json"],
        capture_output=True, text=True,
        env={**os.environ, "PATH": PATH},
        input=stdin_data,
    )
    if result.returncode != 0:
        print(f"[eigenflux-feed] CLI error: {result.stderr.strip()}", file=LOG)
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _emit_legacy_backlog_cards() -> None:
    """Drain pre-migration backlog entries as separate intact cards.

    New items never enter this file; heartbeat_loop owns quiet-hour deferral.
    This one-way compatibility drain prevents old held messages from being
    stranded while refusing to recreate the truncated morning digest.
    """
    p = efd.backlog_path()
    if not p.exists() or p.stat().st_size == 0:
        return
    held = efd.drain()
    print(f"[eigenflux-feed] migrated {len(held)} legacy held card(s) into "
          "the intact memorial carrier", file=LOG)
    for entry in held:
        message = str(entry.get("message", "")).strip()
        if message:
            print(build_card(header="📡 EigenFlux", body=message,
                             source=str(entry.get("source") or "eigenflux-feed")))


def main() -> int:
    _emit_legacy_backlog_cards()

    raw = sys.stdin.read().strip()
    if not raw:
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[eigenflux-feed] JSON parse failed", file=LOG)
        return 0

    # Submit feedback scores via CLI
    fb = data.get("feedback", [])
    if fb:
        items = []
        for i in fb:
            try:
                # item_id MUST be a string — the API rejects a numeric item_id with
                # HTTP 400 "bind body failed, Mismatch type string with value number",
                # which silently black-holed every feedback submission.
                iid = str(i["item_id"]).strip()
                if not iid:
                    raise ValueError("empty item_id")
                # Valid scores are -1..2; the API SILENTLY SKIPS anything else
                # (processed_count stays 0), so an LLM that emits e.g. 3 would
                # lose that item's feedback. Clamp to keep the intended signal.
                score = max(-1, min(2, int(i["score"])))
                items.append({"item_id": iid, "score": score})
            except (ValueError, KeyError, TypeError) as e:
                print(f"[eigenflux-feed] bad feedback entry {i!r}: {e}", file=LOG)
        if items:
            try:
                resp = run_eigenflux("feed", "feedback", "--items", json.dumps(items))
                # Be honest about success: only the API's processed_count proves the
                # scores landed. Logging "N scored" unconditionally masked the 400.
                processed = resp.get("processed_count")
                if processed is not None:
                    print(f"[eigenflux-feed] {processed} items scored "
                          f"(skipped {resp.get('skipped_count', 0)})", file=LOG)
                else:
                    print(f"[eigenflux-feed] feedback REJECTED by API, resp={resp!r}", file=LOG)
            except Exception:
                print("[eigenflux-feed] feedback submission failed:", file=LOG)
                traceback.print_exc(file=LOG)

    # Queue items flagged for deep research
    research_queue = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent)) / "eigenflux" / "needs_research.jsonl"
    research_queue.parent.mkdir(parents=True, exist_ok=True)
    queued = 0
    for item in fb:
        if item.get("needs_research") and item.get("action") == "hold" and int(item.get("score", 0)) >= 1:
            from datetime import datetime, timezone
            entry = {
                "item_id": str(item["item_id"]),
                "queued_at": datetime.now(timezone.utc).isoformat(),
                "score": int(item["score"]),
                "reason": item.get("reason", ""),
            }
            with open(research_queue, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            queued += 1
    if queued:
        print(f"[eigenflux-feed] {queued} items queued for research", file=LOG)

    # Output user message as a Lark card. Render the FULL message inline (no
    # truncation) and link "阅读原文" to the public source — not a localhost
    # richview page, which is dead on Pascal's phone. build_card auto-linkifies
    # any bare URL in the body so it's tappable on mobile too.
    surface_items = []
    for item in data.get("user_messages", []) or []:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or item.get("message") or "").strip()
        if body:
            surface_items.append({**item, "body": body})
    legacy_msg = str(data.get("user_message", "")).strip()
    if not surface_items and legacy_msg:
        surface_items.append({"body": legacy_msg,
                              "source_url": data.get("source_url") or data.get("url", "")})
    if surface_items:
        # heartbeat_loop owns quiet-hour deferral so the exact card (including
        # links, 批红 and Chat) survives. This hook only marks rare urgent items
        # that should bypass that central gate.
        urgent = bool(data.get("urgent", False)) or any(
            bool(item.get("urgent", False)) for item in surface_items)
        if urgent:
            # Tell the downstream heartbeat send layer to bypass ITS batch
            # queue too: it only sees the task-name sidecar, and
            # eigenflux-feed-triage is a batchable source by default — without
            # this an urgent 2am item clears this gate only to be re-held in
            # night_queue until the 10:00 digest.
            try:
                (Path(os.environ.get("JARVIS_DIR", ".")) / ".urgent_send").touch()
            except OSError:
                pass
        for item in surface_items:
            msg = item["body"]
            src = "" if len(_distinct_links(msg)) >= 2 else _source_url(item, msg)
            buttons = [{"text": "阅读原文", "url": src}] if src else None
            print(build_card(
                header="📡 EigenFlux",
                body=msg,
                buttons=buttons,
                source="eigenflux-feed",
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
