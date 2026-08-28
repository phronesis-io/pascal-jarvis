# Engineering Health

This is current-state engineering knowledge, not a count of old audit claims.
Static findings must be reproduced against the repository before they become
work. The supported product surface remains the generated
`docs/capability_inventory.md`.

## Current Evidence

- The generated inventory has 184 active capabilities with complete engineering
  evidence. Product disposition is now independent: 82 `keep`, 86 `quiet`, 16
  `replace-with-codex`, and 0 `unreviewed`, plus four recorded retired surfaces.
  An engineering `keep` still means definition, implementation, entrypoint, and
  executable-test reference exist; it is not a value or coverage claim. New
  capabilities fail closed until `capability_product_policy.yaml` assigns a
  survival value or an explicit Codex migration gate.
- `core.cross_session` is a small facade over discovery, parsing, historical
  indexing, and projection modules.
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
- Protected CI measures the Python runtime surfaces (`core/`, `tasks/`,
  `handlers/`, `sources/`, `admin.py`, and `daemon.py`) with
  `coverage.py`, including Python subprocesses started by the tests. The
  reviewed 2026-08-26 baseline is 81.3% statements and 73.3% branches.
  `scripts/coverage_budget.py` ratchets the total and critical
  runtime modules; it is a regression floor, not a claim that every path is
  sufficiently tested. Ratios between changed test lines and implementation
  lines remain review-volume indicators and are not coverage percentages.
- `core/memorial.py` is 3,694 lines and its longest function is 181 lines;
  `core/intentions.py` is 3,583/324; `core/heartbeat.py` is 2,556/882; and
  `core/delegations.py` is 2,450/218. These are verified maintainability risks
  even though line count alone is not a defect. Their current file/longest-
  function values are checked into `docs/maintainability_budget.json` and run
  by local and protected CI: debt may shrink, but cannot grow silently.
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
| Cross-session findings can collapse into title-only cards | Fixed at the producer boundary | The deterministic cross-session wrapper owns the sole trusted work receipt. Nested model-authored `TITLE`/`OPTIONS`/`RECOMMEND` lines are flattened into prose and nested `WORKED` claims are discarded before gating, dedup, sent-cache writes, or Memorial parsing. An end-to-end regression proves one complete ledger-only card survives the strict work-evidence gate. |
| Real clocks make tests flaky | Partly valid policy risk | A raw text scan finds time calls in many tests, but many create input timestamps or use injected clocks. The repository rule is explicit: behavior that depends on time uses an injected clock/timezone-aware fixture. New failures are fixed at the clock boundary, not by freezing the whole suite. |
| Memorial is still a god module | Valid residual debt | Natural storage/rendering/transport/contract boundaries are extracted. Remaining orchestration, callbacks, continuation queues, and adoption flows should be split by behavior with compatibility tests; line count alone does not authorize a rewrite. |
| No formal migration path anywhere | Fixed for current additive migrations | The shared SQLite DB (`core/db.py`, formerly `dashboard/db.py`) retains its ordered base-schema ledger. Conversation audit, intentions, iteration, and delegation compatibility columns now use one named domain migration executor: pending work obtains an `IMMEDIATE` write transaction before reading state, lock contention retries the whole transaction, compatible physical columns and markers commit together, and type/nullability/default or marker/schema drift fails closed. Rename/type/destructive migrations still require an explicit backup, transform, verification, and rollback plan. |
| Dead letters have no alert | Fixed, with a configuration boundary | Guardian consumes unnotified SQLite dead letters through an independent process path and marks rows only after confirmed notification. It does not pretend this is an independent channel: its Lark send can fail with Lark. Terminal rows are reconciled after verified transport recovery; only valid unresolved work requeues. A configured external dead-man is required for true out-of-band alerting, and its private URL cannot live in this repository. |
| Guardian repeated alerts / only alerted without repair | Fixed with ownership boundary | Guardian distinguishes confirmed, queued, covered, and genuinely lost alert receipts; only genuine loss raises the persisted hourly macOS fallback. Live component degradation, self-diagnostic staleness, task-level brain-health failures, and a completed automatic restart stay in internal evidence while their supervisors, scheduler, and provider chain retry; a card whose only instruction is “you do not need to act” is forbidden. Self-diagnostic dedup compares only the stable owner action, so unrelated internal warning churn cannot reopen the same OAuth request. A component page is allowed only after two red probes and bounded recovery remains unsuccessful, so manual action is actually required. A previously owner-visible breaker incident still receives one recovery receipt when it closes. EigenFlux treats the live WebSocket loop and five-minute poll as redundant ingress: Guardian never terminates a live self-reconnecting loop because the poll is stale, and a fresh poll covers a missing sidecar. Runtime drift makes recovery explicitly unavailable instead of falsely claiming a watchdog restart; drift and dead-man suppression diagnostics are rate-limited. Legacy dead-letter rows are consumed only after durable acceptance. Exact process ancestry tests prevent touching unrelated Claude/Codex sessions. |
| Private host status reached a monitoring group | Fixed externally and specified here | Host/runtime health is `owner_private`. The external dead-man has a dedicated owner endpoint, group and owner ingress reject the wrong data class, and a failed private route never falls back to a group. This repository sends the same audience/recipient contract on every Guardian envelope and contains no recipient IDs or dead-man secrets. |
| No pytest config means no timeout | False as a release blocker | CI has a 15-minute job timeout and the strict local suite is the canonical command. Parallel pytest is deliberately not default because tests exercise shared process and SQLite contracts. |
| Old July plans look current | Fixed | `docs/plans/README.md` marks dated plans as historical evidence; current behavior is governed by product/domain/architecture/decision docs and the PRD portfolio. |
| Import graph is not a CI gate | Fixed | The core-cycle budget runs in pytest, while adjacency remains a review signal rather than a brittle hard limit for central authority modules. |
| Release success is scattered across terminal output | Fixed | A successful governed or same-revision restart now persists one joined SQLite receipt containing release authority, exact SHA, resident-version proof, critical component results, and delivery smoke. Partial or mismatched evidence fails closed and writes no success row. |
| Large-module debt can grow between audits | Fixed as a ratchet; debt remains | `scripts/maintainability_budget.py` is in local and protected CI. It accepts the verified 2026-08-21 baselines for four orchestration modules and rejects file or longest-function growth. Each extraction lowers the checked-in budget. |
| Local shell validation is stronger than protected CI | Fixed | Protected CI now syntax-checks and ShellChecks the same `bot.sh`, `restart.sh`, `tasks/*.sh`, and `scripts/*.sh` surfaces as `scripts/localtest.sh`; an executable contract prevents either list from silently drifting. |
| Runtime coverage is inferred from test-file names | Fixed as a baseline gate | A full branch-coverage run now measures executable `admin`, `core`, `daemon`, `handlers`, `sources`, and `tasks` paths, including spawned Python tools. The gate protects the total plus provider, delivery, memory, session, Matter, EigenFlux and scheduler modules from silent regression; low baselines remain named debt rather than being rounded into “covered”. |
| Standalone task tools are absent from the capability inventory | Fixed | The generator now discovers non-heartbeat `tasks/*.py` entrypoints, requires an active runtime caller and executable-test evidence, and records seven live tools. `watchlater_save` gained concurrent read/deduplicate/write locking, private file modes, atomic persistence, structured failure events, and CLI concurrency tests. `daily_reflect_post` gained direct persistence/error-path tests and no longer swallows journal failures. The orphaned `harness_apply.py` was retired only after its producer was already retired, no caller remained, and the production checkout had no queue or changelog. |
| Shared atomic state writes inherit public `0644` modes | Fixed | `core.safety.atomic_write`, used by JSONL, calendar, Routine and EigenFlux state, now creates a unique `0600` temp inode inside a `0700` directory, flushes and fsyncs before replacement, and leaves the final inode `0600`. Regression tests prove mode repair and cleanup without losing the old file when replacement fails. |
| Model credentials reach unrelated child processes | Fixed | `bot.sh` keeps primary Anthropic, relay, and OpenAI keys shell-private. The heartbeat routing worker receives the configured route set; direct Claude/OpenAI adapters receive only their active credential. Heartbeat task scripts, Codex subprocesses, auxiliary providers, and GPT-started shell tools all receive explicit scrubbed environments. |

## Debt Retirement Sequence

The two large orchestration modules will be reduced in small, behavior-preserving
changes. They must not be rewritten or split by line count alone.

1. Add characterization tests around the longest workflows and their failure
   boundaries before moving code. The first targets are
   `generate_calendar_intents`, `restore_cancelled_intent`,
   `memorialize_output`, and `decide`.
2. Keep raising long-lived failure branches without mocking away their
   lifecycle. `core.heartbeat_loop` now executes normal, forced and governed
   restart ticks in an isolated harness and rose from 69.6% statements / 65.0%
   branches to 75.0% / 71.0%.
   `core.ef_stream_loop` now runs a controlled real subprocess-loop scenario
   through PM acceptance, cursor advancement, health transitions and clean
   stop, raising its ratchet from 58.1% / 47.7% to 72.0% / 61.0%.
   `core.routine_evidence` now exercises all declared providers and rose from
   52.9% / 41.4% to 87.0% / 75.0%. `core.matter_executor` launch,
   provider command, session discovery, artifact attribution and completion
   receipt paths now have a two-provider scenario; its ratchet rose from
   36.0% / 24.1% to 60.0% / 46.0%.
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

## Retention Rule

Do not delete a capability because it looks old, has a large module, or has low
traffic. A retire candidate requires an explicit deprecation marker, no active
entrypoint, a replacement/migration decision, and a data-retention review. The
orphaned harness-apply CLI met that standard: its producer had already been
retired, there were no callers or production data files, and Git history keeps
the implementation. Its removal is recorded under Retired Surfaces. The
current active inventory has no unresolved retire candidate.
