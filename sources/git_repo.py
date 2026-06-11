"""git_repo adapter — signal on new local commits across a repos directory.

Read-only `git log` over already-fetched history; network fetching stays
with the existing repos-sync task (no duplicate fetch traffic). Signals are
per-commit, capped per run.
"""

from __future__ import annotations

import os
import subprocess
import time


MAX_SIGNALS_PER_RUN = 20
FIRST_RUN_LOOKBACK_HOURS = 24


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    repos_dir = os.path.expanduser(cfg.get("repos_dir") or ".")
    exclude = set(cfg.get("exclude") or [])
    # 120s overlap on the cursor: a commit landing in the same second as a
    # collection pass must not be missed forever. The overlap can re-emit a
    # commit — event_id (repo@sha) is stable, so the runtime seen-store drops
    # the duplicate (the adapter contract's intended reconciliation).
    last = state.get("last_epoch")
    since_epoch = (last - 120) if last else (time.time() - FIRST_RUN_LOOKBACK_HOURS * 3600)
    now = time.time()
    signals = []

    if not os.path.isdir(repos_dir):
        return [], {**state, "error_type": "crash"}

    for name in sorted(os.listdir(repos_dir)):
        repo = os.path.join(repos_dir, name)
        if name in exclude or not os.path.isdir(os.path.join(repo, ".git")):
            continue
        since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since_epoch))
        try:
            # ISO --since: git's approxidate handling of a bare epoch integer
            # is unreliable (matched everything in testing)
            r = subprocess.run(
                ["git", "-C", repo, "log", "--all", f"--since={since_iso}",
                 "--date=unix", "--pretty=%H%x1f%an%x1f%ad%x1f%s"],
                capture_output=True, text=True, timeout=15,
            )
        except subprocess.TimeoutExpired:
            return signals, {**state, "error_type": "timeout"}
        except Exception:
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 4 or len(signals) >= MAX_SIGNALS_PER_RUN:
                continue
            sha, author, date_unix, subject = parts
            try:
                ts = float(date_unix)
            except ValueError:
                ts = now
            if ts < since_epoch:
                continue
            signals.append({
                "event_id": f"{name}@{sha[:12]}",
                "ts": _iso(ts),
                "title": f"[{name}] {subject[:120]}",
                "summary": f"{author} committed to {name}: {subject[:80]}",
                "body": "",
                "url": "",
                "actor": {"raw": author, "resolved": author},
                "payload": {"repo": name, "sha": sha},
            })

    return signals, {"last_epoch": int(now), "error_type": None}
