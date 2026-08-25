#!/usr/bin/env python3
"""Post-hook for `metrics-digest` — render probe records as Lark cards.

Watermark handshake with the pre-hook: .digest_pending.json (staged by pre)
is promoted to .digest_watermark.json only on a well-formed Claude reply —
including HEARTBEAT_OK (a valid "nothing card-worthy"). On error-shaped or
unparsable output the pending file is left alone, so the next cycle re-emits
the same records instead of silently losing the day's digest.
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response, sentinel_present


_METRIC_DISPLAY_NAMES = {
    "pgc_pulse": "新闻抓取",
    "user_growth": "用户增长",
    "broken_first_party": "一手数据源异常数",
}
_INTERNAL_METRIC_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_TRANSPORT_STATUS_RE = re.compile(
    r"(?:HTTP\s*|返回\s*)[45]\d{2}\b", re.IGNORECASE)


def _plain_metric_copy(text: str) -> str:
    """Keep probe implementation names and transport codes off user cards."""
    result = str(text or "")
    for internal, display in _METRIC_DISPLAY_NAMES.items():
        result = re.sub(rf"\b{re.escape(internal)}\b", display, result)
    result = _TRANSPORT_STATUS_RE.sub("服务响应异常", result)
    return _INTERNAL_METRIC_RE.sub("相关指标", result)


def _metrics_dir() -> Path:
    jd = os.environ.get("JARVIS_DIR") or str(Path(__file__).resolve().parent.parent)
    return Path(jd) / "data" / "metrics"


def _promote_watermark():
    mdir = _metrics_dir()
    pending = mdir / ".digest_pending.json"
    if pending.exists():
        try:
            os.replace(pending, mdir / ".digest_watermark.json")
        except OSError as e:
            print(f"[metrics_digest] watermark promote failed: {e}", file=sys.stderr)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    if sentinel_present(raw):
        # Sentinel anywhere = the model chose silence for this cycle (any
        # surrounding prose is scratch work). That's a VALID outcome for
        # these records — promote, don't re-emit them forever.
        _promote_watermark()
        return 0
    if looks_like_error(raw):
        print("[metrics_digest] skipping — looks like error output; "
              "records will re-emit next cycle", file=sys.stderr)
        return 0

    parsed = parse_json_response(raw)
    if parsed is None:
        print("[metrics_digest] no JSON envelope; records will re-emit "
              "next cycle", file=sys.stderr)
        return 0

    cards = parsed.get("cards")
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        header = str(card.get("header") or "").strip()
        body = str(card.get("body") or "").strip()
        if header and body:
            print(build_card(
                _plain_metric_copy(header), _plain_metric_copy(body),
                source="metrics-digest",
                work_receipt="聚合运行指标、完成异常归因和重复信号压缩",
            ))
    _promote_watermark()
    return 0


if __name__ == "__main__":
    sys.exit(main())
