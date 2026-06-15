"""Resolve the `claude` CLI binary path robustly.

PATH alone is unreliable: under launchd the daemon/bot start with a minimal PATH
that omits ~/.local/bin, where the native installer puts `claude`
(~/.local/bin/claude → ~/.local/share/claude/versions/<x>). On 2026-06-15 that
gap left the heartbeat unable to spawn `claude` for ~1h ("Claude CLI not found")
— every heartbeat cycle failed silently until an incidental restart. bot.sh now
prepends ~/.local/bin to PATH, but relying on an inherited PATH is the fragile
part; resolving here severs that dependency at the call site.
"""

import os
import shutil

# Native-installer + common manual locations, tried after PATH resolution.
_FALLBACKS = ("~/.local/bin/claude", "~/.claude/local/claude")


def resolve_claude_bin(configured: str = "") -> str:
    """Return a path to the claude binary, or bare 'claude' if none resolves.

    Order: explicit config path → shutil.which('claude') (this process's PATH)
    → ~/.local/bin/claude → ~/.claude/local/claude. Falling back to bare
    'claude' keeps behavior unchanged when nothing is found (the caller still
    handles FileNotFoundError) — this can only ever help, never hurt.
    """
    candidates = []
    if configured:
        candidates.append(os.path.expanduser(configured))
    found = shutil.which("claude")
    if found:
        candidates.append(found)
    candidates.extend(os.path.expanduser(p) for p in _FALLBACKS)
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return "claude"
