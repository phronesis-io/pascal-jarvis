#!/usr/bin/env python3
"""Post-hook: write engagement insights to memory/system/engagement_insights.md.

Stdin: Claude's JSON analysis of engagement data.
Stdout: empty (this is a silent background task, no user message).
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
INSIGHTS_FILE = MEMORY_DIR / "system" / "engagement_insights.md"


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or raw == "HEARTBEAT_OK":
        return 0
    if looks_like_error(raw):
        print("[engagement-analyze] skipping — looks like error", file=sys.stderr)
        return 0

    # Parse Claude's response — expect JSON with insights and adaptations
    data = parse_json_response(raw)
    if data is None:
        print("[engagement-analyze] failed to parse JSON response", file=sys.stderr)
        return 0

    insights = data.get("insights", "")
    adaptations = data.get("adaptations", [])

    if not insights:
        return 0

    # Build the insights markdown file
    lines = [
        f"# Engagement Insights",
        f"",
        f"_Last updated: {now_local_str('%Y-%m-%d %H:%M')}_",
        f"",
        insights,
    ]

    if adaptations:
        lines.append("")
        lines.append("## Suggested Adaptations")
        lines.append("")
        for a in adaptations:
            target = a.get("target", "unknown")
            suggestion = a.get("suggestion", "")
            lines.append(f"- **{target}**: {suggestion}")

    content = "\n".join(lines) + "\n"

    # Write atomically
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)
    tmp = INSIGHTS_FILE.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, INSIGHTS_FILE)

    print(f"[engagement-analyze] wrote insights ({len(content)} chars)", file=sys.stderr)

    # Apply adaptations to heartbeat_state.json effective_interval
    if adaptations:
        _apply_adaptations(adaptations)

    # No user-facing output — this is a silent background task
    return 0


# ── Frequency interval mapping ──
_INTERVAL_MAP = {
    "reduce": 2.0,      # double the interval (less frequent)
    "increase": 0.5,    # halve the interval (more frequent)
    "maintain": 1.0,    # keep current
}


def _apply_adaptations(adaptations: list[dict]):
    """Apply frequency adaptations to heartbeat_state.json.

    Each adaptation has: {"target": "task-name", "suggestion": "reduce frequency..."}
    We parse the suggestion for keywords and adjust effective_interval.
    """
    jarvis_dir = Path(os.environ.get("JARVIS_DIR", "."))
    state_file = jarvis_dir / "heartbeat_state.json"
    if not state_file.exists():
        return

    try:
        state = json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return

    # Load task intervals from HEARTBEAT.md for reference
    sys.path.insert(0, str(jarvis_dir))
    from core.heartbeat import parse_heartbeat
    tasks_def = {t["name"]: t["interval"] for t in parse_heartbeat(jarvis_dir / "HEARTBEAT.md")}

    changed = []
    for a in adaptations:
        target = a.get("target", "")
        suggestion = a.get("suggestion", "").lower()
        if target not in tasks_def:
            continue

        base_interval = tasks_def[target]

        # Determine direction from suggestion text
        multiplier = 1.0
        if any(w in suggestion for w in ["reduce", "decrease", "less", "lower", "降低", "减少"]):
            multiplier = 2.0
        elif any(w in suggestion for w in ["increase", "more", "higher", "提高", "增加"]):
            multiplier = 0.5

        if multiplier == 1.0:
            continue

        new_interval = int(base_interval * multiplier)
        # Clamp: don't go below 5min or above 48h
        new_interval = max(300, min(172800, new_interval))

        if target not in state:
            state[target] = {"last_run": 0}
        state[target]["effective_interval"] = new_interval
        changed.append(f"{target}: {base_interval}s → {new_interval}s")

    if changed:
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, state_file)
        print(f"[engagement-analyze] Applied frequency changes: {changed}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
