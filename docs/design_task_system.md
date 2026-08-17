# Jarvis Task System — Complete Design

> **Current status (2026-08-17): consolidated and partly stale.** The
> praxis/poiesis vocabulary, capture, decay, and weekly review remain useful.
> Free-time nudges were retired by engagement evidence, and user-visible work
> now follows the Item/Matter/Intent boundaries. Do not recreate a parallel
> task inbox from this historical design. See
> [`prd_portfolio.md`](prd_portfolio.md) and [`../DOMAIN.md`](../DOMAIN.md).

## Philosophy (50 words)

Tasks are commitments to finite time, not obligations to productivity. The system practices 知行合一: a task uncommitted-to is noise; a task stale is a signal about desire. Praxis (becoming) is protected before poiesis (producing). Whitespace is sacred. Explicit rejection is freedom. Decay is mercy.

---

## 1. Data Model

### 1.1 Storage: `$MEMORY_DIR/system/tasks.jsonl`

One JSON object per line. Append-only log; archiver removes completed/decayed items periodically.

```jsonl
{
  "id": "t_20260516_001",
  "title": "Write EigenFlux protocol spec",
  "type": "poiesis",
  "status": "committed",
  "created": "2026-05-16T09:12:00+08:00",
  "committed_date": "2026-05-16",
  "when": "2026-05-16T14:00",
  "due": "2026-05-18",
  "energy": "high",
  "time_est_min": 90,
  "source": "conversation",
  "tags": ["eigenflux", "writing"],
  "decay_touches": 0,
  "last_surfaced": "2026-05-16",
  "resolution": null,
  "resolved_date": null,
  "notes": ""
}
```

### 1.2 Field Definitions

| Field | Type | Description |
|---|---|---|
| `id` | string | `t_YYYYMMDD_NNN` auto-generated |
| `title` | string | Human-readable, under 60 chars |
| `type` | enum | `praxis` (being/becoming: exercise, reading, meditation, relationships) or `poiesis` (making: code, writing, deliverables) |
| `status` | enum | `inbox` → `committed` → `done` / `rejected` / `decayed` |
| `created` | ISO8601 | When captured |
| `committed_date` | date | The day it was accepted into "Today" |
| `when` | ISO8601 or null | Time-bound: when you intend to do it (Sorted3 pattern) |
| `due` | date or null | Hard external deadline (if any) |
| `energy` | enum | `high` / `medium` / `low` — required energy level |
| `time_est_min` | int | Estimated minutes (used for capacity) |
| `source` | enum | `conversation` / `morning_plan` / `calendar` / `lark_task` / `weekly_review` |
| `tags` | list | Freeform, for filtering |
| `decay_touches` | int | Number of times surfaced but not acted on |
| `last_surfaced` | date | Last time shown to user |
| `resolution` | enum or null | `done` / `rejected` / `decayed` / `deferred` |
| `resolved_date` | date or null | When resolved |
| `notes` | string | Context, reason for rejection, etc. |

### 1.3 Strategic Layer: `$MEMORY_DIR/system/todos.md` (unchanged)

The existing todos.md continues as the **project-level** layer (weeks/months horizon). The task system lives below it — daily/weekly execution items that may or may not trace to a project.

### 1.4 Praxis Registry: `$MEMORY_DIR/system/praxis.jsonl`

Recurring praxis items (habits, practices) that get **protected time** automatically.

```jsonl
{
  "id": "px_001",
  "title": "Morning stretching + neck exercises",
  "frequency": "daily",
  "preferred_time": "08:30",
  "duration_min": 20,
  "protect": true,
  "streak_current": 0,
  "streak_best": 0,
  "last_done": null
}
```

These are NOT tasks to "complete" — they are practices to inhabit. The system protects calendar time for them (Reclaim.ai pattern) and never guilt-trips about missed days.

---

## 2. Task Lifecycle

```
CAPTURE → INBOX → TRIAGE → COMMIT → EXECUTE → REFLECT → RESOLVE
                     ↓                              ↓
                  REJECT                     DECAY (auto)
```

### 2.1 Capture

Tasks enter via:
- **Conversation**: User says "I should..." / "Remind me to..." / explicit ask → `[ACTION:task_capture|...]`
- **Morning plan**: Claude proposes items based on calendar + projects
- **Weekly review**: Surfaced from projects/open threads
- **Lark Task sync**: Items created in Lark app (bidirectional)

All captured items start as `status: inbox`.

### 2.2 Triage (morning ritual or explicit)

Each inbox item must be explicitly: **accepted** (→ committed) or **rejected** (→ rejected with reason).

No item may linger in inbox more than 48 hours. After 48h untriaged → auto-surface with the question: "This has been waiting. Accept, reject, or defer to next week?"

Rejection is celebrated, not shameful. Every rejection is an act of freedom — choosing what NOT to be.

### 2.3 Commit

Committing means:
- Assigning a `when` (time-binding)
- Checking capacity (see Section 7)
- The item appears on "Today's surface"

### 2.4 Execute

During the day, the system does NOT nag. It:
- Shows committed items in morning plan
- Notes free blocks where uncommitted time exists
- Trusts the user to act (知行合一: if you truly want it, you'll do it)

### 2.5 Reflect (evening)

Daily-reflect now includes: which committed items were done vs not. Stated neutrally — the gap is data.

### 2.6 Resolve

- **done**: Completed. Celebrated briefly.
- **rejected**: Explicitly said no. Reason recorded. Freedom exercised.
- **decayed**: Surfaced 3+ times without action. System auto-archives with message: "This has been waiting — I'm letting it go. You can always bring it back."
- **deferred**: Pushed to next week (max 2 deferrals before forced triage).

---

## 3. Daily Ritual Flow

### 3.1 Morning (08:00-09:30) — Enhanced daily-plan

The `daily_plan_pre.sh` already runs. Additions:

**Pre-script additions** (`tasks/daily_plan_pre.sh`):
```bash
# ── 6. Task inbox items ──
tasks_file="$MEMORY_DIR/system/tasks.jsonl"
if [ -f "$tasks_file" ]; then
  echo "=== TASK INBOX (needs triage) ==="
  grep '"inbox"' "$tasks_file" | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    age = ''  # compute days since created
    print(f'  - {e[\"title\"]} [{e[\"type\"]}] ({e[\"source\"]})')
" 2>/dev/null
  echo ""
  echo "=== TODAY'S COMMITTED ==="
  grep "\"$(date +%Y-%m-%d)\"" "$tasks_file" | grep '"committed"' | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    when = e.get('when','anytime')
    print(f'  - {when} {e[\"title\"]} (~{e[\"time_est_min\"]}min, {e[\"energy\"]})')
" 2>/dev/null
fi
```

**Prompt additions** to daily-plan:

```
TASK TRIAGE (if inbox items exist):
For each inbox item, present to user with three options:
  "今天做" (commit to today with suggested when)
  "这周" (defer to this week, no specific day)
  "不做" (reject — and that's fine)

CAPACITY CHECK:
Sum time_est_min of committed items. If > 300min (5h productive):
  Gently note: "今天已经排了X小时的事，还有空间吗？"
  Never refuse to add — just surface the reality.

PRAXIS PROTECTION:
Always show praxis items first, as the floor — not as tasks to check off,
but as the ground you stand on today.
```

**What user sees in Lark (morning):**

```
☀️ 5月16日 周五

地面：
  08:30 晨起拉伸 (20min)
  
日程：
  10:00-11:00 鱼刺 sync
  14:00-15:00 投资人 call

待办 (已承诺)：
  → 写 EigenFlux protocol spec (~90min, 需要高能量)
  → 回复陈奇邮件 (~15min)

收件箱：
  • "调研 Cursor 的 agent mode" — 今天做 / 这周 / 不做？

空档：11:00-14:00 (3h), 15:00-18:00 (3h)
总负荷：~125min / 300min 余量充足
```

### 3.2 Evening (21:00-22:30) — Enhanced daily-reflect

**Pre-script additions** (`tasks/daily_reflect_pre.sh`):
```bash
# ── 5. Task resolution ──
echo "=== TASK STATUS ==="
grep "\"$(date +%Y-%m-%d)\"" "$tasks_file" | grep '"committed"' | python3 -c "
import json, sys
for line in sys.stdin:
    e = json.loads(line)
    print(f'  - {e[\"title\"]} (status: {e[\"status\"]})')
" 2>/dev/null
```

**Prompt additions** to daily-reflect:

```
TASK CLOSURE:
For items committed today but not marked done:
- Don't ask "why didn't you do it?" — the gap is data, not failure.
- Simply note: "X 今天没做到，明天继续还是放一放？"
- If user doesn't respond, auto-defer to tomorrow (counts as decay_touch +1).

PRAXIS ACKNOWLEDGMENT:
If praxis items were done, update streak. No fanfare — just quiet continuity.
If missed, say nothing. The streak resets silently.
```

**What user sees in Lark (evening):**

```
今天做了：
  • 10:00 鱼刺 sync
  • 11:30-13:00 写了 protocol spec 初稿
  • 14:00 投资人 call
  • 晨起拉伸 ✓ (第4天)

承诺但没做：
  • 回复陈奇邮件 — 明天继续？

一天辛苦了。
```

---

## 4. Weekly Review Ritual

### 4.1 New Heartbeat Task: `weekly-review`

```markdown
### weekly-review
- interval: 7d
- pre: tasks/weekly_review_pre.sh
- post: tasks/weekly_review_post.py
- prompt: |
    [WEEKLY REVIEW — 周省]
    This is the only moment where the full landscape is visible.
    NOT a performance review. A landscape survey.

    STEPS:
    1. PRAXIS CHECK: Show streaks. No judgment. Pattern only.
       "拉伸做了5/7天，冥想2/7。"

    2. STALE SCAN: Any committed items touched 2+ times without completion?
       Present each with: "还想做吗？要么这周真的排进去，要么放手。"
       (Control filter: is this in your control? Is this truly chosen?)

    3. PROJECT PULSE: For each in-progress project in todos.md,
       one sentence on momentum: moving / stuck / dormant.
       Dormant > 2 weeks: "这个项目沉默了两周。暂停是有意的吗？"

    4. INBOX ZERO: Force-triage any remaining inbox items.
       48h+ items get surfaced. Decision required: commit, defer-one-more-week, or release.

    5. NEXT WEEK LANDSCAPE: Show calendar density.
       If >80% filled: "下周很满，想提前砍掉什么吗？"
       If <40% filled: "下周比较松，有没有什么想主动安排的？"

    6. ONE QUESTION: End with one question that reflects their trajectory.
       Not "what are your goals" but "上周你花最多时间的事，是你真正想做的吗？"
       (Existentialist authenticity check — gentle, not aggressive)

    Tone: wise friend doing a walk together, not a coach with a clipboard.
    Under 200 words Chinese. No emojis except minimal structure markers.

    Return JSON: {
      "user_message": "<markdown>",
      "auto_actions": [
        {"action": "decay", "task_id": "...", "reason": "..."},
        {"action": "defer", "task_id": "...", "to_date": "..."}
      ]
    }
```

### 4.2 `tasks/weekly_review_pre.sh`

```bash
#!/usr/bin/env bash
# Only runs on Sunday 10:00-12:00
MEMORY_DIR="${MEMORY_DIR:-$HOME/.jarvis/memory}"

dow=$(date +%u)  # 7 = Sunday
hour=$(date +%H)
if [ "$dow" -ne 7 ] || [ "$hour" -lt 10 ] || [ "$hour" -ge 12 ]; then
  exit 0
fi

echo "=== WEEKLY REVIEW: $(date '+%Y-%m-%d') ==="

# All tasks
echo "=== ALL ACTIVE TASKS ==="
cat "$MEMORY_DIR/system/tasks.jsonl" 2>/dev/null | grep -v '"done"\|"rejected"\|"decayed"'

echo ""
echo "=== PRAXIS REGISTRY ==="
cat "$MEMORY_DIR/system/praxis.jsonl" 2>/dev/null

echo ""
echo "=== RESOLVED THIS WEEK ==="
# Items resolved in last 7 days
python3 -c "
import json, sys
from datetime import datetime, timedelta
cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
with open('$MEMORY_DIR/system/tasks.jsonl') as f:
    for line in f:
        t = json.loads(line)
        if t.get('resolved_date','') >= cutoff:
            print(f'  {t[\"resolution\"]}: {t[\"title\"]}')
" 2>/dev/null

echo ""
echo "=== PROJECTS (todos.md) ==="
grep -A2 "### " "$MEMORY_DIR/system/todos.md" 2>/dev/null | grep -E "###|状态"

echo ""
echo "=== NEXT WEEK CALENDAR DENSITY ==="
cat "$MEMORY_DIR/hot/calendar_today.md" 2>/dev/null
```

### 4.3 `tasks/weekly_review_post.py`

```python
#!/usr/bin/env python3
"""Post-script: execute auto_actions from weekly review, send message."""
import json, sys, os, subprocess

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except:
    sys.exit(0)

tasks_file = os.path.join(os.environ.get("MEMORY_DIR", os.path.expanduser("~/.jarvis/memory")), "system/tasks.jsonl")

# Apply auto_actions (decay, defer)
if "auto_actions" in data:
    lines = open(tasks_file).readlines() if os.path.exists(tasks_file) else []
    updated = []
    for line in lines:
        t = json.loads(line)
        for action in data["auto_actions"]:
            if t["id"] == action["task_id"]:
                if action["action"] == "decay":
                    t["status"] = "decayed"
                    t["resolution"] = "decayed"
                    t["resolved_date"] = __import__("datetime").date.today().isoformat()
                    t["notes"] = action.get("reason", "")
                elif action["action"] == "defer":
                    t["committed_date"] = action["to_date"]
                    t["decay_touches"] = t.get("decay_touches", 0) + 1
        updated.append(json.dumps(t, ensure_ascii=False) + "\n")
    with open(tasks_file, "w") as f:
        f.writelines(updated)

# Send user message
if data.get("user_message"):
    print(json.dumps({"user_message": data["user_message"]}, ensure_ascii=False))
```

---

## 5. Heartbeat Task Definitions (add to HEARTBEAT.md)

```markdown
### task-triage
- interval: 6h
- pre: tasks/task_triage_pre.sh
- post: tasks/task_triage_post.py
- prompt: |
    [TASK TRIAGE — Stale detection]
    Check for:
    1. Inbox items > 48h old — surface to user for decision
    2. Committed items with decay_touches >= 3 — auto-decay with gentle message
    3. Committed items with when < now (overdue today) — note for evening reflect
    If any items need user attention, compose a brief message.
    Return JSON: {"user_message":"<or empty>","auto_decay":[{"task_id":"...","reason":"..."}]}
    If nothing needs attention: HEARTBEAT_OK

### weekly-review
- interval: 7d
- pre: tasks/weekly_review_pre.sh
- post: tasks/weekly_review_post.py
- prompt: |
    [see Section 4.1 above]
```

---

## 6. Decay/Archive Rules

| Condition | Action | Message |
|---|---|---|
| Inbox item > 48h untriaged | Force-surface | "这个等了两天了，做/不做/下周？" |
| Committed item, `decay_touches >= 3` | Auto-decay | "「X」已经被推了三次。我帮你放下了——随时可以捡回来。" |
| Deferred item, deferred 2+ times | Force-triage at weekly review | "这是第三次推迟了。真心话：还想做吗？" |
| Done/rejected/decayed items > 30 days old | Archive (move to `tasks_archive.jsonl`) | Silent |
| Praxis streak broken > 7 days | Remove from praxis registry | Silent (user can re-add) |

**Decay is mercy, not punishment.** The message tone is: "I'm clearing this so it stops weighing on you." Never: "You failed to do this."

---

## 7. Capacity Model

### 7.1 Daily Budget

- **Max committed poiesis**: 300 minutes (5 hours of productive work)
- **Praxis**: uncapped (but typically 60-90 min/day)
- **Mandatory whitespace**: at least 2 hours unscheduled between 9:00-21:00
- **Calendar events** do NOT count toward the 300min budget (they're fixed commitments)

### 7.2 Guardrail Behavior

When morning plan tries to commit items exceeding budget:

```
已有 280min 的事排着了。再加「写 spec」(90min) 就到 370min。
这样安排也行，只是提醒一下——不是所有事都得今天做。
```

Never blocks. Never refuses. Just surfaces the reality and trusts the user.

### 7.3 Energy Matching

Tasks have energy levels. The system notes (doesn't enforce):
- High-energy tasks → suggest for morning/largest-free-block
- Low-energy tasks → suggest for post-lunch or fragmented slots
- If all committed tasks are "high" energy: "今天全是硬菜，要不要穿插个轻松的？"

---

## 8. ACTION Markers (new/modified)

```
[ACTION:task_capture|title=<title>|type=<praxis|poiesis>|energy=<h|m|l>|est=<min>|due=<date_optional>]
[ACTION:task_commit|id=<task_id>|when=<ISO8601_or_today>]
[ACTION:task_done|id=<task_id>]
[ACTION:task_reject|id=<task_id>|reason=<brief>]
[ACTION:task_defer|id=<task_id>|to=<date>]
[ACTION:praxis_done|id=<praxis_id>]
[ACTION:praxis_add|title=<title>|freq=<daily|weekly>|time=<HH:MM>|dur=<min>]
[ACTION:praxis_remove|id=<praxis_id>]
```

The existing `[ACTION:task_create]` and `[ACTION:task_complete]` continue to work for Lark Task API. The new markers manage the local task system. A sync layer can bridge both if desired.

---

## 9. Integration Points

### 9.1 With daily-plan (morning)

- Pre-script reads `tasks.jsonl` for inbox + today's committed items
- Prompt includes triage for inbox items
- Post-script writes triage decisions back via ACTION markers

### 9.2 With daily-reflect (evening)

- Pre-script reads today's committed items and their completion status
- Prompt includes neutral gap analysis
- Post-script applies `decay_touches += 1` for unresolved items

### 9.3 With calendar-sync

- When a task has `when` set, optionally create a "focus block" in calendar (Reclaim pattern)
- Praxis items with `protect: true` auto-create recurring calendar blocks
- Calendar events can spawn tasks (e.g., "prepare for investor call" 1 day before)

### 9.4 With activity-log

- Activity log entries can auto-mark tasks as done (fuzzy match on title)
- If activity log shows work on a committed task, suggest marking done in evening reflect

### 9.5 With memory-consolidate

- Weekly: committed-but-not-done patterns get noted in `patterns.jsonl`
- Monthly: which types of tasks decay most? (Signal about authentic desire vs. should-do)

### 9.6 With free-time-nudge

- When a free block is detected, the nudge can mention the highest-priority committed-but-not-started task
- Fragmented time (<30min): only suggest low-energy tasks
- Never more than one mention per free block

---

## 10. New Files Required

| File | Purpose |
|---|---|
| `$MEMORY_DIR/system/tasks.jsonl` | Primary task store |
| `$MEMORY_DIR/system/praxis.jsonl` | Recurring praxis registry |
| `$MEMORY_DIR/system/tasks_archive.jsonl` | Resolved items older than 30 days |
| `tasks/task_triage_pre.sh` | Pre-script for stale detection |
| `tasks/task_triage_post.py` | Post-script to apply decay/notify |
| `tasks/weekly_review_pre.sh` | Pre-script gathering full landscape |
| `tasks/weekly_review_post.py` | Post-script applying review decisions |
| `tasks/task_actions.sh` | Handler for new ACTION markers (sourced by bot.sh) |

Modifications to existing files:
- `bot.sh`: Add ACTION handlers for `task_capture`, `task_commit`, `task_done`, `task_reject`, `task_defer`, `praxis_done`, `praxis_add`, `praxis_remove`
- `tasks/daily_plan_pre.sh`: Add sections 6-7 (inbox items, committed items, capacity calc)
- `tasks/daily_reflect_pre.sh`: Add section 5 (task resolution status)
- `tasks/free_time_nudge_pre.sh`: Read committed tasks for suggestion
- `HEARTBEAT.md`: Add `task-triage` and `weekly-review` task definitions

---

## 11. Example Interactions (Lark)

### Capture (conversation)

**User**: 下周要准备 investor deck  
**Jarvis**: 收到。[captured as inbox, type=poiesis, energy=high, est=180min, due=next Friday]

```
记下了：「准备 investor deck」
下周五前要完成，预估3小时，需要高能量状态。
周日 review 时帮你排进去，还是现在就定哪天做？
```

### Morning Triage

```
收件箱有一项等你决定：
  • 调研 Cursor agent mode (来自周二对话)
    → 今天做 / 这周某天 / 不做
```

**User**: 不做了，cursor 那个不急  
**Jarvis**: 

```
好，放掉了。
[ACTION:task_reject|id=t_20260514_003|reason=not urgent, user explicit]
```

### Decay (automatic)

```
「整理旧照片」已经被推了三次了。
我帮你放下了——不是做不到，只是现在不是时候。
随时可以捡回来。
```

### Weekly Review (Sunday morning)

```
── 周省 ──

修行：
  晨起拉伸 5/7 · 冥想 2/7 · 跑步 1/3

这周做了：
  ✓ EigenFlux protocol spec
  ✓ 投资人 deck v1
  ✗ → 放手：整理旧照片 (第3次推迟)

项目脉搏：
  proactive-eval: 在动（本周跑了实验）
  开源 harness: 沉默两周了

下周日程偏满 (6/7天有会)，想提前砍掉什么吗？

一个问题：这周花时间最多的是 EigenFlux 相关——
这是你现在最想推进的方向吗？
```

---

## 12. Implementation Sequence

Suggested build order:

1. **Data layer**: Create `tasks.jsonl`, `praxis.jsonl`, write ACTION handlers in `bot.sh`
2. **Capture flow**: Wire `task_capture` action, test via conversation
3. **Morning integration**: Modify `daily_plan_pre.sh` to read tasks, update prompt
4. **Evening integration**: Modify `daily_reflect_pre.sh`, add closure flow
5. **Decay engine**: Build `task-triage` heartbeat task
6. **Weekly review**: Build full ritual as heartbeat task
7. **Praxis protection**: Calendar block creation for protected habits
8. **Lark Task bridge**: Optional bidirectional sync with Lark Task API

Each step is independently deployable and valuable. No big-bang required.
