# Jarvis - Concurrency, Sessions, and Background Jobs

> Current contract as of 2026-08-17. This document covers Claude-compatible,
> Codex CLI, and GPT API routes. Provider choice does not change the session,
> ownership, or replay-safety rules below.

Jarvis can keep a Lark conversation responsive while long work continues, but
it never lets two provider processes mutate the same physical provider session
at once. Parallelism comes from separate execution lanes and explicit logical
context boundaries.

---

## The core tension

A naive design would fork a provider process for every incoming message. But a
Claude-compatible route may resume a transcript, and Codex may resume its own
durable thread. Concurrently resuming either physical session can corrupt or
misorder history. The invariant is:

> **One physical provider session or thread has at most one provider process at
> a time.**

A Lark conversation is not itself the durable memory boundary. It points to a
logical context (`conversation:*` or `matter:*`), and each provider keeps its
own physical session identity underneath that context. Provider changes do not
merge raw transcripts; continuity is projected through bounded recent turns,
pending results, Matter evidence, and the cross-session index.

---

## The three lanes

| Lane | What runs here | Parallel? | Context boundary | Mechanism |
|---|---|---|---|---|
| 1. Owner/shared chat | Lark messages | Serialized per physical session; distinct conversations may run concurrently | Bound logical context plus provider session | ownership-token session lock |
| 2. Background jobs | Explicit jobs and auto-promoted long turns | Yes | Captured logical context and independently registered provider session(s) | job registry, process identity, pending merge |
| 3. Daemons | Heartbeat, Guardian, EigenFlux stream, Admin | Yes | No foreground provider session | separate supervised processes |

---

## Lane 1 — Chat: "instant ack, serial execution"

The `bot.sh` event loop acknowledges each incoming message and dispatches a
handler process, subject to a global handler cap. Handlers for different
conversations can run concurrently. A handler for the same physical session
must first acquire `.session_lock_<session_id>` atomically.

The lock contains more than a PID: it carries a process-start identity and an
owner token. Destructive operations verify that identity and token, preventing
a stale or recycled PID from releasing another handler's lock. The active
provider wrapper PID is published while work runs, so stop/cancel can target the
correct process group.

While waiting for the lock, a handler re-reads the conversation tracker. If a
Matter switch, session reset, or background promotion changed the logical
context, the queued turn is rejected instead of executing in the wrong
context. If only the physical session rotated within the same logical context,
the handler moves to the new session and rebuilds its prompt.

The route can be a Claude-compatible account, owner-private Codex CLI, or the
OpenAI-compatible fallback. `core.model_control` chooses the ordered route;
`core.runtime_provider` stores the owner preference. The lock protects the
turn across route selection. A provider failure may be replayed only when the
boundary proves no side effect could have occurred. Ambiguous tool-capable
failures stop rather than silently rerun on another provider.

Net effect: same-session turns are serialized, different conversations can run
in parallel, and a provider switch does not redefine conversational truth.

A global `MAX_HANDLERS` cap bounds concurrent foreground handlers. The source
of truth is the current `bot.sh` configuration and its dispatch-marker/session-
lock registries; do not copy an old numeric value from this document.

### Honest limitation

Because Lane 1 is serial per physical session, a long turn initially blocks the
next same-session message. Explicit background work starts in Lane 2; an
eligible owner-private foreground turn is also auto-promoted after the bounded
threshold. Group turns are deliberately not auto-promoted because a background
job with broader tools must never inherit authority from untrusted chat.

---

## Lane 2 — Background jobs: true parallelism, isolated memory

When a task is genuinely long-running, it should not occupy the active chat
session. `[ACTION:bg|prompt=...]` creates a registered job. An owner-private
turn that runs beyond the promotion threshold can be re-homed into that same
job lifecycle.

Design (`core/jobs.py` + `run_background_job`):

- **Captured scope.** The registry records `conv_key`, `context_key`, optional
  `matter_id`, and `source_session_id` before work starts.
- **Registered physical sessions.** Every provider session used by the job is
  recorded. Jarvis-owned sessions are excluded from interactive cross-session
  ingestion.
- **Context without transcript collision.** A background job may fork from the
  captured Claude transcript, but runs under a new valid session UUID. Other
  provider routes receive the bounded system prompt and context supported by
  their adapters.
- **File-locked registry.** Job state lives in `jobs/registry.json`, guarded by
  an exclusive `fcntl.flock`; updates use atomic replacement.
- **Atomic writes.** Registry updates write `*.tmp.{pid}` then `os.replace` —
  the file is never observed half-written or corrupted.
- **Killable with PID-reuse protection.** The runner records PID plus process
  start identity. Cancel signals only the matching process group.
- **Scoped result merge.** A successful result is queued in
  `jobs/pending_merge.jsonl` against the captured logical context. It cannot be
  injected after a reset or into a different Matter.
- **Durable completion.** Terminal state and output are retained; a Lark card
  is a notification, not the only copy of the result. A sweeper marks abandoned
  jobs lost instead of leaving them running forever.

Lifecycle:

```
create_job(status=running, captured context)
  -> register provider session(s)
  -> spawn through core.aux_model in its own process group
  -> persist PID + process-start identity
  -> finish_job(completed|failed|cancelled|lost)
  -> queue context-scoped merge when completed
  -> notify through unified Delivery
```

This is why two large tasks can run while chat remains available: each job has
an independent process and provider session, while its result can return only
to the logical context captured at launch.

---

## Lane 3 — Daemons

Supervised background components are independent of foreground sessions:

- **Heartbeat loop** (`core.heartbeat_loop`) runs periodic proactive tasks,
  Routines, memory consolidation, and L3 observation.
- **Guardian/daemon** detects process and channel failures and performs bounded
  recovery.
- **EigenFlux stream** (`core.ef_stream_loop`) receives real-time network
  messages with durable cursor and Delivery boundaries.
- **Admin** (`:3456`) is an operator surface. Dashboard `:3457` is a frozen
  archive/diagnostic surface, not a second product inbox.

Neither touches the chat session lock, so they run concurrently with everything.

---

## Decision guide: which lane?

- A contextual reply expected to finish quickly -> **Lane 1**.
- Owner-approved work that may take minutes or hours -> **Lane 2**.
- Recurring system work with explicit authority and evidence -> **Lane 3**.

Rule of thumb: if you'd otherwise be tempted to make Lane 1 parallel "to stay
responsive," that's the signal the work belongs in Lane 2 instead.

---

## Failure modes & guards

- **Stale/recycled PID:** process-start identity and lock ownership token are
  checked before reclaim, release, stop, or cancel.
- **Restart drops in-flight work:** dispatch markers make interrupted messages
  observable; background-job terminal state is reconciled separately.
- **Session/Matter changes while queued:** dispatch revalidation refuses the
  turn rather than executing with stale authority.
- **Provider transport failure:** deterministic text-only work may fail over;
  ambiguous tool-capable work is not replayed.
- **Empty or model-level no-output:** kept distinct from infrastructure failure;
  Routines can defer and re-arm on infrastructure outages.
- **Runaway provider call:** bounded timeout/watchdog terminates the owned
  process group.
- **Cancelled job finishes late:** registry terminal state prevents a late
  process from overwriting `cancelled` or `lost` with `completed`.

---

## Evidence and source of truth

- `bot.sh`: dispatch, lock ownership, routing, promotion, and job runner.
- `core/session.py`: logical-context-to-physical-session tracker and rotation.
- `core/jobs.py`: durable job lifecycle and process identity.
- `core/model_control.py`, `core/runtime_provider.py`: route catalog and owner
  preference.
- `core/codex_fallback.py`: Codex thread/process boundary.
- `core/conversation_context.py`: context-scoped pending-result merge.
- `tests/test_provider_continuity_e2e.py`, `tests/test_session_lifecycle.py`,
  `tests/test_process_lifecycle.py`, and `tests/test_jobs*.py`: executable
  contract.

Line numbers intentionally are not copied here; use symbol search so this
document remains useful after refactors.
