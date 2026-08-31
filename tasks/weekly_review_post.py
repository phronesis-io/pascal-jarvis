#!/usr/bin/env python3
"""Render the deterministic weekly Matter result review."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.card import build_rich_card
from core.matter_review import render_matter_review
from core.timeutil import now_local_str


def _stamp_success() -> None:
    jarvis_dir = Path(
        os.environ.get("JARVIS_DIR")
        or Path(__file__).resolve().parent.parent
    )
    stamp = jarvis_dir / "data" / ".weekly_review_stamp"
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(now_local_str("%Y-W%V"), encoding="utf-8")


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 1
    try:
        report = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return 1
    if (not isinstance(report, dict)
            or report.get("schema") != "jarvis.matter-review.v1"):
        return 1

    if not report.get("material"):
        _stamp_success()
        return 0

    body = render_matter_review(report, per_section=1)
    if body:
        summary = body.splitlines()[0]
        card = build_rich_card(
            header="📋 周省",
            summary=summary,
            sections=[{"type": "markdown", "content": body}],
            meta={
                "source": "weekly_review",
                "date": now_local_str("%Y-%m-%d"),
            },
            work_receipt="已核对本周完成证据、待收口产出和下一步",
        )
        if not card:
            return 1
        print(card)

    _stamp_success()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
