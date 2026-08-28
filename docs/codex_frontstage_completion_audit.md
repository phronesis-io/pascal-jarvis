# Codex Frontstage Completion Audit

- Audited: 2026-08-28
- Production baseline: `1254b84` (PR #131); later release candidates remain
  separate until reviewed, authorized, merged, deployed, and observed
- Rule: repository implementation, production deployment, and product
  acceptance are separate claims.

## Requirement Ledger

| Requirement | Authoritative implementation | Verification | Current state |
|---|---|---|---|
| Codex is the normal desktop/mobile interaction surface | `PRODUCT.md`, `docs/codex_jarvis_user_journey.md`, repo-owned `jarvis-matters` plugin | Plugin manifest/install tests and real stdio MCP smoke | Plugin deployed at PR #131; current pre-install task cannot gain tools retroactively; real desktop/mobile acceptance pending |
| Jarvis owns durable continuity rather than another chat UI | `core.codex_frontstage`, `core.matter_runs`, `core.matter_context` | Matter contract, continuation, lease, recovery, and context tests | Implemented |
| A clean task can continue the right Matter without replaying raw history | `jarvis_matter_continue`, Context Packet v2 | Ambiguous/missing/exact continuation and provenance tests | Implemented; real desktop/mobile acceptance pending |
| Codex, Claude Code, and Lark share current cross-product memory | `core.memory_compiler` and source-linked claims | Compiler, conflict, privacy, and cross-session E2E tests | Deployed; ongoing production replay observation remains part of Phase 2 acceptance |
| Models, harnesses, and product state are independent | `core.model_control`, `core.model_runtime`, provider adapters, Matter contract | Route, effect-replay, receipt, provider/fallback, and continuity tests | Control plane is in PR #133, heartbeat in #135, and owner-private Lark migration is in the current stacked candidate; shared/untrusted Lark migration and provider quota without an API remain open |
| Package usage is visible without opening billing pages | `core.model_usage`, Codex MCP, deterministic owner-Lark query | Model-usage and Matter-continuity tests plus read-only local smoke | Deployed; exact Codex windows and honest unknowns are available through the MCP contract |
| Every legacy capability has an explicit place in the new product | `capability_product_policy.yaml`, generated capability inventory | Exact policy coverage, migration-gate, retired-surface, and drift tests | 185 active capabilities classified: 82 keep, 87 quiet, 16 replace-with-codex, 0 unreviewed; release pending |
| Git/GitHub remains the code-evidence plane | Native Git/GitHub plus existing `git`/`github` Matter artifact providers | Product-contract test and existing artifact/provider tests | Product boundary complete; no duplicate Jarvis Git state machine is intended |
| Lark is a bounded wake-up/native-integration surface | Existing unified delivery and the frontstage journey contract | Delivery, attention, Lark transport, and weekly review tests | Retained until Codex acceptance passes; no long-output migration claimed |
| Result evidence cannot silently complete a Matter | `core.matter_runs`, `core.matter_closure`, Delegation verifier | Receipt, closure, effect, and result-review tests | Implemented |
| One owner confirmation converges linked state | `core.matter_closure` | Intent/Item/Handoff reconciliation and replay tests | Deployed |
| Desktop/mobile migration is measured by the user, not the Agent | `core.frontstage_acceptance`, bounded MCP tools, plugin skill | Exact-label, once-only prompt, immutability, version-binding, and MCP E2E tests | Instrumentation complete; 20 desktop + 20 mobile production samples pending |
| The release cannot claim a plugin that does not start | governed deploy plus `scripts/check_codex_frontstage.py` | Installed-plugin readback and real stdio tool handshake | Deployed at PR #131; later connector changes still require the same governed evidence |

The production plugin is installed and its MCP registration is enabled, but the
Codex task used for this audit started before that install and therefore does not
have the Jarvis tools in its immutable task toolset. Its real thread ID remains
linked to a terminal Matter; PR #132 is the tested terminal-link rebind fix. A
new Codex task is still required to collect the first honest desktop acceptance
sample after release. Neither an independent stdio smoke nor this shell-based
audit counts as user acceptance.

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
