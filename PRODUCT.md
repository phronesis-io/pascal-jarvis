# Jarvis Product

## Purpose

Jarvis is a persistent personal AI system that helps a person use their time,
attention, and judgment for higher-value work and life. It is not a
notification maximizer, a generic chatbot shell, or a substitute for human
agency.

## Primary User Outcomes

Jarvis should let the user:

- start a clean Codex task on desktop or mobile and continue the right Matter
  without re-explaining settled context;
- let Codex carry deep interactive work while Jarvis quietly preserves
  long-lived state, time, authority, and outcome evidence;
- receive only bounded wake-ups in Lark when a real-time, native Lark, or
  genuinely interrupting event cannot wait for the next Codex review;
- continue one body of work across Codex, Lark, Claude Code, and later
  executors without copying business state between them;
- delegate an external action and know whether it was truly completed;
- see which provider/model actually handled a request and whether each fallback
  is currently usable, without exposing credentials;
- trust reminders, delivery status, system health, and model fallback;
- preserve useful context across sessions without leaking private data;
- ask from desktop or mobile what was previously decided and receive the
  current source-linked answer without replaying whole conversations;
- see package usage, reset time, likely exhaustion, and the active fallback
  route without visiting each provider's billing page;
- ask what was actually accomplished and what is worth continuing next,
  organized by authoritative Matter outcomes rather than Agent activity;
- spend less time operating the assistant than the assistant returns.

## Product Surfaces

Ruling (2026-08-27, superseding the 2026-08-07 Lark-only ruling): **Codex is
the primary interactive frontstage; Jarvis is the continuity and control
backstage; Lark is a bounded wake-up and native-integration channel.** The
earlier evidence correctly retired an unread Jarvis-owned web inbox, but it did
not compare Lark with Codex on mobile. The owner now uses Codex on both desktop
and mobile, where separate tasks, long content, artifacts, and execution are a
better fit than an indefinitely growing bot conversation.

The repository now contains the Phase-1 Codex plugin, local MCP connector, and
verified Context Packet/Result Receipt contract. This is not yet a claim that
the production migration is complete. Until 20 desktop and 20 mobile journeys
pass acceptance, Lark remains the current reliable proactive transport. No
feature may drop its existing reachable path merely because code exists.

- **Codex desktop and mobile**: the primary place to begin, split, continue,
  inspect, and finish substantive work. Each task is a bounded executor
  session attached to a durable Matter, not the long-term source of truth.
- **Jarvis backstage**: Matter identity, memory compilation, time triggers,
  attention policy, authority, model routing, verified effects, and terminal
  reconciliation. It should become less visible as it improves.
- **Lark**: urgent/time-bound wake-ups, concise decision prompts during the
  transition, and native calendar/contact/group/document communication. It is
  not the default home for long analysis or a second task workspace.
- **Item ledger + morning digest**: ambient and archival content is recorded
  in the Item ledger without a card; the morning anchor batches accumulated
  entries into one bounded review. It remains authoritative across surfaces;
  a Codex task and Lark notification must never create separate Items.
- **Jarvis Calendar**: the next concrete fire time and closure state of
  existing Intents; it is not the engineering task-health calendar.
- **Dashboard (`:3457`)** — retired (frozen 2026-08-07, retired 2026-08-21);
  archive duty moved to the morning-anchor batch line and the Admin console.
- **Mobile gateway (`:3458`)** — retired (2026-08-11, completed 2026-08-14);
  Jarvis has no Tailscale runtime, setup, health check, or recovery path.
- **Routines** — constrained recurring analysis: exercise nags and test
  schedules are retired; retained Routines may surface only an evidence-backed
  result with a work receipt. During the product freeze, at most one or two
  routines may be reactivated after real-use review.
- **Matter**: durable topic context, decisions, artifacts, outcomes, and next
  action. It is the cross-surface continuity source, not a second inbox.
- **Claude Code and other harnesses**: optional deep executors attached to the
  same Matter. They do not own product state or model policy.
- **Admin and Ops (`:3456`)**: operator-only diagnosis and recovery, never a
  daily user workflow.

The concrete decision table and desktop/mobile journeys are authoritative in
`docs/codex_jarvis_user_journey.md`.

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
15. Work precedes interruption: every new visible card has private evidence
    of what Jarvis completed, while its visible text leads with the useful
    result in natural language. Missing work evidence suppresses the card;
    informational cards leave the live attention queue after 24 hours.
16. Short Session, long Matter: executor windows may be freely created or
    discarded; settled decisions and outcome state survive outside them.
17. Context is compiled, not dumped: a task receives the smallest traceable
    packet that preserves correctness, never the raw history by default.
18. Frontstage and backstage are different products: Codex owns interactive
    work; Jarvis owns continuity, coordination, authority, and closure.
19. Model, harness, and product policy remain separate. A model outage or
    replacement must not change Matter, permission, or completion semantics.
20. A Jarvis capability survives only when it adds continuity, offline value,
    governance, multi-executor coordination, or verified closure beyond what
    a standalone Codex task already provides.
21. Usage is a product fact, not an operator chore: expose known quota, spend,
    reset, throttling, and predicted exhaustion in one model-runtime view;
    label unknowns honestly and interrupt only when the route is at risk.
22. The protocol stays backstage: the user says “continue” or “done” in normal
    language; deterministic code resolves identity, acquires the run, and
    converges linked state. Ambiguity or missing authority stops the action.
23. Review measures useful outcomes, not visible busyness: only owner-confirmed
    Matter closure counts as completed; executor receipts remain “awaiting
    closure,” and the review itself never mutates work state.

## Non-Goals

- Supporting every chat or notification backend before a real user needs it.
- Converting every utterance into a task, Intent, or Delegation.
- Showing internal tool narration or chain-of-thought.
- Letting an LLM invent completion, identity, delivery, or health facts.
- Building a company-wide project-management suite inside the personal
  assistant.
- Rebuilding a weaker Codex chat, editor, artifact viewer, or task list inside
  Jarvis.
- Treating a provider transcript, session summary, or model statement as the
  durable source of Matter truth.
- Moving every long output into Lark merely because Lark can deliver it.

## Success Measures

- useful closed Matters per ten minutes of owner attention;
- user messages needed to finish one Matter, trending down;
- repeated-explanation rate across new executor sessions, targeted below 5%;
- correct Matter binding and continuation from desktop/mobile Codex, targeted
  at 95% or better with ambiguous cases left unbound;
- proactive interruptions per useful outcome, with ordinary interruptions
  bounded to two per day plus one batch and four-week usefulness at 70% or
  better;
- one open decision per Matter, zero stale decisions after Matter completion,
  and zero informational Items older than 24 hours in the live queue;
- verified external-action completion, wrong-target, duplicate-action, false
  completion, and private-data leakage rates;
- 100% model-call attribution to task, Matter, provider, and model, with cost
  per useful outcome trending down;
- percentage of configured model routes with fresh usage/reset evidence,
  predicted-exhaustion calibration, and zero surprise all-routes exhaustion;
- context-token reduction without regression in replayed continuity tests;
- silent component outage duration and owner-visible self-heal noise;
- active capability count by `keep`, `quiet`, `replace-with-codex`, and
  `retire`, so existence and tests are never mistaken for product value.
