"""Protected-document write guard (REQ-65) — deterministic, read-back based.

The single highest trust risk in the interaction audit: Jarvis reported precise
FAKE success on Pascal's most-valued artifacts — '<latex> 16→56, 改好了 ✅' on a
whitepaper that ended up with ZERO formulas (revision 798→799 self-refuted), and
a travel-handbook "rebuild" that wiped Pascal's hand-entered parking points and
point-to-point drive times. The v2 闸2 maker-verifier was fooled because it
counted its OWN generation-side output, not an independent read-back of the live
doc.

This module is the deterministic check the agent MUST run before asserting a doc
write succeeded (behavioral_rules mandates it):
  - readback_count(): count a feature in the LIVE canonical text, never trust a
    generation-side number. If the claimed-changed feature is absent, the write
    did not do what was claimed.
  - diff_blocks(): block-level before/after diff — how many blocks were deleted
    vs added. A "patch" that deletes most of the doc is a destructive overwrite.
  - verify_write(): combine both into a verdict the agent acts on — OK, or
    FAILED with a reason to surface to Pascal ("I didn't manage to change it")
    instead of a fake ✅.

All functions are pure (text in, verdict out) so they are fully testable and the
agent can call them via `python3 -m core.doc_guard ...` after pulling the live
doc back.
"""

from __future__ import annotations

import re
import sys

# A write that removes more than this fraction of the pre-existing blocks is
# treated as a destructive overwrite, not a patch — reject unless explicitly
# confirmed. Pascal's handbook lost hand-entered data to exactly this.
MAX_BLOCK_DELETION_RATIO = 0.30


def _blocks(text: str) -> list[str]:
    """Split a doc into comparable blocks: non-empty, stripped lines/paragraphs.
    Block identity is the normalized line — robust to reflow/whitespace."""
    out = []
    for raw in (text or "").splitlines():
        s = re.sub(r"\s+", " ", raw.strip())
        if s:
            out.append(s)
    return out


def readback_count(live_text: str, pattern: str, *, regex: bool = False) -> int:
    """Independent count of a feature in the LIVE doc (the only honest N).

    pattern: a substring (default) or a regex (regex=True). Example: count
    '<latex>' tags by readback_count(doc, '<latex>'). If this returns 0 the
    claimed feature is NOT in the doc — never report 'fixed N' off generation."""
    if not live_text or not pattern:
        return 0
    if regex:
        try:
            return len(re.findall(pattern, live_text))
        except re.error:
            return 0
    return live_text.count(pattern)


def diff_blocks(before: str, after: str) -> dict:
    """Block-level before/after diff. Returns counts + the deletion ratio."""
    b = _blocks(before)
    a = set(_blocks(after))
    before_set = set(b)
    deleted = [x for x in before_set if x not in a]
    added = [x for x in set(_blocks(after)) if x not in before_set]
    n_before = len(before_set)
    ratio = (len(deleted) / n_before) if n_before else 0.0
    return {
        "blocks_before": n_before,
        "blocks_after": len(a),
        "deleted": len(deleted),
        "added": len(added),
        "deletion_ratio": round(ratio, 3),
        "deleted_samples": deleted[:5],
    }


def verify_write(before: str, after: str, *,
                 claim_feature: str | None = None,
                 claim_min_count: int = 0,
                 regex: bool = False,
                 allow_overwrite: bool = False) -> dict:
    """Verdict for a protected-doc write. ok=True only if BOTH hold:

    (1) Not a destructive overwrite — deletion_ratio <= MAX_BLOCK_DELETION_RATIO
        (unless allow_overwrite, an explicit confirmed full-rewrite).
    (2) If a feature was claimed changed (claim_feature), the LIVE post-write
        doc actually contains >= claim_min_count of it (read-back, not
        generation count). 0 ⇒ the change is not in this doc.

    Returns {ok, reason, diff, readback}. The agent surfaces reason verbatim
    on failure ('我没改成…' / 'target not in this doc') instead of a fake ✅.
    """
    d = diff_blocks(before, after)
    readback = None
    reasons = []

    if not allow_overwrite and d["deletion_ratio"] > MAX_BLOCK_DELETION_RATIO:
        reasons.append(
            f"destructive: this write deletes {d['deleted']}/{d['blocks_before']} "
            f"blocks ({int(d['deletion_ratio']*100)}% > "
            f"{int(MAX_BLOCK_DELETION_RATIO*100)}%) — likely a full overwrite "
            f"that would wipe hand-entered content; sample lost: {d['deleted_samples']}")

    if claim_feature:
        readback = readback_count(after, claim_feature, regex=regex)
        if readback < max(1, claim_min_count):
            reasons.append(
                f"claim unmet: read-back finds {readback} of '{claim_feature}' in "
                f"the live doc (claimed >= {max(1, claim_min_count)}). Do NOT report "
                f"success — the target feature is absent or the write did not land.")

    return {"ok": not reasons, "reason": "; ".join(reasons),
            "diff": d, "readback": readback}


def _main(argv: list[str]) -> int:
    """CLI: python3 -m core.doc_guard verify <before_file> <after_file>
            [--feature STR] [--min N] [--regex] [--allow-overwrite]
    Reads the two files, prints the verdict JSON, exit 0 if ok else 1."""
    import argparse, json
    ap = argparse.ArgumentParser(prog="core.doc_guard")
    ap.add_argument("cmd", choices=["verify", "count"])
    ap.add_argument("before", nargs="?")
    ap.add_argument("after", nargs="?")
    ap.add_argument("--feature", default=None)
    ap.add_argument("--min", type=int, default=0)
    ap.add_argument("--regex", action="store_true")
    ap.add_argument("--allow-overwrite", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "count":
        text = open(args.before, encoding="utf-8", errors="replace").read() if args.before else sys.stdin.read()
        print(readback_count(text, args.feature or "", regex=args.regex))
        return 0
    before = open(args.before, encoding="utf-8", errors="replace").read() if args.before else ""
    after = open(args.after, encoding="utf-8", errors="replace").read() if args.after else ""
    v = verify_write(before, after, claim_feature=args.feature,
                     claim_min_count=args.min, regex=args.regex,
                     allow_overwrite=args.allow_overwrite)
    print(json.dumps(v, ensure_ascii=False, indent=2))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
