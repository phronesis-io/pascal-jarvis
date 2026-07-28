# Jarvis Architecture

## Runtime Topology

```text
launchd
  -> taskline sidecar :8787 (optional engineering queue)
  -> daemon.py
       -> bot.sh
            -> Lark event listener
            -> core.heartbeat_loop
            -> core.ef_stream_loop
            -> admin.py :3456
  -> dashboard.main :3457
  -> dashboard.mobile_gateway :3458
```

`components.yaml` is the only manifest of what should be alive. The daemon,
doctor, restart/status tooling, and self-diagnostic consume it.

## Main Flows

### Conversation

```text
Lark event
  -> bot.sh parsing and trust boundary
  -> core.prompt current memory + Matter context
  -> selected model/provider
  -> deterministic action processors
  -> core.delivery reply envelope
  -> Lark receipt + Matter timeline
```

Group chat is fail-closed: curated group context, restricted tools, and no
owner-private writes.

### Proactive Work

```text
HEARTBEAT.md task
  -> pre-hook gathers typed evidence
  -> HeartbeatRunner batches model work
  -> post-hook parses a bounded contract
  -> Memorial / Intent / verified action
  -> core.delivery
```

The model authors content. Deterministic code owns scheduling, side effects,
state transitions, retries, and completion.

### Routines

```text
user sentence in Lark
  -> core.routines definition (trigger + evidence + autonomy)
  -> routine-run pre-hook: claim due, advance watermark, gather evidence
  -> HeartbeatRunner batches model work
  -> post-hook: authorize actions against the STORED autonomy level
  -> Memorial (propose/act) or audit-only record (observe)
  -> routine_runs audit row, always terminal
```

Routines reuse the Intent scheduler's `next_fire_at` catch-up primitive rather
than adding a scheduler, and everything they show the user is an ordinary
Memorial routed by `core.delivery`. `observe` output exists only in the audit
trail. The `act` allow-list is internal and reversible; external mutation stays
with Verified Delegation, which owns read-back evidence.

### Perception

```text
sources.yaml
  -> sources/<type>.py collect()
  -> core.perception Signal
  -> cross-source dedup + sensitivity
  -> memory/system inbox
  -> task-specific consolidation or delivery
```

Adding a source extends the adapter registry. It does not add a new scheduler
or delivery stack.

### Cross-Device Continuity

```text
Item / Matter
  -> Handoff lease
  -> exact stable route on phone or desktop
  -> action on the same underlying object
  -> all stale handoffs close
```

### Verified Delegation

```text
accepted outcome contract
  -> stable target + risk/authorization binding
  -> required-step DAG with claim/lease
  -> connector mutation with idempotency key
  -> authoritative read-back verifier
  -> evidence-derived Delegation state
  -> one-way Item/Matter/Intent/Handoff projections
```

The reconciler scans only bounded non-terminal work. Shadow capture is
observation-only and excluded from active Delegation lists and product metrics.
Qualifying evidence persists its trusted verifier identity and authority;
expired or untrusted evidence reopens active steps instead of satisfying
completion. A failed execution creates one retry/cancel attention Item. An
active `verifying` step is read-back-only: user recovery resumes the verifier
and never resets the external mutation to pending.

### Engineering Loops

```text
L3 signal -> deduplicated Proposal -> human accept
  -> L2 Taskline dependency queue + claim/lease/worktree
  -> L1 spec -> dev -> test -> review -> merged PR
  -> release gate -> runtime verify/smoke
  -> L3 outcome observation
```

Taskline is an optional external sidecar with a separate database. Its tasks
never become personal Intents. `core.taskline_bridge` links engineering
evidence into Delegation so an Agent can recover context without treating its
own prose as proof. A merged task starts its pending runtime-verification step
even when its release SHA was bound in an earlier pass. Runtime proof accepts
the exact release commit or a healthy resident descendant that contains it.

## Module Boundaries

- `core.delivery`: the only user-facing retry, dedup, quiet-hour, throttle,
  routing, and delivery-state machine. It uses short-lived connections,
  initializes schema once per database inode, and never holds a transaction
  across a network send.
- `core.memorial`: visible Item and decision ledger.
- `core.proactive`: narrow reach policy over already-durable notices. It may
  request a paired-phone push for explicitly selected sources, but never owns
  storage, quiet hours, retries, or delivery state.
- `core.intentions` and `core.intent_*`: time, trigger, retry, and closure.
- `core.routines`: user-authored recurring work — definition, claim, autonomy
  enforcement, action allow-list, and the per-run audit trail.
  `core.routine_evidence` is its read-only, path-guarded, size-bounded
  provider registry; it never mutates and never shells out except for a
  bounded `git log`.
- `core.attention_roi`: measures per-source engagement from the Memorial
  ledger and may quiet a decision lane into a notice. It can only lower a
  class, never raise or silence one, never touches a protected source, and
  announces every change. It reads `memorial.natural_attention`, not
  `_default_attention`, so its own overrides cannot become evidence for
  themselves.
- `core.mail_draft`: reply drafts and their per-user voice configuration.
  Storage and rendering only — Jarvis has no mail send transport, and no
  status in this module means "sent".
- `core.matters`: durable topic identity and executor context.
- `core.continuity`: device handoff leases and resume state. Each operation
  owns and closes its SQLite connection; active-handoff creation is atomic.
- `core.perception`: typed inbound signals and sensitivity.
- `core.eigenflux_messages`: deterministic friend identity, message
  idempotency, paginated discovery, send receipt, and authoritative read-back.
- `core.delegations`: accepted outcome contracts, step DAGs, claims, evidence,
  state transitions, links, metrics, and shadow labels.
- `core.delegation_verify`: read-only authoritative verifier registry.
- `core.delegation_projection`: one-way projections into existing user and
  execution objects. Projection failures enter a durable retry queue; the
  reconciler drains that queue for active and terminal Delegations.
- `core.iteration_loop`: L3 signals, proposals, human acceptance, and
  post-release outcome observations.
- `core.taskline_bridge`: L2 sidecar health, claims, leases, isolated
  worktrees, and Delegation links.
- `core.provider_health`: bounded provider canaries and sanitized model-chain
  observability.
- `core.release_gate`: fail-closed merged-PR, CI, branch-protection, and
  independent-review evidence before a production code restart. The separate
  `restart.sh --runtime` path is configuration-only: it revalidates release
  authority, requires a clean worktree, and proves the running bot/heartbeat
  already match `HEAD`, so it cannot preserve or deploy unreviewed code.
- `core.aux_model`: bounded Primary/Backup 1/Backup 2/GPT routing for
  background jobs and text-only auxiliary calls. Untrusted or derived text
  enters with all Claude/OpenAI tools disabled. Every configured provider
  selected by the route receives one bounded call before the chain advances.
- `core.actions`: narrow dispatch for explicit system actions.
- `core.memory`: tiered context selection, not an authority for mutable
  external facts.
- `dashboard/`: human and operator projections over the same durable state.
- `views/`: JSON files for RichView interactive card payloads (created by
  `core.richview`, consumed by mobile gateway and dashboard). Each file is a
  `{view_id}.json` produced at card creation time.

## Durable State

`data/jarvis.db` uses SQLite WAL for cross-process state:

- delivery envelopes, attempts, events, and dead letters;
- Intent state and breaches;
- schedule events and runtime versions;
- Matters, Handoffs, and cross-device state;
- Delegations, steps, evidence, events, links, shadow labels, and projection
  retries;
- L3 signals, proposals, and post-release observations;
- verified external-action receipts.

Append-only JSONL remains where event history itself is useful, notably
Memorial and compatibility ledgers. New policy must not depend on two writable
sources for the same state.

`sched_events.jsonl` is an intentional exception: `core.sched_events.emit()`
writes both the JSONL file (durable append-only audit trail) and a SQLite
projection (indexed query surface). The JSONL file is authoritative; the
SQLite table is a read-through projection rebuilt on demand.

## Reach Policy

| Attention | Durable surface | Interrupting reach |
|---|---|---|
| conversation reply | Lark thread | immediate Lark reply |
| urgent/conversation-bound decision | Item | immediate Lark card |
| ordinary decision | Item | phone/web batch review |
| selected proactive signal | Item + Signals projection | paired-phone Push, quiet hours, max 2/day |
| ordinary notice | Item + Signals projection | none |

`web_only` means durable placement, not verified human reach. A source may be
web-first only when it has a named navigation entry or source filter, text
search, a documented reach rule, and deterministic discovery tests.

## Authority Matrix

| Claim | Authority |
|---|---|
| component is healthy | `core.components` live check |
| message was sent to an EigenFlux friend | server friend record + message history |
| Delegation is complete | all required steps have matching authoritative evidence |
| Lark output was delivered | transport `message_id` / delivery row |
| Item was decided | Memorial ledger projected into delivery state |
| Intent completed | Intent lifecycle and closure evidence |
| deployment is complete | git revision + runtime version + components + smoke |
| model fallback is usable | bounded provider canary plus live routing state |
| calendar is current | calendar API/sync artifact with freshness |

Model prose and memory summaries are never authorities for these claims.

## Dependency Direction

Adapters and task hooks may call domain services. Domain services may call
small infrastructure helpers. User interfaces read domain projections and
invoke domain commands. No producer may bypass `core.delivery` for ordinary
user-facing output, and no LLM prompt may directly own a mutation's terminal
state.
