#!/usr/bin/env python3
"""Post-hook for harness-evolve.

Parses Claude's JSON: auto-applies A-level hygiene directly to memory files,
queues B-level proposals (contract/code changes) for Feishu approval, and prints
the digest to stdout (→ sent to Lark) only when there are proposals to review.

A-level NEVER touches hot/behavioral_rules.md or hot/feedback_rules.md — those
are the always-loaded contract and must go through propose→approve (B-level).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.safety import looks_like_error, parse_json_response
from core.jsonl import read_jsonl, append_jsonl
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
PENDING = JARVIS_DIR / "harness_proposals_pending.jsonl"
STATE_FILE = JARVIS_DIR / ".harness_evolve_state"
CHANGELOG = JARVIS_DIR / "harness_changelog.md"

# A-level hygiene must never rewrite the loaded contract — that is B-level only.
PROTECTED = {"hot/behavioral_rules.md", "hot/feedback_rules.md"}


def _resolve(filename: str) -> Path | None:
    """Resolve a memory-relative path, guarding traversal + existence + protection."""
    if filename in PROTECTED:
        print(f"[harness-evolve] BLOCKED A-level edit to protected file: {filename}", file=sys.stderr)
        return None
    target = MEMORY_DIR / filename
    try:
        target.resolve().relative_to(MEMORY_DIR.resolve())
    except ValueError:
        print(f"[harness-evolve] BLOCKED path traversal: {filename}", file=sys.stderr)
        return None
    if not target.exists():
        print(f"[harness-evolve] skip {filename} — does not exist", file=sys.stderr)
        return None
    return target


def _apply_hygiene(item: dict, ts: str) -> str | None:
    """Apply one A-level hygiene op. Returns a changelog line if applied."""
    op = (item.get("op") or "").strip()
    fn = (item.get("file") or "").strip()
    target = _resolve(fn)
    if target is None:
        return None
    if op == "update":
        content = (item.get("content") or "").strip()
        if not content:
            return None
        with target.open("a", encoding="utf-8") as f:
            f.write(f"\n<!-- harness-evolve {ts} -->\n- {content}\n")
        return f"[A] UPDATE {fn}: {content[:80]}"
    if op == "replace":
        old = (item.get("old") or "").strip()
        new = (item.get("new") or "").strip()
        if not old:
            return None
        text = target.read_text(encoding="utf-8")
        if old not in text:
            print(f"[harness-evolve] REPLACE no-op on {fn} — match not found", file=sys.stderr)
            return None
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"[A] REPLACE {fn}: {old[:50]} -> {new[:50]}"
    return None


def _next_id() -> int:
    existing = read_jsonl(PENDING)
    return max((int(r.get("id", 0)) for r in existing), default=0) + 1


def _log(lines: list[str], ts: str) -> None:
    if not lines:
        return
    header = f"\n## {ts}\n"
    body = "\n".join(f"- {l}" for l in lines) + "\n"
    with CHANGELOG.open("a", encoding="utf-8") as f:
        f.write(header + body)


def main() -> int:
    raw = sys.stdin.read().strip()
    # Always stamp the run so the next delta window is correct, even on no-op.
    ts = now_local_str("%Y-%m-%d")
    STATE_FILE.write_text(ts, encoding="utf-8")

    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[harness-evolve] skipping — output looks like error", file=sys.stderr)
        return 0

    data = parse_json_response(raw)
    if data is None:
        print("[harness-evolve] non-JSON response, skipping", file=sys.stderr)
        return 0

    changelog: list[str] = []

    # 1) A-level hygiene — auto-apply
    for item in data.get("hygiene", []) or []:
        if isinstance(item, dict):
            line = _apply_hygiene(item, ts)
            if line:
                changelog.append(line)

    # 2) B-level proposals — queue for approval (do NOT apply)
    proposals = [p for p in (data.get("proposals", []) or []) if isinstance(p, dict) and p.get("old") and p.get("new")]
    queued = []
    nid = _next_id()
    for p in proposals:
        rec = {
            "id": nid,
            "ts": ts,
            "status": "pending",
            "target": (p.get("target") or "").strip(),
            "summary": (p.get("summary") or "").strip(),
            "old": p.get("old"),
            "new": p.get("new"),
            "rationale": (p.get("rationale") or "").strip(),
            "signal": (p.get("signal") or "").strip(),
        }
        append_jsonl(PENDING, rec)
        queued.append(rec)
        changelog.append(f"[B] QUEUED #{nid} {rec['target']}: {rec['summary']}")
        nid += 1

    _log(changelog, ts)

    if changelog:
        print(f"[harness-evolve] applied {sum(1 for c in changelog if c.startswith('[A]'))} hygiene, "
              f"queued {len(queued)} proposal(s)", file=sys.stderr)

    # 3) Digest to user — ONLY when there are proposals to approve (打扰低频).
    if not queued:
        return 0

    digest = (data.get("digest") or "").strip()
    if not digest:
        # Build a fallback digest from the queued proposals.
        lines = ["🧬 **Harness 进化提案**（今日）", ""]
        for r in queued:
            lines.append(f"**#{r['id']}** `{r['target']}` — {r['summary']}")
            if r["rationale"]:
                lines.append(f"  · {r['rationale']}")
        digest = "\n".join(lines)
    n_hyg = sum(1 for c in changelog if c.startswith("[A]"))
    if n_hyg:
        digest += f"\n\n_（另自动做了 {n_hyg} 条卫生，见 harness_changelog.md）_"
    digest += "\n\n回复「harness 通过 " + ",".join(str(r["id"]) for r in queued) + "」逐条选，或「harness 全部通过」/「harness 都不要」。"
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
