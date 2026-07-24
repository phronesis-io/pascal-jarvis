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

## Interaction Rules

- Ask only when ambiguity changes the target, authority, cost, or irreversible
  outcome.
- A success phrase must be generated from a structured success state.
- `verifying` is shown as "executed, awaiting verification", never "done".
- A repeated callback or retry returns the original receipt.
- An explicit "send again" creates a new contract version.
- Missing evidence means unknown, not failure and not success.
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
