# PRD: Jarvis Matter Workspace and Mobile Home

- Date: 2026-07-22
- Status: Implemented for personal production use
- Owner: Pascal
- Product principle: one Jarvis, many entrances; entrances are replaceable,
  state is not allowed to fork.

## 0. Delivery record

Implemented on 2026-07-22:

- Phase 1: Matter schema, API, dashboard, recent-session discovery, and PWA.
- Phase 2: bounded handoff bundles, Matter-aware Claude/Codex launcher,
  completion summaries, session links, and artifact links.
- Phase 3: Lark conversation binding and commands, actual-model reporting,
  web-to-Lark memorial card synchronization, Intent/Job/Memorial
  reconciliation, and deterministic EigenFlux/perception routing.
- Phase 4A: authenticated mobile gateway on `:3458`, one-time pairing,
  revocable device credentials, TLS with an installable local CA, access
  audit, and Web Push.

2026-07-22 evening hardening round:

- The PWA manifest fetch defaults to credentials-omit and 401'd at the
  gateway; the manifest link now carries `crossorigin="use-credentials"`.
- The local CA is name-constrained (critical X.509 Name Constraints:
  localhost + RFC1918 + CGNAT). A leaked CA key can no longer mint
  certificates for arbitrary public sites. Pre-existing unconstrained CAs are
  rotated with a `.unconstrained.bak` backup; paired phones re-install the
  new `.cer` once.
- Phase 4B path chosen: Tailscale (userspace tailscaled, no root, outbound
  only, zero public attack surface) instead of a self-built relay. The
  gateway additionally binds loopback so `tailscale serve` can front it with
  a real `ts.net` certificate — off-LAN phone access needs no local CA trust.

2026-07-23 convergence round:

- Attention is now routed by meaning: explicit choices are pending memorials,
  urgent non-choice alerts may still reach Lark, and routine FYI output stays
  in the web `知会` stream. Legacy FYI cards remain readable but no longer
  inflate the pending-decision count.
- Matter detail exposes the same bounded handoff path for Claude Code and
  Codex. The UI, Lark `/matter handoff`, and CLI all produce the canonical
  `scripts/jarvis-matter launch` command.
- The mobile gateway discovers the userspace Tailscale socket, configures
  private `tailscale serve` idempotently, reports its health, and presents LAN
  and tailnet pairing routes separately. Funnel is never enabled by Jarvis.

The installed personal gateway binds only the machine's current private LAN
address (plus loopback for the tailnet path) and proxies only
`127.0.0.1:3457`. A stable internet relay is not
silently enabled: it needs an explicitly owned relay account/domain and a
separate operational decision. Anonymous quick tunnels are rejected because
their address changes after restart and they create an unnecessary public
attack surface. This does not affect phone use on the same trusted network.

## 1. Executive decision

Jarvis will have one first-party home and several replaceable entrances:

- `:3457` is the first-party home for overview, decisions, matters, artifacts,
  and execution state.
- Lark is the real-time conversation channel plus sparse decisions and alerts.
  Routine informational output belongs to the web notice stream.
- Claude Code and Codex are execution runtimes. Their sessions are ephemeral
  workers, not projects or long-term memory.
- EigenFlux is an external agent signal and communication source.
- Lark Docs and local files are artifacts attached to work.
- `:3456` remains an internal admin/API surface and is never part of the
  everyday user journey.

The durable product object is a **Matter**: one recognizable thing Pascal is
thinking about, deciding, or doing. A Matter survives model changes, channel
changes, session rotation, restarts, and deployment.

## 2. Problem

Today the system is capable but cognitively fragmented:

1. A conversation can start in Lark, continue in a Claude Code session, move
   to Codex, produce a document, and end as a card, with no durable object
   connecting the steps.
2. `active_sessions.json` maps a Lark conversation directly to one Claude
   session. It cannot represent several sessions contributing to one piece of
   work or one session handing off to another provider.
3. The dashboard exposes useful surfaces, but not the user's primary question:
   "Where is this matter now, and what is the next move?"
4. The dashboard is responsive but local-only. It has no installable PWA shell,
   remote authentication, or safe mobile access path.
5. Lark, memorials, intentions, jobs, and session files each carry partial
   state. Without a shared identity, UI state and conversation state drift.

The issue is not too many tools. The issue is no single durable home for work.

## 3. North-star experience

Pascal opens Jarvis on the phone and sees only:

1. what needs a decision now;
2. what is currently moving;
3. what changed since the last visit;
4. where to resume any matter.

He can then enter through whichever surface is natural:

- reply in Lark;
- batch-decide on the web;
- open Claude Code for a long research or coding session;
- open Codex for implementation and verification;
- read or edit the resulting Lark Doc.

Every action lands back on the same Matter timeline.

## 4. Product vocabulary

### 4.1 Matter

A durable unit of attention. Examples:

- "Jarvis mobile home and multi-session architecture"
- "EigenFlux onboarding funnel"
- "Prepare the next whitepaper review"
- "Recover exercise after the July bodywork cycle"

A Matter has:

- stable `matter_id`;
- title and concise current summary;
- kind: project, decision, research, personal, or incident;
- status: active, waiting, blocked, done, or archived;
- priority and next action;
- links to sessions, memorials, intents, jobs, artifacts, messages, and commits;
- an append-only event timeline.

### 4.2 Session

One execution or conversation context inside a provider. A session is linked to
a Matter but never owns the Matter. Rotation creates another session link; it
does not create another project.

### 4.3 Artifact

A durable output: Lark Doc, local file, PR, commit, report, spreadsheet, or
external URL. Artifacts are linked, not copied into the Matter database.

### 4.4 Memorial

A decision request. Only content requiring explicit attention should remain
pending. Informational events may appear on a Matter timeline without becoming
pending memorials.

## 5. Information architecture

```text
Jarvis Core
  Matter
    Timeline
    Next action
    Links
      Lark conversation/thread
      Memorial
      Intent
      Job
      Claude Code session
      Codex session
      Artifact / commit / PR

Entrances
  Lark IM       -> notify, converse, quick decision
  Web / PWA     -> overview, compare, batch, resume
  Claude Code   -> deep reasoning and tool execution
  Codex         -> implementation and verification
  EigenFlux     -> external agent signals and messages
```

## 6. User journeys

### Journey A: start in Lark, continue in Codex

1. Pascal discusses an idea with Jarvis in Lark.
2. Jarvis creates or selects a Matter.
3. The Lark conversation/thread is linked to that Matter.
4. Pascal opens Codex and selects the Matter, or runs a Matter-aware launcher.
5. Codex receives a bounded context bundle: current summary, decisions,
   relevant artifacts, and next action.
6. On completion, the Codex session, final summary, commits, and files are
   linked back to the Matter.
7. Jarvis creates one memorial only when a decision or review is required.
   Batchable decisions wait on the phone desk; only immediate decisions
   interrupt Lark.

### Journey B: resume from the phone

1. Pascal opens the installed PWA.
2. The home screen shows active Matters and pending decisions.
3. He opens a Matter and sees its current summary and chronological spine.
4. He can mark the next action, open an artifact, or choose "Go to Lark chat".
5. The selected Matter context is injected into the next Lark turn.

### Journey C: several sessions, one matter

1. A Claude Code research session produces a recommendation.
2. A Codex session implements it.
3. Another Codex session reviews and tests it.
4. All sessions appear under one Matter; only their distilled outcomes are
   promoted into the Matter summary.
5. Raw transcripts remain searchable at source and are not copied wholesale.

### Journey D: external Agent signal

1. EigenFlux delivers a signal or PM.
2. Jarvis classifies it as information, conversation, or decision.
3. Information joins an activity feed or existing Matter.
4. Conversation goes to Lark without creating a pending memorial.
5. A real decision creates one memorial linked to the Matter.

### Journey E: finish and archive

1. The final session or decision records the outcome.
2. The Matter receives a concise outcome and links to final artifacts.
3. Open intents and pending memorials are reconciled.
4. The Matter moves to done, then may be archived without deleting history.

## 7. Surface responsibilities

| Surface | Owns | Does not own |
|---|---|---|
| `:3457` | Matters, overview, decision UI, artifact index | model sessions, raw provider transcripts |
| Lark | attention, conversation, quick decisions | canonical work state |
| Claude Code | one execution session | long-term project identity |
| Codex | one execution session | long-term project identity |
| EigenFlux | external messages/signals | user-facing state |
| `:3456` | local admin and operational control | everyday product UX |

### 7.1 Human-centered attention routing

The product exists to help Pascal spend more time on work and relationships
that create value, not to maximize notification handling. A memorial therefore
stores both its semantic attention class and its preferred approval surface.
The same ledger backs every surface, so approving once closes it everywhere.

| Situation | Preferred surface | Product behavior |
|---|---|---|
| Ordinary project, research, publishing, friend, or planning decision | Phone | `手机集中批`; no Lark interruption |
| Urgent decision, active Lark-conversation ask, Lark-native callback | Lark | `飞书即时批`; also remains visible on the phone |
| Calendar decision with a closing time window | Lark | `飞书即时批` |
| Urgent non-choice alert | Lark notification | Clearly says `无需批`; not counted as a pending decision |
| Routine information, sync result, ambient signal | Web notice | `知会 · 无需批`; no Lark push |

New ordinary decisions default to the phone. Historical pending decisions that
were already delivered to Lark remain labeled as Lark decisions so their
existing cards do not become misleading or orphaned.

## 8. Functional requirements

### R1 Matter kernel

- Create, read, update, list, close, and archive Matters.
- Every write creates a Matter event.
- Validate status, kind, priority, and link types.
- Idempotently link external entities by provider + type + external ID.
- Never require migration of the linked system's native store.

### R2 Entity links

Initial types:

- `session` with provider `claude` or `codex`;
- `memorial`;
- `intent`;
- `job`;
- `artifact` with provider `lark`, `file`, `git`, `github`, or `url`;
- `conversation` with provider `lark` or `eigenflux`.

### R3 Session discovery

- Discover recent Claude Code sessions from `~/.claude/projects`.
- Discover recent Codex sessions from `~/.codex/sessions`.
- Read metadata and a short first-user-message label only.
- Do not ingest reasoning, tool payloads, or full transcripts.
- Discovery must be bounded by time and result count.
- Existing historical sessions remain searchable on demand.

### R4 Matter context bundle

The bundle supplied to an executor contains:

- Matter title and current summary;
- next action;
- confirmed decisions;
- selected recent timeline events;
- artifact pointers;
- relevant memory pointers, not the whole memory store.

It excludes unrelated private memory and raw transcripts by default.

### R5 Dashboard

- Add Matters as a primary navigation destination.
- Show active/waiting/done counts.
- Matter cards show status, next action, update time, and linked runtimes.
- Matter detail shows a chronological spine of events and links.
- Create and edit flows use plain language and work on mobile.
- A recent-session picker allows attaching Claude/Codex sessions.

### R6 PWA

- Installable manifest and stable app icon.
- Mobile viewport, theme color, standalone display.
- Service worker caches only the application shell and static assets.
- Dynamic/private API data is network-first and is not stored in a public
  shared cache.
- Mobile navigation keeps Today, Matters, Memorials, and More reachable with
  one thumb.

### R7 Lark handoff

- "Go to Lark chat" says exactly where the conversation will continue.
- Web and Lark decisions update the same ledger.
- A Matter ID can ride in card callback metadata without being shown to the
  user.
- Later phase: update the original Lark card when a web decision changes it.

### R8 Artifacts and outcomes

- Link artifacts without copying their content.
- Record a final outcome separate from the rolling summary.
- A done Matter can retain open follow-up intents only after explicit warning.

## 9. Data model

Add to the existing dashboard SQLite database.

```sql
matters(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  next_action TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  closed_at TEXT
)

matter_links(
  id INTEGER PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  entity_type TEXT NOT NULL,
  provider TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(provider, entity_type, entity_id)
)

matter_events(
  id INTEGER PRIMARY KEY,
  matter_id TEXT NOT NULL REFERENCES matters(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  actor TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
)
```

The unique external-entity key deliberately makes one session belong to one
canonical Matter. Moving a link is an explicit action and creates events on
both Matters.

## 10. API

Initial local API:

- `GET /api/matters`
- `POST /api/matters`
- `GET /api/matters/{id}`
- `PATCH /api/matters/{id}`
- `POST /api/matters/{id}/links`
- `DELETE /api/matters/{id}/links/{link_id}`
- `GET /api/work-sessions?provider=&limit=`

All writes use the existing JSON and Origin guard. Remote exposure requires a
new authentication middleware before host binding changes.

## 11. Security and remote access

### Personal validation phase

- Keep `:3456` and `:3457` on localhost.
- Use an authenticated private network/tunnel for Pascal's devices.
- Do not add a public bind flag as a shortcut.

### Product phase

- A local Jarvis agent establishes an outbound authenticated WebSocket to a
  relay; no inbound home port is opened.
- The phone authenticates with a device-bound session/passkey.
- Relay stores encrypted transit state only; canonical private state stays on
  the Jarvis host.
- Every mutation has CSRF/origin protection, audit identity, and rate limits.
- `:3456` is never proxied.

## 12. Migration

1. Add new tables without changing current stores.
2. Create one dogfood Matter for this project.
3. Link new sessions prospectively.
4. Offer manual linking for recent sessions.
5. Backfill only high-value active matters, never all historical transcripts.
6. After observed use, add automatic classification and linking.

Current scale is small at the structured layer (hundreds of memorials and
intents) but large at the transcript layer (thousands of Claude files). This
is why the migration is links-first and prospective.

## 13. Delivery phases

### Phase 1: usable home (complete)

- Matter tables and core API.
- Recent Claude/Codex discovery.
- Matter list/detail/create UI.
- Manual session attachment.
- PWA shell and mobile navigation.
- Dogfood this redesign as the first Matter.

### Phase 2: automatic handoff (complete)

- Matter-aware Claude/Codex launchers/hooks.
- Context bundle generation.
- Session completion summaries and artifact discovery.
- Lark conversation and memorial linkage.

### Phase 3: channel convergence (complete)

- Neutral delivery adapters.
- Information/conversation/decision routing.
- Web-to-Lark card state synchronization.
- Matter-aware EigenFlux ingestion.
- Decision/alert/notice attention routing plus a persisted review surface:
  ordinary decisions are phone-first, immediate decisions and sparse alerts
  may reach Lark, and routine notices remain web-first.

### Phase 4A: secure personal mobile access (complete)

- Private-LAN TLS gateway bound to the detected private address.
- One-time pairing codes and hashed device credentials.
- Device revocation, access audit, trusted local CA, and Web Push.
- `:3456` remains unreachable through the gateway.

### Phase 4B: private off-LAN access (complete)

- Tailscale Serve fronts only the authenticated `:3458` gateway on the private
  tailnet; it never exposes Funnel or `:3456`.
- The launchd gateway can repair Serve configuration after restart and the
  component manifest reports tailnet-path health.
- Optional native shell only after PWA usage proves a native capability gap.

## 14. Operations

Daily use:

```bash
# List or create Matters
python3 -m core.matters list --status active,waiting,blocked
python3 -m core.matters create "Matter title" --next-action "Concrete next move"

# Continue a Matter in an executor
./scripts/jarvis-matter context mat_xxx
./scripts/jarvis-matter launch mat_xxx codex
./scripts/jarvis-matter launch mat_xxx claude

# Pair or revoke a phone from the CLI (also available under /settings)
python3 -m core.mobile_access pair --label "My phone"
python3 -m core.mobile_access devices
python3 -m core.mobile_access revoke dev_xxx
```

Lark commands:

```text
/matter new <name>
/matter use <matter_id>
/matter current
/matter list
/matter done <outcome>
/matter handoff codex|claude
/matter clear
/model
```

The phone pairing screen links the public `jarvis-mobile-ca.cer` certificate.
The CA private key, gateway key, VAPID private key, pair codes, device tokens,
and runtime status all stay under ignored local data paths. Only token hashes
are stored in SQLite. Installing and trusting the public CA on the phone makes
the PWA and Push APIs a trusted secure context.

The LAN and tailnet hostnames have separate cookie scopes. Pair on the route
the phone will actually use: LAN pairing needs the local CA; tailnet pairing
uses the `ts.net` certificate and needs only the Tailscale app/account.

## 15. Acceptance criteria

1. A Matter can be created, edited, closed, reopened, and listed through both
   Python and HTTP APIs.
2. A Claude session and a Codex session can be discovered and attached.
3. Reattaching the same session is idempotent; moving it is explicit.
4. `/matters` renders on desktop and a 390px mobile viewport without overflow.
5. A Matter detail page shows links and events in chronological order.
6. The site publishes a valid manifest and service worker.
7. `:3457` remains loopback-only; dashboard content on `:3458` requires an
   active paired device (only pairing and the public CA are unauthenticated).
8. Existing dashboard, memorial, intent, job, and full test suites remain green.
9. Existing uncommitted user work is not modified or included in the change.
10. Lark `/model` reports the provider and model used by the last successful
    delivered reply.
11. A web memorial decision updates every known delivered Lark card copy.
12. A Matter cannot close over a live Intent, Memorial, or Job without an
    explicit audited override.
13. The mobile gateway presents a CA-signed certificate, rejects foreign
    origins, supports device revocation, and has no route to `:3456`.
14. Every pending memorial is labeled `手机集中批` or `飞书即时批`; notices and
    alerts say `无需批`.
15. A normal decision created outside a Lark conversation is durable on the
    phone without sending a Lark card; urgent and calendar decisions still
    reach Lark.

## 16. Metrics

- time to find and resume an active Matter;
- percentage of new Claude/Codex sessions linked to a Matter;
- Matters with an explicit next action;
- duplicate pending decisions across channels;
- number of times Pascal must explain prior context after changing runtime;
- PWA weekly opens versus Lark-only use.

## 17. Non-goals

- Migrating or embedding every historical transcript.
- Replacing Lark immediately.
- Building a native mobile app before PWA evidence.
- Making Jarvis multi-tenant in Phase 1.
- Copying full Lark Docs into SQLite.
- Letting a model silently merge or split Matters without an auditable event.

## 18. Key product decisions

- Matter, not Session, is the continuity primitive.
- `:3457`, not Lark, is the canonical visual home.
- Lark remains the preferred immediate conversation channel during the first
  three phases.
- The phone is the default deliberate decision desk; Lark is the exception for
  time-sensitive attention, not a duplicate inbox.
- Remote access is a security feature, not a port-forwarding setting.
- History is linked and searched on demand; it is not bulk-ingested.
- Better models may replace classifiers and summarizers, but they do not
  replace authority, durable state, or auditability.
