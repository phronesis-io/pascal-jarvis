"""file_watch adapter — signal on new/changed files matching globs.

Covers Pascal's named ask: "任何 report 的改动" (ship reports, handoffs,
docs). First run baselines silently (records what exists, emits nothing) so
enabling a new watch never floods the inbox with pre-existing files.
"""

from __future__ import annotations

import glob
import hashlib
import os
import time


MAX_SIGNALS_PER_RUN = 20
HEAD_BYTES = 100 * 1024  # hash the first 100KB — enough to detect change


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            h.update(f.read(HEAD_BYTES))
    except OSError:
        return ""
    return h.hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    globs = cfg.get("globs") or []
    known: dict = dict(state.get("files") or {})
    # Explicit flag, not inferred from `known` being empty — a watch whose
    # directory was empty on the first pass would otherwise baseline forever.
    first_run = not state.get("baselined")
    signals = []

    for pattern in globs:
        try:
            matches = glob.glob(os.path.expanduser(pattern), recursive=True)
        except Exception:
            continue
        for path in sorted(matches):
            try:
                if not os.path.isfile(path):
                    continue
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            prev = known.get(path)
            if prev and prev.get("mtime", 0) >= mtime:
                continue
            digest = _file_hash(path)
            if prev and prev.get("hash") == digest:
                known[path] = {"mtime": mtime, "hash": digest}
                continue

            known[path] = {"mtime": mtime, "hash": digest}
            if first_run:
                continue  # baseline pass: record, don't flood
            if len(signals) >= MAX_SIGNALS_PER_RUN:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    body = f.read(2048)
            except OSError:
                body = ""
            name = os.path.basename(path)
            signals.append({
                "event_id": f"{path}:{int(mtime)}",
                "ts": _iso(mtime),
                "title": f"{'新增' if prev is None else '变更'}: {name}",
                "summary": f"文件{'新增' if prev is None else '变更'} {path}",
                "body": body,
                "url": "",
                "actor": {"raw": "", "resolved": ""},
                "payload": {"path": path},
            })

    # Drop tracking for files that vanished (keeps state bounded)
    known = {p: v for p, v in known.items() if os.path.exists(p)}
    return signals, {"files": known, "baselined": True}
