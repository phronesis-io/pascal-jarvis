# Jarvis Architecture

## Runtime Topology

```text
launchd
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

## Module Boundaries

- `core.delivery`: the only user-facing retry, dedup, quiet-hour, throttle,
  routing, and delivery-state machine. It uses short-lived connections,
  initializes schema once per database inode, and never holds a transaction
  across a network send.
- `core.memorial`: visible Item and decision ledger.
- `core.intentions` and `core.intent_*`: time, trigger, retry, and closure.
- `core.matters`: durable topic identity and executor context.
- `core.continuity`: device handoff leases and resume state. Each operation
  owns and closes its SQLite connection; active-handoff creation is atomic.
- `core.perception`: typed inbound signals and sensitivity.
- `core.eigenflux_messages`: deterministic friend identity, message
  idempotency, paginated discovery, send receipt, and authoritative read-back.
- `core.actions`: narrow dispatch for explicit system actions.
- `core.memory`: tiered context selection, not an authority for mutable
  external facts.
- `dashboard/`: human and operator projections over the same durable state.

## Durable State

`data/jarvis.db` uses SQLite WAL for cross-process state:

- delivery envelopes, attempts, events, and dead letters;
- Intent state and breaches;
- schedule events and runtime versions;
- Matters, Handoffs, and cross-device state;
- verified external-action receipts.

Append-only JSONL remains where event history itself is useful, notably
Memorial and compatibility ledgers. New policy must not depend on two writable
sources for the same state.

## Authority Matrix

| Claim | Authority |
|---|---|
| component is healthy | `core.components` live check |
| message was sent to an EigenFlux friend | server friend record + message history |
| Lark output was delivered | transport `message_id` / delivery row |
| Item was decided | Memorial ledger projected into delivery state |
| Intent completed | Intent lifecycle and closure evidence |
| deployment is complete | git revision + runtime version + components + smoke |
| calendar is current | calendar API/sync artifact with freshness |

Model prose and memory summaries are never authorities for these claims.

## Dependency Direction

Adapters and task hooks may call domain services. Domain services may call
small infrastructure helpers. User interfaces read domain projections and
invoke domain commands. No producer may bypass `core.delivery` for ordinary
user-facing output, and no LLM prompt may directly own a mutation's terminal
state.
