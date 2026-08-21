"""Local Claude Code usage stats — read-only, numbers-only.

Parses the local Claude Code transcripts under ~/.claude/projects/*/*.jsonl and
aggregates session counts, token consumption (by model / day / project), and an
activity heatmap (hour-of-day x weekday, local time). It reads token/usage
metadata ONLY — no message text is stored or emitted, and nothing is uploaded.

Inspired by the open-source local usage panels (e.g. ccusage): the transcripts
already carry per-assistant-turn `usage` (input/output/cache tokens), a
`timestamp`, `model`, and `sessionId` — everything needed for the numbers.

Performance: 2000+ files / hundreds of MB. We keep a per-file summary cache
keyed by (path, mtime, size) so only new/changed transcripts are re-parsed;
unchanged files are reused. Cold parse is a one-time cost; later loads are cheap.

CLI (ccusage-style terminal view):
    python3 -m core.usage_stats            # overall summary
    python3 -m core.usage_stats --days 14  # daily table for last 14 days
    python3 -m core.usage_stats --rebuild  # ignore cache, full re-parse
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CACHE_PATH = Path.home() / ".claude" / "jarvis_usage_cache.json"
CACHE_VERSION = 1

# In-memory memo so repeated polls don't re-walk on every refresh.
_AGG_MEMO: dict = {"sig": None, "agg": None}


# --------------------------------------------------------------------------- #
# Per-file parsing (numbers only — no message text ever retained)
# --------------------------------------------------------------------------- #
def _parse_file(path: Path) -> dict:
    """Return a numbers-only summary of one transcript. No text retained."""
    sessions: set[str] = set()
    models: dict[str, dict[str, int]] = defaultdict(
        lambda: {"in": 0, "out": 0, "cache_creation": 0, "cache_read": 0}
    )
    by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"turns": 0, "in": 0, "out": 0, "cache_creation": 0, "cache_read": 0}
    )
    heat: dict[str, int] = defaultdict(int)  # "hour,weekday" -> assistant turns
    assistant_turns = 0
    user_msgs = 0
    first_ts = ""
    last_ts = ""

    try:
        fh = path.open("r", encoding="utf-8")
    except OSError:
        return {}
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue

            sid = rec.get("sessionId")
            if sid:
                sessions.add(sid)

            rtype = rec.get("type")
            ts = rec.get("timestamp") or ""
            if ts:
                if not first_ts or ts < first_ts:
                    first_ts = ts
                if ts > last_ts:
                    last_ts = ts

            if rtype == "user":
                user_msgs += 1
                continue
            if rtype != "assistant":
                continue

            msg = rec.get("message")
            usage = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue

            model = (msg.get("model") or "unknown") if isinstance(msg, dict) else "unknown"
            tin = int(usage.get("input_tokens") or 0)
            tout = int(usage.get("output_tokens") or 0)
            tcc = int(usage.get("cache_creation_input_tokens") or 0)
            tcr = int(usage.get("cache_read_input_tokens") or 0)

            assistant_turns += 1
            m = models[model]
            m["in"] += tin
            m["out"] += tout
            m["cache_creation"] += tcc
            m["cache_read"] += tcr

            local = _to_local(ts)
            if local is not None:
                day = local.strftime("%Y-%m-%d")
                d = by_day[day]
                d["turns"] += 1
                d["in"] += tin
                d["out"] += tout
                d["cache_creation"] += tcc
                d["cache_read"] += tcr
                heat[f"{local.hour},{local.weekday()}"] += 1

    return {
        "v": CACHE_VERSION,
        "sessions": sorted(sessions),
        "models": {k: dict(v) for k, v in models.items()},
        "by_day": {k: dict(v) for k, v in by_day.items()},
        "heat": dict(heat),
        "assistant_turns": assistant_turns,
        "user_msgs": user_msgs,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def _to_local(ts: str):
    """Parse an ISO8601 'Z' timestamp into local tz. None on failure."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    try:
        return dt.astimezone()
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# Cache + aggregation
# --------------------------------------------------------------------------- #
def _load_cache() -> dict:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("_v") != CACHE_VERSION:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(files: dict) -> None:
    # Atomic replace: this ~2MB file has concurrent writers by design (every
    # open /usage tab rewarms off-thread every 60s, plus the CLI). A torn
    # write_text would zero the cache and force a multi-second full rebuild
    # on the next page hit.
    try:
        tmp = CACHE_PATH.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps({"_v": CACHE_VERSION, "files": files}), encoding="utf-8"
        )
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _slug_label(slug: str) -> str:
    """Turn a project dir slug into a readable-ish project name."""
    s = slug.lstrip("-")
    for prefix in ("Users-pascal-Desktop-", "Users-pascal-", "private-"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s or slug


def load_aggregate(rebuild: bool = False) -> dict:
    """Walk transcripts (incremental) and return the aggregated stats dict."""
    if not PROJECTS_DIR.exists():
        return _empty_aggregate()

    # Recurse: main-session transcripts live at <slug>/<uuid>.jsonl, but
    # subagent / workflow transcripts are nested under
    # <slug>/<uuid>/subagents/workflows/wf_*/...  — they burn real tokens
    # (a workflow fans out dozens of agents) so they must be counted, but they
    # are not user sessions and are attributed to the top-level project slug.
    files = sorted(PROJECTS_DIR.rglob("*.jsonl"))
    # Cheap signature: count + newest mtime. Skip rework when nothing changed.
    latest = max((f.stat().st_mtime for f in files), default=0.0)
    sig = (len(files), round(latest, 3), rebuild)
    if not rebuild and _AGG_MEMO["sig"] == sig and _AGG_MEMO["agg"] is not None:
        return _AGG_MEMO["agg"]

    cache = {} if rebuild else _load_cache()
    new_cache: dict = {}
    parsed = 0
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        key = str(f)
        try:
            parts = f.relative_to(PROJECTS_DIR).parts
        except ValueError:
            continue
        slug = parts[0] if parts else f.parent.name
        is_sub = "subagents" in parts
        prev = cache.get(key)
        if (
            prev
            and prev.get("mtime") == st.st_mtime
            and prev.get("size") == st.st_size
            and isinstance(prev.get("summary"), dict)
            and prev["summary"].get("v") == CACHE_VERSION
        ):
            summary = prev["summary"]
        else:
            summary = _parse_file(f)
            parsed += 1
        if not summary:
            continue
        new_cache[key] = {
            "mtime": st.st_mtime,
            "size": st.st_size,
            "slug": slug,
            "is_sub": is_sub,
            "summary": summary,
        }

    if parsed or len(new_cache) != len(cache):
        _save_cache(new_cache)

    agg = _aggregate(new_cache)
    agg["files_parsed"] = parsed
    _AGG_MEMO["sig"] = sig
    _AGG_MEMO["agg"] = agg
    return agg


def _empty_aggregate() -> dict:
    return {
        "sessions": 0,
        "assistant_turns": 0,
        "subagent_turns": 0,
        "subagent_tokens": 0,
        "user_msgs": 0,
        "files": 0,
        "tokens": {"in": 0, "out": 0, "cache_creation": 0, "cache_read": 0},
        "by_model": [],
        "by_day": [],
        "by_project": [],
        "heat": [[0] * 24 for _ in range(7)],
        "heat_max": 0,
        "active_days": 0,
        "first_ts": "",
        "last_ts": "",
        "files_parsed": 0,
    }


def _aggregate(cache: dict) -> dict:
    sessions: set[str] = set()
    tokens = {"in": 0, "out": 0, "cache_creation": 0, "cache_read": 0}
    by_model: dict[str, dict[str, int]] = defaultdict(
        lambda: {"in": 0, "out": 0, "cache_creation": 0, "cache_read": 0, "turns": 0}
    )
    by_day: dict[str, dict[str, int]] = defaultdict(
        lambda: {"turns": 0, "in": 0, "out": 0, "cache_creation": 0, "cache_read": 0}
    )
    by_project: dict[str, dict] = defaultdict(
        lambda: {"turns": 0, "in": 0, "out": 0, "cache_read": 0, "cache_creation": 0, "sessions": set()}
    )
    heat = [[0] * 24 for _ in range(7)]  # heat[weekday][hour]
    assistant_turns = 0
    subagent_turns = 0
    subagent_tokens = 0
    user_msgs = 0
    first_ts = ""
    last_ts = ""

    for entry in cache.values():
        s = entry.get("summary") or {}
        slug = entry.get("slug", "?")
        is_sub = bool(entry.get("is_sub"))
        sess = s.get("sessions") or []
        # Only main-session transcripts count as user sessions.
        if not is_sub:
            sessions.update(sess)

        turns = int(s.get("assistant_turns") or 0)
        assistant_turns += turns
        user_msgs += int(s.get("user_msgs") or 0)
        if is_sub:
            subagent_turns += turns

        p = by_project[slug]
        p["sessions"].update(sess)

        for model, mv in (s.get("models") or {}).items():
            tokens["in"] += mv.get("in", 0)
            tokens["out"] += mv.get("out", 0)
            tokens["cache_creation"] += mv.get("cache_creation", 0)
            tokens["cache_read"] += mv.get("cache_read", 0)
            if is_sub:
                subagent_tokens += mv.get("in", 0) + mv.get("out", 0) + mv.get("cache_read", 0)
            bm = by_model[model]
            for k in ("in", "out", "cache_creation", "cache_read"):
                bm[k] += mv.get(k, 0)
            p["in"] += mv.get("in", 0)
            p["out"] += mv.get("out", 0)
            p["cache_read"] += mv.get("cache_read", 0)
            p["cache_creation"] += mv.get("cache_creation", 0)

        for day, dv in (s.get("by_day") or {}).items():
            d = by_day[day]
            for k in ("turns", "in", "out", "cache_creation", "cache_read"):
                d[k] += dv.get(k, 0)

        for day, dv in (s.get("by_day") or {}).items():
            p["turns"] += dv.get("turns", 0)

        for hw, cnt in (s.get("heat") or {}).items():
            try:
                h_str, w_str = hw.split(",")
                h, w = int(h_str), int(w_str)
            except (ValueError, AttributeError):
                continue
            if 0 <= w < 7 and 0 <= h < 24:
                heat[w][h] += cnt

        ft = s.get("first_ts") or ""
        lt = s.get("last_ts") or ""
        if ft and (not first_ts or ft < first_ts):
            first_ts = ft
        if lt > last_ts:
            last_ts = lt

    # turns per model (sum of per-day turns is global; recompute from by_day is
    # not per-model, so derive model turns from token presence is unreliable —
    # instead sum assistant_turns is global). Fill model turns from by_day none;
    # keep model rows without a turns figure being misleading -> drop the field.
    model_rows = sorted(
        (
            {"model": m, **{k: v[k] for k in ("in", "out", "cache_creation", "cache_read")}}
            for m, v in by_model.items()
        ),
        key=lambda r: r["in"] + r["out"] + r["cache_read"],
        reverse=True,
    )
    day_rows = [
        {"day": day, **by_day[day]} for day in sorted(by_day.keys())
    ]
    project_rows = sorted(
        (
            {
                "project": _slug_label(slug),
                "sessions": len(v["sessions"]),
                "turns": v["turns"],
                "in": v["in"],
                "out": v["out"],
                "cache_read": v["cache_read"],
            }
            for slug, v in by_project.items()
        ),
        key=lambda r: r["in"] + r["out"] + r["cache_read"],
        reverse=True,
    )
    heat_max = max((max(row) for row in heat), default=0)

    return {
        "sessions": len(sessions),
        "assistant_turns": assistant_turns,
        "subagent_turns": subagent_turns,
        "subagent_tokens": subagent_tokens,
        "user_msgs": user_msgs,
        "files": len(cache),
        "tokens": tokens,
        "by_model": model_rows,
        "by_day": day_rows,
        "by_project": project_rows,
        "heat": heat,
        "heat_max": heat_max,
        "active_days": len(by_day),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


# --------------------------------------------------------------------------- #
# Formatting helpers (shared with the CLI report)
# --------------------------------------------------------------------------- #
def fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli(argv: list[str]) -> int:
    rebuild = "--rebuild" in argv
    days = 0
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (ValueError, IndexError):
            days = 14

    agg = load_aggregate(rebuild=rebuild)
    t = agg["tokens"]
    print("Claude Code — local usage (read-only, no text stored)")
    print("-" * 56)
    print(f"  transcripts   {agg['files']:>10,}   ({agg['files_parsed']} re-parsed)")
    print(f"  sessions      {agg['sessions']:>10,}   (main only)")
    print(f"  assistant turns {agg['assistant_turns']:>8,}   "
          f"({agg['subagent_turns']:,} from subagents)")
    print(f"  subagent tokens {fmt_tokens(agg['subagent_tokens']):>8}")
    print(f"  active days   {agg['active_days']:>10,}")
    print(f"  input         {fmt_tokens(t['in']):>10}")
    print(f"  output        {fmt_tokens(t['out']):>10}")
    print(f"  cache write   {fmt_tokens(t['cache_creation']):>10}")
    print(f"  cache read    {fmt_tokens(t['cache_read']):>10}")
    if agg["first_ts"]:
        print(f"  span          {agg['first_ts'][:10]} → {agg['last_ts'][:10]}")

    print()
    print("  top models by tokens:")
    for r in agg["by_model"][:6]:
        print(
            f"    {r['model']:<24} in {fmt_tokens(r['in']):>8}  "
            f"out {fmt_tokens(r['out']):>8}  cache-r {fmt_tokens(r['cache_read']):>8}"
        )

    print()
    print("  top projects by tokens:")
    for r in agg["by_project"][:8]:
        print(
            f"    {r['project'][:34]:<34} sess {r['sessions']:>4}  "
            f"turns {r['turns']:>6}  in {fmt_tokens(r['in']):>8}  out {fmt_tokens(r['out']):>8}"
        )

    if days:
        print()
        print(f"  last {days} days:")
        for r in agg["by_day"][-days:]:
            print(
                f"    {r['day']}  turns {r['turns']:>5}  "
                f"in {fmt_tokens(r['in']):>8}  out {fmt_tokens(r['out']):>8}  "
                f"cache-r {fmt_tokens(r['cache_read']):>8}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
