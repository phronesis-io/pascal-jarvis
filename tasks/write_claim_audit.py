#!/usr/bin/env python3
"""REQ-88 (shadow mode): audit "已记录/已写入" style write-claims in replies.

Background (PRD prd_interaction_v4.md REQ-88): on 6/17 Jarvis said "记进去了"
without actually writing anything — a trust-level failure. This script is the
SHADOW phase: after each outgoing reply it detects first-person, perfective
claims that something was PERSISTED ("记进memory了" / "已写入记忆" /
"写进 tasks 了" / "已存档" ...), checks whether any known write surface
actually changed in the last N minutes, and appends one reconciliation row
per claim to data/write_claim_audit.jsonl.

STRICTLY LOG-ONLY: it never sends a message, never writes memory/journal on
Jarvis's behalf, never mutates anything except its own audit JSONL. It is
invoked fire-and-forget (backgrounded) from bot.sh's reply path and is fully
guarded — any failure is silent, so it can never delay or break a reply.
core.heartbeat_loop imports audit_message() directly for the card/digest
send path (the coverage gap the 7/8 audit found: most real claims flowed
through heartbeat sends this script never saw); rows carry a "channel"
field so the two paths stay distinguishable in the gate review.

Detection is deliberately narrow (评审红线: 宁漏勿误纠): only first-person
perfective + persistence-pointing phrasing counts. "之前已记录" (past
reference), "你可以记录" (suggestion), "我会记下来" (future), questions and
negations are all excluded.

Write surfaces checked (mtime within the window):
  - Claude auto-memory dirs: ~/.claude/projects/<slug>/memory for the repo
    dir, its parent and grandparent (covers the three dirs in CLAUDE.md)
  - $MEMORY_DIR (jarvis tiered memory; default $JARVIS_DIR/memory)
  - $JARVIS_DIR/data/jarvis.db (+ -wal) — the intent DB
  - $MEMORY_DIR/system/tasks.jsonl
  - journal (Feishu doc) is remote and NOT locally observable — recorded in
    each row under "unchecked" so review can account for it.

Env:
  JV_REPLY                    the outgoing reply text (required)
  JARVIS_DIR                  repo root (required)
  MEMORY_DIR                  jarvis memory dir (optional)
  JV_WRITE_CLAIM_WINDOW_MIN   lookback window in minutes (default 10)
  JV_CLAUDE_PROJECTS          override ~/.claude/projects (tests)
"""

import json
import os
import re
import sys
import time
from pathlib import Path

JARVIS_DIR = os.environ.get("JARVIS_DIR", "")
if JARVIS_DIR:
    sys.path.insert(0, JARVIS_DIR)

AUDIT_FILE = "write_claim_audit.jsonl"  # under $JARVIS_DIR/data/
DEFAULT_WINDOW_MIN = 10
_MAX_CLAIMS_PER_REPLY = 5
_MAX_FILES_PER_DIR = 5000

# ── Claim detection ──────────────────────────────────────────────────
# Persistence targets a bare "写进X了" must name for pattern P2.
_TARGETS = (
    r"(?:memory|MEMORY(?:\.md)?|记忆|备忘|笔记|日志|journal|"
    r"tasks?(?:\.jsonl)?|待办|档案|存档|open[_ ]?threads|todo)"
)
_CLAUSE_STOP = r"，。;；!！?？\n"

_PATTERNS = [
    # P1: 已/已经 + directional write verb (已写入记忆 / 已记录到 open_threads /
    #     已经把这条写进了 / 已存档)
    re.compile(
        r"(?:已|已经)\s*(?:帮你|给你|替你|为你)?(?:把[^" + _CLAUSE_STOP + r"]{0,30})?"
        r"(?:写入|写进|写到|记进|记入|记到|记录到|记录在|记录进|"
        r"存入|存进|存到|归档|存档|落盘)"
    ),
    # P2: write verb + 进/入/到 + named persistence target + …了
    #     (记进memory了 / 写进 tasks 了)
    re.compile(
        r"(?:记|写|存|加)(?:进|入|到)\s*[^" + _CLAUSE_STOP + r"]{0,30}?"
        + _TARGETS + r"[^" + _CLAUSE_STOP + r"]{0,15}?了"
    ),
    # P2b: 已/已经 + broad write verb + direction + NAMED target
    #      (已保存到记忆 / 已同步到 memory) — broad verbs only count when the
    #      persistence target is named, so "已同步到服务器" stays out.
    re.compile(
        r"(?:已|已经)\s*(?:把[^" + _CLAUSE_STOP + r"]{0,30})?"
        r"(?:保存|同步|更新|写|记|存)(?:到|进|入|在)\s*"
        r"[^" + _CLAUSE_STOP + r"]{0,30}?" + _TARGETS
    ),
    # P3: 已/已经 + bare perfective note verb (已记录 / 已经记下来了)
    re.compile(r"(?:已|已经)\s*(?:记录|记下来?|记好|存档|归档)"),
    # P4: colloquial perfective (记下来了 / 存好了)
    re.compile(r"(?:记|存)(?:下来|好)了"),
]

# Guards evaluated on the sub-clause immediately BEFORE the match (after the
# last comma). Any hit → not a fresh first-person claim → skip.
_NEG_GUARDS = [
    re.compile(r"(?:之前|以前|上次|此前|先前|早已|早就|原本|本来)"),  # past ref
    re.compile(r"(?<![帮给替为])[你您]"),                              # 2nd person subject
    re.compile(r"(?:请|建议|可以|不妨|记得|别忘|需要)"),               # suggestion
    re.compile(r"(?:会|将|打算|计划|准备|稍后|待会|回头|等会)"),        # future intent
    re.compile(r"(?:没有?|还没|尚未|未能|没能|不会)"),                  # negation
    re.compile(r"(?:如果|若是|假如|一旦|要是)"),                        # conditional
]

_CODE_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def detect_claims(text: str) -> list:
    """Return the clauses of `text` containing fresh first-person
    persistence claims. Narrow by design; never raises."""
    try:
        text = _CODE_FENCE.sub("", text or "")
        text = _INLINE_CODE.sub("", text)
        claims = []
        for clause in re.split(r"[。！？!?\n]", text):
            clause = clause.strip()
            if not clause or "吗" in clause:
                continue
            for pat in _PATTERNS:
                m = pat.search(clause)
                if not m:
                    continue
                before = clause[: m.start()]
                sub = re.split(r"[，,;；]", before)[-1]
                if any(g.search(sub) for g in _NEG_GUARDS):
                    continue
                if clause not in claims:
                    claims.append(clause)
                break
            if len(claims) >= _MAX_CLAIMS_PER_REPLY:
                break
        return claims
    except Exception:
        return []


# ── Write-surface checks ─────────────────────────────────────────────

def _slug(path: Path) -> str:
    return str(path).replace("/", "-").replace(".", "-")


def _surfaces(jarvis_dir: str) -> dict:
    """Map surface name → path (file or dir). Read-only discovery."""
    out = {}
    jd = Path(jarvis_dir).resolve()
    projects = Path(
        os.environ.get("JV_CLAUDE_PROJECTS")
        or (Path.home() / ".claude" / "projects")
    )
    seen = set()
    for p in (jd, jd.parent, jd.parent.parent):
        mdir = projects / _slug(p) / "memory"
        key = str(mdir)
        if key not in seen:
            seen.add(key)
            out[f"auto_memory:{_slug(p)}"] = mdir
    memory_dir = Path(os.environ.get("MEMORY_DIR") or (jd / "memory"))
    out["jarvis_memory"] = memory_dir
    out["jarvis_db"] = jd / "data" / "jarvis.db"
    out["jarvis_db_wal"] = jd / "data" / "jarvis.db-wal"
    out["tasks_jsonl"] = memory_dir / "system" / "tasks.jsonl"
    return out


def _changed_since(path: Path, cutoff: float):
    """True if `path` (file, or any file under it if a dir) has
    mtime >= cutoff; False otherwise; None if it doesn't exist."""
    try:
        if not path.exists():
            return None
        if path.is_file():
            return path.stat().st_mtime >= cutoff
        n = 0
        for root, _dirs, files in os.walk(path):
            try:
                if os.path.getmtime(root) >= cutoff:
                    return True
            except OSError:
                pass
            for f in files:
                n += 1
                if n > _MAX_FILES_PER_DIR:
                    return False
                try:
                    if os.path.getmtime(os.path.join(root, f)) >= cutoff:
                        return True
                except OSError:
                    pass
        return False
    except Exception:
        return None


def _now_str() -> str:
    try:
        from core.timeutil import now_local_str
        return now_local_str("%Y-%m-%d %H:%M")
    except Exception:
        return time.strftime("%Y-%m-%d %H:%M")


def audit_message(text: str, jarvis_dir: str, *, channel: str = "reply",
                  window_min: int | None = None) -> None:
    """Reconcile write-claims in one outbound message; append shadow rows.

    Importable entry point (core.heartbeat_loop hooks the card/digest send
    path through it). Same contract as the bot.sh invocation: record-only,
    swallows every failure — the caller's delivery must never depend on it.
    """
    try:
        text = (text or "").strip()
        if not text or not jarvis_dir:
            return
        claims = detect_claims(text)
        if not claims:
            return

        if window_min is None:
            try:
                window_min = int(os.environ.get("JV_WRITE_CLAIM_WINDOW_MIN", "")
                                 or DEFAULT_WINDOW_MIN)
            except ValueError:
                window_min = DEFAULT_WINDOW_MIN
        cutoff = time.time() - window_min * 60

        checked, hit = [], []
        for name, path in _surfaces(jarvis_dir).items():
            result = _changed_since(Path(path), cutoff)
            if result is None:
                continue  # surface doesn't exist on this machine — not checked
            checked.append(name)
            if result:
                hit.append(name)

        verdict = "confirmed" if hit else "unverified"
        ts = _now_str()
        out = Path(jarvis_dir) / "data" / AUDIT_FILE
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "a", encoding="utf-8") as f:
            for claim in claims:
                row = {
                    "ts": ts,
                    "channel": channel,
                    "claim": claim[:120],
                    "surfaces_hit": hit,
                    "surfaces_checked": checked,
                    "unchecked": ["journal"],  # Feishu doc: not locally observable
                    "window_min": window_min,
                    "verdict": verdict,
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main() -> None:
    audit_message(os.environ.get("JV_REPLY") or "", JARVIS_DIR)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
