#!/usr/bin/env bash
# Emit shell variables from jarvis.yaml schedule/thresholds sections.
# Usage in pre-scripts:
#   eval $(bash "$JARVIS_DIR/scripts/config_env.sh")
# Then use: $WORK_START, $WORK_END, $FREE_BLOCK_MIN, etc.

JARVIS_DIR="${JARVIS_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

python3 -c "
import sys, os, shlex
sys.path.insert(0, os.environ.get('JARVIS_DIR', '$JARVIS_DIR'))
from core.config import Config
c = Config(os.path.join('$JARVIS_DIR', 'jarvis.yaml'))
s = c.schedule
t = c.thresholds
wh = s.get('working_hours', {})
print(f'WORK_START={shlex.quote(str(wh.get(\"start\", 9)))}')
print(f'WORK_END={shlex.quote(str(wh.get(\"end\", 22)))}')
dp = s.get('daily_plan_window', {})
print(f'PLAN_START={shlex.quote(str(dp.get(\"start\", \"08:00\")))}')
print(f'PLAN_END={shlex.quote(str(dp.get(\"end\", \"09:30\")))}')
dr = s.get('daily_reflect_window', {})
print(f'REFLECT_START_HOUR={shlex.quote(str(int(dr.get(\"start\", \"20:30\").split(\":\")[0])))}')
print(f'REFLECT_START_MIN={shlex.quote(str(int(dr.get(\"start\", \"20:30\").split(\":\")[1])))}')
print(f'REFLECT_END_HOUR={shlex.quote(str(int(dr.get(\"end\", \"23:30\").split(\":\")[0])))}')
print(f'REFLECT_END_MIN={shlex.quote(str(int(dr.get(\"end\", \"23:30\").split(\":\")[1])))}')
wr = s.get('weekly_review', {})
print(f'REVIEW_DOW={shlex.quote(str(wr.get(\"day\", 7)))}')
print(f'REVIEW_START={shlex.quote(str(wr.get(\"start\", 10)))}')
print(f'REVIEW_END={shlex.quote(str(wr.get(\"end\", 12)))}')
print(f'FREE_BLOCK_MIN={shlex.quote(str(t.get(\"free_block_min_minutes\", 30)))}')
print(f'NUDGE_MAX={shlex.quote(str(t.get(\"nudge_max_per_day\", 2)))}')
print(f'CHECKIN_TRANSITION_MIN={shlex.quote(str(t.get(\"checkin_transition_minutes\", 15)))}')
print(f'MAX_BATCH_SIZE={shlex.quote(str(t.get(\"max_batch_size\", 4)))}')
" 2>/dev/null
