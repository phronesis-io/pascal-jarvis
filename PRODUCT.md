# Jarvis Product

## Purpose

Jarvis is a persistent personal AI system that helps a person use their time,
attention, and judgment for higher-value work and life. It is not a
notification maximizer, a generic chatbot shell, or a substitute for human
agency.

## Primary User Outcomes

Jarvis should let the user:

- speak naturally in Lark and receive a concise, context-aware response;
- batch ordinary decisions on phone or desktop without cluttering chat;
- continue one body of work across Lark, phone, Claude Code, and Codex;
- delegate an external action and know whether it was truly completed;
- see which provider/model actually handled a request and whether each fallback
  is currently usable, without exposing credentials;
- trust reminders, delivery status, system health, and model fallback;
- preserve useful context across sessions without leaking private data;
- spend less time operating the assistant than the assistant returns.

## Product Surfaces

- **Lark conversation**: immediate dialogue, clarification, urgent or
  conversation-bound decisions.
- **Items on phone/web**: canonical batch-review surface for notices and
  decisions.
- **Matter detail**: durable topic context and continuation, not a second
  inbox.
- **Claude Code and Codex**: deep execution environments attached to the same
  Matter.
- **Admin and Ops**: operator-only diagnosis and recovery, never a daily user
  workflow.

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

## Non-Goals

- Supporting every chat or notification backend before a real user needs it.
- Converting every utterance into a task, Intent, or Delegation.
- Showing internal tool narration or chain-of-thought.
- Letting an LLM invent completion, identity, delivery, or health facts.
- Building a company-wide project-management suite inside the personal
  assistant.

## Success Measures

- verified external-action completion rate;
- wrong-target and duplicate-action rate;
- user messages needed to finish one intent;
- proactive messages per useful outcome;
- ordinary decisions resolved in batch rather than chat;
- cross-device continuation success;
- silent component outage duration;
- false completion and private-data leakage, both targeted at zero.
