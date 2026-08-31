# Codex Frontstage Completion Audit

- Audited: 2026-08-30
- Production baseline: `1254b84` (PR #131); later release candidates remain
  separate until reviewed, authorized, merged, deployed, and observed
- Reviewed integration evidence: local branch
  `release/codex-frontstage-control-plane-20260828` at `03d1cecc`, product tree
  `264ae499`; this 45-commit exploration branch is not the release vehicle
- Review/release candidate: ready PR #139 from
  `release/codex-frontstage-control-plane-20260829`; the PR head is the
  authoritative candidate SHA and remains unset as a release SHA until
  independent review, Owner authorization, and protected-main integration
- Rule: repository implementation, production deployment, and product
  acceptance are separate claims.

## Requirement Ledger

| Requirement | Authoritative implementation | Verification | Current state |
|---|---|---|---|
| A clean Codex task can explain when Jarvis is needed | `core.operating_model`, repo-owned plugin skill, `docs/jarvis_codex_daily_use.md` | Pure contract, immutability, plugin discovery, and real MCP tool tests | Implemented in integration candidate; not yet installed in production |
| Codex is the normal desktop/mobile interaction surface | `PRODUCT.md`, `docs/codex_jarvis_user_journey.md`, repo-owned `jarvis-matters` plugin | Plugin manifest/install tests and real stdio MCP smoke | Plugin deployed at PR #131; current pre-install task cannot gain tools retroactively; real desktop/mobile acceptance pending |
| Jarvis owns durable continuity rather than another chat UI | `core.codex_frontstage`, `core.matter_runs`, `core.matter_context` | Matter contract, continuation, lease, recovery, and context tests | Implemented |
| A clean task can continue the right Matter without replaying raw history | `jarvis_matter_continue`, Context Packet v2, exact wake receipt | Ambiguous/missing/exact continuation, wake-thread binding, terminal projection, and provenance tests | Implemented integration candidate; real desktop/mobile acceptance pending |
| Codex, Claude Code, and Lark share current cross-product memory | `core.memory_compiler` and source-linked claims | Compiler, conflict, privacy, envelope-first protocol, bounded poison-batch, and cross-session E2E tests | Deployed foundation; current candidate prevents quoted idle/error tokens from dropping valid compile envelopes and keeps invalid output on a bounded, auditable failure lifecycle |
| Product state, models, attended executors, and release authority are independent | `core.model_control`, `core.model_runtime`, provider adapters, Matter and release contracts | Route, effect-replay, receipt, provider/fallback, continuity, and release-gate tests | Codex/Claude are owner-started executors; provider choice cannot mutate product truth or grant release authority; shared/untrusted Lark keeps its restricted adapter by design |
| Package usage is visible without opening billing pages | `core.model_usage`, Codex MCP, deterministic owner-Lark query | Model-usage and Matter-continuity tests plus read-only local smoke | Deployed; exact Codex windows and honest unknowns are available through the MCP contract |
| Every legacy capability has an explicit place in the new product | `capability_product_policy.yaml`, generated capability inventory | Exact policy coverage, migration-gate, retired-surface, and drift tests | 186 active capabilities classified: 82 keep, 88 quiet, 16 replace-with-codex, 0 unreviewed; release pending |
| Unattended code mutation is absent | `core.iteration_loop`, `iteration-observe`, capability inventory and policy | No active self-improve heartbeat, mutating harness CLI, script, import, or capability entry; quarantined history remains non-runnable; full regression suite | A real Seatbelt/launchctl probe invalidated process-containment assumptions, so background work now stops at evidence and proposals; owner-started Codex/Claude tasks perform mutation and the normal release gates still apply |
| Git/GitHub remains the code-evidence plane | Native Git/GitHub plus existing `git`/`github` Matter artifact providers | Product-contract test and existing artifact/provider tests | Product boundary complete; no duplicate Jarvis Git state machine is intended |
| Lark is a bounded wake-up/native-integration surface | Existing unified delivery, `core.codex_wake`, and the frontstage journey contract | Delivery, attention, Lark transport, zero-turn preparation, exact wake consumption, stale-projection audit, and weekly review tests | Explicit private handoff can prepare a verified empty Codex task without a run lease; first continuation binds the real thread and advances the receipt; retained until desktop/mobile acceptance passes |
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

## Release Evidence

For `1254b84c72b5cf265203bb1a50c044fb93d62545`, the production release has:

1. explicit Owner release authorization recorded on PR #131;
2. protected-main CI green for the exact merge SHA;
3. governed deploy, plugin install/readback, runtime restart, and release receipt;
4. 15/15 same-revision components, delivery smoke, provider observation, and
   stdio MCP smoke;
5. post-release L3 observation without an execution error.

The draft PR #139 candidate, including connector `0.4.0`, the executable
operating model, interruption contract, model-runtime migration, and unattended-
mutation retirement boundary, is not covered by that old receipt. Its release
requires:

1. independent non-author review of the final candidate SHA;
2. explicit Owner release authorization bound to that exact SHA;
3. merge to protected `main` with CI green;
4. governed deploy installs dependencies and plugin, then restarts the runtime;
5. same-revision component, delivery, provider, and stdio MCP smoke pass;
6. post-release L3 observation finds no new P0/P1 regression;
7. 20 real desktop and 20 real mobile continuation samples meet the published
   quality thresholds before any corresponding Lark path is reduced. Wake
   samples must separately prove task visibility, correct Matter continuation,
   zero premature execution, and no duplicate task on repeated owner action.

Tests and Agent prose cannot manufacture any of these production observations.
