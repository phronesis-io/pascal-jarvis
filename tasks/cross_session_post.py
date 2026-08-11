#!/usr/bin/env python3
"""Post-hook: write cross-session digest to memory; surface user_message when warranted.

Receives Claude's summary from stdin and writes to memory/system/cross_session_digest.md.
Keeps max 50 lines, newest first. NOT a silent task: the digest write is silent, but a
"user_message" field in the JSON envelope is printed to stdout. (This docstring
claimed "silent" until 2026-07-07, which made the noisiest push path in the system
invisible to anyone auditing from the docs — see the HEARTBEAT.md task index.)
Delivery semantics since REQ-119 (2026-08-11): cross-session-sync is an
AMBIENT source, so the printed user_message becomes a ledger-only memorial
surfaced via the morning-anchor 攒批 line — not a realtime Lark card. The
three gates below still matter: they keep junk out of the ledger and digest.

Every user_message must pass three gates before it reaches Pascal (2026-07-07 incident:
PRs #71/#73/#75 auto-merged 08:18–08:55, yet "3 个 PR 等你批" was pushed 8 more times
until 21:24, surviving three live corrections to Pascal's face — the LLM rewords the
ping each 10-min cycle, so the exact-match outbox dedup in heartbeat_loop never fired):
  1. anchor_guard — no fabricated clock times (core/anchor_guard).
  2. Pending-PR claims are verified live via `gh pr list`. Segment-level since
     2026-07-08: the old whole-message AND of PR-token + await-token suppressed
     reminders whose call-to-action was NOT the PR (and the exempt short-circuit
     ate any message that merely co-mentioned pgc). Now a claim exists only where
     both tokens share one clause; stale/exempt clauses are removed, the rest of
     the message still goes out; on gh failure the claim is DROPPED — stale
     silence is recoverable, false-positive nagging is what burned trust that night.
  3. Trigram-Jaccard dedup against messages actually pushed recently (own sent-cache,
     24h + outbox tail, 6h) — catches the rewordings that exact-match cannot.
     Sent-cache entries are trusted only with delivery evidence in
     engagement_log (or within a 30-min grace): the cache records at PRINT
     time, and a failed Lark send must not become 24h of suppression.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.anchor_guard import unverified_anchors
from core.safety import looks_like_error, parse_json_response
from core.timeutil import now_local, now_local_str

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR",
    Path.home() / ".jarvis" / "memory"))
DIGEST_FILE = MEMORY_DIR / "system" / "cross_session_digest.md"
MAX_LINES = 50

# Sent-cache: user_messages that actually printed (suppressed ones are NOT
# recorded — they must not poison future dedup).
SENT_FILE = MEMORY_DIR / "system" / "cross_session_sent.jsonl"
# Delivery log written by core/heartbeat_loop after each send; read-only here.
# JARVIS_DIR is exported by bot.sh (== repo dir in production); the fallback
# covers direct invocation, and tests point it at a sandbox.
_JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
    Path(__file__).resolve().parent.parent))
OUTBOX_FILE = _JARVIS_DIR / "heartbeat_outbox.jsonl"
# Engagement log: heartbeat_loop appends a type=="sent" row ONLY inside its
# delivered branch, so rows here are the delivery layer's proof a send left.
ENGAGEMENT_FILE = _JARVIS_DIR / "engagement_log.jsonl"

# user_message dedup windows: 24h over our own sent-cache, 6h over the outbox
# (mirrors heartbeat_loop.DEDUP_WINDOW_SECONDS). Threshold is deliberately
# lower than the digest's 0.75: a wrongly suppressed ping costs a stderr line,
# a wrongly sent one pings Pascal — the 8 stale pushes settled which is worse.
SENT_WINDOW_SECONDS = 24 * 3600
OUTBOX_WINDOW_SECONDS = 6 * 3600
USER_MSG_SIM_THRESHOLD = 0.5
# A sent-cache entry older than this with no matching delivery row in the
# engagement log is treated as never-delivered (2026-07-08: the cache records
# at print time; heartbeat's Lark send can still fail afterwards, and it
# deliberately skips the outbox on failure so its own retry survives — the
# cache must not turn that failure into 24h of suppression).
SENT_UNCONFIRMED_GRACE_SECONDS = 30 * 60

# PRs in these repos merge without Pascal clicking anything (auto-merge flow),
# so any "PR 等你批" reminder about them is false by construction — exempt.
AUTOMERGE_EXEMPT_REPOS = {"phronesis-io/eigenflux-pgc"}
# Repo extraction from free-text LLM prose is brittle; match against a fixed
# name→slug map instead (longest name first, matched spans masked out so
# "eigenflux-pgc" doesn't also count as "eigenflux" and "pgc").
KNOWN_REPOS = {
    "eigenflux-pgc": "phronesis-io/eigenflux-pgc",
    "pgc": "phronesis-io/eigenflux-pgc",
    "eigenflux": "phronesis-io/eigenflux",
    "pascal-jarvis": "phronesis-io/pascal-jarvis",
}

HEADER = """\
---
name: Cross-Session Digest
description: Recent activity from other Claude Code projects
type: reference
---

# Cross-Session Digest
"""


def _normalize(text: str) -> str:
    """Normalize text for dedup comparison: lowercase, collapse whitespace, strip timestamps."""
    import re
    text = text.lower()
    text = re.sub(r"\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}", "", text)  # strip timestamps
    text = re.sub(r"[（）()\"'\s]+", " ", text).strip()
    return text


def _char_ngrams(text: str, n: int = 3) -> set:
    """Extract character n-grams for language-agnostic comparison."""
    return {text[i:i+n] for i in range(len(text) - n + 1)} if len(text) >= n else {text}


def _similarity(a_norm: str, b_norm: str) -> float:
    """Trigram-Jaccard similarity between two already-_normalize()d strings."""
    a_grams = _char_ngrams(a_norm)
    b_grams = _char_ngrams(b_norm)
    if not a_grams or not b_grams:
        return 0.0
    union = len(a_grams | b_grams)
    return len(a_grams & b_grams) / union if union else 0.0


def _is_duplicate(new_raw: str, existing_body: str) -> bool:
    """Check if new entry is essentially the same as the most recent entry.
    Uses character trigram Jaccard similarity — works for Chinese+English mixed text.
    Short texts require higher similarity to avoid false positives.
    """
    if not existing_body:
        return False
    import re
    # Extract the first entry's content (up to the next ## header)
    first_entry_match = re.match(r"\n## [^\n]+\n(.*?)(?=\n## |\Z)", existing_body, re.DOTALL)
    if not first_entry_match:
        return False
    last_content = _normalize(first_entry_match.group(1))
    new_content = _normalize(new_raw)
    if not last_content or not new_content:
        return False
    # Skip dedup for very short texts (< 15 chars) — too little signal
    if len(new_content) < 15 or len(last_content) < 15:
        return new_content == last_content  # exact match only for short texts
    # Use high threshold — only skip near-exact duplicates.
    # Previous threshold (0.45) was too aggressive: ongoing work on the same
    # project produces similar-looking digests that are actually different updates.
    # (Lexical similarity cannot distinguish a paraphrase of one session from a
    # real successive update — the durable fix for re-digesting an unchanged
    # session is the source watermark in cross_session_pre.sh, not this gate.)
    threshold = 0.75
    return _similarity(last_content, new_content) > threshold


_PR_TOKEN_RE = re.compile(r"\bPR\b|pull.{0,2}request|拉取请求", re.IGNORECASE)
# 2026-07-08: the bare "approv" branch classified merged-PR NEWS ("已获
# approve 并合并") as pending — anchored to await-shaped contexts only.
_AWAIT_ACTION_RE = re.compile(
    r"待批|等批|待合并|挂着|等[你您]|等 ?pascal|网页[端上].{0,4}(批|merge|合并)"
    r"|await|等.{0,6}approv|pending\s+approv",
    re.IGNORECASE,
)
# Resolved/past-tense contexts are never pending claims, whatever else matches.
_RESOLVED_RE = re.compile(r"已合并|已获\s*approve|merged|已上线|已批", re.IGNORECASE)
# Clause boundaries for segment-level classification (2026-07-08: the old
# whole-message AND let an await-token in one clause arm a PR mention in
# another, suppressing reminders whose actual ask was not the PR at all).
_SEGMENT_DELIM_RE = re.compile(r"([。；;\n])")


def _is_pending_pr_segment(segment: str) -> bool:
    """True when THIS clause tells Pascal a PR is waiting on his action."""
    if _RESOLVED_RE.search(segment):
        return False
    return bool(_PR_TOKEN_RE.search(segment)) and bool(_AWAIT_ACTION_RE.search(segment))


def _repo_slugs(text: str) -> list:
    """Known repo slugs mentioned in text (longest name first, spans masked
    out so "eigenflux-pgc" doesn't also count as "eigenflux" and "pgc")."""
    masked = text.lower()
    slugs = []
    for name in sorted(KNOWN_REPOS, key=len, reverse=True):
        if name in masked:
            masked = masked.replace(name, " ")
            if KNOWN_REPOS[name] not in slugs:
                slugs.append(KNOWN_REPOS[name])
    return slugs


def _gh_open_pr_count(repo: str):
    """Open-PR count via gh, or None on ANY failure (timeout/auth/parse/missing).

    Short timeout on purpose: heartbeat runs this post-hook synchronously with
    a 60s cap (core/heartbeat.run_script) — a hung gh must not eat the budget.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--json", "number", "--limit", "50"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        return len(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _segment_pending_pr_blocked(segment: str, message_slugs: list):
    """Reason to suppress ONE pending-PR clause, or None if verified live.

    2026-07-07: "3 个 PR 挂着等你网页端批" kept firing 13h after #71/#73/#75
    auto-merged. Rule: never relay "PR awaiting approval" from transcripts
    alone — verify open state live, and drop claims about auto-merge repos or
    repos we cannot identify. On gh failure we also drop the claim: stale
    silence is recoverable, false nagging is not.

    The exempt short-circuit applies only when ALL the clause's repo
    references auto-merge (2026-07-08: eigenflux PRs about PGC naturally
    co-mention pgc — "ANY slug exempt → suppress" ate the live eigenflux#67
    reminder); non-exempt slugs still get gh-verified.
    """
    slugs = _repo_slugs(segment) or list(message_slugs)
    if not slugs:
        return "pending-PR claim names no known repo — unverifiable, dropping"
    remaining = [s for s in slugs if s not in AUTOMERGE_EXEMPT_REPOS]
    if not remaining:
        return f"{slugs[0]} auto-merges — '等批' reminders about it are noise by construction"
    for slug in remaining:
        count = _gh_open_pr_count(slug)
        if count is None:
            return f"gh verification failed for {slug} — dropping unverified claim"
        if count == 0:
            return f"{slug} has 0 open PRs — claim is stale"
    return None


def _filter_pending_pr_claims(user_message: str):
    """(message_to_send_or_None, drop_reason_or_None) after live verification.

    Segment-level: a clause is a pending-PR claim only when a PR token and an
    await-action pattern occur in the SAME clause (split on 。；; and
    newlines). A clause naming no repo borrows the message-level mentions —
    the repo often sits in a neighbouring clause. Stale/exempt clauses are
    REMOVED and the rest of the message still goes out; only when removal
    guts the message (<15 chars of substance) is it dropped whole.
    """
    parts = _SEGMENT_DELIM_RE.split(user_message)
    message_slugs = _repo_slugs(user_message)
    kept = []
    dropped_reasons = []
    for i in range(0, len(parts), 2):
        seg = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if seg.strip() and _is_pending_pr_segment(seg):
            reason = _segment_pending_pr_blocked(seg, message_slugs)
            if reason:
                dropped_reasons.append(reason)
                print(
                    f"[cross-session] removing stale pending-PR segment — "
                    f"{reason}: {seg.strip()!r}",
                    file=sys.stderr,
                )
                continue
        kept.append(seg + delim)
    if not dropped_reasons:
        return user_message, None
    remainder = "".join(kept).strip().rstrip("；;，, \n")
    if len(_normalize(remainder)) < 15:
        return None, dropped_reasons[0]
    return remainder, None


def _delivery_stamps() -> list:
    """Datetimes of confirmed cross-session deliveries (engagement_log tail).

    heartbeat_loop writes a type=="sent" row only inside its delivered branch
    (and in the night-queue flush after a successful send), so these are
    delivery-layer facts. Tail-only read keeps the cost flat.
    """
    try:
        lines = ENGAGEMENT_FILE.read_text(encoding="utf-8").splitlines()[-200:]
    except OSError:
        return []
    stamps = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("type") != "sent":
            continue
        if "cross-session" not in str(entry.get("source", "")):
            continue
        try:
            stamps.append(datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M"))
        except (TypeError, ValueError):
            continue
    return stamps


def _recent_push_texts() -> list:
    """Texts pushed to Pascal recently: own sent-cache (24h) + outbox tail (6h).

    Sent-cache entries are written at PRINT time, before heartbeat attempts
    the actual Lark send — so each one is cross-checked against the delivery
    layer's engagement_log: no matching "sent" row and older than the 30-min
    grace means the delivery failed (or the batch was dropped), and the entry
    must not suppress the retry (2026-07-08: a failed send otherwise became
    24h of silence, re-creating the REQ-04-cancels-REQ-11 bug that
    heartbeat_loop's own delivery branch deliberately avoids).

    The outbox pass catches pushes recorded before the sent-cache existed and
    cross-session lines embedded in batched multi-task messages (split on the
    '---' batch separator). Same read pattern as heartbeat_loop's
    _is_duplicate_send: last 30 lines, ts parsed in the clock that wrote them.
    """
    texts = []
    now_dt = now_local().replace(tzinfo=None)

    def _within(entry, window_seconds):
        try:
            sent_dt = datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return False
        return (now_dt - sent_dt).total_seconds() < window_seconds

    try:
        sent_lines = SENT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        sent_lines = []
    deliveries = _delivery_stamps() if sent_lines else []
    for line in sent_lines:
        try:
            entry = json.loads(line)
            sent_dt = datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M")
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        age = (now_dt - sent_dt).total_seconds()
        if age >= SENT_WINDOW_SECONDS:
            continue
        confirmed = any(
            -300 <= (d - sent_dt).total_seconds() <= SENT_UNCONFIRMED_GRACE_SECONDS
            for d in deliveries)
        if age > SENT_UNCONFIRMED_GRACE_SECONDS and not confirmed:
            continue  # printed but never delivered — must not suppress a retry
        texts.append(str(entry.get("message", "")))

    try:
        out_lines = OUTBOX_FILE.read_text(encoding="utf-8").splitlines()[-30:]
    except OSError:
        out_lines = []
    for line in out_lines:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if entry.get("source") != "heartbeat":
            continue
        if not _within(entry, OUTBOX_WINDOW_SECONDS):
            continue
        for segment in str(entry.get("text", "")).split("\n---\n"):
            texts.append(segment.replace("📡 跨 Session 动态：", ""))
    return texts


# LLM reminders are templated: a NEW item pushed the same day differs from
# yesterday's push by one number ("（#67 面板）" vs "（#72 告警）" scores 0.75).
# Identifier tokens are compared as sets first — differing #N / PR N / 第N篇
# means different work items, never a duplicate, whatever the prose says.
_IDENTIFIER_RES = (
    re.compile(r"#\s?(\d+)"),
    re.compile(r"\bpr\s*#?\s*(\d+)", re.IGNORECASE),
    re.compile(r"第\s*(\d+)\s*[篇期号轮]"),
)


def _identifier_tokens(text: str) -> set:
    """Issue/PR/deliverable numbers mentioned in text, as a set of strings."""
    ids = set()
    for rx in _IDENTIFIER_RES:
        ids.update(rx.findall(text))
    return ids


def _duplicate_recent_push(user_message: str):
    """Reason string when user_message near-duplicates a recent push, else None.

    Compares against EVERY entry in the window, not just the newest — the
    observed 2026-07-07 dupes were 30min–4h apart with other messages between.
    """
    cand = _normalize(user_message)
    if len(cand) < 15:
        return None  # too little signal for trigram comparison
    cand_ids = _identifier_tokens(cand)
    for prev in _recent_push_texts():
        prev_norm = _normalize(prev)
        if len(prev_norm) < 15:
            continue
        prev_ids = _identifier_tokens(prev_norm)
        if cand_ids and prev_ids and cand_ids != prev_ids:
            continue  # different item numbers sharing a template — not a repeat
        sim = _similarity(cand, prev_norm)
        if sim > USER_MSG_SIM_THRESHOLD:
            return f"similarity {sim:.2f} to recent push {prev[:60]!r}"
    return None


def _record_sent(user_message: str):
    """Append to the sent-cache, pruning entries older than the dedup window."""
    now_dt = now_local().replace(tzinfo=None)
    kept = []
    try:
        for line in SENT_FILE.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                sent_dt = datetime.strptime(entry.get("ts", ""), "%Y-%m-%d %H:%M")
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if (now_dt - sent_dt).total_seconds() < SENT_WINDOW_SECONDS:
                kept.append(json.dumps(entry, ensure_ascii=False))
    except OSError:
        pass
    kept.append(json.dumps(
        {"ts": now_local_str("%Y-%m-%d %H:%M"), "message": user_message},
        ensure_ascii=False))
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SENT_FILE.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmp, SENT_FILE)


def main() -> int:
    raw = sys.stdin.read().strip()
    if not raw or "HEARTBEAT_OK" in raw:
        return 0
    if looks_like_error(raw):
        print("[cross-session] skipping — output looks like error", file=sys.stderr)
        return 0

    # Parse JSON envelope: {"digest": "...", "user_message": "..."}
    # Not JSON → envelope is None, treat raw as a plain digest (backward compatible).
    user_message = ""
    envelope = parse_json_response(raw)
    if envelope is not None and "digest" in envelope:
        user_message = envelope.get("user_message", "").strip()
        raw = envelope["digest"].strip()
        if not raw:
            return 0

    # If there's a user-facing message, print to stdout (sent to Lark) — but
    # only after all three gates pass (see module docstring): grounded time
    # anchors, live-verified PR claims, and no near-duplicate of a recent push.
    # Suppression skips ONLY the print — the digest below is still written for
    # continuity (an early return here would also break the consecutive
    # no-new-data dedup and the digest history).
    if user_message:
        bad = unverified_anchors(user_message)
        filtered, pr_reason = ((None, None) if bad
                               else _filter_pending_pr_claims(user_message))
        if bad:
            print(
                "[cross-session] suppressing user_message — unverifiable time "
                f"anchor(s) {[a.raw for a in bad]}: {user_message!r}",
                file=sys.stderr,
            )
        elif pr_reason:
            # Every pending-PR clause failed verification AND their removal
            # gutted the message — nothing substantive left to send.
            print(
                f"[cross-session] suppressing user_message — {pr_reason}: "
                f"{user_message!r}",
                file=sys.stderr,
            )
        elif (reason := _duplicate_recent_push(filtered)):
            print(
                f"[cross-session] suppressing user_message — {reason}: "
                f"{filtered!r}",
                file=sys.stderr,
            )
        else:
            print(f"📡 跨 Session 动态：{filtered}")
            _record_sent(filtered)

    # Skip consecutive "No new data" entries — they waste index space
    is_no_data = "no new data" in raw.lower() or "no significant" in raw.lower()
    if is_no_data and DIGEST_FILE.exists():
        content = DIGEST_FILE.read_text(encoding="utf-8")
        # Check if the most recent entry is also "no new data"
        import re as _re
        first_entry = _re.search(r"\n## [^\n]+\n(.*?)(?=\n## |\Z)", content, _re.DOTALL)
        if first_entry and "no new data" in first_entry.group(1).lower():
            print("[cross-session] skipping — consecutive 'No new data' entry", file=sys.stderr)
            return 0

    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "system").mkdir(parents=True, exist_ok=True)

    # Read existing entries (skip header)
    existing_body = ""
    if DIGEST_FILE.exists():
        content = DIGEST_FILE.read_text(encoding="utf-8")
        # Split off the header (everything before the first ## date entry)
        parts = content.split("\n## ", 1)
        if len(parts) > 1:
            existing_body = "\n## " + parts[1]

    # Dedup: skip if new content is essentially the same as last entry
    if _is_duplicate(raw, existing_body):
        print("[cross-session] skipping — duplicate of latest entry", file=sys.stderr)
        return 0

    ts = now_local_str("%Y-%m-%d %H:%M")
    new_entry = f"\n## {ts}\n{raw.strip()}\n"

    # Combine: new entry first, then existing
    combined = new_entry + existing_body

    # Limit to MAX_LINES of body content
    body_lines = combined.strip().splitlines()
    if len(body_lines) > MAX_LINES:
        body_lines = body_lines[:MAX_LINES]

    final = HEADER + "\n".join(body_lines) + "\n"

    # Atomic write
    tmp = DIGEST_FILE.with_suffix(".md.tmp")
    tmp.write_text(final, encoding="utf-8")
    os.replace(tmp, DIGEST_FILE)

    print(f"[cross-session] digest updated at {ts}", file=sys.stderr)
    # Digest write itself is silent; any user-facing line was printed (gated) above.
    return 0


if __name__ == "__main__":
    sys.exit(main())
