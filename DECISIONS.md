# Jarvis Architecture Decisions

This file records current decisions that are easy to blur when several Agents
work in the same area. It is current-state knowledge, not a history of every
implementation discussion. Product history remains in `docs/prd_portfolio.md`;
runtime topology and domain boundaries remain in `ARCHITECTURE.md`.

## ADR-001: Runtime Choice, Codex Execution, and Cross-Session Context

**Status:** accepted
**Decision:** executor choice, executor implementation, and external-session
continuity are three different responsibilities.

| Module | Owns | Must not own |
|---|---|---|
| `core.runtime_provider` | The owner's durable per-conversation preferred executor (`auto` or `codex`). | Recording which provider actually answered, starting a model process, or reading provider transcripts. |
| `core.codex_fallback` | One bounded, owner-private Codex CLI execution; process control; and one durable Codex thread per Lark conversation. | Provider preference, group/untrusted traffic, or cross-session discovery and projection. |
| `core.cross_session` | Bounded discovery, parsing, redaction, and projection of owner-operated Claude Code/Codex sessions into immediate prompt context and the heartbeat digest. | Selecting or invoking a provider, claiming that an external session completed work, or replacing provider transcripts as source of truth. |
| `core.matter_bridge` | The provider-neutral Lark conversation-turn ledger and the actual provider/model/session record after a successful answer. | Choosing a provider, invoking a model, or scraping external coding sessions. |

The short version is:

```text
runtime_provider stores the owner's route preference
codex_fallback executes one allowed Codex turn
cross_session observes and projects external interactive context
matter_bridge records the route that actually answered
```

### Control Flow

```mermaid
flowchart LR
    User["Owner command or saved preference"] --> Choice["runtime_provider: route preference"]
    Choice --> Router["bot route selection"]
    Router -->|Codex selected or fallback reached| Codex["codex_fallback: bounded Codex turn"]
    Router -->|Other provider selected| Other["Claude or GPT adapter"]
    Codex --> Actual["matter_bridge: actual provider/model record"]
    Other --> Actual

    Interactive["Owner-operated Claude Code / Codex sessions"] --> Continuity["cross_session: discover, parse, redact"]
    Continuity --> Prompt["Prompt context"]
    Continuity --> Digest["Heartbeat digest"]
    Continuity -. no provider invocation .-> Router
```

### Change Routing

- Change “prefer Codex / prefer Claude”, route order, or `/model` truth in
  `core.runtime_provider` and the caller that applies that preference.
- Change Codex CLI arguments, timeout/process behavior, sandboxing, or durable
  Lark-to-Codex thread reuse in `core.codex_fallback`.
- Change which external sessions are found, excluded, redacted, parsed, or
  projected in `core.cross_session`.
- A model response is never completion evidence. Cross-session context may
  help reconstruct intent, but authoritative receipts and domain state still
  decide whether work finished.

### Dependency Rule

`cross_session` must not call `codex_fallback`, and `codex_fallback` must not
consult cross-session projections to decide whether to run. Route selection may
call the execution adapter and then record the actual result through
`matter_bridge`, but execution, continuity, and the conversation ledger must
not create a second preference store.

## Architecture Adjacency Check

Use the stdlib-only graph check before and after a broad self-improve round:

```bash
python3 scripts/import_graph.py core --threshold 20
python3 scripts/import_graph.py core --format mermaid --focus core.cross_session
```

The first command ranks modules by unique adjacent internal modules and warns
when a module exceeds the chosen review threshold. The second emits a Mermaid
one-hop view suitable for a PR or architecture note. A threshold is a review
trigger, not proof that a module is badly designed; central authority modules
can have intentionally high fan-in. CI can opt into a hard gate with
`--fail-on-threshold` once a reviewed baseline exists.

The current delivery retry and cap decision is documented, with its state
machine, in `docs/delivery_retry_and_caps.md`.
