# Jarvis Product

## Purpose

Jarvis is a persistent personal AI system that helps a person use their time,
attention, and judgment for higher-value work and life. It is not a
notification maximizer, a generic chatbot shell, or a substitute for human
agency.

## Primary User Outcomes

Jarvis should let the user:

- speak naturally in Lark and receive a concise, context-aware response;
- batch ordinary decisions inside Lark — one morning docket card, not a
  card storm and not a second inbox;
- continue one body of work across Lark, Claude Code, and Codex;
- delegate an external action and know whether it was truly completed;
- see which provider/model actually handled a request and whether each fallback
  is currently usable, without exposing credentials;
- trust reminders, delivery status, system health, and model fallback;
- preserve useful context across sessions without leaking private data;
- spend less time operating the assistant than the assistant returns.

## Product Surfaces

Product expansion is frozen as of 2026-08-17. Existing behavior remains
supported, and reliability, privacy, documentation, evidence, and engineering
debt work continue. The freeze forbids silently adding another surface,
notification lane, task system, or authority level; it does not turn off an
existing user-defined Routine.

Ruling (2026-08-07, reaffirmed 2026-08-11): **Lark is the product.** A
feature counts as delivered only when the user can complete it inside Lark —
see it, decide, see the result. Web pages are archives and operator
references, never delivery. Measured basis: over 14 days to 2026-08-11, Lark
cards were read 95.7% while web cards were read 1.8%.

- **Lark conversation**: the sole delivery surface — dialogue, cards,
  decisions, alerts, and the daily docket. Anything that leaves Lark leaves
  the product.
- **Item ledger + morning digest**: ambient and archival content is recorded
  in the Item ledger without a card; the morning anchor batches accumulated
  entries into one line (threshold ≥5). This is the current implementation of
  principle 12 — quiet is not invisible.
- **Jarvis Calendar**: the next concrete fire time and closure state of
  existing Intents; it is not the engineering task-health calendar.
- **Dashboard (`:3457`)** — frozen: archive + ops reference. No new features
  land here.
- **Mobile gateway (`:3458`)** — retired (2026-08-11, completed 2026-08-14);
  Jarvis has no Tailscale runtime, setup, health check, or recovery path.
- **Routines** — existing definitions remain active in Lark; product expansion
  is frozen pending evidence for consolidation or retirement.
- **Matter detail**: durable topic context and continuation, not a second
  inbox.
- **Claude Code and Codex**: deep execution environments attached to the same
  Matter.
- **Admin and Ops (`:3456`)**: operator-only diagnosis and recovery, never a
  daily user workflow.

## Product Principles

1. Human value over system activity.
2. One event, one visible Item, one authoritative state.
3. Conversation is sparse; routine work is batched.
4. Evidence before completion language.
5. Ambiguity stops external action.
6. Life practices are supported without turning life into a completion score.
7. Private data stays local and purpose-bounded.
8. A degraded model or channel is visible when it changes trust.
9. Failure is honest, bounded, and recoverable.
10. New complexity must retire more confusion than it creates.
11. Automatic capture earns authority through reviewed production evidence;
    code existence or elapsed time never promotes it.
12. Quiet is not invisible: anything removed from an interrupting channel
    needs a named, searchable surface and a bounded way to regain attention.
13. The user can add a recurring behavior without a release, and anything that
    can act on its own carries a declared authority level and a readable
    record of what it did.
14. Where a source sits in the attention hierarchy is answered by measured
    engagement, not by a hand-edited list nobody revisits — and every
    adjustment is announced.

## Non-Goals

- Supporting every chat or notification backend before a real user needs it.
- Converting every utterance into a task, Intent, or Delegation.
- Showing internal tool narration or chain-of-thought.
- Letting an LLM invent completion, identity, delivery, or health facts.
- Building a company-wide project-management suite inside the personal
  assistant.
- Adding a second mobile/web inbox, Tailscale path, device pairing flow, or
  product surface while the product freeze is in force.

## Success Measures

- verified external-action completion rate;
- wrong-target and duplicate-action rate;
- user messages needed to finish one intent;
- proactive messages per useful outcome;
- ordinary decisions resolved in batch rather than chat;
- Lark-to-executor continuation success;
- silent component outage duration;
- false completion and private-data leakage, both targeted at zero.
