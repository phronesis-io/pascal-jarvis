#!/usr/bin/env bash
# Pre-hook for the `pgc-improvement` heartbeat task — Pascal's DAILY PGC pulse.
#
# PGC (eigenflux-pgc on aliap) runs its own daily improvement loop server-side
# (systemd pgc-improvement-loop, 03:00 UTC) that turns the first-source leaderboard
# into a prioritized to-do + a trend ledger. This pre-script just READS those
# products over ssh and emits a compact data block; the prompt turns it into a
# tight daily digest for Pascal (trend + the single highest-leverage action +
# any on-chain wins + any regression). No mutation here. Designed to finish in
# <60s (the heartbeat hard cap): one ssh, a few small file reads.
set -uo pipefail
export LC_ALL=C

# Pipe the reader to the remote python over stdin (no ssh-quote collisions).
OUT="$(ssh -o ConnectTimeout=10 -o BatchMode=yes aliap \
  'cd /data/git/rsshub_crawl && venv/bin/python3 -' 2>/dev/null <<'PYEOF'
import json, glob, sqlite3, datetime as dt, urllib.request

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
cur = led[-1] if led else {}
prev = led[-2] if len(led) >= 2 else {}

def delta(k):
    if cur.get(k) is not None and prev.get(k) is not None:
        return round(cur[k] - prev[k], 2)
    return None

rt = jload(BASE + "/first_source_leaderboard/_realtime/realtime.json", {})

broken = "?"
try:
    body = urllib.request.urlopen("http://127.0.0.1:9090/metrics", timeout=8).read().decode()
    for ln in body.splitlines():
        if ln.startswith("pgc_first_party_feeds_broken_count "):
            broken = ln.split()[1].split(".")[0]
except Exception:
    pass

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

print("=== PGC DAILY PULSE ===")
print(f"date: {cur.get('date', '?')}  (report {il[-1].split('/')[-1] if il else 'none'})")
print(f"win_rate_winnable: {rep.get('win_rate_winnable_pct', '?')}%  (Δ vs prev day: {delta('winnable')})")
print(f"win_rate_overall:  {rep.get('win_rate_overall_pct', '?')}%  (Δ vs prev day: {delta('overall')})")
print(f"realtime(last2d): wins={rt.get('wins', '?')}  late_slower={rt.get('misses_slower', '?')}  bench={rt.get('benchmark_items', '?')}")
print(f"broken_first_party_sources: {broken}  (0 = healthy)")
print(f"on-chain items (24h): {onchain_n}")
for t in onchain_titles:
    print(f"  - {t}")
print("TOP CAPTURE GAPS (no first-party source; add one):")
for c in (rep.get("capture_gaps") or [])[:6]:
    print(f"  - {c.get('domain')}: {c.get('no_first_party')} missed, domain rate {c.get('win_rate_pct')}%")
print("MISSING-EVENT SAMPLES (what to capture):")
for m in (rep.get("missing_event_samples") or [])[:5]:
    print(f"  - [{m.get('benchmark_source')}] {m.get('benchmark')}")
print("TOP SPEED CANDIDATES (auto-verdict: 抓取坏=we can fix · 源端慢=change channel/accept):")
for s in (rep.get("speed_gaps") or [])[:6]:
    print(f"  - {s.get('source')}: late {s.get('count')}x ~{s.get('median_late_hours')}h "
          f"→ [{s.get('verdict','?')}] {s.get('advice','')}")
print("ledger length (days tracked):", len(led))
PYEOF
)"

if [ -z "${OUT//[$'\n\t ']/}" ]; then
  # Unreachable host (no ssh alias / offline / not Pascal's machine): emit
  # NOTHING — empty stdout means the heartbeat skips the task without ever
  # spending a Claude call (empty-stdout=skip convention).
  echo "pgc-improvement: aliap unreachable, skipping" >&2
  exit 0
else
  echo "$OUT"
fi
