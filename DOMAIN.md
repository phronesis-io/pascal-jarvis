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

### Intent

A time-bound internal promise to trigger, retry, and optionally ask a closure
question.

Invariant: the model may author the prompt, but only lifecycle code changes
trigger, execution, breach, and closure state.

### Delivery

One attempt to place output on a surface. It moves through queued, attempting,
delivered, read, acted, or suppressed.

Invariant: producer code cannot maintain a parallel retry, dedup, or
delivered-state truth.

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

### Signal

A typed, source-attributed inbound observation with event identity,
sensitivity, and optional routing metadata.

Invariant: source importance does not grant permission to disclose private
content or interrupt the user.

### Handoff

A lease indicating where the next interaction should continue.

Invariant: a Handoff moves attention between devices or executors; it never
duplicates the Item, Matter, or Intent.

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
- Memory summarizes objects but never replaces their authoritative stores.

## Risk Classes

- `R0`: local read-only query.
- `R1`: reversible private write with deterministic verification.
- `R2`: message or shared-object change to a confirmed target.
- `R3`: public, destructive, permission, cost, or formal commitment action.
- `R4`: legal, major financial, irreversible, or unbounded authority.

Risk depends on target and effect, not merely tool name. R2 ambiguity requires
confirmation. R3 requires explicit approval. R4 stays human-operated.
