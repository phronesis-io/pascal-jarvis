# Jarvis — Heartbeat Tasks & the pre/post Pattern

How the 38 background behaviours (checkin, calendar sync, EigenFlux,
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

- A normal **pre.sh** gathers bounded context and prints it. A small, explicit
  claimed-work family (`intention-check`, routines, Tier 0 collectors) may
  mutate an inflight/watermark state before execution; those tasks are marked
  pipeline tasks and their post/recovery path must always reconcile the claim.
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
4. Tier 0 work goes directly to its deterministic post-hook. Remaining work is
   grouped by compatible trust/privacy/model policy; GPT and outbound work are
   isolated, while compatible Claude tasks may share one batch.
5. Split the response per task, pipe each into its `*_post.py`.
6. Route post-script stdout: `CARD:`/card-JSON → `lark_send_card`, plain text →
   `lark_send`, raw JSON → **blocked**. Update state.

`daemon.py` is a separate guardian that restarts `bot.sh` if the loop dies or goes
stale. See `docs/concurrency_and_bg_jobs.md` for the three execution lanes.

## Model And Privacy Policy

`HEARTBEAT.md` may declare `model: opus|sonnet|haiku|gpt`. Missing means the
runner default. GPT is a provider route, not a Claude model alias, and always
runs solo. A Claude batch selects the strongest explicitly declared tier in
that batch. Requested lower tiers remain lower tiers through relays.

`untrusted-input: true` disables tools and withholds personal memory. Those
tasks receive only an allowlisted `triage_profile` when relevance context is
needed. `memory-purpose: outbound` removes private inbox buffers and forces an
isolated call. No task may put untrusted external text and private memory in
the same prompt.

The call chain has one wall-clock budget. Provider health is measured by
`provider-canary`; a full task prompt is never reused as a probe. A timeout
after tool access is ambiguous and cannot be replayed.

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
| `memory.py` | Load stable-first tiered memory; indexed warm references are the production default |
| `triage_profile.py` | Sanitized, bounded relevance config for untrusted inputs |
| `memory_relevance.py` | Exact bounded warm evidence for named due intents |
| `change_gate.py` | Digest-only skip gate for unchanged maintenance work |
| `eigenflux_publish_material.py` | New-material gate without persisting candidate prose |
| `heartbeat.py` / `heartbeat_loop.py` | Task scheduling/orchestration vs. the I/O loop |
| `tasks.py` / `intentions.py` | Persistent stores for the task-triage and intent subsystems |

Other `core/` modules (`session`, `prompt`, `actions`, `jobs`, `compact`,
`ef_stream*`) belong to the **interactive** `bot.sh` path, not the heartbeat
tasks, and are not part of this contract.

---

## Task families

| Family | Members | Shares |
|--------|---------|--------|
| Memory upkeep | hourly / daily / weekly / consolidate / tidy | rolling timeline, digest, change gate |
| EigenFlux | inbox reconcile / feed / friends / profile / publish / preinstall | EigenFlux CLI, private approval, material gate |
| Daily rhythm | daily-plan / daily-reflect / activity-log / checkin / morning-anchor / exercise-week | companion budget, cards, rolling logs |
| Intent and task | intention-check / routine-run / weekly-review / delegation-reconcile | claim/reconcile lifecycle, Matters |
| Operations | calendar-sync / perception / metrics / provider-canary / log-maintenance / self-diagnostic | deterministic Tier 0 and bounded alerts |
| Analysis | cross-session / engagement / phronesis / thinking / repos / iteration | private digests and proposal state |

---

## Deliberately NOT done: a `PostScript` base class

A base class could absorb the `sys.path` line and the read-guard boilerplate, but
it would force all ~27 working scripts into a class shape and add a layer of
indirection. For a system maintained by one or two people, the explicit
small-script form reads better and is easier to debug. The chosen lever is
**small composable helpers** (above), adopted incrementally, not a framework.
If a hook ever needs something new, add a tested helper to `core/` rather than a
local copy.
