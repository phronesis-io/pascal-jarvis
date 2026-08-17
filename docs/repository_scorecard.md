# Repository Scorecard

- Assessment date: 2026-08-17
- Source assessed: `main` at `7df0b6b` (PR #82)
- Product policy: expansion frozen
- Runtime verdict: not yet verified for this source revision

This scorecard separates source quality from release evidence. A merged PR is
not a deployment, a healthy process is not proof of the running revision, and
a tiny provider canary is not proof that a production-context call will fit
inside the same budget.

## Source Quality: 87/100 (A-)

| Dimension | Score | Evidence |
|---|---:|---|
| Product and architecture | 18/20 | Lark-only product boundary, explicit Item/Matter/Intent/Routine/Delegation responsibilities, model control separated from harness execution, and current-state docs. |
| Correctness and testing | 18/20 | More than 3,000 collected tests, focused provider-continuity and Routine recovery scenarios, strict write isolation, import-cycle budget, shell checks, and protected PR CI. Runtime branch coverage is not yet measured. |
| Security and private data | 14/15 | Bot/user identity separation, private cross-session index, credential redaction, symlink refusal, public-repository hygiene, fail-closed receipts, and verified backups. |
| Reliability and recovery | 13/15 | Unified delivery, direct bot transport, provider cooldown/fallback, safe replay boundary, Routine deferral, session acquire/run/release receipts, Guardian, and dead-letter reporting. Real provider and resident-runtime proof is still required after each release. |
| Maintainability | 11/15 | Cross-session and Memorial boundaries have been extracted and architecture checks exist. `core.memorial` and `core.intentions` remain large orchestration modules; measured branch coverage and smaller lifecycle slices are still needed. |
| Operations and release | 9/10 | Components manifest, generated capability inventory, deploy receipts, exact revision verification, smoke tests, and a fail-closed release gate. The remaining friction is evidence acquisition, not an absent release model. |
| Human value and attention | 4/5 | Lark-first interaction, one ledger, batching, quiet hours, honest closure, and a product freeze protect attention. Production engagement and recall quality remain ongoing observations. |

The codebase is production-oriented and substantially better than the earlier
“large but unstructured” audit implied. Its main engineering risk is no longer
missing mechanisms; it is the interaction between many mechanisms and the two
large compatibility facades. The right next move is measured debt retirement,
not another feature wave.

## Release Evidence: 63/100 (Blocked)

| Gate | Evidence at snapshot | Verdict |
|---|---|---|
| Focused regressions | Routine/provider batch passed locally | Pass |
| Protected PR CI | PR #81 and PR #82 workflows passed | Pass |
| Merge to `main` | PR #82 merged as `7df0b6b` | Pass |
| Trusted review / owner receipt | The owner authorized completion in conversation, but the exact merged-SHA receipt was not yet recorded where `core.release_gate` can verify it | Missing |
| Merged-main CI | Not independently verified in the local release record | Missing |
| Release gate | Cannot pass until the two evidence items above exist | Blocked by design |
| Governed restart | Not run for `7df0b6b` | Pending |
| Runtime revision and components | Existing local receipts referenced PR #81; the current agent sandbox could not inspect process state, so no claim is made | Unverified |
| Delivery/provider/UI smoke | Not run against a proven `7df0b6b` resident runtime | Pending |
| Post-release L3 observation | Requires the verified deployment first | Pending |

The blocked status is correct behavior. Do not weaken `core.release_gate`,
invent a reviewer, treat a chat message as a GitHub receipt, or use
`restart.sh --runtime` to smuggle in changed code.

## What To Keep

- One Lark product surface and one Item/Delivery truth.
- One model control plane that distinguishes upstream, model, harness, tools,
  trust scope, and health.
- Private, bounded cross-session continuity for Claude Code and Codex.
- Direct application-bot delivery independent of user OAuth/Keychain.
- Deterministic Routine scheduling, evidence, autonomy, and audit.
- Fail-closed external-action and release evidence.
- Generated capability inventory and explicit retirement decisions.

## Engineering Priorities Under The Freeze

### P0: finish release evidence

1. Record the exact-SHA owner receipt for the merged revision, or obtain a
   trusted independent review bound to it.
2. Verify merged-main required checks.
3. Run the governed full restart, then prove bot/heartbeat revision,
   components, delivery, provider canary, and local desktop UI.
4. Observe the actual Routine retry and Lark delivery path after release.

### P1: make runtime truth cheap to obtain

1. Keep delivery ratio, oldest due envelope, terminal-failure streak, last
   real receipt, and resident revision first-class health signals.
2. Compare tiny canaries with real-route failures so a small green probe cannot
   mask production-context timeout.
3. Track `deferred`, `no_output`, delivery, and duplicate outcomes for active
   Routines without turning internal recovery into user noise.
4. Measure cross-session retrieval precision, stale facts, missed decisions,
   and rejected context.

### P2: retire debt without changing product behavior

1. Establish reproducible line and branch coverage for `core.memorial` and
   `core.intentions`.
2. Characterize their longest workflows before extracting lifecycle-owned
   slices behind compatibility facades.
3. Reduce reviewed direct import cycles and explain central-module adjacency
   growth rather than hiding it behind arbitrary thresholds.
4. Keep runtime writes outside the public repository and verify backup restore,
   not just backup creation.

## Decision

`GO` for continued engineering on the reviewed source.

`NO-GO` for claiming `7df0b6b` deployed until the exact release evidence,
governed restart, runtime revision, smoke tests, and post-release observation
are all present.
