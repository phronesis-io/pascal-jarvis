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
```

The NiceGUI dashboard (`dashboard.main :3457`) is retired (2026-08-21):
archive duty moved to the morning-anchor batch line and the Admin console,
and the code archive is git history. The mobile gateway
(`dashboard.mobile_gateway :3458`) and every Jarvis-owned Tailscale path are
retired (2026-08-11, REQ-120). Jarvis neither installs, configures, probes,
nor depends on Tailscale; Lark is the only mobile surface.

`components.yaml` is the only manifest of what should be alive. The daemon,
doctor, restart/status tooling, and self-diagnostic consume it.

## Main Flows

### Conversation

```text
Lark event
  -> bot.sh parsing and trust boundary
  -> core.prompt current memory + Matter context
  -> selected model/provider
       owner p2p: preferred executor -> Claude chain -> Codex CLI -> GPT API
       shared/untrusted: restricted Claude chain -> text-only GPT API
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
  -> work-receipt gate (missing receipt => withhold)
  -> Memorial / Intent / verified action
  -> core.delivery
```

The model authors content and a compact statement of the preparation it
completed. Deterministic code owns scheduling, side effects, state transitions,
retries, completion, and the work-receipt gate. `core.heartbeat_loop` invokes
`memorialize_output(require_work_receipt=True)`; missing evidence returns an
empty delivery payload instead of falling through as raw prose. Native card
builders carry the producer's receipt in an internal structured marker; the
adoption boundary removes and persists it. It never invents a generic receipt
for an unprepared legacy card.

Clipped Memorials keep the complete source in the append-only ledger.
`core.memorial` owns card rendering and continuation offsets, while
`core.memorial_reader` runs the user-triggered background transfer through
`core.delivery`. Each chunk advances only after a confirmed Lark receipt; an
interrupted transfer resumes from its last confirmed offset.

### Routines

```text
user sentence in Lark
  -> core.routines definition (trigger + evidence + autonomy)
  -> routine-run pre-hook: claim due, advance watermark, gather evidence
  -> HeartbeatRunner batches model work
  -> post-hook: require work_receipt, then authorize actions against the STORED autonomy level
  -> Memorial (propose/act) or audit-only record (observe)
  -> routine_runs audit row, always terminal
```

Routines reuse the Intent scheduler's `next_fire_at` catch-up primitive rather
than adding a scheduler, and everything they show the user is an ordinary
Memorial routed by `core.delivery`. `observe` output exists only in the audit
trail. A `propose` or `act` run without `work_receipt` becomes terminal
`withheld`: it sends nothing and executes no requested action. The `act`
allow-list is internal and reversible; external mutation stays
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

### Cross-Entry Continuity

```text
Item / Matter
  -> Handoff lease
  -> Lark conversation or desktop executor
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

A complete same-source observation also closes an absent signal before human
acceptance and lapses its pending Item locally. Incomplete or stale coverage
fails closed; accepted work continues through its normal lifecycle, and
shipped work cannot bypass the post-release observation gate.

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
  across a network send. Every alert receives a stable incident identity. A
  verified transport recovery reconciles terminal failures but requeues only
  unresolved, unexpired work, once, under its original idempotency identity;
  regenerated routine/Guardian/calendar output and stale alerts become audited
  suppressions. A recovered envelope never bypasses attention budgets: a full
  daily/source/metric cap defers it to the next budget window, where its TTL is
  checked again, instead of terminating the still-valid Item. Its retry/cap
  interaction is frozen in
  `docs/delivery_retry_and_caps.md`.
- `core.lark_bot_transport`: the Keychain-independent application-bot adapter
  used by replies, cards, proactive delivery, Memorials, and EigenFlux stream
  messages. It caches tenant tokens only in process memory and returns success
  only with a provider `message_id`. Owner-identity APIs remain behind the
  separate `lark-cli --as user` OAuth boundary, so calendar/docs degradation
  cannot take bot delivery down with it.
- `core.ef_stream_loop` + `core.eigenflux_ingress`: one EigenFlux private-
  message ingestion boundary with two transports. WebSocket is the instant
  path; a deterministic five-minute `msg fetch` + CLI-cache reconciliation is
  the no-loss path. Both serialize on the same local lock, deduplicate on the
  server's canonical `msg_id`, and mark the receipt only after a Memorial or
  delivery envelope is durable. Terminal retries are automatic only for a
  typed, definitive no-send failure; closed user decisions never reopen.
- `docs/capability_inventory.md`: generated evidence map for supported
  components, scheduled work, CLIs, pages, APIs, and Lark commands. It detects
  missing contracts and drift; it never authorizes deletion without explicit
  retirement, replacement, migration, and data-retention evidence.
- `core.memorial`: visible Item and decision ledger. New cards persist a
  `work_receipt`, rendered above the body; proactive model output must supply
  one `WORKED:` directive per card block.
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
- `core.conversation_context`: logical-session identity and derived-context
  reset. A Matter is the user-facing session; Claude/Codex session IDs are
  disposable provider windows. Compact summaries, recent provider turns, and
  Codex threads are scoped by the logical key rather than the Lark chat. Reset
  advances a context generation: old transcripts remain auditable, while old
  turns, compacts, deferred results, and late receipts cannot re-enter prompts.
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
- `core.model_control`: the model control plane. It separates upstream account,
  requested model, execution adapter, capabilities, trust scope, route order,
  health cooldown, and real provider diversity. It emits the private
  compatibility environment consumed by harnesses but never starts a model
  process or exposes credentials on a status surface.
- `core.provider_health`: bounded provider canaries and sanitized model-chain
  observability over the shared `model_control` catalog. Canary and real-request
  evidence remain separate: a green tiny canary cannot erase a production
  timeout. Real failures own an explicit escalating cooldown; only a real
  success clears the failure streak.
- `core.codex_fallback`: owner-private Codex CLI execution, bounded process
  control, and one durable Codex thread per logical Matter context. It uses the
  logical context as a cross-transport process lock, so two entrances cannot
  concurrently resume the same Codex thread. It uses the
  workspace-write review sandbox and never serves group or non-owner traffic.
  A later provider may replay the request only when Codex emits a recognized
  terminal unavailability event before any executable item; incomplete,
  unknown, or post-tool failures stop fail-closed.
- `core.runtime_provider`: per-conversation executor preference. Preference
  changes route order only; `conversation_runtime` records what actually ran.
- `core.cross_session`: bounded, redacted owner-only continuity across
  interactive Claude Code and Codex sessions. It excludes Jarvis-managed
  provider calls and supplies both immediate prompt context and the durable
  heartbeat digest; provider transcripts remain the source of truth. The
  choice/execution/continuity ownership split is recorded in `DECISIONS.md`.
- `core.cross_session_index`: private, WAL-backed, rebuildable indexing of
  redacted visible turns from owner-operated Claude Code and Codex sessions.
  Small heartbeat batches converge through old transcripts; the current owner
  request retrieves only relevant older turns. It is never injected into a
  group or named Matter and never turns remembered prose into current truth.
- `core.release_gate`: fail-closed merged-PR, CI, branch-protection, and
  independent-review evidence before a production code restart. The default
  deploy and its `--full` alias refresh and verify every installed resident
  component, so the launchd-owned daemon process cannot stay on the
  previous revision. The separate
  `restart.sh --runtime` path is configuration-only: it revalidates release
  authority, requires a clean worktree, and proves the running bot/heartbeat
  already match `HEAD`, so it cannot preserve or deploy unreviewed code.
- `core.sqlite_migrations`: named, transactional additive migrations for
  domain stores that must initialize without importing the base-schema owner
  (`core.db`). A
  pending batch owns an `IMMEDIATE` transaction, so concurrent processes
  serialize before reading migration state; a marker and its compatible
  physical column commit together. Type/nullability/default or marker/schema
  drift fails closed. Destructive or type-changing migrations still require an
  explicit backup, transform, verification, and rollback plan.
- `core.aux_model`: bounded Primary/Backup 1/Backup 2/GPT routing for
  background jobs and text-only auxiliary calls. Untrusted or derived text
  enters with all Claude/OpenAI tools disabled. Every configured provider
  selected by the route receives one bounded call before the chain advances.
- `core.actions`: narrow dispatch for explicit system actions.
- `core.memory`: tiered context selection, not an authority for mutable
  external facts.
- `views/`: JSON files for RichView interactive card payloads (created by
  `core.richview`, served through the admin console's RichView routes). Each
  file is a `{view_id}.json` produced at card creation time.

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

Lark is the only delivery surface (REQ-119, 2026-08-11). A card either goes
to Lark or stays ledger-only; no envelope may route to the retired web
channel, whose transport used to fake success unconditionally.

| Attention | Durable surface | Interrupting reach |
|---|---|---|
| conversation reply | Lark thread | immediate Lark reply |
| decision | Item | Lark card |
| alert | Item | Lark card (quiet-hours bypass) |
| ordinary notice | Item | Lark card |
| ambient exhaust (`AMBIENT_SOURCES`) | Item, `delivery_status=ledger_only` | none — batched into the morning anchor digest line (`core.presence`) |

`ledger_only` means durable placement, not verified human reach; the morning
digest line is its one batched reach. `web_only`/`phone_ready` survive only
as legacy ledger values from the pre-REQ-119 era.

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
| model fallback is usable | real-request outcome plus non-authoritative bounded canary and live routing state |
| calendar is current | calendar API/sync artifact with freshness |

Model prose and memory summaries are never authorities for these claims.

Lark has two explicit identities. The **bot identity** uses the private app
credential and direct OpenAPI transport for messaging and bot metadata. The
**owner identity** uses user OAuth for personal calendar, docs, mail, and task
mutations. Their health is reported separately; neither identity may silently
substitute for the other. An interactive token can remain valid while a
background process is denied Keychain access; that runtime-context failure is
reported as retryable degradation and never presented as an authorization
request.

The Guardian is an independent process path, not an independent communication
channel. Its Lark alerts can fail with Lark. A configured external dead-man is
the actual out-of-band detector: the daemon withholds its success ping after
three consecutive real Lark transport failures, as well as when the local
stack is unhealthy.

Guardian follows `observe -> bounded repair -> verify -> notify`. It may only
recycle a process after proving that process descends from this repository's
live bot and matches the exact component command. A first red probe starts
recovery and is silent. A user-facing alert means the repair grace expired and a second probe
was still red. Delivery receipts are three-valued: confirmed/covered closes an
incident, queued/attempting leaves it durably in flight without a local banner,
and only a refused or dropped alert invokes the rate-limited macOS fallback.
All Guardian and external dead-man payloads are `owner_private`; a failed owner
route fails closed and never falls back to a group or public monitoring route.

The host itself is not a component and cannot be supervised into existence. A
closed lid on battery sleeps the Mac whatever any `caffeinate` assertion says,
and while it sleeps the runtime, the Guardian watching it, and the surface it
would report through are all stopped together, so from the inside silence is
indistinguishable from health. Three consequences are designed for rather than
fought:

- `core.hostclock` is the single sleep meter: the drift between the wall and
  monotonic clocks, so absence is measured wherever in a tick it happened —
  including inside a model call — and a long model call is never mistaken for
  it. The daemon is its only writer.
- Age-derived verdicts in `core.components` count only time the host was
  actually up. The daemon's bounded post-wake grace remains the fallback where
  no sleep is recorded, but it cannot overrule recorded evidence: a component
  wedged through a laptop's hourly naps still turns red.
- `core.absence` turns a qualifying absence into one ordinary notice card on
  the next confirmed wake, naming what it cost. Sleep is not a fault and does
  not page; absence through the owner's active hours is a receipt he is owed,
  and it has exactly one surface. Only the external dead-man can report an
  absence while it is still happening.

## Dependency Direction

Adapters and task hooks may call domain services. Domain services may call
small infrastructure helpers. User interfaces read domain projections and
invoke domain commands. No producer may bypass `core.delivery` for ordinary
user-facing output, and no LLM prompt may directly own a mutation's terminal
state.
