#!/usr/bin/env python3
"""Apply or reject queued harness proposals (B-level changes) by id.

Run by the main conversation when Pascal approves via Feishu, e.g.:
    python3 tasks/harness_apply.py --approve 12 14
    python3 tasks/harness_apply.py --all
    python3 tasks/harness_apply.py --reject 13
    python3 tasks/harness_apply.py --list

Approving a proposal applies its verbatim old→new edit to the target file under
MEMORY_DIR (path-guarded), logs it to harness_changelog.md, and removes it from
the pending queue. A proposal whose `old` no longer matches is left pending and
reported (so a drifted anchor never silently corrupts the contract).
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.jsonl import read_jsonl, write_jsonl
from core.timeutil import now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
PENDING = JARVIS_DIR / "harness_proposals_pending.jsonl"
CHANGELOG = JARVIS_DIR / "harness_changelog.md"


def _resolve(filename: str) -> Path | None:
    target = MEMORY_DIR / filename
    try:
        target.resolve().relative_to(MEMORY_DIR.resolve())
    except ValueError:
        return None
    return target if target.exists() else None


def _log(line: str, ts: str) -> None:
    with CHANGELOG.open("a", encoding="utf-8") as f:
        f.write(f"\n<!-- {ts} approve/reject -->\n- {line}\n")


def _apply_one(rec: dict, ts: str) -> tuple[bool, str]:
    target = _resolve(rec.get("target", ""))
    if target is None:
        return False, f"#{rec['id']} 目标文件不存在/越界: {rec.get('target')}"
    old, new = rec.get("old"), rec.get("new")
    if not old:
        return False, f"#{rec['id']} 缺少 old 锚点"
    text = target.read_text(encoding="utf-8")
    if old not in text:
        return False, f"#{rec['id']} 锚点已漂移（old 不再匹配 {rec.get('target')}）——留在队列，需重新生成"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    line = f"APPLIED #{rec['id']} {rec.get('target')}: {rec.get('summary','')}"
    _log(line, ts)
    return True, line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", nargs="*", type=int, default=[])
    ap.add_argument("--reject", nargs="*", type=int, default=[])
    ap.add_argument("--all", action="store_true", help="approve all pending")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    queue = read_jsonl(PENDING)
    pending = [r for r in queue if r.get("status", "pending") == "pending"]

    if args.list or (not args.approve and not args.reject and not args.all):
        if not pending:
            print("（无待审提案）")
            return 0
        for r in pending:
            print(f"#{r['id']} [{r.get('target')}] {r.get('summary')}")
            if r.get("rationale"):
                print(f"    理由: {r['rationale']}")
            if r.get("signal"):
                print(f"    信号: {r['signal']}")
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    approve_ids = {r["id"] for r in pending} if args.all else set(args.approve)
    reject_ids = set(args.reject)

    results, keep = [], []
    for r in queue:
        rid = r.get("id")
        if r.get("status", "pending") != "pending":
            continue  # drop already-resolved rows
        if rid in approve_ids:
            ok, msg = _apply_one(r, ts)
            results.append(("✅" if ok else "⚠️") + " " + msg)
            if not ok:
                keep.append(r)  # anchor drifted — keep for regeneration
        elif rid in reject_ids:
            _log(f"REJECTED #{rid} {r.get('target')}: {r.get('summary','')}", ts)
            results.append(f"🗑️ 拒绝 #{rid} {r.get('summary','')}")
        else:
            keep.append(r)

    write_jsonl(PENDING, keep)

    if not results:
        print("没有匹配的待审提案 id。")
    else:
        print("\n".join(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
