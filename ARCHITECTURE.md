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
nor depends on Tailscale. Lark remains the only production proactive-delivery
surface while the Codex desktop/mobile frontstage contract is implemented and
verified. That target does not revive a Jarvis-owned mobile web application.

`components.yaml` is the only manifest of what should be alive. The daemon,
doctor, restart/status tooling, and self-diagnostic consume it.

## Main Flows

### Current Lark Conversation

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

### Codex-First Session Lifecycle

```text
Codex task on desktop or mobile
  -> acquire one Matter lease
  -> compile a provider-neutral Context Packet
  -> run a bounded executor session
  -> verify artifacts and external effects
  -> write one Result Receipt
  -> reconcile Matter / Item / Intent / Handoff state
  -> release the lease; keep the raw transcript as audit evidence only
```

The Phase-1 protocol is implemented in the repository; production migration
still requires review, merge, release-gate and desktop/mobile acceptance
evidence. A Codex task is a replaceable execution window; the Matter is the
long-lived product object. Jarvis owns context compilation, authority,
continuity, asynchronous work, and reconciliation. Codex owns the interactive
work surface. Lark remains a bounded wake-up and native-integration channel
until desktop and mobile acceptance tests prove that a class of interaction can
move without losing delivery or closure evidence.

Context Packet inputs are authoritative object state and selected memory, not a
raw transcript dump. Result Receipts contain artifacts, decisions, verified
effects, unresolved blockers, and the exact next action. Provider prose alone
cannot satisfy a receipt.

The implementation boundary is explicit:

- `core.matter_runs` owns the single active lease, run sequence, expiry and
  immutable Result Receipt;
- `core.matter_run_evidence` verifies present/deleted workspace artifacts and
  resolves external effects against current Delegation evidence;
- `core.matter_run_projection` projects timeline/session/artifact views after
  the authoritative state commits, logging failures without falsifying the
  acquire or release outcome;
- `core.matter_run_audit` reports stale leases, legacy prose-only completion
  events, missing outcomes and terminal runs without receipts;
- `core.matter_context` compiles `jarvis.context-packet.v2`, removes raw event
  payloads, adds source references, and writes owner-only packet files;
- `core.matter_executor` is the shared Claude/Codex adapter. Process exit and
  the last assistant message are observations only; they never close a Matter;
- `core.codex_frontstage` exposes the provider-independent application
  contract used by interactive harnesses: one-step continuation, search/create,
  acquire, renew, release, owner-confirmed closure, abort, result review, and
  audit. It contains no MCP protocol code;
- `core.matter_review` is the bounded read model shared by Codex and the
  weekly Lark surface. It counts only `matter_closure_completed` as an outcome,
  separates released runs awaiting owner closure, and never reads transcripts
  or mutates Matter/Task state;
- `core.matter_closure` owns the separate owner-authorized terminal saga. It
  reconciles linked Intent/Item/Handoff state before Matter completion and
  fails closed on live runs, Jobs, or Delegations;
- `core.codex_mcp` adapts that contract to the official MCP Python SDK over
  local stdio. `plugins/jarvis-matters` supplies the Codex skill and launcher;
  it does not read Codex's private task store or create a second conversation;
- `core.frontstage_acceptance` stores explicit owner reviews for real desktop
  and mobile journeys. It atomically claims at most one optional prompt per
  successful run and maps only Pascal's exact published labels to immutable
  version-bound evidence. MCP cannot submit free-form scores, a surface, or a
  reviewer identity;
- `scripts/jarvis-matter` exposes the same contract through `context`,
  `launch`, `run-status`, `finish`, and `audit`.

One partial unique index enforces a single active run per Matter across
processes. A context reset invalidates late release attempts by generation.
An external-effect claim is accepted only when it references current,
qualifying evidence from the Delegation verifier. Release persists the receipt
before projecting session/artifact links, so projection failure is observable
without losing the authoritative receipt.
Foreground launcher sessions renew a bounded six-hour lease while alive;
abandoned prepared handoffs still expire. Replayed handoff requests reuse the
same unstarted run only when executor, task, workspace and packet all match.

Codex itself owns task creation, resumption, streaming, approvals, diffs,
mobile Remote, and ordinary task memory. Jarvis integrates through the
supported MCP/plugin boundary for application-owned capabilities. A future
host-driven workflow may use Codex app-server to create or resume tasks, but
app-server event state must still project into the same Matter Run contract;
it may not become a competing lifecycle store.

Git and GitHub remain outside the Jarvis state machine. They own source
history, commits, pull requests, review, CI, and merge evidence. Matter links
may reference `git` or `github` artifacts, and Result Receipts may hash files
inside the acquired workspace, but repository state is never copied into a
second Jarvis branch/PR model. A green CI check is delivery evidence, not owner
confirmation that the product Matter is complete.

The weekly review is a deterministic Tier-0 heartbeat task. Its pre-hook reads
the same `core.matter_review` contract exposed as `jarvis_matter_review`; its
post-hook renders at most one bounded card. It makes no model call and cannot
decay, defer, archive, or otherwise edit a parallel task system.

Owner conversations and tool-capable heartbeat calls default to indexed warm
memory. Stable identity and standing guidance remain inline at the start of
the reusable prompt prefix. An owner conversation stores one exact private
system-prompt snapshot per provider session and reuses it until the session,
reviewed runtime revision, prompt implementation, or one-hour freshness window
changes. Private snapshots are capped at 128. Heartbeat reuses one bounded snapshot per
trust/tool profile; frequently changing task DATA and current time stay in the
user request, outside the system cache block.
The snapshot directory lock covers cache reads, pruning, and atomic publication
only. Memory and cross-session assembly run outside that lock, followed by a
second cache read before publication, so unrelated new sessions do not block
one another while the first complete snapshot still wins each session key.
Models fetch indexed reference notes from disk only when relevant. Restricted
or no-tool calls retain full inline memory so index mode can never make
knowledge unreachable.

Provider credentials follow the same execution boundary. `bot.sh` keeps the
primary Anthropic, relay, and OpenAI keys shell-private. The heartbeat routing
worker receives the configured route set, while a direct provider adapter
receives only its active credential. Ordinary task scripts and model-started
Codex/GPT tools receive a scrubbed environment; child-process handling inside
a third-party provider CLI remains that CLI's responsibility.

Heartbeat model choice is task policy, separate from provider execution.
`HEARTBEAT.md` may select a quality tier or the GPT route; `core.heartbeat`
validates that declaration, isolates outbound/untrusted contexts, selects a
compatible provider, and records the model that actually answered. One
logical call owns one wall-clock budget. Provider recovery is measured by the
small `provider-canary`; a full production prompt is never sacrificed as a
health probe, and a timed-out tool-capable request is never replayed.
Claude-compatible relays also have an independent
`claude.relay_attempt_timeout` cap inside that budget, so a green tiny canary
cannot let a stalled production-size relay monopolize the scheduler.
Outbound model stages are no-tools and receive only the curated public group
context; deterministic post-hooks remain the only effect boundary.

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
  -> Codex desktop/mobile task, Lark conversation, or another executor
  -> action on the same underlying object
  -> all stale handoffs close
```

Sessions remain bounded and replaceable. Matter identity, decisions, artifacts,
receipts, and next action outlive every individual Codex, Claude Code, Lark, or
provider session.

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
  -> release gate -> runtime verify/components/smoke -> durable receipt
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

The daily self-improvement coding session is detached from heartbeat model
work. Its heartbeat pre-hook is expected to return no text; health is derived
from an `acquire -> run -> release` receipt containing prompt/output digests,
exit status, and timestamps. A missing release is reconciled as interrupted,
failed sessions retry on a bounded clock, and self-diagnostic records a
warning only after the automatic retry budget is exhausted.

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
- `core.model_runtime`: the provider-neutral execution orchestrator. It consumes
  `model_control` policy, enforces one wall-clock and effect-replay budget, and
  records task, Matter, route, requested/observed model, latency, optional cost,
  and terminal reason without storing prompts or credentials. The auxiliary
  Claude/OpenAI path, compaction, EigenFlux analysis, and idle-noise
  classification use it. Route/model replay stops after an uncertain write or
  external effect; untrusted contexts cannot enable tools. Product state,
  permissions, and completion receipts stay outside this runtime. The main
  Lark conversation loop and primary heartbeat executor still have legacy
  route loops and are the remaining migration boundary; this is not yet a
  system-wide Phase-3 completion claim. Tiny canaries and real-workload health
  remain distinct signals.
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
- `core.cross_session`: bounded, redacted discovery and explicit audit search
  across owner-operated Claude Code and Codex sessions. It excludes
  Jarvis-managed provider calls. Raw turns remain evidence and are never a
  default prompt or durable truth surface.
- `core.cross_session_index`: private, WAL-backed, rebuildable indexing of
  redacted visible turns from owner-operated Claude Code and Codex sessions.
  Small batches converge through old transcripts and explicit searches can
  retrieve relevant turns. It never turns remembered prose into current truth.
- `core.memory_compiler`: deterministic reconciliation between source turns
  and prompt-safe claims. A model may extract bounded candidates only when it
  supplies an exact quote and covers every source. Owner-authored claims may
  become active; assistant-authored claims remain candidates. New decisions
  supersede old ones, contradictory facts suspend both values, and only an
  explicit Pascal review can confirm, choose, or reject a disputed claim.
  Applied compile batches erase transcript payloads while retaining source
  digests, references, claim lifecycle, and audit evidence.
- `core.model_usage`: the joined product view over package allowance,
  reset windows, account metadata, real-request health, and the current
  fallback plan. Codex allowance comes from the signed-in local app-server;
  providers without a provider-defined quota surface remain `unknown`. Numeric
  observations support exhaustion forecasts but never store credentials,
  opaque credit identifiers, or provider billing prose. The hourly Tier-0
  usage task refreshes this view and emits only the first warning in a new
  critical/exhausted episode; recovery rearms it.
- `core.release_gate`: fail-closed merged-PR, CI, branch-protection, and
  independent-review evidence before a production code restart. The default
  deploy and its `--full` alias refresh and verify every installed resident
  component, so the launchd-owned daemon process cannot stay on the
  previous revision. The separate
  `restart.sh --runtime` path is configuration-only: it revalidates release
  authority, requires a clean worktree, and proves the running bot/heartbeat
  already match `HEAD`, so it cannot preserve or deploy unreviewed code.
- `core.deploy`: runtime registrations, delivery smoke, and durable release
  receipts. A receipt is written only when the release-gate SHA equals `HEAD`,
  all registered resident versions match, every critical component is healthy,
  and unified-delivery smoke acts successfully. It is joined evidence, not a
  log-line claim; `python3 -m core.deploy receipt-latest` reads it back.
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
- `core.heartbeat_task_config`: parses task cadence/model/privacy declarations
  and owns shared-batch eligibility; it never invokes a provider.
- `core.heartbeat_provider`: normalizes provider usage and strips benign CLI
  banners before failure evidence reaches the scheduler.
- `core.triage_profile`: bounded, sanitized context for untrusted feed, mail,
  friend-request, and recommendation inputs. It is configuration, not a copy
  of private inboxes or full memory.
- `core.memory_relevance`: exact, bounded warm-memory evidence for a named
  due intent when indexed memory would otherwise hide the relevant file.
- `core.change_gate` and `core.eigenflux_publish_material`: digest-only gates
  that prevent unchanged maintenance or publication work from spending a
  model call. Candidate private content is never stored in gate state.
- `core.runtime_hygiene`: allowlisted permission repair and bounded retention
  for runtime state. It preserves source/user-visible rows and open audit
  evidence; it does not vacuum databases while active writers may exist.
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
- joined release receipts (authority, exact revision, components, and smoke);
- Matters, Handoffs, and cross-device state;
- Delegations, steps, evidence, events, links, shadow labels, and projection
  retries;
- L3 signals, proposals, and post-release observations;
- verified external-action receipts.
- Memory Compiler batches, source digests, traceable claims, source links, and
  unresolved/resolved conflicts. These records are remembered assertions, not
  independent evidence that an external action or release succeeded.
- Numeric model-usage observations by route, limit, window, and reset epoch.
  They are private telemetry for trend/forecast calculations, not billing
  authority and not proof that a production-sized request will succeed.
- Provider-neutral model call and attempt receipts, including effect authority,
  task/Matter attribution, route/model/timing and terminal reason. Prompt text,
  credentials and raw provider errors are not stored.

Append-only JSONL remains where event history itself is useful, notably
Memorial and compatibility ledgers. New policy must not depend on two writable
sources for the same state.

`sched_events.jsonl` is an intentional exception: `core.sched_events.emit()`
writes both the JSONL file (durable append-only audit trail) and a SQLite
projection (indexed query surface). The JSONL file is authoritative; the
SQLite table is a read-through projection rebuilt on demand.

## Reach Policy

Today Lark is the only production proactive-delivery surface (REQ-119,
2026-08-11). A card either goes to Lark or stays ledger-only; no envelope may
route to the retired web channel, whose transport used to fake success
unconditionally. The accepted target adds Codex desktop/mobile as the primary
interactive frontstage, not as a second broadcast inbox. A delivery class moves
only after its Codex notification, resume, and closure receipts pass real
desktop/mobile acceptance tests.

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
| deployment is complete | latest durable release receipt joining release authority + exact git revision + runtime versions + components + smoke |
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

Process inspection is tri-state. An unavailable `ps` snapshot is unknown, not
proof that the Lark listener died. Listener recovery targets only its owned
sidecar; whole-stack recovery respects live session locks, requests a graceful
bot shutdown first, and applies startup/wake grace before judging children.
Recurring incident delivery uses a 24-hour dedup window, so one old recovery
receipt cannot silence every future incident forever.

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
