# Jarvis Experience Design

## Attention Model

Every user-facing output declares one attention class:

- `reply`: stay in the active Lark conversation;
- `decision`: a Lark card awaiting 批红 (REQ-119: the phone/web review desk
  is retired — Lark is the only surface a decision can wait on);
- `alert`: interrupt only when time or safety materially requires it;
- `notice`: a Lark card that demands nothing and leaves the live queue after
  24 hours — except ambient monitoring
  exhaust (`AMBIENT_SOURCES`), which stays ledger-only and reaches the user
  as one batched morning-anchor line.

The system must not route content based on what a transport happens to support.
It routes based on the human attention cost.

## Surface Responsibilities

Surface reality check (owner verdict, 2026-08-07): every real interaction —
taps, replies, reads — happens in Lark; recorded web-dashboard traffic is
zero and the phone desk was never successfully paired. Therefore:

- **Lark is the product.** A user-facing feature counts as delivered only
  when its full loop (see it, decide it, see the outcome) works inside
  Lark. Web visibility is not delivery.
- **Lark arrival volume is the product's pulse.** `core.presence` pages
  selfmon when it falls below floor; treat that page as a P0, not a metric.
- **Web dashboard is retired** (frozen 2026-08-07, retired 2026-08-21):
  archive duty lives in the morning-anchor batch line and the Admin console
  (`:3456`); content that only reaches the archive gets its one shot via the
  morning-anchor batch line. The mobile gateway (`:3458`) and all
  Jarvis-owned Tailscale setup, routing, and recovery paths are retired.
- Routines is retained only for completed, evidence-backed recurring analysis.
  Exercise nags and integration-test schedules are retired; product expansion
  stays frozen.

### Lark

- Short, immediate, conversational.
- Clarify ambiguous targets before an external action.
- Report verified outcomes in one line with the human recipient/object name.
- Start every proactive card with a compact `已完成` receipt. A proposal may
  ask for the remaining irreversible choice, never for research Jarvis could
  have completed first.
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
- Actionable Items are delivered and resolved in Lark; the delivery ledger
  keeps history for archive and diagnosis (inspected through the Admin
  console and CLI), not as a second inbox.
- Pending decisions lead the Lark docket; ambient notices stay ledger-only and
  return as one bounded morning-anchor summary.
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
- Supports continuation into an executor.
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
- Lark and executor sessions operate the same Item and Delegation IDs. A
  Handoff moves attention; it does not copy state.
- Do not infer that calendar presence means physical presence, or that missing
  activity signals mean inactivity.

## Mobile Rules

- The dedicated mobile gateway (`:3458`) and all Jarvis-owned Tailscale paths
  are retired; Lark is the only mobile surface.
- A compact preview must never destroy source text. The full body is preserved
  through card adoption, stored in the private Memorial ledger, and stripped
  from the outbound card envelope before delivery.

## Content and Visual Rules

- Cards are compact and decision-first.
- Compact is not lossy: the card is a summary surface, while its full source
  remains reachable in the same Lark conversation without typing a command.
- Use familiar icons for controls and text for consequential commands.
- Avoid cards nested inside cards and explanatory marketing copy inside the
  product.
- Text must wrap without overlap on phone and desktop.
- Color communicates state but never carries the only meaning.
