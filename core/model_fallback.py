"""Model-crash / account-limit graceful fallback (REQ-77) + provider gate.

Real sessions died mid-task on "You've hit your monthly spend limit" and
"There's an issue with the selected model (claude-fable-5)", followed by the
harness injecting "Continue from where you left off." → "No response
requested." — an empty death-loop, with the in-flight work lost. Pascal became
the retry button.

This module gives the bot a deterministic fallback: detect a model-unavailable /
spend-limit error in claude's stderr, and pick the next authorized model in a
degrade chain instead of crashing. Detection is pure + testable; bot.sh
consumes the CLI.

Degrade chain: opus → sonnet → haiku. (Fable is banned — never in the chain.)
A HARD account limit is account-wide, so it never degrades within the provider
(2026-07-07 incident: the opus→haiku detour just burned a second doomed call);
callers jump straight to their backup-provider branch instead.

Sticky provider gate (2026-07-07 spend-limit incident): the primary account
hit its monthly limit at 16:10 and every single call — bot replies, heartbeat
cycles, background jobs — re-discovered that from scratch for 6.5h: ~11s of
doomed primary probes per reply, raw error text recorded as assistant turns in
the live session transcript, and Pascal never told. All failover state was
per-call locals. gate()/trip()/clear() persist the outage in
data/provider_state.json (atomic tmp+os.replace under flock, same recipe as
memory.set_fact — bot.sh handlers, heartbeat and jobs are separate processes)
so every caller starts on the backup provider, one elected caller re-probes
primary at most every 30 min, and the first trip pages Pascal once in plain
Chinese through the delivery dead-letter channel (survives a dead loop).
Stdlib-only on purpose; the dead-letter producer is lazily imported and
failure to page never breaks the gate.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

# Ordered capability tiers. A failed model degrades to the NEXT one.
DEGRADE_CHAIN = ["opus", "sonnet", "haiku"]

# Provider-gate state (relative to JARVIS_DIR). Epoch-float fields:
#   spend_limit_since  — set while the primary account is known-exhausted
#   last_primary_probe — last time a caller was elected to try primary
#   notified_at        — last time Pascal was paged (once per episode + 6h
#                        anti-flap across episodes; persisted because
#                        heartbeat restarts must not re-page)
STATE_FILE = "data/provider_state.json"
PROBE_INTERVAL_S = 30 * 60
NOTIFY_COOLDOWN_S = 6 * 3600

# stderr signatures that mean "this model can't serve the request right now"
# (as opposed to a transient network blip, which the normal retry handles).
# Tightened (red-team fix): drop over-broad 'does not support' / 'insufficient' /
# bare 'quota' that could match unrelated error text and falsely degrade.
_MODEL_ERROR = re.compile(
    r"issue with the selected model"
    r"|the selected model"
    r"|model is (currently )?unavailable"
    r"|invalid model"
    r"|model not found"
    r"|model .* (is )?(banned|disabled|deprecated)"
    r"|claude-fable",
    re.IGNORECASE,
)
# HARD account exhaustion vs transient throttling, split on 2026-07-07: the
# old combined _SPEND_ERROR made is_spend_limit() fire on "rate limit / too
# many requests / 429" too, and tripping a 30-min sticky gate on a transient
# 429 would needlessly divert whole processes to the backup provider.
_HARD_SPEND = re.compile(
    r"monthly spend limit|spend limit"
    r"|you(?:'ve| have) (?:hit|reached|exceeded) "
    r"(?:your )?(?:daily|weekly|monthly) "
    r"(?:usage )?limit"
    r"|(?:daily|weekly|monthly) (?:usage )?limit "
    r"(?:has been )?(?:reached|exceeded)"
    r"|usage limit (reached|exceeded)"
    r"|credit balance is too low|insufficient credits",
    re.IGNORECASE)
_SESSION_LIMIT = re.compile(
    r"(?:you(?:'ve| have) hit (?:your )?)?session limit"
    r"|session usage limit (?:reached|exceeded)",
    re.IGNORECASE,
)
_RATE_LIMIT = re.compile(
    r"rate limit (reached|exceeded)|rate_limit|too many requests",
    re.IGNORECASE)


def is_model_error(stderr: str) -> bool:
    """True if stderr indicates the MODEL (not the network) is the problem."""
    if not stderr:
        return False
    return bool(_MODEL_ERROR.search(stderr) or is_account_limit(stderr)
                or _RATE_LIMIT.search(stderr))


def limit_reason(stderr: str) -> str | None:
    """Return the stable reason for a hard account-wide provider limit."""
    if not stderr:
        return None
    if _SESSION_LIMIT.search(stderr):
        return "session_limit"
    if _HARD_SPEND.search(stderr):
        return "spend_limit"
    return None


def is_account_limit(stderr: str) -> bool:
    """True for account-wide limits that require immediate provider failover."""
    return limit_reason(stderr) is not None


def is_account_limit_line(text: str) -> bool:
    """True when an account-limit error starts the supplied output line."""
    if not text:
        return False
    for pattern in (_SESSION_LIMIT, _HARD_SPEND):
        match = pattern.search(text)
        if match and not text[:match.start()].strip():
            return True
    return False


def is_spend_limit(stderr: str) -> bool:
    """Backward-compatible predicate for hard monetary/usage exhaustion."""
    return bool(stderr and _HARD_SPEND.search(stderr))


def _tier(model: str) -> str:
    """Normalize a model id/alias to its chain tier (opus/sonnet/haiku)."""
    m = (model or "").lower()
    for t in DEGRADE_CHAIN:
        if t in m:
            return t
    return "opus"  # unknown/empty → treat as top so we can still degrade


def next_model(current: str) -> str | None:
    """The next model to try after `current` failed, or None if exhausted.

    A spend-limit error should jump straight to the cheapest tier (haiku) —
    degrading opus→sonnet still burns premium quota. Callers pass that intent
    via fallback_for_stderr; next_model alone does the one-step degrade."""
    tier = _tier(current)
    try:
        idx = DEGRADE_CHAIN.index(tier)
    except ValueError:
        idx = 0
    return DEGRADE_CHAIN[idx + 1] if idx + 1 < len(DEGRADE_CHAIN) else None


def fallback_for_stderr(current: str, stderr: str) -> str | None:
    """Given the failed model + its stderr, the model to retry with (or None).

    - HARD account limit → None: the limit is account-wide, so degrading models
      on the SAME provider just burns another doomed call (2026-07-07: every
      reply paid an opus+haiku probe pair). Both consumers fall through to
      their backup-provider branch on None + is_model_error.
    - transient rate limit → cheapest tier (haiku): the throttle may be
      per-model/tier, and haiku is the cheapest way to keep serving
    - model unavailable → one-step degrade
    - not a model error → None (let the normal transient-retry handle it)
    """
    if not is_model_error(stderr):
        return None
    if is_account_limit(stderr):
        return None
    if _RATE_LIMIT.search(stderr):
        cheapest = DEGRADE_CHAIN[-1]
        return cheapest if _tier(current) != cheapest else None
    return next_model(current)


# ── Sticky cross-process provider gate ──────────────────────────────────────

# Pascal-facing text (no jargon, no backup-provider names).
_TRIP_NOTES = {
    "session_limit": (
        "Claude 主通道暂时达到本次会话额度，我已自动切到备用通道。"
        "额度重置后会自动探测并切回。"
    ),
    "spend_limit": (
        "Claude 主通道额度已达到上限，我已自动切到备用通道，功能不受影响。"
        "额度重置或调整后会自动探测并切回。"
    ),
}
_CLEAR_NOTE = "主通道恢复了，已切回。"


def _jarvis_dir(jarvis_dir: str | Path | None = None) -> Path:
    if jarvis_dir:
        return Path(jarvis_dir)
    return Path(os.environ.get("JARVIS_DIR")
                or Path(__file__).resolve().parent.parent)


def _state_path(jarvis_dir: str | Path | None = None) -> Path:
    return _jarvis_dir(jarvis_dir) / STATE_FILE


def _read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    # Atomic tmp+replace so a concurrent gate() never reads a half-written
    # file; callers hold the .lock flock for the read-modify-write.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class _state_lock:
    """flock guard for read-modify-write on the state file (set_fact recipe)."""

    def __init__(self, path: Path):
        self._lock_path = path.with_suffix(".lock")
        self._fh = None

    def __enter__(self):
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX)
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self._fh.close()
        except Exception:
            pass
        return False


def _notify_pascal(jarvis_dir: str | Path | None, detail: str,
                   since_ts: float) -> None:
    """Page Pascal through the delivery dead-letter file (daemon.py consumer
    has its own Claude-independent Lark channel — the whole point during a
    provider outage). Lazily imported + guarded: a page failure must never
    break the gate itself."""
    try:
        from core.delivery_deadletter import record_overdue
        due = time.strftime("%Y-%m-%d %H:%M", time.localtime(since_ts))
        record_overdue(_jarvis_dir(jarvis_dir), "provider_failover", detail, due)
    except Exception:
        pass


def gate(jarvis_dir: str | Path | None = None, probe: bool = True) -> str:
    """Which provider should this call start on?

    - 'primary': no outage flag — normal operation.
    - 'backup': flag set and primary was probed < PROBE_INTERVAL_S ago —
      don't touch primary (every probe writes a junk error turn into live
      session transcripts and costs ~5s).
    - 'probe': flag set and a re-probe is due. The probe stamp is written
      HERE, before returning — this call is thereby elected the single
      prober; concurrent callers keep getting 'backup'. The elected caller
      tries primary ONCE and reports back via clear() (success) or trip()
      (still limited).

    probe=False marks auxiliary callers (background jobs, idle-noise judge)
    that never call clear() on success — letting them win the election would
    burn the probe slot without ever reopening primary.
    """
    path = _state_path(jarvis_dir)
    if not path.exists():
        return "primary"
    with _state_lock(path):
        state = _read_state(path)
        if not state.get("spend_limit_since"):
            return "primary"
        now = time.time()
        if probe and now - float(state.get("last_primary_probe", 0)) >= PROBE_INTERVAL_S:
            state["last_primary_probe"] = now
            _write_state(path, state)
            return "probe"
        return "backup"


def trip(reason: str = "spend_limit",
         jarvis_dir: str | Path | None = None) -> None:
    """Record 'primary is account-limited' — every subsequent gate() answers
    'backup'. Re-tripping (a failed probe) just re-arms the probe timer.
    Pages Pascal ONCE per outage EPISODE (first trip only — a re-trip within
    the same episode never re-pages, however long it lasts: the old 6h
    cadence re-paged him ~4×/day for a month-long spend limit, 2026-07-08
    red-team fix). A NEW episode (fresh spend_limit_since after a clear())
    pages again, but only after NOTIFY_COOLDOWN_S since the last page — a
    trip/clear flap every 30 min must not page each round. The stamp persists
    in the state file: in-memory alert stamps re-page on every restart — the
    52bd63e review flagged exactly that debt."""
    path = _state_path(jarvis_dir)
    now = time.time()
    notify_since = None
    with _state_lock(path):
        state = _read_state(path)
        state.setdefault("spend_limit_since", now)
        state["reason"] = str(reason)[:64]
        state["last_primary_probe"] = now
        notified_at = float(state.get("notified_at", 0) or 0)
        if (notified_at < float(state["spend_limit_since"])
                and now - notified_at >= NOTIFY_COOLDOWN_S):
            state["notified_at"] = now
            notify_since = float(state["spend_limit_since"])
        _write_state(path, state)
    if notify_since is not None:
        _notify_pascal(
            jarvis_dir,
            _TRIP_NOTES.get(reason, _TRIP_NOTES["spend_limit"]),
            notify_since,
        )


def clear(jarvis_dir: str | Path | None = None) -> None:
    """Primary answered while the flag was set — reopen it. Tells Pascal the
    main channel is back, but only if he was told about the failover (a
    cooldown-suppressed flap must not produce a lone '恢复了')."""
    path = _state_path(jarvis_dir)
    if not path.exists():
        return
    notify = False
    with _state_lock(path):
        state = _read_state(path)
        since = float(state.get("spend_limit_since", 0) or 0)
        if not since:
            return
        notified_at = float(state.get("notified_at", 0) or 0)
        notify = notified_at >= since
        # Keep notified_at: it is the 6h page cooldown, and a flapping
        # trip/clear cycle (probe every 30 min) must not page each round.
        _write_state(path, {"notified_at": notified_at})
    if notify:
        _notify_pascal(jarvis_dir, _CLEAR_NOTE, time.time())


if __name__ == "__main__":
    # CLI for bot.sh: args = <current_model> ; stderr text on stdin.
    # Gate verbs: --gate [no-probe] prints primary|backup|probe;
    # --trip [reason] / --clear manage the sticky flag;
    # --limit-reason prints session_limit|spend_limit for account-wide limits.
    # Legacy --is-spend-limit remains for callers that need monetary limits.
    if len(sys.argv) > 1 and sys.argv[1] == "--is-model-error":
        sys.exit(0 if is_model_error(sys.stdin.read()) else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--is-spend-limit":
        sys.exit(0 if is_spend_limit(sys.stdin.read()) else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--is-account-limit":
        sys.exit(0 if is_account_limit(sys.stdin.read()) else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "--limit-reason":
        reason = limit_reason(sys.stdin.read())
        if reason:
            print(reason)
            sys.exit(0)
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "--gate":
        print(gate(probe=(len(sys.argv) < 3 or sys.argv[2] != "no-probe")))
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--trip":
        trip(sys.argv[2] if len(sys.argv) > 2 else "spend_limit")
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        clear()
        sys.exit(0)
    cur = sys.argv[1] if len(sys.argv) > 1 else "opus"
    err = sys.stdin.read()
    nxt = fallback_for_stderr(cur, err)
    print(nxt or "")  # empty line = no fallback (not a model error / exhausted)
