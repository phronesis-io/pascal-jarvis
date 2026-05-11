#!/usr/bin/env python3
"""Post-hook: write engagement insights to memory/system/engagement_insights.md.

Stdin: Claude's JSON analysis of engagement data.
Stdout: empty (this is a silent background task, no user message).
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error
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
    cleaned = re.sub(r"^```json?\s*", "", raw)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip())

    # Try to extract JSON from the response
    json_start = cleaned.find("{")
    json_end = cleaned.rfind("}")
    if json_start >= 0 and json_end > json_start:
        cleaned = cleaned[json_start : json_end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print(f"[engagement-analyze] failed to parse JSON response", file=sys.stderr)
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

    # No user-facing output — this is a silent background task
    return 0


if __name__ == "__main__":
    sys.exit(main())
