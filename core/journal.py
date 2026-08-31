"""Append-only writer forthe owner's persistent 每日复盘日志 (Feishu docx).

The journal is a single private Feishu document (token in jarvis.yaml →
journal.doc_token). daily-reflect appends one dated section per day so the
reflection accumulates into a longitudinal, searchable record the owner reads
on Feishu mobile — the doc-centric collaboration surface from the PRD.

Design notes:
- Append-only via `lark-cli docs +update --command append`. Never overwrites.
- Fully guarded: any failure returns False and is logged, NEVER raised, so a
  journal hiccup can never break the caller (e.g. the daily-reflect card send).
"""

import json
import subprocess
import sys

from core.config import Config
from core.timeutil import now_local_str

_CN_WEEKDAY = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _doc_token() -> str:
    try:
        return (Config().get("journal.doc_token") or "").strip()
    except Exception:
        return ""


def append_entry(content: str, heading: str | None = None, timeout: int = 60) -> bool:
    """Append a dated section to the journal doc. Returns True on success.

    content: markdown body for today's entry.
    heading: optional H2 title; defaults to '## YYYY-MM-DD 周X'.
    """
    content = (content or "").strip()
    if not content:
        return False

    token = _doc_token()
    if not token:
        return False

    if heading is None:
        try:
            wd = _CN_WEEKDAY[int(now_local_str("%w") or 0) - 1]
        except Exception:
            wd = ""
        heading = f"## {now_local_str('%Y-%m-%d')} {wd}".rstrip()

    # heading == "" → append a sub-entry under the current day (no new H2),
    # e.g. the owner's own reply quoted back into today's reflection.
    if heading:
        block = f"\n{heading}\n\n{content}\n\n---\n"
    else:
        block = f"\n{content}\n\n---\n"

    try:
        proc = subprocess.run(
            [
                "lark-cli", "docs", "+update",
                "--doc", token,
                "--command", "append",
                "--doc-format", "markdown",
                "--content", block,
                "--format", "json",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            _warn(f"lark-cli exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[:200]}")
            return False
        # lark-cli may prefix non-JSON status lines; parse from first '{'.
        out = proc.stdout or ""
        brace = out.find("{")
        if brace < 0:
            _warn(f"no JSON in lark-cli output: {out[:200]}")
            return False
        resp = json.loads(out[brace:])
        if not resp.get("ok"):
            _warn(f"append rejected: {json.dumps(resp.get('error', {}), ensure_ascii=False)[:200]}")
            return False
        return True
    except Exception as e:
        _warn(f"append raised {type(e).__name__}: {e}")
        return False


def _warn(msg: str) -> None:
    """Journal failures must be LOUD in the heartbeat log. The 6/20 journal
    doc was deleted and every nightly append failed with zero trace for 17
    days — a guarded bool return is not observability. stderr is captured
    into jarvis.log by the heartbeat's script runner."""
    print(f"[journal] append FAILED — {msg}", file=sys.stderr)
