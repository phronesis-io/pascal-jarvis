#!/usr/bin/env python3
"""Post-hook: deterministic replacement for the perception-collect model call.

The old prompt asked the model one yes/no question every 15 minutes — "does
DATA show errors>0 with the same source failing repeatedly?" — and 98% of
runs answered a bare HEARTBEAT_OK, each at the price of a solo full-memory
call (~43% of all heartbeat LLM traffic). This script answers the same
question from evidence the model never even had: core.perception keeps a
consecutive-failure counter per source in perception_state.json (read here
READ-ONLY; core.perception stays its sole writer).

Contract (Tier 0: stdin = the pre-script's one-line summary):
- "errors=" absent or 0 → no output, ever. One-off blips stay silent too.
- errors>0 AND the source is named in THIS pass's notes AND its error_count
  streak has reached REPEAT_STREAK → ONE plain-Chinese notice card for that
  source (纯周知: TITLE + WORKED receipt + short body + 「知道就行」, no
  OPTIONS), then 24h of silence for that source. The notes gate keeps the
  old prompt's "notes mention it" clause: a frozen streak left behind by a
  since-disabled/removed source (run_collect never prunes state) must not
  ride an unrelated source's error into a false card.
Own bookkeeping (first-seen-failing time, last-alert time) lives in
data/perception_alert_state.json — data/ per the privacy rules.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import is_idle_reply
from core.timeutil import now_local
from core.textutil import ellipsize, task_display_name

# "Failing repeatedly" = this many consecutive failed passes. Deliberately
# below core.perception's STUCK_ERROR_STREAK (10, the dead-channel line the
# silent self-diagnostic watches): this card is the early single heads-up,
# not the paging machinery.
REPEAT_STREAK = 3
# One card per source per 24h, healed-or-not — a still-broken source must
# not buzz Pascal every 15 minutes.
REALERT_INTERVAL_S = 24 * 3600

_ERRORS_RE = re.compile(r"\berrors=(\d+)\b")
_NOTES_RE = re.compile(r"\bnotes: (.+)$", re.S)


def _sources_named_in_notes(summary: str) -> set[str]:
    """Source ids named in THIS pass's notes ("notes: sid: reason; sid2: …").

    run_collect writes a "sid: <reason>" note (first 5, "; "-joined) for four
    failure classes: adapter-missing, config-invalid, collect-crash, and
    adapter-reported error_type. Only the last two also advance error_count,
    so every source the streak gate can fire on is one this filter can name
    while it is genuinely failing; setup errors (missing adapter/bad config)
    never build a streak and stay a --dry-run / --diag concern.
    """
    match = _NOTES_RE.search(summary)
    if not match:
        return set()
    return {chunk.split(":", 1)[0].strip()
            for chunk in match.group(1).split(";") if ":" in chunk}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _since_phrase(epoch: float, now: datetime) -> str:
    """「（从 14:05 起）」 same-day /「（从 8/23 14:05 起）」 — '' when unknown."""
    try:
        dt = datetime.fromtimestamp(epoch, tz=now.tzinfo)
    except (OverflowError, OSError, ValueError):
        return ""
    when = (dt.strftime("%H:%M") if dt.date() == now.date()
            else f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}")
    return f"（从 {when} 起）"


def _card(sid: str, count: int, since: str) -> str:
    # 纯周知 notice: conclusion first, no jargon, no OPTIONS line. The WORKED
    # line is the delivery layer's admission ticket — memorialize_output(...,
    # require_work_receipt=True) drops receipt-less prose before create() —
    # and states only what this script verifiably did.
    display = task_display_name(sid)
    short_display = ellipsize(display, 20)
    return (f"TITLE: 感知源「{short_display}」连续{count}次没抓到数据\n"
            f"WORKED: 核对了「{display}」的连续失败记录，确认 24 小时内没提醒过\n"
            f"感知源「{display}」连续 {count} 次没抓到数据{since}，我在自动重试。\n"
            f"修好之前，这个源盯着的动静会漏。知道就行。")


def run(summary: str, *, jarvis_dir: Path, now: datetime) -> str:
    """Return card text ('' = stay silent) and persist the alert clock."""
    summary = (summary or "").strip()
    if is_idle_reply(summary):
        return ""
    m = _ERRORS_RE.search(summary)
    errors = int(m.group(1)) if m else 0

    perception_state = _load_json(jarvis_dir / "perception_state.json")
    alert_file = jarvis_dir / "data" / "perception_alert_state.json"
    alert_state = _load_json(alert_file)
    failing_since = dict(alert_state.get("failing_since") or {})
    last_alert = dict(alert_state.get("last_alert") or {})
    now_epoch = now.timestamp()

    # Track when each streak was first observed — perception_state keeps the
    # count but not its start. A healed source (error_count back to 0) drops
    # out; its 24h alert clock deliberately stays.
    streaks: dict[str, int] = {}
    for sid, src_state in perception_state.items():
        if not isinstance(src_state, dict):
            continue
        count = src_state.get("error_count")
        count = count if isinstance(count, int) else 0
        if count > 0:
            streaks[sid] = count
            failing_since.setdefault(sid, now_epoch)
        else:
            failing_since.pop(sid, None)
    for bookkeeping in (failing_since, last_alert):
        for sid in list(bookkeeping):
            if sid not in perception_state:
                bookkeeping.pop(sid)

    named = _sources_named_in_notes(summary)
    cards = []
    if errors > 0:
        for sid in sorted(streaks):
            count = streaks[sid]
            # Named-this-pass gate: a stale streak (source since disabled or
            # dead-but-not-due) must not be carded on another source's error.
            if count < REPEAT_STREAK or sid not in named:
                continue
            if now_epoch - (last_alert.get(sid) or 0) < REALERT_INTERVAL_S:
                continue
            since = _since_phrase(failing_since.get(sid, now_epoch), now)
            cards.append(_card(sid, count, since))
            last_alert[sid] = now_epoch

    _save_json(alert_file, {"failing_since": failing_since,
                            "last_alert": last_alert})
    # "---" is the multi-matter separator the memorial layer splits on; in
    # practice one pass rarely alerts on more than one source.
    return "\n---\n".join(cards)


def main() -> int:
    jarvis_dir = Path(os.environ.get("JARVIS_DIR", "."))
    output = run(sys.stdin.read(), jarvis_dir=jarvis_dir, now=now_local())
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
