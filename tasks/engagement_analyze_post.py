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
CONTENT_MIX_FILE = MEMORY_DIR / "system" / "engagement_content_mix.md"


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
    content_mix = data.get("content_mix", [])

    if not insights and not content_mix:
        return 0

    # Build the insights markdown file
    if insights:
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
    if content_mix:
        _write_content_mix(content_mix)

    # No user-facing output — this is a silent background task
    return 0


def _write_content_mix(content_mix: list[dict]) -> None:
    """Persist content-mix guidance for pre-hooks to consume.

    This is deliberately advisory: it does not change scheduling. The next
    checkin/content prompts can read this as steering context while frequency
    changes stay in interval_overrides.json with guardrails.
    """
    rows = []
    for item in content_mix:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "").strip()[:80]
        mode = str(item.get("mode") or item.get("topic") or "").strip()[:120]
        weight = str(item.get("weight") or item.get("direction") or "").strip()[:40]
        rationale = str(item.get("rationale") or item.get("reason") or "").strip()[:240]
        if not target or not mode:
            continue
        rows.append({
            "target": target,
            "mode": mode,
            "weight": weight or "observe",
            "rationale": rationale,
        })
    if not rows:
        return

    lines = [
        "# Engagement Content Mix",
        "",
        f"_Last updated: {now_local_str('%Y-%m-%d %H:%M')}_",
        "",
        "Advisory steering from engagement-analyze. Frequency changes still live in interval_overrides.json.",
        "",
    ]
    for row in rows[:12]:
        lines.append(
            f"- **{row['target']} / {row['mode']}**: {row['weight']} — {row['rationale']}"
        )
    content = "\n".join(lines).rstrip() + "\n"

    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)
    tmp = CONTENT_MIX_FILE.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, CONTENT_MIX_FILE)
    print(f"[engagement-analyze] wrote content mix ({len(rows)} item(s))", file=sys.stderr)


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
    # Infrastructure tasks are exempt from engagement tuning: they are silent
    # by design, so 0% engagement is their NORMAL state, not a signal to slow
    # them. 2026-06-11 the analyzer slowed Tier-0 calendar-sync to 2h within
    # hours of the apply path going live — stale-calendar complaints (5/25)
    # would have recurred with nothing reporting why.
    # Mirrored from HeartbeatRunner.PRIORITY_TASKS | .TIER0_TASKS — avoids
    # importing the full HeartbeatRunner class and its dependency tree in this
    # subprocess post-hook.
    _PRIORITY_TASKS = {"calendar-sync", "memory-hourly", "activity-log", "cross-session-sync",
                       "eigenflux-friends", "eigenflux-messages", "intention-check"}
    _TIER0_TASKS = {"calendar-sync"}
    protected = _PRIORITY_TASKS | _TIER0_TASKS

    changed = []
    for a in adaptations:
        target = str(a.get("target") or "")
        suggestion = str(a.get("suggestion") or "").lower()
        if target not in tasks_def:
            continue
        if target in protected:
            print(f"[engagement-analyze] {target}: protected infrastructure task, "
                  "skipping frequency adaptation", file=sys.stderr)
            continue

        base_interval = tasks_def[target]

        # Direction: the structured field is AUTHORITATIVE when present (the
        # analyzer's JSON contract carries "direction" since 2026-06-12 —
        # Pascal's 去关键词化 ask). A present-but-unrecognized value must
        # SKIP, never fall through to prose keywords: the rationale text can
        # contain words like "more relevant" and apply the OPPOSITE change.
        # Keyword parsing of the prose runs ONLY when direction is absent
        # (older-format responses).
        direction = str(a.get("direction") or "").strip().lower()
        multiplier = 1.0
        if direction:
            if direction in ("reduce", "decrease", "lower", "降频", "降低", "减少"):
                multiplier = 2.0
            elif direction in ("increase", "raise", "加频", "提高", "增加"):
                multiplier = 0.5
            elif direction in ("keep", "maintain", "no_change", "hold", "不变", "维持"):
                continue
            else:
                print(f"[engagement-analyze] {target}: unrecognized direction "
                      f"'{direction}' — skipping (not falling back to prose keywords)",
                      file=sys.stderr)
                continue
        elif any(w in suggestion for w in ["reduce", "decrease", "less", "fewer",
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
        # keep adapting, but clamp: don't go below 5min or above 48h, and
        # never drift beyond 4x/0.25x of the HEARTBEAT.md base — auto-tuning
        # adjusts cadence, it must not be able to effectively disable a task.
        current = overrides.get(target) or base_interval
        new_interval = max(300, min(172800, int(current * multiplier)))
        new_interval = max(base_interval // 4, min(base_interval * 4, new_interval))

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
