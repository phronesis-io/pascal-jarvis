# Jarvis Experience Design

## Attention Model

Every user-facing output declares one attention class:

- `reply`: stay in the active Lark conversation;
- `decision`: wait in phone/web Items unless urgent or conversation-bound;
- `alert`: interrupt only when time or safety materially requires it;
- `notice`: remain available without demanding action.

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
- **Web dashboard and the mobile gateway are frozen**: archive + ops
  reference only. No new feature investment lands there; content that only
  reaches the archive gets its one shot via the morning-anchor batch line.
- Routines is frozen pending fold-into-Lark or retirement (five days live,
  zero uses).

### Lark

- Short, immediate, conversational.
- Clarify ambiguous targets before an external action.
- Report verified outcomes in one line with the human recipient/object name.
- Do not expose retries, tool calls, scheduler logs, or duplicate cards.
- A thread about one Memorial retains that Memorial as its context.

### Phone and Desktop Items

- One card represents one matter requiring one reading or decision.
- The same Item is resolved everywhere; device handoff never forks it.
- Pending decisions lead, routine notices follow.
- Details and evidence are available on demand, not forced into the first
  viewport.

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
- Keep progress inspectable on phone and desktop without pushing routine
  transitions into Lark.
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
- Mobile and desktop operate the same Item and Delegation IDs. A Handoff moves
  attention; it does not copy state.
- Do not infer that calendar presence means physical presence, or that missing
  activity signals mean inactivity.

## Mobile Rules

- Stable routes can be saved to the home screen.
- Pairing is revocable, audited, and separate from Tailscale availability.
- Network changes and app suspension must preserve the current object and
  resume position.
- Admin is never proxied through the mobile gateway.

## Content and Visual Rules

- Cards are compact and decision-first.
- Use familiar icons for controls and text for consequential commands.
- Avoid cards nested inside cards and explanatory marketing copy inside the
  product.
- Text must wrap without overlap on phone and desktop.
- Color communicates state but never carries the only meaning.
