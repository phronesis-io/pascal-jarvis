# Jarvis — Heartbeat Tasks & the pre/post Pattern

How the ~30 background behaviours (checkin, daily plan, calendar sync, eigenflux
feed, memory upkeep …) are structured, and the conventions every task follows so
they stay consistent instead of each re-inventing the same plumbing.

---

## The shape of a task

Each task is a **pair** of files in `tasks/`:

```
tasks/<name>_pre.sh     # GATHER  — read-only, fetch fresh data, print to stdout
tasks/<name>_post.py    # APPLY   — read Claude's reply on stdin, write state, emit card
```

The heartbeat runs them as a pipeline, with Claude in the middle:

```
<name>_pre.sh ──stdout──▶ Claude (HEARTBEAT.md prompt + DATA) ──stdout──▶ <name>_post.py ──stdout──▶ Lark
   (gather)                        (decide / write prose)                      (apply + format)        (user)
```

- **pre.sh** never mutates state. It only collects context (calendar, feed items,
  recent logs) and prints it. Silent on error (empty output, no stderr noise).
- **post.py** is where all side effects live: parse Claude's response, write to
  memory/logs, and decide whether to surface a Lark card or stay silent.
- The two share **no in-process state** — the pipe is the only channel. This makes
  each task idempotent and individually testable (pipe a fixture into the post
  script, assert on stdout + files).

A handful of tasks are **Tier 0** (e.g. `calendar-sync`): the pre-script output is
already the product, so it pipes straight to the post-script with no Claude call.

## Who runs them

`core/heartbeat.py` (`HeartbeatRunner`) is the orchestrator; `core/heartbeat_loop.py`
is the I/O loop that calls it every ~10s and routes output to Lark. Per cycle:

1. Parse `HEARTBEAT.md` (task defs + intervals; cached on mtime).
2. Pick tasks whose interval is due.
3. Run their `*_pre.sh`, collect DATA.
4. One batched Claude call with all due prompts + DATA.
5. Split the response per task, pipe each into its `*_post.py`.
6. Route post-script stdout: `CARD:`/card-JSON → `lark_send_card`, plain text →
   `lark_send`, raw JSON → **blocked**. Update state.

`daemon.py` is a separate guardian that restarts `bot.sh` if the loop dies or goes
stale. See `docs/concurrency_and_bg_jobs.md` for the three execution lanes.

---

## The post-hook contract (and the shared primitives)

Every `*_post.py` does the same four things. Use the shared helpers — **do not
re-handroll them per file.** This is a hard convention: the recurring "raw JSON /
internal field leaked to the user" bugs all came from each hook parsing and
truncating slightly differently.

| Step | Use | Not |
|------|-----|-----|
| **Guard** error/empty/heartbeat output | `core.safety.looks_like_error`, check `HEARTBEAT_OK` | bespoke regexes |
| **Parse** Claude's JSON envelope | `core.safety.parse_json_response(raw) -> dict\|None` | local `extract_json` + `json.loads` + find-`{}` retries |
| **Store** a rolling log | `core.jsonl.read_jsonl / write_jsonl / append_jsonl` | inline read-loop + tmp-rename |
| **Emit** to the user | `core.card.build_card / build_rich_card`, `core.safety.summarize` for the card preview | hand-built card dicts, `splitlines()[:4]` |

Supporting rules baked into those helpers:

- **Never print raw JSON to stdout.** `parse_json_response` returns `None` on
  unparseable input; the caller then either salvages a human field
  (`safety.salvage_field` / `salvage_task_ids`) or suppresses. The heartbeat
  loop also blocks any line that parses as JSON as a backstop.
- **All file writes are atomic** (`safety.atomic_write`, which `core.jsonl` uses):
  the main session and the heartbeat read these files concurrently, so a
  half-written file must never be observable.
- **Time** always via `core.timeutil` (`now_local` / `now_local_str`) — robust to
  TZ-env corruption; never `datetime.now()` directly.

### core/ module map (what a task may lean on)

| Module | Responsibility |
|--------|----------------|
| `safety.py` | Output gate: error detection, JSON parse/salvage, summarize, atomic_write |
| `jsonl.py` | Rolling JSONL store (read/write/append) |
| `card.py` | Lark interactive card JSON (+ `richview.py` for full-page detail links) |
| `timeutil.py` | TZ-robust local time |
| `memory.py` | Load tiered memory (hot/warm/timeline/system) — used by the prompt builder |
| `heartbeat.py` / `heartbeat_loop.py` | Task scheduling/orchestration vs. the I/O loop |
| `tasks.py` / `intentions.py` | Persistent stores for the task-triage and intent subsystems |

Other `core/` modules (`session`, `prompt`, `actions`, `jobs`, `compact`,
`ef_stream*`) belong to the **interactive** `bot.sh` path, not the heartbeat
tasks, and are not part of this contract.

---

## Task families

| Family | Members | Shares |
|--------|---------|--------|
| Memory upkeep | hourly / daily / weekly / monthly / consolidate / tidy | timeline files, archive logic |
| EigenFlux | feed / research / friends / messages / profile / publish | `eigenflux` CLI calls, the publish-confirm flow |
| Daily rhythm | daily-plan / daily-reflect / activity-log / free-time-nudge / checkin | rich cards, JSONL logs |
| Task system | task-triage / weekly-review | `core.tasks` store |
| Standalone | calendar-sync, intentions, content-recommend, cross-session, engagement-analyze, phronesis-monitor, thinking-review, perception-collect | — |

---

## Deliberately NOT done: a `PostScript` base class

A base class could absorb the `sys.path` line and the read-guard boilerplate, but
it would force all ~27 working scripts into a class shape and add a layer of
indirection. For a system maintained by one or two people, the explicit
small-script form reads better and is easier to debug. The chosen lever is
**small composable helpers** (above), adopted incrementally, not a framework.
If a hook ever needs something new, add a tested helper to `core/` rather than a
local copy.
