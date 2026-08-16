# Repository Scorecard

Assessment date: 2026-08-16

This scorecard separates source quality from the runtime Pascal is actually
using. A healthy process tree is not evidence that the product can deliver a
message, and a strong release candidate is not deployed software.

## Scores

### Release candidate: 84/100 (B+)

The `release/model-memory-runtime-hardening` branch is a credible production
candidate. It is not yet a releasable production revision because the GitHub
PR, protected CI, independent trusted review, merge to `main`, and post-merge
runtime evidence have not happened.

| Dimension | Score | Evidence |
|---|---:|---|
| Product and architecture | 18/20 | Explicit model control plane, cross-product session index, unified delivery, capability inventory, L1/L2/L3 lifecycle, and documented ownership boundaries. |
| Correctness and testing | 18/20 | More than 3,000 collected tests, focused provider-continuity scenarios, strict write isolation, import-cycle budget, shell syntax and CI-parity shell checks. Restricted process/socket tests cannot run in the current Codex sandbox. |
| Security and private data | 14/15 | Bot/user identity separation, private SQLite creation, symlink refusal, secure deletion, WAL truncation, fail-closed receipts, and a public-repository hygiene gate. |
| Reliability and recovery | 12/15 | Provider fallback, session acquire/run/release receipts, delivery retries, independent Guardian path, bot API delivery, and explicit delivery-ledger health. User OAuth capabilities still require a human reauthorization boundary. |
| Maintainability | 10/15 | Good documentation and extracted boundaries, but `core.memorial` and `core.intentions` remain large orchestration modules and central-module adjacency is high. Runtime branch coverage is not measured. |
| Operations and release | 8/10 | Components manifest, deploy receipts, revision verification, smoke tests, and a strong fail-closed release gate. The current workflow still depends on GitHub review evidence and a clean production checkout. |
| Human value and attention | 4/5 | Lark-first interaction, memorial approval semantics, quiet hours, deduplication, and proactive intent closure are product strengths. Failure-state explanations and cross-session retrieval quality still need ongoing production measurement. |

### Current production runtime: 58/100 (D+)

The production processes are alive, but the runtime is not healthy enough to
consider available:

- The running bot and heartbeat were started before current runtime-code
  edits; `core.deploy verify` reports revision drift.
- The production checkout contains another agent's uncommitted delivery work.
- The delivery ledger shows a recent consecutive terminal-failure streak.
- Calendar sync is persistently failing at the user OAuth/Keychain boundary.
- The old component report exposes calendar failure but does not include
  delivery-ledger health, so it can look mostly green while Pascal receives
  nothing.

The low runtime score is not an indictment of the candidate code. It records
the gap between validated source and what is currently executing.

## Keep

- One system-owned model control plane; harnesses execute, models are routed.
- One cross-session index for Claude Code and Codex history, with private
  storage and bounded prompt projection.
- One unified delivery ledger with real provider receipts and idempotency.
- Bot-identity Lark delivery independent from user OAuth capabilities.
- Fail-closed release evidence. A restart must never become a deployment
  bypass.
- The capability inventory and explicit retirement process. Features are not
  deleted based on age or file size alone.

## Improve

### P0: restore one trustworthy production revision

1. Freeze the dirty production checkout and reconcile its delivery work
   against the stronger `core.lark_bot_transport` candidate implementation.
2. Open one protected PR from the release branch, pass CI and independent
   review, merge to `main`, then update the production checkout cleanly.
3. Run the governed restart and require revision, component, delivery smoke,
   provider canary, and desktop/mobile UI evidence.
4. Confirm a real Lark receipt clears the delivery failure streak. Keep the
   calendar degraded until owner OAuth is reauthorized; do not weaken Keychain
   storage to make the alert disappear.

### P1: make product health match human experience

1. Track delivery success ratio, oldest due envelope, terminal-failure streak,
   and last real receipt as first-class health signals.
2. Distinguish `self_healing`, `recovered`, `needs_owner_action`, and
   `exhausted` states. Notify Pascal only for the last two states.
3. Fail fast after the first calendar fetch failure instead of issuing the
   remaining daily requests in the same 30-day batch.
4. Add production observations for cross-session recall precision: useful
   retrievals, ignored retrievals, stale facts, duplicate facts, and missed
   decisions.

### P2: retire maintainability debt without a rewrite

1. Establish reproducible line and branch coverage for `core.memorial` and
   `core.intentions`.
2. Add characterization tests around their longest workflows, then extract
   lifecycle-owned slices behind compatibility facades.
3. Turn central-module adjacency growth into a reviewed budget with explicit
   exceptions for authority modules.
4. Add a model-control operations view for route, provider, model, fallback
   reason, health window, cost/usage, and last verified canary.

## Release Decision

`NO-GO` for the currently running production revision.

`CONDITIONAL GO` for the release candidate after GitHub PR, protected CI,
independent trusted review, merge to `main`, clean deployment, and real runtime
receipts. The gate must not be bypassed for a local restart.
