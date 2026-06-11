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
    """Apply frequency adaptations to interval_overrides.json.

    Each adaptation has: {"target": "task-name", "suggestion": "reduce frequency..."}
    We parse the suggestion for keywords and adjust the override interval.

    Overrides live in a sidecar file, NOT heartbeat_state.json: this hook runs
    as a child process in the middle of a heartbeat cycle, so writes to the
    state file were silently clobbered when run_cycle saved its in-memory copy
    at the end of the cycle. HeartbeatRunner.load_interval_overrides() reads
    the sidecar each cycle.
    """
    import time

    jarvis_dir = Path(os.environ.get("JARVIS_DIR", "."))
    overrides_file = jarvis_dir / "interval_overrides.json"
    meta_file = jarvis_dir / "interval_overrides_meta.json"
    # Re-compound cooldown: the analyzer often repeats the same suggestion on
    # largely the same data for days; without a cooldown a few repeats
    # geometrically run any task to the 48h clamp.
    RECOMPOUND_COOLDOWN = 3 * 86400

    try:
        overrides = json.loads(overrides_file.read_text())
    except (json.JSONDecodeError, OSError):
        overrides = {}
    try:
        meta = json.loads(meta_file.read_text())
    except (json.JSONDecodeError, OSError):
        meta = {}

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

        # Determine direction from suggestion text. ("降频" was the exact word
        # the insights used for two months while the old list only knew
        # 降低/减少 — keep both Chinese and English variants broad.)
        multiplier = 1.0
        if any(w in suggestion for w in ["reduce", "decrease", "less", "fewer",
                                         "lower", "降低", "减少", "降频", "减半"]):
            multiplier = 2.0
        elif any(w in suggestion for w in ["increase", "more", "higher",
                                           "提高", "增加", "加频"]):
            multiplier = 0.5

        if multiplier == 1.0:
            continue
        if target in overrides and time.time() - meta.get(target, 0) < RECOMPOUND_COOLDOWN:
            print(f"[engagement-analyze] {target}: adjusted recently, skipping recompound",
                  file=sys.stderr)
            continue

        # Compound from the current override (if any) so repeated suggestions
        # keep adapting, but clamp: don't go below 5min or above 48h
        current = overrides.get(target) or base_interval
        new_interval = max(300, min(172800, int(current * multiplier)))

        if new_interval == current:
            continue
        overrides[target] = new_interval
        meta[target] = int(time.time())
        changed.append(f"{target}: {current}s → {new_interval}s")

    if changed:
        tmp = overrides_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(overrides, indent=2))
        os.replace(tmp, overrides_file)
        tmp_meta = meta_file.with_suffix(".json.tmp")
        tmp_meta.write_text(json.dumps(meta, indent=2))
        os.replace(tmp_meta, meta_file)
        print(f"[engagement-analyze] Applied frequency changes: {changed}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
