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
| `core.runtime_provider` | The owner's durable per-conversation preferred executor (`auto` or `codex`). | Defining route capabilities/order, recording which provider answered, starting a model process, or reading provider transcripts. |
| `core.model_control` | The sanitized model catalog, private harness environment, route order, trust/tool policy, health cooldown application, and upstream-diversity truth. | Starting a model process, parsing provider output, storing conversation preference, or treating model prose as a receipt. |
| `core.codex_fallback` | One bounded, owner-private Codex CLI execution; process control; and one durable Codex thread per Lark conversation. | Provider preference, group/untrusted traffic, or cross-session discovery and projection. |
| `core.cross_session` | Bounded discovery, parsing, redaction, recent projection, and incremental digest of owner-operated Claude Code/Codex sessions. | Selecting or invoking a provider, claiming that an external session completed work, or replacing provider transcripts as source of truth. |
| `core.cross_session_index` | A private, rebuildable SQLite index of redacted owner-operated session turns and query-focused historical projection. | Copying tool payloads, entering groups/Matters, or becoming authority for mutable facts. |
| `core.matter_bridge` | The provider-neutral Lark conversation-turn ledger and the actual provider/model/session record after a successful answer. | Choosing a provider, invoking a model, or scraping external coding sessions. |

The short version is:

```text
runtime_provider stores the owner's route preference
model_control turns config + health + context into an eligible route plan
codex_fallback executes one allowed Codex turn
cross_session observes recent external interactive context
cross_session_index retrieves relevant older external context
matter_bridge records the route that actually answered
```

### Control Flow

```mermaid
flowchart LR
    User["Owner command or saved preference"] --> Choice["runtime_provider: route preference"]
    Choice --> Policy["model_control: catalog + route plan"]
    Policy --> Router["harness applies bounded route sequence"]
    Router -->|Codex selected or fallback reached| Codex["codex_fallback: bounded Codex turn"]
    Router -->|Other provider selected| Other["Claude or GPT adapter"]
    Codex --> Actual["matter_bridge: actual provider/model record"]
    Other --> Actual

    Interactive["Owner-operated Claude Code / Codex sessions"] --> Continuity["cross_session: discover, parse, redact"]
    Continuity --> Prompt["Prompt context"]
    Continuity --> Digest["Heartbeat digest"]
    Continuity --> Index["cross_session_index: private historical index"]
    Index --> Query["Query-focused historical context"]
    Continuity -. no provider invocation .-> Router
```

### Change Routing

- Change “prefer Codex / prefer Claude” persistence in `core.runtime_provider`.
- Change route definitions, order, capability/trust policy, cooldown
  application, upstream diversity, or `/model` route truth in
  `core.model_control`.
- Change Codex CLI arguments, timeout/process behavior, sandboxing, or durable
  Lark-to-Codex thread reuse in `core.codex_fallback`.
- Change which external sessions are found, excluded, redacted, parsed, or
  projected in `core.cross_session`.
- Change historical indexing, retention, or query ranking in
  `core.cross_session_index`.
- A model response is never completion evidence. Cross-session context may
  help reconstruct intent, but authoritative receipts and domain state still
  decide whether work finished.

### Dependency Rule

`model_control` must not call an execution adapter. `cross_session` and
`cross_session_index` must not call `codex_fallback`, and `codex_fallback` must
not consult cross-session projections to decide whether to run. A harness may
apply the route plan, call an adapter, and record the actual result through
`matter_bridge`, but execution, continuity, and the conversation ledger must
not create a second preference or route-policy store.

## Architecture Adjacency Check

Use the stdlib-only graph check before and after a broad self-improve round:

```bash
python3 scripts/import_graph.py core --threshold 20
python3 scripts/import_graph.py core --format mermaid --focus core.cross_session
python3 scripts/import_graph.py core --max-direct-cycles 11
```

The first command ranks modules by unique adjacent internal modules and warns
when a module exceeds the chosen review threshold. The second emits a Mermaid
one-hop view suitable for a PR or architecture note. A threshold is a review
trigger, not proof that a module is badly designed; central authority modules
can have intentionally high fan-in. CI can opt into a hard gate with
`--fail-on-threshold` once a reviewed baseline exists. Direct two-module
cycles are different: pytest enforces the reviewed current baseline and fails
on any new pair, while removals need no allowlist change.

## ADR-002: Memorial Boundaries and Failure Evidence

**Status:** accepted

- `core.memorial_ledger` owns append/fold storage primitives.
- `core.memorial_cards` owns card parsing and composition.
- `core.memorial_transport` owns the low-level Lark send attempt and emits
  structured, payload-free failure evidence.
- `core.memorial_contracts` owns shared state values imported by readers.
- `core.memorial` remains the compatibility facade and orchestration layer.

Delivery truth is still `core.delivery` plus SQLite receipts/dead letters.
Structured log events make failure diagnosable; they never substitute for a
receipt and must not contain private card bodies or provider stderr.

The current delivery retry and cap decision is documented, with its state
machine, in `docs/delivery_retry_and_caps.md`.
