"""Model-crash / spend-limit graceful fallback (REQ-77).

Real sessions died mid-task on "You've hit your monthly spend limit" and
"There's an issue with the selected model (claude-fable-5)", followed by the
harness injecting "Continue from where you left off." → "No response
requested." — an empty death-loop, with the in-flight work lost. Pascal became
the retry button.

This module gives the bot a deterministic fallback: detect a model-unavailable /
spend-limit error in claude's stderr, and pick the next authorized model in a
degrade chain instead of crashing. Pure + testable; bot.sh consumes the CLI.

Degrade chain: opus → sonnet → haiku. (Fable is banned — never in the chain.)
"""

from __future__ import annotations

import re
import sys

# Ordered capability tiers. A failed model degrades to the NEXT one.
DEGRADE_CHAIN = ["opus", "sonnet", "haiku"]

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
_SPEND_ERROR = re.compile(
    r"monthly spend limit|spend limit|usage limit (reached|exceeded)"
    r"|credit balance is too low|insufficient credits",
    re.IGNORECASE)


def is_model_error(stderr: str) -> bool:
    """True if stderr indicates the MODEL (not the network) is the problem."""
    if not stderr:
        return False
    return bool(_MODEL_ERROR.search(stderr) or _SPEND_ERROR.search(stderr))


def is_spend_limit(stderr: str) -> bool:
    return bool(stderr and _SPEND_ERROR.search(stderr))


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

    - spend limit → cheapest tier (haiku) immediately (don't keep burning quota)
    - model unavailable → one-step degrade
    - not a model error → None (let the normal transient-retry handle it)
    """
    if not is_model_error(stderr):
        return None
    if is_spend_limit(stderr):
        cheapest = DEGRADE_CHAIN[-1]
        return cheapest if _tier(current) != cheapest else None
    return next_model(current)


if __name__ == "__main__":
    # CLI for bot.sh: args = <current_model> ; stderr text on stdin.
    cur = sys.argv[1] if len(sys.argv) > 1 else "opus"
    err = sys.stdin.read()
    nxt = fallback_for_stderr(cur, err)
    print(nxt or "")  # empty line = no fallback (not a model error / exhausted)
