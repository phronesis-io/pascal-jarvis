#!/usr/bin/env bash
# PGC daily pulse probe — EXAMPLE. The real one is gitignored personal config
# at data/probes/pgc_pulse.sh; hosts come from PGC_SSH_HOST or your edit.
# Migrated from tasks/pgc_improvement_pre.sh to the metrics_probe command
# contract: reads the server-side improvement-loop products over ssh and
# prints {"metrics": {...}, "details": "..."} — scalars in metrics (the
# adapter computes day-over-day deltas), LLM-digest lists in details.
# Unreachable host / bad output → non-zero exit (adapter records error_type).
set -uo pipefail
export LC_ALL=C

# Any ssh/host failure → non-zero exit (adapter: error_type=network) — that
# path was never the problem; the blind spot was a *successful* run whose
# metrics fetch failed silently. See `errors` below.
OUT="$(ssh -o ConnectTimeout=10 -o BatchMode=yes "${PGC_SSH_HOST:-your-crawl-host}" \
  'cd /data/git/rsshub_crawl && venv/bin/python3 -' 2>/dev/null <<'PYEOF'
import datetime as dt
import glob
import json
import os
import sqlite3
import urllib.request

BASE = "/data/git/rsshub_crawl/data"

def jload(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default

il = sorted(glob.glob(BASE + "/improvement_loop/*.json"))
rep = jload(il[-1], {}) if il else {}

led = []
try:
    for line in open(BASE + "/improvement_loop/ledger.jsonl"):
        line = line.strip()
        if line:
            led.append(json.loads(line))
except Exception:
    pass

# realtime.json lost its writer around 2026-06-22; the file lingers on disk, so
# a plain read republishes two-month-old wins/bench every day as a stable "+0".
# Gate on file age: stale → drop the three scalars and expose the staleness
# itself, so a dead measurement reads as dead instead of as green.
_RT_PATH = BASE + "/first_source_leaderboard/_realtime/realtime.json"
_RT_MAX_AGE_DAYS = 2
rt_stale_days = None
try:
    _age = (dt.datetime.now().timestamp() - os.path.getmtime(_RT_PATH)) / 86400.0
    rt_stale_days = round(_age, 1)
    rt = jload(_RT_PATH, {}) if _age <= _RT_MAX_AGE_DAYS else {}
except Exception:
    rt = {}

broken = None
broken_names = []
errors = {}


def _env_value(paths, key):
    """KEY=VALUE from dotenv / systemd Environment= files, never sourced,
    never printed. Unreadable files are simply skipped."""
    for path in paths:
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("Environment="):
                line = line[len("Environment="):].strip()
            if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
                line = line[1:-1]
            if line.startswith(key + "="):
                v = line[len(key) + 1:].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                return v
    return ""


def _listeners(port):
    """Addresses listening on `port` per ss — how we find the viewer's
    bind host without reading root-only config."""
    import re as _re
    import subprocess as _sp
    try:
        out = _sp.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        m = _re.match(r"^\[?([^\]]*)\]?:(\d+)$", parts[3])
        if not m or m.group(2) != str(port):
            continue
        host = m.group(1)
        if host in ("", "*", "0.0.0.0", "::"):
            host = "127.0.0.1"
        if host not in found:
            found.append(host)
    return found


def _fetch_metrics():
    """The viewer binds PGC_VIEWER_BIND_HOST (moved off 127.0.0.1 in the
    2026-08-25 hardening; this probe kept asking 127.0.0.1 and reported
    broken_first_party=None for six days). /metrics is auth-exempt in the
    viewer, but if the credentials file is readable we send them anyway.
    Returns (body, error_text)."""
    import base64
    env_paths = ["/etc/pgc/viewer.env", "/data/git/rsshub_crawl/.env"]
    port = 9090
    hosts = []
    bind = _env_value(env_paths, "PGC_VIEWER_BIND_HOST")
    for h in ([bind] if bind else []) + _listeners(port) + ["127.0.0.1"]:
        if h not in hosts:
            hosts.append(h)
    headers = {}
    user = _env_value(env_paths, "PGC_VIEWER_BASIC_USER")
    if user:
        token = base64.b64encode(
            f"{user}:{_env_value(env_paths, 'PGC_VIEWER_BASIC_PASSWORD')}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    last_err = "no listener on port %d" % port
    for h in hosts:
        url = f"http://{h}:{port}/metrics"
        try:
            req = urllib.request.Request(url, headers=headers)
            body = urllib.request.urlopen(req, timeout=8).read().decode()
        except Exception as e:  # noqa: BLE001
            last_err = f"{h}: {type(e).__name__}: {str(e)[:80]}"
            continue
        if "pgc_first_party_feeds_broken_count" in body:
            return body, ""
        last_err = f"{h}: no pgc metrics in body"
    return "", last_err


try:
    import re
    body, fetch_err = _fetch_metrics()
    if fetch_err:
        raise RuntimeError(fetch_err)
    # The old pgc_first_party_feed_broken{error=...} label is split on ':' and
    # cut at 30 chars upstream, so an SSL error ending in "_ssl.c:1000)" renders
    # as junk like "(1000)'))))". Since 2026-07-31 the owner source-health gauge
    # carries the same errors readable (240 chars, unsplit) — prefer it, and
    # fall back to the old label when a source has no row there.
    detail_err = {}
    pending = []
    for ln in body.splitlines():
        if ln.startswith("pgc_first_party_feeds_broken_count "):
            broken = int(float(ln.split()[1]))
        elif ln.startswith("pgc_source_health_problem_source_info{"):
            m_src = re.search(r'source="([^"]*)"', ln)
            m_err = re.search(r'last_error="([^"]*)"', ln)
            if m_src and m_err and m_err.group(1).strip():
                detail_err.setdefault(m_src.group(1), m_err.group(1).strip()[:160])
        elif ln.startswith("pgc_first_party_feed_broken{"):
            # pgc_first_party_feed_broken{error="HTTP 403",source="X"} 1.0
            m_src = re.search(r'source="([^"]*)"', ln)
            m_err = re.search(r'error="([^"]*)"', ln)
            if m_src:
                pending.append((m_src.group(1),
                                m_err.group(1) if m_err else ""))
    broken_names = [f"{s} ({detail_err.get(s) or e or '?'})" for s, e in pending]
    if broken is None:
        raise RuntimeError("pgc_first_party_feeds_broken_count missing from /metrics")
except Exception as e:  # noqa: BLE001
    # Say it out loud: the adapter turns `errors` into a「失明」state flip.
    errors["metrics"] = f"{type(e).__name__}: {str(e)[:160]}" if not isinstance(e, RuntimeError) else str(e)[:200]

onchain_n, onchain_titles = 0, []
try:
    d = sqlite3.connect("/data/git/rsshub_crawl/pgc_items.db")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    onchain_titles = [r[0][:70] for r in d.execute(
        "SELECT title FROM items WHERE source LIKE '%稳定币%' AND indexed_at > ? "
        "ORDER BY indexed_at DESC LIMIT 5", (since,)).fetchall()]
    onchain_n = d.execute("SELECT COUNT(*) FROM items WHERE source LIKE '%稳定币%' AND indexed_at > ?",
                          (since,)).fetchone()[0]
except Exception:
    pass

metrics = {}
def put(k, v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        metrics[k] = v

# win_rate_winnable retired server-side by eigenflux-pgc #114 ("退役从来没算出
# 来过的「可赢胜率」死指标"), but the report still emits the field as a constant
# 0.0. Republishing it here just pads the panel with a metric its own owner
# already declared dead.
put("win_rate_overall", rep.get("win_rate_overall_pct"))
put("realtime_wins", rt.get("wins"))
put("realtime_misses_slower", rt.get("misses_slower"))
put("realtime_bench", rt.get("benchmark_items"))
put("realtime_stale_days", rt_stale_days)
put("broken_first_party", broken)
put("onchain_24h", onchain_n)
put("ledger_days", len(led))

lines = [f"report: {il[-1].split('/')[-1] if il else 'none'}  "
         f"date: {(led[-1] if led else {}).get('date', '?')}"]
if broken_names:
    lines.append("BROKEN FIRST-PARTY (sustained >=2h, name: error):")
    lines += [f"  - {n}" for n in broken_names]
if onchain_titles:
    lines.append("ON-CHAIN ITEMS (24h):")
    lines += [f"  - {t}" for t in onchain_titles]
lines.append("TOP CAPTURE GAPS (no first-party source; add one):")
for c in (rep.get("capture_gaps") or [])[:6]:
    lines.append(f"  - {c.get('domain')}: {c.get('no_first_party')} missed, "
                 f"domain rate {c.get('win_rate_pct')}%")
lines.append("MISSING-EVENT SAMPLES (what to capture):")
for m in (rep.get("missing_event_samples") or [])[:5]:
    lines.append(f"  - [{m.get('benchmark_source')}] {m.get('benchmark')}")
lines.append("TOP SPEED CANDIDATES (抓取坏=we can fix · 源端慢=change channel/accept):")
for s in (rep.get("speed_gaps") or [])[:6]:
    lines.append(f"  - {s.get('source')}: late {s.get('count')}x ~{s.get('median_late_hours')}h "
                 f"→ [{s.get('verdict','?')}] {s.get('advice','')}")

payload = {"metrics": metrics, "details": "\n".join(lines)}
if errors:
    payload["errors"] = errors
print(json.dumps(payload, ensure_ascii=False))
PYEOF
)" || exit 3

case "$OUT" in
  "{"*) printf '%s\n' "$OUT" ;;
  *) exit 3 ;;
esac
