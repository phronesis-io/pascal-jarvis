# PRD: Unified Delivery and Memorial-First Items

- Date: 2026-07-23
- Status: Implemented
- Owner: Pascal
- Product principle: protect human attention; every output has one durable
  state and every decision has one visible object.

## 1. Decision

Jarvis has one user-facing work object: an **Item**, implemented by Memorial.

- Memorial is the visible card and decision contract.
- Matter is an optional topic used to group and resume work.
- Intent is an internal timer and closure state. The user sees only that an
  Item has a timed reminder.
- Lark, web/PWA, bot replies, heartbeat, and Guardian all submit the same
  delivery envelope to `core.delivery`.

This replaces channel-specific retry, deduplication, throttling, quiet-hour,
queue, and dead-letter decisions with one SQLite-backed state machine.

## 2. Human Outcome

The product is successful when Pascal can:

1. open `Items` on desktop or phone and immediately see what needs action;
2. batch ordinary decisions without turning Lark into a task wall;
3. receive only urgent, time-bound, conversation-bound, or native Lark
   decisions in Lark;
4. close the same Intent from either a Lark button or a web/PWA Item;
5. jump from an Item to its Lark conversation rather than entering a second,
   incomplete web chat;
6. trust that a delivered, read, or acted state means the same thing across
   every producer.

The design optimizes time returned to the user, not notification volume.

## 3. Vocabulary

| User concept | Internal object | Meaning |
|---|---|---|
| Item | Memorial | One visible notice or decision card |
| Topic | Matter | Optional durable grouping and handoff context |
| Timed reminder | Intent | Internal schedule and closure state |
| Delivery | DeliveryEnvelope | One attempt to place output on a surface |

Matter remains a durable system identity for long-running work, but it is no
longer a competing top-level inbox. Its detail page is a drill-down from an
Item topic or a direct handoff link.

## 4. Delivery Contract

### 4.1 Envelope

`DeliveryEnvelope` carries:

- identity: `id`, `source`, `kind`;
- attention: `decision`, `notice`, `alert`, or `reply`;
- routing hints: requested channel, urgent, conversation-bound, reply target,
  chat target;
- entity links: Memorial and Matter IDs;
- policy identity: content hash, explicit dedup key, throttle key;
- forensics: provider, model, and structured metadata;
- payload: text or interactive card JSON.

Producer code may describe intent but cannot decide retries, durable queue
state, global deduplication, or dead-letter handling.

### 4.2 Middleware Order

```text
producer
  -> sanitize
  -> global dedup (6 hours)
  -> source/metric/global throttle
  -> quiet-hours policy
  -> attention route
  -> transport retry (0s, 2s, 5s; 15s timeout)
  -> durable confirmation
```

Sanitization blocks:

- `HEARTBEAT_OK`;
- internal task framing and raw machine envelopes;
- proactive error surfaces;
- English tool-call narration and execution-error fragments;
- the same leaks inside nested Lark card text.

### 4.3 Routing

| Output | Default surface |
|---|---|
| direct reply / quoted reply | Lark reply |
| urgent or active-conversation alert | Lark |
| ordinary decision | phone/web Items |
| routine notice | web `Notice` stream |
| explicit phone route | web/PWA |
| Web Push kind | registered phone subscription |

An explicit, trusted route can override the default. Quiet hours never delay a
direct reply, a conversation-bound output, an urgent alert, or a web placement.

### 4.4 State Machine

```text
queued -> attempting -> delivered -> read -> acted
   |          |
   |          +-> queued after bounded retry exhaustion
   +-> attempting when due

queued/attempting -> suppressed
```

`suppressed` is a durable policy outcome, not a transport failure. Reasons
include leak sentinel, deduplication, throttle, or empty output.

Read receipts update delivery rows by Lark `message_id`. Memorial decisions
update every delivered copy by `memorial_id`.

## 5. Durable State

SQLite WAL in `data/jarvis.db` is authoritative for:

- `delivery_envelopes`;
- `delivery_attempts`;
- `delivery_events`;
- `delivery_dead_letters`;
- `intent_breaches`;
- `schedule_events`;
- `runtime_versions`.

`memorials.jsonl` remains an append-only audit ledger because the card event
history is useful evidence. Legacy delivery/night/breach JSON files are
upgrade adapters and audit inputs only; new policy does not depend on them.

Operational pages read SQLite first and fall back to legacy files only before
the migration is present.

## 6. Items Experience

The `/items` screen is the canonical user inbox.

Filters:

- state: pending, notice, decided, all;
- topic: Matter;
- time: 24 hours, 7 days, 30 days, all;
- review surface: phone batch or immediate Lark.

Each card shows:

- topic label;
- review surface;
- visible title and bounded body;
- optional `Timed reminder` attribute;
- decision buttons;
- source-native links;
- `Go to Lark chat`.

`/memorials`, `/intentions`, and the top-level `/matters` route redirect to
Items. Matter detail remains available at `/matters/{id}` as context, not an
inbox.

APIs:

- `GET /api/items`
- `POST /api/items/{id}/decide`
- `POST /api/items/{id}/chat`
- `GET /api/deliveries`
- `POST /api/deliveries/{id}/confirm`

## 7. Intent Module Boundaries

New production callers use:

- `core.intent_lifecycle`: CRUD and state transitions;
- `core.intent_scheduler`: due selection, inflight reconciliation, breach
  delivery;
- `core.intent_closure`: user closure, re-ask, and closure statistics.

`core.intentions` remains the compatibility implementation and CLI during the
migration. The boundary modules dynamically delegate so existing monkeypatch,
plugin, and CLI behavior remains compatible while new coupling stops.

## 8. Deploy as Verification

Every long-running component registers:

- component and PID;
- Git commit;
- maximum runtime-source mtime;
- start time;
- optional HEARTBEAT hash and parsed task count.

`python3 -m core.deploy verify` detects:

- missing required components;
- dead registered processes;
- a running commit different from `HEAD`;
- source files changed after process start;
- `HEARTBEAT.md` changed or failed to parse after heartbeat startup.

`python3 -m core.deploy smoke --timeout 3` sends a non-interrupting web
delivery through the full policy/state machine and marks it acted only after
successful transport confirmation.

`restart.sh` now:

1. restarts the selected processes;
2. waits for critical component health;
3. requires the delivery smoke to pass within three seconds;
4. verifies bot and heartbeat runtime versions.

The source-controlled pre-commit hook reminds the developer to restart when
runtime code is staged.

## 9. Mobile Onboarding

Pairing is one-time POST confirmation. A successful pair:

1. creates a revocable device credential;
2. stores it in a secure, HTTP-only cookie;
3. writes an audited access event;
4. automatically creates one `Phone connected` notice Item;
5. redirects to the authenticated PWA.

The test Item proves that the device can see the same durable user surface
without sending an extra Lark interruption.

## 10. Migration

1. Create SQLite tables without removing old files.
2. Migrate Memorial delivery to `core.delivery`.
3. Migrate heartbeat output and queue retry.
4. Migrate real-time bot replies.
5. Migrate Guardian alerts and dead-letter escalation.
6. Project scheduler and breach state into SQLite.
7. Introduce `/items`; redirect competing top-level inboxes.
8. Register runtimes and require smoke/verify after restart.
9. Keep compatibility drains until all existing queued records expire.

Rollback is code-only: old ledgers are retained, SQLite changes are additive,
and no historical Memorial event is deleted.

## 11. Security and Privacy

- The pipeline stores payloads locally in the existing Jarvis database.
- Secrets are never added to delivery metadata.
- The mobile gateway still exposes only authenticated `:3458`; neither
  `:3456` nor `:3457` is public.
- Pair links are preview-safe and one-time codes are consumed only by POST.
- Cross-origin dashboard writes require JSON and reject foreign origins.
- Provider/model labels contain model identity, not API credentials.
- Public-repository secret scans remain part of release review.

## 12. Acceptance Matrix

| Requirement | Verification |
|---|---|
| all production output uses one envelope | Memorial, heartbeat, bot, Guardian integration tests |
| no duplicate morning message within 6h | cross-source content dedup test |
| no alert storm | source, metric, and global daily-cap tests |
| no tool narration or sentinel leak | text and nested-card sanitization tests |
| retry is exactly 0/2/5 with 15s transport timeout | attempt ledger tests |
| durable failure recovery | queue and dead-letter tests |
| delivered/read/acted converge | delivery confirmation and Lark read tests |
| web closes Intent | Item decision invokes Memorial action |
| mobile sees canonical inbox | PWA route and pairing notice tests |
| model fallback is attributable | provider/model envelope tests |
| runtime matches deployed code | deploy registration/verify tests |
| HEARTBEAT integrity survives deploy | parsed task count and hash tests |
| old routes do not compete | route redirect tests |
| existing behavior remains compatible | complete repository test suite |

## 13. Metrics

Primary:

- pending decisions older than 24 hours;
- duplicate suppression count;
- delivery queue age and dead-letter count;
- Lark interrupt count by attention class;
- median Item decision time;
- delivery-to-read and delivery-to-acted conversion.

Guardrails:

- sanitized leak count;
- source and global throttle count;
- closure re-ask count;
- runtime version mismatch duration;
- mobile pairing failures and revoked-device access attempts.

## 14. Deliberate Non-Goals

- building a second web chat;
- copying raw Claude/Codex transcripts into Matter;
- exposing Intent as another task manager;
- making every notice a pending decision;
- replacing Memorial's append-only audit history;
- using Tailscale VPN as a daily mobile dependency.
