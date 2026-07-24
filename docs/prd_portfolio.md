# Jarvis PRD Portfolio

- Date: 2026-07-24
- Status: Authoritative portfolio index
- Purpose: distinguish current product contracts from historical records,
  superseded designs, observation-only experiments, and rejected scope.

## 1. Critical Decision

"Implement every PRD" does not mean executing every sentence ever written.
The documents span several generations and contain mutually incompatible
decisions. A requirement is executable only when it is:

1. still tied to a current user outcome;
2. not already implemented or superseded;
3. consistent with the current domain model and authority boundaries;
4. testable against an authoritative signal;
5. worth its added cognitive and operational cost.

Historical PRDs remain evidence. This file is the portfolio authority.

## 2. Portfolio

| Document | Portfolio status | Critical judgment |
|---|---|---|
| `prd_interaction_quality.md` | Historical, substantially shipped | REQ-01 to 20 and most later items became the reliability baseline. REQ-21's ingestion core shipped. REQ-23 was absorbed into the current 30-day calendar/check-in flow. REQ-27 is an ongoing code-placement rule, not a finite product task. |
| `prd_system_iteration_v2.md` | Historical, shipped | REQ-30 to 58 are represented in Intent lifecycle, components, backup, dashboard/Admin, state durability, and alerts. Its P2 table is an archive of architecture options, not an active queue. |
| `prd_interaction_v3.md` | Historical, shipped | REQ-59 to 77 are the v1.0 reliability and interaction baseline. |
| `prd_interaction_v4.md` | Historical with two deliberate shadows | REQ-78, 79.1, 80 to 85, 89, and 90 shipped. REQ-79.2 was rejected after production evidence showed safe parse refusal and bounded retry. REQ-86 and REQ-88 remain observation-only; auto-writing from inferred replies/claims is less trustworthy than explicit domain actions and must not be promoted on age alone. REQ-87 was never allocated. |
| `plans/2026-07-14-*` | Historical, shipped | REQ-91 to 102 cover memory, stream, and group-chat boundaries. |
| `plans/2026-07-20-self-improve-round.md` | Historical, shipped | REQ-103 to 111 shipped and were deployed. |
| REQ-112 to 118 implementation wave | Historical, shipped; documentation backfilled here | Geography, weather, life-log/anchors, one-card-one-matter, and Memorial-bound conversations are in code and tests. |
| `prd_perception_ingestion.md` | Core accepted and shipped; expansion demand-gated | Registry, Signal, dedup, sensitivity, buffers, dry-run, and several adapters exist. Adding every possible Lark/API source would increase noise and permissions without a current outcome. New adapters require a named blind spot and privacy test. |
| `prd_delivery_connectors.md` | Rejected as current scope; partially superseded | `core.delivery` already unifies product delivery across Lark reply, Lark, web, and push. Telegram/Slack/email portability has no current user and would force neutral-card and callback abstractions before evidence. Reopen only with a committed second backend/user. |
| `design_task_system.md` | Consolidated and partly stale | Praxis/poiesis, capture, decay, and weekly review exist. Free-time nudge was later retired by engagement evidence. User-facing work now belongs to Item/Matter/Intent boundaries; do not resurrect a parallel task inbox from this design. |
| `prd_matter_workspace_mobile.md` | Current, shipped | Canonical cross-entry topic and mobile workspace contract. |
| `prd_unified_delivery_items.md` | Current, shipped | Canonical Item, attention routing, and delivery contract. |
| `prd_cross_device_continuity.md` | Current, shipped | Canonical device handoff and pairing contract. |
| `prd_verified_delegation.md` | Active, narrowed by incident evidence | The full generic control plane is too broad to land as one new state machine. Implement connector-specific verified actions first and extract shared Delegation machinery only after two distinct connectors prove the common contract. |

## 3. Active Work

### Completed: Verified EigenFlux friend message

Real incident: a user request to send a directors' liability insurance brief
to a family member's agent was interrupted, incorrectly reported as sent to a
different agent, attempted with a transcribed wrong ID, and succeeded only
after the live friend list was refreshed.

Accepted requirements:

- resolve a human name/remark against the current server friend list;
- reject numeric model-supplied IDs and ambiguous matches;
- support gitignored relationship aliases;
- reserve idempotency before mutation;
- reconcile a send interrupted between server commit and local receipt;
- read message history and verify recipient, content hash, conversation, and
  message ID;
- report `verifying` honestly and never retry an uncertain result manually;
- require a new contract token for an explicit repeat.

Implementation: `core/eigenflux_messages.py`, action integration, local
identity binding, skill guard, cursor pagination, clock-skew-safe receipt
matching, and synthetic regression suite. The generic Delegation control plane
remains an active design, not a completed implementation.

### Completed: Resident SQLite descriptor exhaustion

Real incident: the heartbeat process approached launchd's 256-FD soft limit,
then failed heartbeat locks, DB operations, queue flushes, and interrupted a
live conversation.

Accepted requirements:

- close every short-lived SQLite connection in delivery and scheduler paths;
- regression-test connection closure;
- monitor the resident heartbeat against launchd's real limit, not the much
  larger terminal-process limit;
- restart and sample the deployed process to prove the count stays bounded.

Delivery now uses two short-lived connections for a normal accepted send
(acceptance and attempt), initializes schema once per database inode, and
reuses the attempt connection without holding a transaction over transport.
Continuity no longer borrows the dashboard singleton.

### Completed: Delivery terminality and audit hardening

- rejected payloads retain distinct raw audit hashes;
- delivery attempts accumulate across flushes and terminate at nine;
- only terminal failures create dead letters;
- state update columns are allowlisted;
- operational projections expose failed state;
- runtime verification computes dirty paths once and binds SQL columns
  explicitly.

### L1 Engineering Harness

The repository now carries current-state `AGENTS.md`, `PRODUCT.md`,
`DESIGN.md`, `ARCHITECTURE.md`, and `DOMAIN.md`, plus a local-test skill and
script. These are knowledge, not process history.

## 4. Deliberate Non-Implementation

The following are not hidden backlog:

- Telegram, Slack, and email delivery without a real adopter.
- Automatic promotion of REQ-86 inferred journal writes.
- Automatic promotion of REQ-88 prose-based write claims.
- REQ-79.2 batch parse clamping after safe retry proved sufficient.
- A second personal task system beside Item/Matter/Intent.
- A home-grown clone of taskline inside Jarvis.
- A generic Delegation mega-migration before connector-specific evidence.

Current external constraints and accepted residuals:

- The installed EigenFlux CLI only accepts message content through
  `--content`; the wrapper offers `--content-file`, but the child CLI still
  exposes the value briefly in its process arguments. A real fix requires an
  upstream stdin/file option or a stable direct API.
- The active dashboard log is no longer copy-truncated by heartbeat because
  launchd owns an open append descriptor and concurrent data loss cannot be
  excluded. Bounded rotation requires swapping the file and restarting the
  supervised dashboard as one deploy operation.

These decisions protect simplicity, authority boundaries, and the user's
attention. Reopening one requires new evidence, not elapsed time.

## 5. Engineering Loop Adoption

The three-loop model is adopted in this bounded form:

- **L1**: repo knowledge, tests, localtest, review, CI, deploy evidence.
- **L2**: use an external claim/lease/dependency sidecar when concurrent
  engineering work exceeds one active Agent per worktree. Do not mix this
  queue with Jarvis's personal Task/Intent domain.
- **L3**: conversation audit, production incidents, engagement, component
  health, and user feedback generate proposals. A human owns value and scope;
  only accepted proposals enter L1/L2.

Taskline is a plausible L2 sidecar, not yet a dependency: it has the right
claim/lease/DAG semantics but no stable release history in the reviewed state.
A pilot should use a separate DB and one low-risk queue before adoption.

## 6. Definition of Done

A portfolio item is done only when:

- the accepted contract is represented in code or an explicit rejection;
- regression and affected tests pass;
- public-repo hygiene passes;
- the change is committed and pushed;
- resident services are restarted when required;
- runtime revision, component health, and smoke checks pass;
- real external mutation is tested only with owner authorization;
- the portfolio status is updated without rewriting historical evidence.
