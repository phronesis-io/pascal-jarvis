# Jarvis Domain

## Vocabulary

### Item / Memorial

The single user-visible unit of notice or decision. Memorial is the
implementation and audit ledger; Item is the product term.

Invariant: one real event has at most one active user-visible Item per review
surface, and deciding it resolves every delivered copy.

### Matter

A durable topic that groups context, sessions, artifacts, outcomes, and a next
action.

Invariant: Matter is context and continuation, not a competing inbox or a
completion claim.

### Logical Session

The user's active view into one Matter. Opening or switching a logical session
rotates the physical provider window while preserving the Matter's goal,
decisions, artifacts, and next action.

Invariant: recent turns, compact summaries, and provider threads never cross a
logical-session key. Reset removes derived context, not raw transcripts or the
Matter ledger. Each reset advances the context generation; receipts and
background results captured under an older generation are historical evidence,
not input to the current model window.

### Context Packet

A minimal, provider-neutral projection of one Matter for a bounded executor
session. It contains the current goal, accepted decisions, relevant artifacts,
constraints, unresolved questions, permissions, and next action, with a source
reference for every mutable fact.

Invariant: a Context Packet is compiled from authoritative state and selected
memory. It is not a transcript dump, does not own domain state, and cannot make
an old decision current merely because it appeared in conversation history.

### Result Receipt

The release contract of one bounded executor session. It records artifacts,
decisions requested or made, verified effects, unresolved blockers, and the
exact next action against the same Matter and context generation.

Invariant: model prose or a process exit code is not a Result Receipt. Claimed
effects must link to authoritative verification, and a missing or stale receipt
cannot advance the Matter to done.

The current `jarvis.result-receipt.v1` proves the execution boundary: matching
Context Packet generation/digest, executor exit, hashed workspace artifacts,
qualifying Delegation evidence for external effects, and Matter state at
release. Its model-authored narrative is explicitly marked unverified. The
receipt always says `matter_completed: false`; completion remains a separate
domain transition with its own closure guards.

### Matter Run

One leased execution attempt against one Matter and Context Packet. It has a
monotonic run sequence and exactly one terminal state: released, failed, or
expired.

Invariant: at most one acquired/running Matter Run exists per Matter. Expired
leases are recoverable; a second release is idempotent only when it describes
the exact same evidence. A conflicting receipt is rejected, never overwritten.

### Intent

A time-bound internal promise to trigger, retry, and optionally ask a closure
question.

Invariant: the model may author the prompt, but only lifecycle code changes
trigger, execution, breach, and closure state.

### Routine

A recurring body of work the *user* defined, carrying four things: a trigger, a
declared set of read-only evidence sources gathered before any model call, an
autonomy level, and an audit run per firing.

Autonomy is a three-level contract — `observe` (records, reaches nobody),
`propose` (one Item, consequences need 批红), `act` (propose plus a fixed
allow-list of internal, reversible actions). External mutations are never on
that list; those are Verified External Actions.

Invariant: the autonomy level is enforced by code against the stored Routine.
A model may request anything and is granted only what its Routine already
holds. Every claimed run reaches a terminal audit row.

Distinguished from Intent: an Intent is one promise at one future moment. A
Routine is a standing rhythm that gathers its own evidence each time.

### Delivery

A durable contract to place output on a surface. It moves through queued,
attempting, delivered, read, acted, suppressed, or terminal failed. Transport
attempts are children of the Delivery and share one cumulative retry budget.

Invariant: producer code cannot maintain a parallel retry, dedup, or
delivered-state truth. A failed Delivery is never left looking queued.

### Verified External Action

A side effect against another system with a stable target, idempotency key,
mutation attempt, authoritative read-back, and compact receipt.

Invariant: a tool return is not completion until the connector's verification
policy passes. Ambiguous identity is a stopped action, not a best guess.

### Delegation

An accepted responsibility for an outcome that may contain several verified
external actions or engineering steps.

Invariant: partial step success does not make the whole Delegation complete.
Ordinary conversation and life aspirations are not automatically Delegations.
Shadow Delegations are labeled observations, not accepted responsibilities,
and never appear in the user's active-work metrics.

### Proposal

A normalized L3 recommendation derived from one or more feedback signals. It
contains the desired outcome, non-goals, acceptance evidence, impact, cost,
priority, and dependency context.

Invariant: a Proposal cannot enter the executable engineering queue until a
human explicitly accepts it. Repeated signals update one Proposal instead of
creating unbounded tasks. A newer complete observation from the same source may
resolve an absent signal and mark its still-pending Proposal `superseded`, so a
recovered problem no longer consumes human attention. This never cancels work
that a human already accepted, and shipped work still requires post-release
verification.

### Engineering Task

An L2 Taskline object with dependency, priority, owner, lease, stage documents,
PR, CI, review, and merge evidence.

Invariant: it is not a personal Intent or Item. Losing an Agent context does
not lose task ownership or stage evidence, and an expired lease is recoverable.

### Signal

A typed, source-attributed inbound observation with event identity,
sensitivity, and optional routing metadata.

Invariant: source importance does not grant permission to disclose private
content or interrupt the user.

### Handoff

A lease indicating where the next interaction should continue.

Invariant: a Handoff moves attention between surfaces, devices, or executors;
it never duplicates the Item, Matter, or Intent.

### Job

An asynchronous execution with process ownership, status, output, and
reconciliation.

Invariant: a dead worker cannot leave a Job permanently "running", and Job
completion is not automatically product-outcome completion.

## Cross-Object Rules

- An Item may reference a Matter and expose an Intent as a timed attribute.
- An Intent may create an Item, but the Item does not become a second Intent.
- A verified external action may add evidence to a Delegation and Matter.
- Delivery transports an Item or reply; it does not own their business state.
- A Handoff closes when its exact Item or Matter continuation is consumed.
- A Logical Session acquires one Matter through a Context Packet and releases it
  through a Result Receipt; neither object becomes a second source of truth.
- Memory summarizes objects but never replaces their authoritative stores.
- L3 proposes, L2 schedules engineering work, and L1 proves delivery; none of
  these may infer a product outcome from an Agent's completion sentence.

## Risk Classes

- `R0`: local read-only query.
- `R1`: reversible private write with deterministic verification.
- `R2`: message or shared-object change to a confirmed target.
- `R3`: public, destructive, permission, cost, or formal commitment action.
- `R4`: legal, major financial, irreversible, or unbounded authority.

Risk depends on target and effect, not merely tool name. R2 ambiguity requires
confirmation. R3 requires explicit approval. R4 stays human-operated.
