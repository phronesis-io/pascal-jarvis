# Jarvis Experience Design

## Attention Model

Every user-facing output declares one attention class:

- `reply`: stay in the active Codex task or Lark conversation;
- `decision`: one authoritative Item awaiting judgment. During the Codex-first
  migration it may wake the owner in Lark, but it must resolve the same Item
  and Matter reviewed from Codex;
- `alert`: interrupt only when time or safety materially requires it;
- `notice`: a non-interrupting result that leaves the live queue after 24
  hours — except ambient monitoring
  exhaust (`AMBIENT_SOURCES`), which stays ledger-only and reaches the user
  as one bounded review batch.

The system must not route content based on what a transport happens to support.
It routes based on the human attention cost.

## Surface Responsibilities

Surface ruling (owner verdict, 2026-08-27): Codex is available on both desktop
and mobile and is the better place for separate tasks, long material,
artifacts, and execution. The 2026-08-07 evidence still proves that the old
Jarvis-owned web inbox failed; it no longer proves that Lark should own every
interaction. Therefore:

- **Codex is the interactive frontstage.** Substantive work starts or
  continues in a bounded task attached to one Matter.
- **Jarvis is the backstage.** It compiles context, coordinates executors,
  protects authority, and reconciles outcomes without competing for chat
  attention.
- **Lark is a wake-up and native-integration channel.** It retains current
  proactive delivery until the Codex adapter is production-proven, then
  carries only time-sensitive interrupts, small decisions, and native
  calendar/contact/group/document actions.
- **No migration by documentation.** The reachable production path remains
  until desktop and mobile Codex continuation plus result receipts pass their
  acceptance sample.
- **Web dashboard is retired** (frozen 2026-08-07, retired 2026-08-21):
  archive duty lives in the morning-anchor batch line and the Admin console
  (`:3456`); content that only reaches the archive gets its one shot via the
  morning-anchor batch line. The mobile gateway (`:3458`) and all
  Jarvis-owned Tailscale setup, routing, and recovery paths are retired.
- Routines is retained only for completed, evidence-backed recurring analysis.
  Exercise nags and integration-test schedules are retired; product expansion
  stays frozen.

### Codex

- The default surface for a new body of substantive work on desktop or mobile.
- One objective gets one bounded task; a new objective starts a new task rather
  than inheriting an indefinitely growing conversation.
- On acquire, show the Matter objective, settled decisions, relevant evidence,
  constraints, unresolved conflicts, and required result receipt. Do not dump
  raw transcripts or tool narration.
- On release, persist artifacts, decisions, verified effects, blockers, and
  next action back to the Matter before calling the task complete.
- Long results and reviewable artifacts stay here. Lark may wake the owner with
  a concise pointer but must not carry a lossy duplicate of the work.
- A Codex task is replaceable execution context, not the source of product
  truth. Failure to read or write a task never corrupts the Matter ledger.

### Lark

- Short, immediate, and native to communication/time-sensitive workflows.
- Do not send a long analysis merely because it can fit in a card. Prefer one
  useful sentence and continue the Matter in Codex once that handoff is
  production-proven.
- For replies still running after 20 seconds, send one natural progress line;
  after 90 seconds release the conversation to background work. Never expose
  provider names, tool narration, job IDs, retry commands, or log directions.
- Clarify ambiguous targets before an external action.
- Report verified outcomes in one line with the human recipient/object name.
- Require a private, structured work receipt before every proactive card, but
  do not render mechanical `已完成` boilerplate. The visible body says the
  useful result naturally. A proposal may ask for the remaining irreversible
  choice, never for research Jarvis could have completed first.
- A clipped card exposes `查看全文` as a first-class action. One tap sends the
  complete source in receipt-backed chunks; transport failure keeps the last
  confirmed offset so the same button resumes instead of restarting. On a
  narrow screen the preview is capped at six source lines / 480 characters,
  and `查看全文` owns the first action row instead of competing with other
  controls.
- Do not expose retries, tool calls, scheduler logs, or duplicate cards.
- A thread about one Memorial retains that Memorial as its context.

### Ledger and Desktop Archive

- One ledger row represents one matter requiring one reading or decision; an
  informational row older than 24 hours becomes 留中, not attention debt.
- Actionable Items resolve one authoritative state regardless of whether the
  owner reaches it from Codex or a transitional Lark card. The delivery ledger
  keeps transport history, not a second business state.
- Pending decisions lead one bounded review batch; ambient notices stay
  ledger-only and do not create a competing inbox.
- A card the daily attention budget drops is ledger-only, never gone: the cap
  means "no room today", not "no longer true", so it joins that morning
  summary. Suppressions that mean the content is stale (recovery replay of an
  obsolete incident, an expired TTL) stay out of it. A dropped `decision`
  publishes the summary line on its own — the 攒批≥5 threshold governs 周知,
  and holding a lost judgment back for lacking companions is the same silent
  drop the line exists to end. A dropped decision still pending on the
  morning its 48h deadline expires gets one last call in that summary, with
  the clock time — its creation-morning mention arrives while the deadline is
  still over a day away, so without the last call the expiry day itself has
  no surface (the 2026-08-21 broadcast draft lapsed exactly this way).
- Details and evidence are available on demand, not forced into the first card.

### Matter

- Shows current objective, next action, sessions, artifacts, and outcomes.
- Compiles a minimal, traceable Context Packet for a new executor session.
- Accepts one Result Receipt on release and reconciles related Items, Intents,
  Handoffs, and stale sessions.
- Does not compete with Items as a second top-level inbox.

### Ops

- Dense, factual, and diagnosis-oriented.
- Uses live timestamps and authoritative component state.
- Destructive controls require explicit operator intent and honest results.
- Provider/model health shows configured position, observed model, last
  success, latency, and sanitized failure category without credentials.

### Delegation

- Show the current required step, evidence state, blocker, and next action.
- Keep consequential progress inspectable in Lark and detailed evidence
  available to executor sessions without pushing routine transitions.
- Never use completed styling for an unverified mutation.
- Shadow predictions stay out of the active user surface.

## Interaction Rules

- Ask only when ambiguity changes the target, authority, cost, or irreversible
  outcome.
- A success phrase must be generated from a structured success state.
- `verifying` is shown as "executed, awaiting verification", never "done".
- A repeated callback or retry returns the original receipt.
- An explicit "send again" creates a new contract version.
- Missing evidence means unknown, not failure and not success.
- Missing work receipt means no card. It is recorded as withheld at the
  producing boundary and cannot fall through as raw proactive prose.
- Codex, Lark, and other executor sessions operate the same Item, Matter, and
  Delegation IDs. A Handoff moves attention; it does not copy state.
- Do not infer that calendar presence means physical presence, or that missing
  activity signals mean inactivity.
- One matter keeps at most one open intent-authored decision card: the ask
  is keyed `intent-decision:<matter_id>` (the matter, never the identity
  string, which changes once a row is linked), and a reworded twin — from a
  sibling intent or its auto follow-up — folds into the pending card until he
  answers. Tasks that draft asks also see what he answered and what still
  waits in the last 7 days (`core.memorial_verdicts`); a settled matter
  (「先都放着」) is not reopened before its stated deadline.

## Mobile Rules

- The dedicated mobile gateway (`:3458`) and all Jarvis-owned Tailscale paths
  remain retired. Codex is the primary mobile work surface; Lark is the mobile
  wake-up and native-integration channel.
- A compact preview must never destroy source text. The full body is preserved
  through card adoption, stored in the private Memorial ledger, and stripped
  from the outbound card envelope before delivery.
- A mobile handoff must open or identify the exact Matter/task without making
  the user search through an unrelated chat history. Until that path is
  verified, the Lark message must remain independently understandable and may
  not promise a broken deep link.

## Content and Visual Rules

- Cards are compact and decision-first.
- Compact is not lossy: the card is a summary surface, while its full source
  remains reachable in the same Lark conversation without typing a command.
- Use familiar icons for controls and text for consequential commands.
- Avoid cards nested inside cards and explanatory marketing copy inside the
  product.
- Text must wrap without overlap on phone and desktop.
- Color communicates state but never carries the only meaning.
