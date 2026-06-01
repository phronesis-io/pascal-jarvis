# Jarvis — Concurrency & Background Jobs

How Jarvis appears to "talk to you and run long tasks at the same time" without
ever losing conversational memory. The short version: **it never runs the same
conversation in parallel.** Apparent parallelism comes from three physically
separate execution lanes.

---

## The core tension

A naive design would fork a new process per incoming message to reply faster.
But each Claude reply must `--resume` the conversation's session file to keep
full memory. If two processes resume the *same* session file concurrently, they
race on it and corrupt/lose history. So the rule is absolute:

> **One conversation = one session file = at most one Claude process at a time.**

Everything below is built to honor that rule while still feeling responsive.

---

## The three lanes

| Lane | What runs here | Parallel? | Shares chat memory? | Mechanism |
|------|----------------|-----------|---------------------|-----------|
| 1. Chat | Your messages in a Lark conversation | No — serialized | Yes (full) | session lock |
| 2. Background jobs | Explicit long tasks (`[ACTION:bg]`) | **Yes** | No (own snapshot) | `bg-${job_id}` session |
| 3. Daemons | Heartbeat, EigenFlux stream | Yes (always-on) | No | separate Python procs |

---

## Lane 1 — Chat: "instant ack, serial execution"

`bot.sh` main loop, per incoming message (`bot.sh:~1282`):

1. Add a "Typing" reaction so you *feel* it was received.
2. `handle_message ... &` — dispatch to a **background subshell** and the main
   loop *immediately* returns to read your next message (`bot.sh:1294`).

So if you fire 3 messages, all 3 are "accepted" within seconds — this is the
source of the *illusion* of simultaneous replies. But execution is **not**
parallel:

- All messages in one Lark conversation map to the same `conv_key`, which maps
  to the same `session_id` (`get_session_id`, `bot.sh:192`) — i.e. the same
  Claude memory file.
- Before running Claude, each handler must acquire a per-session lock file
  `.session_lock_${session_id}` (`bot.sh:475`). If a previous handler holds it,
  the new one **sleeps and waits** (up to 620s).
- When the holder finishes and releases the lock, the next handler runs with
  `claude --resume $session_id` — so it sees *everything* the previous one did.

Net effect: same-conversation messages execute **strictly serially, in order,
each carrying full memory.** That serialization is exactly what protects memory.

A global cap (`MAX_HANDLERS=5`, `bot.sh:165`) bounds how many *distinct*
conversations can be in-flight at once; it's measured by counting live
`.session_lock_*` files (`bot.sh:1289`).

### Honest limitation

Because Lane 1 is serial, **a long task run *inside* a chat turn blocks your
next quick message** until it finishes (or the 620s lock-wait elapses). The
escape hatch is Lane 2.

---

## Lane 2 — Background jobs: true parallelism, isolated memory

When a task is genuinely long-running, it should not occupy the chat session.
Emitting `[ACTION:bg|prompt=...]` routes it to `run_background_job`
(`bot.sh:696`), which is the only place real parallelism with the chat happens.

Design (`core/jobs.py` + `run_background_job`):

- **Isolated session.** Each job gets a brand-new session id `bg-${job_id}` and
  its own directory (`output.md`, `log.txt`) — physically separate from your
  chat session. This is the root of "run in parallel without losing memory":
  the job doesn't *need* conversational continuity, so there's nothing to lose.
- **Memory snapshot at launch.** The job's system prompt embeds a one-time
  `load_memory()` snapshot (`bot.sh:702`). It runs self-contained; it does not
  participate in the live back-and-forth.
- **File-locked registry.** Job state lives in `jobs/registry.json`, guarded by
  an exclusive `fcntl.flock` (same pattern as `session.py`) so concurrent jobs
  never clobber each other's status.
- **Atomic writes.** Registry updates write `*.tmp.{pid}` then `os.replace` —
  the file is never observed half-written or corrupted.
- **Killable.** The real PID is recorded; cancel kills the whole process group
  (`os.killpg(SIGTERM)`), so a runaway hour-long job can be stopped cleanly.
- **Reports back, doesn't interrupt.** On completion it flips the registry to
  `completed`/`failed` and pushes a result **card** to you. You pull full output
  later via `[ACTION:job_output|id=...]`.

Lifecycle:

```
create_job(status=running)
  -> spawn independent `claude -p --session-id bg-${job_id}` (1h timeout)
  -> set-pid in registry
  -> wait
  -> finish_job(completed|failed)
  -> notify card
```

This is why "two big tasks are running and I can still chat": those tasks are in
**different processes, different sessions** — they physically cannot block the
chat lock.

---

## Lane 3 — Daemons

Two always-on Python processes, launched once at boot, independent of both
lanes above:

- **Heartbeat loop** (`bot.sh:291` → `core/heartbeat_loop.py`) — periodic
  proactive tasks (checkins, calendar sync, feed triage, memory consolidation).
- **EigenFlux stream** (`bot.sh:298` → `core/ef_stream_loop.py`) — real-time
  inbound signal/message stream.

Neither touches the chat session lock, so they run concurrently with everything.

---

## Decision guide: which lane?

- A reply that needs the conversation's context and finishes quickly → **Lane 1**
  (just answer; the lock handles ordering).
- A task that takes minutes–hours and is self-describing (you can hand it a
  prompt and walk away) → **Lane 2** (`[ACTION:bg]`); keep chatting meanwhile.
- Recurring, system-level, no user prompt → **Lane 3** (heartbeat task).

Rule of thumb: if you'd otherwise be tempted to make Lane 1 parallel "to stay
responsive," that's the signal the work belongs in Lane 2 instead.

---

## Failure modes & guards

- **Stale lock after a crash/restart:** locks are cleared on startup
  (`bot.sh:173`); a handler waiting >620s force-clears a presumed-dead lock
  (`bot.sh:484`).
- **Restart drops in-flight handlers:** the user is told which messages were
  interrupted so they can resend (`bot.sh:282`).
- **Empty Claude response:** handler auto-retries once (`bot.sh:_attempt 1 2`).
- **Runaway Claude call:** a watchdog kills it after 600s.
- **bg job cancelled mid-run:** registry status is checked post-wait so a
  cancelled job isn't reported as completed.

---

*Source of truth is the code; line numbers are pointers as of 2026-05-31 and may
drift. Grep for `handle_message`, `.session_lock_`, `run_background_job`, and
`core/jobs.py` to re-anchor.*
