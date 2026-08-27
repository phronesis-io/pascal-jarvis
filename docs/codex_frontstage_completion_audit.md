# Codex Frontstage Completion Audit

- Audited: 2026-08-27
- Release candidate: PR #129
- Rule: repository implementation, production deployment, and product
  acceptance are separate claims.

## Requirement Ledger

| Requirement | Authoritative implementation | Verification | Current state |
|---|---|---|---|
| Codex is the normal desktop/mobile interaction surface | `PRODUCT.md`, `docs/codex_jarvis_user_journey.md`, repo-owned `jarvis-matters` plugin | Plugin manifest/install tests and real stdio MCP smoke | Implemented in release candidate; production plugin install pending release |
| Jarvis owns durable continuity rather than another chat UI | `core.codex_frontstage`, `core.matter_runs`, `core.matter_context` | Matter contract, continuation, lease, recovery, and context tests | Implemented |
| A clean task can continue the right Matter without replaying raw history | `jarvis_matter_continue`, Context Packet v2 | Ambiguous/missing/exact continuation and provenance tests | Implemented; real desktop/mobile acceptance pending |
| Codex, Claude Code, and Lark share current cross-product memory | `core.memory_compiler` and source-linked claims | Compiler, conflict, privacy, and cross-session E2E tests | Implemented in release candidate; production replay observation pending |
| Models, harnesses, and product state are independent | `core.model_control`, provider adapters, Matter contract | Provider/fallback/continuity tests | Implemented; provider quota remains honestly unknown where no API exists |
| Package usage is visible without opening billing pages | `core.model_usage`, Codex MCP, deterministic owner-Lark query | Model-usage and Matter-continuity tests plus read-only local smoke | Implemented in release candidate |
| Git/GitHub remains the code-evidence plane | Native Git/GitHub plus existing `git`/`github` Matter artifact providers | Product-contract test and existing artifact/provider tests | Product boundary complete; no duplicate Jarvis Git state machine is intended |
| Lark is a bounded wake-up/native-integration surface | Existing unified delivery and the frontstage journey contract | Delivery, attention, Lark transport, and weekly review tests | Retained until Codex acceptance passes; no long-output migration claimed |
| Result evidence cannot silently complete a Matter | `core.matter_runs`, `core.matter_closure`, Delegation verifier | Receipt, closure, effect, and result-review tests | Implemented |
| One owner confirmation converges linked state | `core.matter_closure` | Intent/Item/Handoff reconciliation and replay tests | Implemented in release candidate |
| Desktop/mobile migration is measured by the user, not the Agent | `core.frontstage_acceptance`, bounded MCP tools, plugin skill | Exact-label, once-only prompt, immutability, version-binding, and MCP E2E tests | Instrumentation complete; 20 desktop + 20 mobile production samples pending |
| The release cannot claim a plugin that does not start | governed deploy plus `scripts/check_codex_frontstage.py` | Installed-plugin readback and real stdio tool handshake | Implemented; same-revision production deploy pending Owner authorization |

## Normal Use Contract

- Start ordinary work in a fresh Codex task. Jarvis stays absent unless the
  outcome must survive the task, device, product, executor, or day.
- Use native Git/GitHub for code history, diffs, PRs, review, CI, and merge.
  Bind only the evidence needed by a durable Matter.
- Talk directly to Jarvis in Lark for quick capture, urgent/time-bound replies,
  native Lark actions, or when Codex is unavailable.
- Ask Codex for prior decisions, package usage, current Matters, completed
  outcomes, and next actions; these read Jarvis backstage state through MCP.
- Start a new Codex task for a distinct outcome. Reuse the Matter, not an
  indefinitely growing chat, when the underlying outcome continues.

## Remaining Release Evidence

The release candidate is not the production product until all of these are
true for one exact merged SHA:

1. explicit Owner release authorization;
2. merge to protected `main` with CI green;
3. governed deploy installs dependencies and plugin, then restarts the runtime;
4. same-revision component, delivery, provider, and stdio MCP smoke pass;
5. post-release L3 observation finds no new P0/P1 regression;
6. 20 real desktop and 20 real mobile continuation samples meet the published
   quality thresholds before any corresponding Lark path is reduced.

Tests and Agent prose cannot manufacture any of these production observations.
