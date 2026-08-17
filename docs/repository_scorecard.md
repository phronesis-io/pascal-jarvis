# Repository Scorecard

- Assessment date: 2026-08-17
- Source assessed: `main` at `7df0b6b` (PR #82)
- Product policy: expansion frozen
- Runtime verdict: governed deployment verified for this source revision

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

## Release Evidence: 100/100 (Deployed)

| Gate | Evidence at snapshot | Verdict |
|---|---|---|
| Focused regressions | Routine/provider batch: 48 passed | Pass |
| Full local gate | 3,103 passed, 4 skipped; shell syntax and ShellCheck passed | Pass |
| Protected PR CI | PR #81 and PR #82 workflows passed | Pass |
| Merge to `main` | PR #82 merged as `7df0b6b` | Pass |
| Trusted review / owner receipt | Admin-owner exact merge-SHA decision recorded after merge with a substantive reason | Pass |
| Merged-main CI | Required `test` check passed on `7df0b6b` | Pass |
| Release gate | `core.release_gate` accepted branch protection, checks, PR, and owner evidence without policy changes | Pass |
| Governed restart | `restart.sh --yes` completed its settle and verification sequence | Pass |
| Runtime revision and components | daemon, bot, heartbeat, Admin, and Dashboard receipts all report `7df0b6b`; 17/17 configured components healthy | Pass |
| Delivery/provider/UI smoke | Acted delivery receipt; Primary, Backup 1, Codex, and GPT canaries healthy; desktop and 390px mobile UI clean | Pass |
| Post-release L3 observation | Four observations processed with no coverage or reconciliation errors and no automatic proposal creation | Pass |

This release used the repository's explicit zero-required-review owner path.
The conversation authorization was converted into a GitHub receipt bound to
the merge SHA; the gate then independently verified admin authority and the
receipt timestamp. Future releases must repeat the same evidence chain rather
than treating this decision as standing approval.

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

`GO`: `7df0b6b` is the verified resident release. Continue only
behavior-preserving engineering under the product freeze; a later merge is a
new candidate and must earn its own release evidence.
