#!/usr/bin/env bash
# Pre-hook for the `metrics-digest` heartbeat task.
#
# Emits metrics_probe history records (data/metrics/*.jsonl, written by
# sources/metrics_probe.py) that are newer than the digest watermark, and
# stages the candidate watermark in .digest_pending.json. The post-hook
# promotes pending → .digest_watermark.json only after a well-formed Claude
# reply, so a failed cycle re-emits the same records instead of silently
# eating the day's card.
#
# No probes configured / nothing new → empty stdout → the heartbeat skips
# the task without spending a Claude call (empty-stdout=skip convention).
set -uo pipefail
export LC_ALL=C

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$JARVIS_DIR" || exit 0

JARVIS_DIR="$JARVIS_DIR" python3 - <<'PYEOF'
import glob
import json
import os

MAX_RECORDS = 12  # bound the prompt; older overflow re-emits next cycle

jd = os.environ.get("JARVIS_DIR", ".")
mdir = os.path.join(jd, "data", "metrics")
wm_file = os.path.join(mdir, ".digest_watermark.json")
pending_file = os.path.join(mdir, ".digest_pending.json")

try:
    watermark = json.load(open(wm_file)).get("ts") or ""
except (OSError, ValueError):
    watermark = ""

records = []
for path in sorted(glob.glob(os.path.join(mdir, "*.jsonl"))):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        continue
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        # ISO-8601 local timestamps from one machine compare correctly as
        # strings; a record without ts is malformed → skip.
        if isinstance(rec, dict) and rec.get("ts", "") > watermark:
            records.append(rec)

if records:
    records.sort(key=lambda r: r.get("ts", ""))
    records = records[:MAX_RECORDS]
    with open(pending_file, "w", encoding="utf-8") as f:
        json.dump({"ts": max(r["ts"] for r in records)}, f)
    print("=== METRICS RECORDS ===")
    for rec in records:
        print(json.dumps(rec, ensure_ascii=False))
PYEOF
