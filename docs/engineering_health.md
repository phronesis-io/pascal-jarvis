# Engineering Health

This is current-state engineering knowledge, not a count of old audit claims.
Static findings must be reproduced against the repository before they become
work. The supported product surface remains the generated
`docs/capability_inventory.md`.

## Current Evidence

- Source baseline for this review is `main` at `7df0b6b` (PR #82), following
  model/memory/delivery hardening in PR #81. Both PR test workflows passed.
  PR #82 also passed merged-main CI, exact-SHA admin-owner release approval,
  the fail-closed release gate, governed restart, runtime revision checks,
  component/delivery/provider/UI smoke, and post-release L3 observation.
- The generated inventory has 231 active capabilities: 231 `keep`, 0 `fix`,
  0 `retire-candidate`. A `keep` row means definition, implementation,
  entrypoint, and executable-test reference exist; it is not a coverage claim.
- `core.cross_session` is a small facade over discovery, parsing, historical
  indexing, and projection modules.
- Routine infrastructure failure no longer spends an occurrence as
  `no_output`: the post-hook records `deferred`, re-arms on a bounded delay,
  and keeps receipt removal behind the SQLite commit. Focused Routine and
  heartbeat regressions cover the distinction.
- Memorial storage, card composition, transport, and shared state contracts
  live in `core.memorial_ledger`, `core.memorial_cards`,
  `core.memorial_transport`, and `core.memorial_contracts`. The facade still
  owns orchestration and interaction workflows and remains a refactor target.
- The strict test write guard is enabled by default. `JARVIS_TEST_STRICT_GUARD=0`
  is a diagnostic opt-out and cannot be quoted as release evidence.
- The provider scenario extracts the relevant `bot.sh` production handler
  functions verbatim and executes them in an isolated harness: Claude limit ->
  Codex lock/session -> reliable Lark delivery -> provider/model turn record ->
  next-prompt continuity. It does not claim to start the full listener process;
  startup and wiring are covered separately by shell/install/runtime checks.
- Bot delivery no longer shares the user OAuth/Keychain failure domain.
  `core.lark_bot_transport` uses the private app credential, in-memory tenant
  tokens, and verified `message_id` receipts; owner calendar/docs/mail/task
  capabilities remain independently fail-closed behind user OAuth.
- Runtime line coverage is not currently measured: the repository has no
  `coverage.py`/`pytest-cov` configuration. Ratios between changed test lines
  and changed implementation lines are review-volume indicators, not coverage
  percentages, and must not be used to compare this repository with another
  codebase.
- The generated inventory and more than 3,000 collected tests demonstrate a
  broad executable contract, not uniform branch coverage. The current release
  supplied governed restart, real receipts, canaries, and post-release
  observation; every later revision must supply its own evidence again.
- `core/memorial.py` is 3,477 lines with 115 functions; its longest function is
  165 lines. `core/intentions.py` is 3,605 lines with 84 functions; its longest
  function is 324 lines. These are verified maintainability risks even though
  line count alone is not a defect.
- A Python-wide scan for commented-out `def`/`class` declarations has no
  production matches. The earlier claim of seven such files is not
  reproducible and therefore is not an active cleanup task.

## Verified Audit Decisions

| Claim | Verdict | Evidence / decision |
|---|---|---|
| 66 silent broad catches in memorial/delivery | Fixed as a gate; original count overstated | Broad catches in the conversation-audit, delivery, memorial, and memorial-transport hot paths now either re-raise or emit a privacy-safe structured event; only the logging wrapper itself may fail open. `tests/test_observability_contract.py` parses the current AST so a new silent broad catch fails CI. Narrow exception types further whenever the protected boundary is edited. |
| Conversation audit DB lacks WAL | Fixed | `core.conversation_audit.connect` enables WAL, `synchronous=NORMAL`, and a 5s busy timeout; a regression reads the live pragmas. WAL improves concurrent access; it is not described as the mechanism that prevents SQLite corruption. |
| Memorial import cycles can grow unnoticed | Fixed as a gate; debt remains | `scripts/import_graph.py` reports direct cycles and pytest rejects any pair outside the reviewed 11-pair baseline. Existing deferred cycles remain explicit refactor debt. |
| Memorial/delivery failures use unstructured stderr | Fixed at hot boundaries | Memorial transport and orchestration events use `core.log`; delivery emits `retry_batch_exhausted` and `terminal_failure`. CLI output remains ordinary stdout/stderr by contract. |
| `usage_stats`, `matter_bridge`, `routine_evidence`, `card_split`, and `lark_auth` have no tests | False | They are exercised by capability contracts, matter continuity/prompt/provider E2E, routine tests, memorial split tests, and dedicated Lark auth tests. Test-file naming is not coverage evidence. |
| Real clocks make tests flaky | Partly valid policy risk | A raw text scan finds time calls in many tests, but many create input timestamps or use injected clocks. The repository rule is explicit: behavior that depends on time uses an injected clock/timezone-aware fixture. New failures are fixed at the clock boundary, not by freezing the whole suite. |
| Memorial is still a god module | Valid residual debt | Natural storage/rendering/transport/contract boundaries are extracted. Remaining orchestration, callbacks, continuation queues, and adoption flows should be split by behavior with compatibility tests; line count alone does not authorize a rewrite. |
| No formal migration path anywhere | Fixed for current additive migrations | The shared dashboard DB retains its ordered base-schema ledger. Conversation audit, intentions, iteration, and delegation compatibility columns now use one named domain migration executor: pending work obtains an `IMMEDIATE` write transaction before reading state, lock contention retries the whole transaction, compatible physical columns and markers commit together, and type/nullability/default or marker/schema drift fails closed. Rename/type/destructive migrations still require an explicit backup, transform, verification, and rollback plan. |
| Dead letters have no alert | False | The Guardian consumes unnotified SQLite dead letters, alerts through its independent channel, and marks rows only after confirmed notification. The ops page and self-diagnostic projection also expose counts. |
| No pytest config means no timeout | False as a release blocker | CI has a 15-minute job timeout and the strict local suite is the canonical command. Parallel pytest is deliberately not default because tests exercise shared process and SQLite contracts. |
| Old July plans look current | Fixed | `docs/plans/README.md` marks dated plans as historical evidence; current behavior is governed by product/domain/architecture/decision docs and the PRD portfolio. |
| Import graph is not a CI gate | Fixed | The core-cycle budget runs in pytest, while adjacency remains a review signal rather than a brittle hard limit for central authority modules. |

## Debt Retirement Sequence

The two large orchestration modules will be reduced in small, behavior-preserving
changes. They must not be rewritten or split by line count alone.

1. Establish a reproducible runtime coverage baseline for `core/memorial.py`
   and `core/intentions.py`, publish branch coverage as information, and keep
   the strict local suite as the release gate until the baseline is stable.
2. Add characterization tests around the longest workflows and their failure
   boundaries before moving code. The first targets are
   `generate_calendar_intents`, `restore_cancelled_intent`,
   `memorialize_output`, and `decide`.
3. Continue the existing Memorial extraction by moving orchestration into
   workflow modules that depend on `memorial_ledger`, `memorial_cards`, and
   `memorial_transport`; keep `core.memorial` as a compatibility facade while
   callers migrate.
4. Split Intentions by lifecycle ownership: repository/schema access,
   lifecycle transitions, calendar generation, reconciliation, and CLI. Each
   slice requires an import-graph check, focused regressions, the strict local
   suite, and an adversarial review.
5. Remove compatibility facades only after the capability inventory proves
   there are no live callers. Production data migrations require backup,
   verification, and rollback evidence.

This debt program follows the current audited product-closure release.
Combining a large module split with memory, provider-routing, and attention
behavior changes would make regressions harder to attribute and weaken the
release evidence for both changes.

The 2026-08-17 product freeze makes this sequence the default development
direction: preserve behavior, improve evidence, and reduce coupling before
considering any new product capability.

## Retention Rule

Do not delete a capability because it looks old, has a large module, or has low
traffic. A retire candidate requires an explicit deprecation marker, no active
entrypoint, a replacement/migration decision, and a data-retention review. The
current inventory has no capability meeting that standard, so this round
deletes none.
