# Release Acceptance Ledger - 2026-07-24

- Status: Implementation and local verification complete; release in progress
- Scope: the two supplied review documents, Verified Delegation, provider
  fallback reliability, and L1/L2/L3 engineering loops.
- Rule: implementation, deterministic test, and production evidence are
  separate columns. An unavailable credential or time-based sample is never
  represented as completed code debt.

## 1. Product PRDs

| Item | Outcome contract | Implementation | Deterministic evidence | Production evidence |
|---|---|---|---|---|
| PRD-1 Unified Delivery | One policy/state machine for replies, proactive output, cards, web, and push | `core/delivery.py`; bot, heartbeat, Memorial, EigenFlux stream integrations | delivery, reply, heartbeat, Memorial, stream, quiet-hour tests | post-restart delivery smoke |
| PRD-2 Memorial-first Items | One user inbox; Matter is topic; Intent is timer; old inboxes redirect | `/items`, Item APIs, Matter detail, Intent closure bridge, route redirects | dashboard, Item, Matter, continuity, mobile tests | desktop/mobile browser smoke |
| PRD-3 Module boundaries | Policy leaves producers; lifecycle/scheduler/closure have stable imports | `core.delivery`, `core.intent_*`; low-level transport remains an adapter only | intent-boundary and producer integration tests | runtime paths use unified pipeline |
| PRD-4 Deploy as verify | A restart is forbidden without merged/reviewed/green code and runtime proof | `core/release_gate.py`, `core/deploy.py`, `restart.sh` | release-gate and deploy tests | merged PR, CI, restart, component verify, smoke |
| PRD-5 Cross-process state | Shared mutable truth uses short-lived SQLite WAL connections | Delivery, continuity, schedule, runtime, Delegation, iteration tables | connection, concurrency, queue, continuity tests | bounded live process descriptors |

## 2. Verified Delegation

| Requirement | What it means | Implementation / verifier | Evidence |
|---|---|---|---|
| VD-01 capture | Only accepted responsibilities become active Delegations | explicit/authorized capture modes; shadow excluded | capture and shadow-classifier tests |
| VD-02 target | Bind stable target and principal before mutation | target/authorization bindings; ambiguity rejection | wrong-target and privacy tests |
| VD-03 risk | R2 ambiguity confirms; R3 approves; R4 stays human | risk and authorization validation | Delegation contract tests |
| VD-04 idempotency | Retries, callbacks, and handoffs cannot duplicate mutation | action key, source event key, contract version | duplicate callback/replay tests |
| VD-05 read-back | Each connector names an authority | verifier registry for Git, runtime, Delivery, EF, Lark, calendar, doc | verifier tests |
| VD-06 completion | Model prose cannot complete work | matching deterministic evidence required | false/missing evidence tests |
| VD-07 DAG | Partial success remains partial | required-step DAG and aggregate evaluator | multi-step tests |
| VD-08 convergence | Existing objects project one-way from Delegation | link table and projection service | projection tests |
| VD-09 external wait | Waiting is durable and resumable | waiting reason, resume event, reconciler | wait/resume tests |
| VD-10 versioning | Changed or repeated contracts have a boundary | version conflict and supersede operations | revision/conflict tests |
| Phase 0 gate | Automatic capture needs broad, sustained production quality evidence | shadow labels and `phase1_ready` calculation | threshold tests; gate remains closed until 50 reviewed samples span 14 days and 5 connector classes while meeting quality thresholds |

## 3. Incident Repairs

| # | Review finding | Resolution | Regression surface |
|---|---|---|---|
| 1 | Sanitized rejects shared one hash | Hash raw input before sanitization | delivery audit tests |
| 2 | Failed transport retried forever | Cumulative budget 9, terminal `failed`, dead letter only at terminal | delivery retry tests |
| 3 | Continuity shared a singleton SQLite connection | Per-operation connection and schema cache | continuity concurrency tests |
| 4 | Nested EigenFlux transaction risk | Plain connection plus one explicit immediate transaction | EigenFlux idempotency tests |
| 5 | Clock skew broke message verification | Exact message ID first; bounded skew-safe history fallback | message verification tests |
| 6 | Heartbeat file read lacked encoding | Explicit UTF-8 | heartbeat tests |
| 7 | Runtime SQL depended on column order | Explicit column tuple | deploy tests |
| 8 | Memorial flush consumed unrelated message IDs | Slice only IDs created by that send | heartbeat state tests |
| 9 | Schema and connection churn | Cache schema by DB inode; close short operations | delivery resource tests |
| 10 | Friend/message discovery missed later pages | Cursor pagination | pagination tests |
| 11 | Private message content appeared in process argv | Direct HTTPS JSON body | privacy and messenger tests |
| 12 | Verification performed broad friend-history N+1 reads | Target conversation plus receipt ID | messenger call-bound tests |
| 13 | Runtime verification repeated expensive dirty checks | One Git status snapshot | deploy tests |
| 14 | Scheduler record errors were silent | Structured warning | heartbeat tests |
| 15 | Copy-truncate could lose active log writes | Stop writer, rotate, restart same launchd plist | log-maintenance tests |
| 16 | Delivery update fields accepted arbitrary SQL names | Explicit allowlist | delivery state tests |

## 4. Independent Review Remediation

An independent Codex review of the complete `main...feature` diff found 15
blocking or material edge cases. All 15 were fixed before release:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 1 | R4 Delegations could become executable | R4 is permanently human-operated; create, bind, confirm, claim, and Item-action tests |
| 2 | A revised R3 contract retained old approval | Revision clears authorization and requires fresh approval; revision tests |
| 3 | Captured, shadow, or unbound rows could claim steps | Add-step/claim guards require a bound executable contract; lifecycle tests |
| 4 | Failed Item actions looked successful | Delegation and iteration actions now raise on failure; action tests |
| 5 | Non-zero scripts with JSON output looked healthy | Pre/post outcome is checked separately from stdout; heartbeat tests |
| 6 | Backup 1 transport errors skipped Backup 2 | Every failed Backup 1 route may continue to enabled Backup 2; fallback tests |
| 7 | L3 Taskline enqueue could duplicate after a crash | Transactional `queueing` reservation plus label read-back recovery; iteration tests |
| 8 | Repeated rejected signals recreated proposals | Latest unresolved/rejected proposal deduplicates until new verified evidence; iteration tests |
| 9 | Queued cards fell back to duplicate plain text | Durable accepted states suppress transport fallback; reply-delivery tests |
| 10 | Taskline's completion contract could not verify | Contract now uses runtime and component truth plus a runtime-deploy step; bridge/verifier tests |
| 11 | Existing worktree paths bypassed reconciliation | Git worktree/branch ownership is verified and links are repaired; bridge tests |
| 12 | Friend read-back stopped at the first page | Bounded cursor pagination in action and verifier paths; friend tests |
| 13 | Sequential provider probes exceeded heartbeat budget | Providers are probed concurrently within one timeout window; provider tests |
| 14 | Log rotation failure could leave launchd stopped | Every stop-path attempts restore and authoritative status read-back; maintenance tests |
| 15 | Background verification could starve user decisions | Attention states are scanned before verification states; reconciler tests |

The review gate itself was also hardened: generic `COMMENTED` reviews no longer
count as approval. A second independent red-team review then found ten
authority, revision, and recovery gaps; all ten were fixed before release:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 16 | Model/CLI markers could invoke owner-only approvals | Owner decisions require an authenticated Item/dashboard callback; model prose is suppressed with an authoritative refusal; action tests |
| 17 | Worker-controlled `strong` evidence could complete a Delegation | Worker CLI invokes only the registered verifier; store completion also checks contract authority, verifier identity, and execution phase; CLI/Delegation tests |
| 18 | Review evidence was not bound to the deployed commit | Approvals require the exact commit ID; attestations require `REVIEW-GATE: PASS <40-char SHA>` on their own line; release-gate tests |
| 19 | A healthy unrelated runtime could complete a Taskline task | Runtime contracts include the merged release SHA; completed Taskline tasks recover it from the linked PR and start a fresh versioned verifier step; bridge/verifier tests |
| 20 | Suppressed EigenFlux events were marked seen and lost | Only queued/delivered outcomes advance the upstream cursor; suppression dead-letters and remains replayable; stream tests |
| 21 | Uncertain EigenFlux sends lacked a reconciliation key | Message receipts expose the non-secret idempotency hash and persist it in verifier policy; messenger/reconciler tests |
| 22 | Retry moved an unapproved R3 item out of confirmation | `needs_user` is not retryable and the dashboard no longer offers that action; state-machine/UI tests |
| 23 | Contract revisions retained stale approval Items | Reconciliation resolves stale/decided cards and creates one version-bound current Item; reconciliation tests |
| 24 | Welcome delivery re-resolved an ambiguous display name | The welcome path uses the already verified friend ID and rechecks it against the authoritative friend list; friend tests |
| 25 | L3 proposals stayed queued after shipping | Daily observation now reconciles Taskline done state, merged PR SHA, deployed HEAD, and same-source outcome; failed outcomes create a fresh gated follow-up; iteration tests |

The third independent review examined the revised authority and recovery
contracts. Its 13 findings, plus one related Lark callback provenance gap found
during remediation, were also closed:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 26 | Dashboard owner mutations trusted any local caller | Mobile mutations require a validated paired-device Bearer token; direct NiceGUI callbacks carry explicit owner provenance; route tests |
| 27 | Worker CLI exposed Delegation confirmation | `confirm` is absent; worker create/bind cannot set authorization; worker terminal reports only failure; CLI tests |
| 28 | Shell CLI exposed L3 proposal approval | Proposal review is available only through the authenticated owner surface; CLI boundary tests |
| 29 | The same worker label could claim one live step twice | Every active lease rejects a second claim; renewal uses the dedicated lease operation; claim tests |
| 30 | Bound Taskline Delegations were never refreshed | Reconciliation scans bound Taskline rows and refreshes them in the same Delegation database; bridge tests |
| 31 | Verification retries reset the timeout clock | Timeout age comes from the stable step start, with Delegation creation fallback; reconciliation tests |
| 32 | Missing observations could be mistaken for success | Each source records fresh coverage; absence closes work only when that exact source was read successfully; iteration tests |
| 33 | A failed attempt closed linked responsibility | `failed` remains active and retryable; Item, Matter, Intent, and Handoff projections stay open; projection tests |
| 34 | An approved proposal could remain stranded after Taskline failure | Daily observation retries approved/ambiguous queueing states with label-based idempotent read-back; iteration tests |
| 35 | Rejected proposals could never return after stronger evidence | Severity, occurrence growth, or changed evidence creates a fresh human-gated proposal; iteration tests |
| 36 | Shadow promotion lacked time and connector diversity | Phase 1 now also requires 14 observation days and 5 connector classes; metric tests |
| 37 | A loaded Taskline launch agent counted as healthy | Component health calls `taskline status` and requires both server health and workspace registration; component tests |
| 38 | Fresh installs loaded a missing optional Taskline binary | Installer removes/skips the optional service before bootstrap when its binary is absent; installer tests |
| 39 | Lark card actions assumed every callback was Pascal | The sidecar matches the callback operator to the configured owner before any owner-only action; callback and Memorial tests |

The fourth independent review concentrated on long-tail recovery and evidence
integrity. Its six findings were closed with dedicated regressions:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 40 | An executing contract could be rebound without a new version | `bind` is limited to an unbound pre-execution contract; target changes use `revise_contract`, which invalidates old steps and leases; claimed-step regression |
| 41 | An uncertain EigenFlux send only reread its local receipt row | The verifier now queries authoritative conversation history by target, payload hash, conversation ID, and message ID, then atomically converges the stored action without resending; interruption-recovery test |
| 42 | The EigenFlux stream persisted its cursor before durable delivery acceptance | Cursor writes now occur atomically only after queue/delivery acceptance or a proven duplicate; suppression leaves the prior cursor replayable; cursor acceptance test |
| 43 | Release evidence inspected only the first GitHub page | Reviews and comments use bounded GitHub pagination and flatten every returned page; second-page approval regression |
| 44 | A reopened L3 proposal retained the prior rejection baseline | Material-change comparison uses the prior baseline while the new proposal stores the current one; latest rows have deterministic `created_at,rowid` ordering; repeated-rejection regression |
| 45 | The dashboard offered confirmation for non-confirmable states | UI and domain both allow confirmation only for a bound, unauthorized R3 contract awaiting risk approval; R4, clarification, and verification recovery remain non-confirmable; state-machine tests |

The fifth independent review traced the same contracts through HTTP, resident
processes, stream cursors, Lark actions, optional steps, and durable errors:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 46 | An unauthenticated producer could create an already-authorized R3 contract | The capture API always clears producer-supplied authorization; only the protected confirm endpoint or an in-process trusted rule can grant it; route regression |
| 47 | The release gate ignored non-ignored untracked runtime files | Deployment now rejects the full Git worktree status, including untracked `sitecustomize.py` or dynamically discovered modules while Git-ignored private state remains ignored; release-gate regression |
| 48 | L3 treated checked-out `HEAD` as proof that services were deployed | Shipping now requires matching resident bot and heartbeat revisions, critical component health, and a successful delivery smoke; deployment-evidence regressions |
| 49 | A later accepted stream event could checkpoint past an earlier delivery gap | Any non-accepted event terminates the stream and reconnects from the last contiguous cursor before later events are read; cursor-gap regression |
| 50 | Verification recovery rendered a confirmation button that the domain rejected | Domain, Lark, ActionProcessor, API, and dashboard share a retryable predicate; “重新核验” resumes authoritative read-back without replaying the external mutation; recovery regressions |
| 51 | L3 operational errors exited successfully and waited 24 hours | Observation still persists safe partial results but exits nonzero when coverage or reconciliation errors exist, activating the scheduler's bounded retry path; CLI regressions |
| 52 | A failed optional step failed the whole Delegation | Delegation failure is derived only from required steps; optional failure remains visible on its step while qualifying required evidence can complete the contract; parallel-step regression |
| 53 | Payload-bearing EigenFlux HTTP errors could enter durable state | HTTP bodies and provider messages are discarded; durable diagnostics retain only operation, exception type, or HTTP status class; privacy regression |

The sixth independent review and the subsequent all-entry model-route audit
closed ten more authority, state, and resilience gaps:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 54 | Any public GitHub account could post an exact-SHA release attestation | Review evidence now requires repository association or an authoritative write/triage permission read-back; hostile-public-attestation regression |
| 55 | Selecting Backup 2 mutated process-wide Backup 1 credentials | Each Lark message carries provider credentials in call-local variables; cross-message scope regression |
| 56 | Direct EigenFlux HTTPS used a different home path than the CLI | API credential lookup mirrors the CLI's `EIGENFLUX_HOME/.eigenflux` rule without duplicating an existing suffix; path regressions |
| 57 | A tripped heartbeat gate still probed primary when only Backup 2 existed | Gate initialization selects configured Backup 2 directly with its own model and credentials; zero-primary-call regression |
| 58 | Delivery verification read non-existent receipt field names | The verifier maps persisted `id` and `route_channel` to contract `delivery_id` and `channel`; matching-receipt regression |
| 59 | A missing Delivery receipt raised an unhandled attribute error | Missing read-back is a typed verification deferral and remains recoverable; missing-receipt regression |
| 60 | Worker CLI input could impersonate an owner during verification recovery | Worker retry refuses every `needs_user` state regardless of supplied actor text; spoofed-owner regression |
| 61 | Claiming an optional step overwrote a required step's verification state | Optional execution updates only its step and timestamps; required-step aggregate state remains authoritative; completion-order regression |
| 62 | Background and auxiliary calls stopped after Backup 1 | `core.aux_model` gives jobs, compaction, progress narration, EigenFlux analysis, and noise classification the same bounded Primary/Backup 1/Backup 2/GPT order; route regressions |
| 63 | External or derived text could invoke local Claude tools during analysis | EigenFlux, narration, compaction, and classification explicitly use `--tools ""`; only owner background jobs retain agentic tools; permission-boundary regressions |
| 64 | Expired trusted evidence could be replaced by a strong row from an untrusted actor | Evidence now persists `trusted` provenance and `verifier_id`; completion requires current trusted evidence from the contract's expected verifier and authority; forged-refresh regression |
| 65 | Step-specific verifier overrides were executed but never trusted | Evidence validation now resolves the same effective per-step policy as the verifier registry, including verifier and authority overrides; per-step completion regression |
| 66 | A crash between external verification and resume could strand a completed wait | Reconciler-owned external readback clears `waiting_on`, records the transition, and evaluates completion in one transaction; legacy interrupted rows have an atomic recovery path; interruption regression |
| 67 | A same-name check from the wrong GitHub App could satisfy the release gate | Required checks are matched by `(context, app_id)` whenever branch protection binds an app; wrong-app and configured-app regressions |
| 68 | A large user-attention backlog could starve all verification work | Reconciliation selects non-empty status buckets round-robin within its bounded limit, preserving priority without starvation; two-bucket fairness regression |
| 69 | One parallel step could overwrite the parent while another mutation was executing | Parent state is derived from every current required step after attempts and lease expiry; live execution remains authoritative; parallel-attempt and lease regressions |
| 70 | Expired evidence left its step completed and impossible to refresh | Active contracts reopen completed steps whose trusted proof has expired, making them eligible for authoritative re-verification; expiry-reopen regression |

The eighth independent review traced retry boundaries, release recovery,
provider exhaustion, and ambiguous connector receipts. Its six findings were
closed before release:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 71 | Retrying an active verifier reset the step and could replay an external mutation | Direct retry is limited to failed/blocked execution; verification recovery resumes read-back without resetting the step; state-machine regression |
| 72 | A Taskline Delegation with a release SHA but a pending step never started verification | Reconciliation refreshes when either the SHA is absent or a required release step is pending; bridge/reconciler regression |
| 73 | A later healthy deployment could not prove an earlier merged release | Runtime proof uses `git merge-base --is-ancestor`, accepting exact or descendant resident revisions while rejecting unrelated commits; deploy/verifier/L3 regressions |
| 74 | Backup 2 could be selected on the final loop iteration without receiving a call | The bounded route has enough iterations for every selected Claude-compatible provider, and backup failures advance providers without model-tier detours; routing regression |
| 75 | Failed Delegations remained active but invisible to Pascal | Failure creates one state-bound retry/cancel Item; compact mutation results are rehydrated before stale-card convergence; reconciler/API regressions |
| 76 | Two explicit uncertain sends without message IDs collapsed into one Delegation | Connector projection uses the action idempotency key as its stable source reference, preserving separately requested repeats; message regression |

The ninth independent review inspected provider security, release authority,
runtime verification, and recovery visibility. Its four findings were closed
before release:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 77 | Provider canaries trusted a prompt instruction instead of enforcing read-only execution | Claude canaries use `dontAsk`, an empty tool set, an empty strict MCP config, and no session persistence; command-contract regression |
| 78 | Strict branch protection with zero required checks passed the release gate vacuously | The release gate rejects an empty protected-check set before reading check runs; fail-closed protection regression |
| 79 | Optional unhealthy services blocked otherwise valid deployment evidence | Runtime Delegation verification reads critical components only, matching the L3 deployment contract; critical-component regression |
| 80 | Failed Delegations were omitted from the unified attention query | `needs_attention` includes failed recovery decisions alongside user confirmation and clarification states; attention-query regression |

The tenth independent review exercised timeout behavior, fallback exhaustion,
connector interruption, deployment scoping, and post-release evidence. Ten
formal findings and one additional reproduced idempotency gap were closed:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 81 | Auxiliary timeout killed only the parent while descendants held capture pipes | Auxiliary Claude processes run in a separate session; timeout sends TERM and then KILL to the whole process group, closes descriptors, and returns within the hard bound; real descendant-process regression |
| 82 | A hung primary could consume the entire budget before any backup ran | Each Claude-compatible attempt receives a bounded share while preserving one global deadline and budget for later providers; hung-primary-to-backup regression |
| 83 | Optional stale runtime registrations could invalidate a required-component deployment proof | `verify_runtime(required=...)` evaluates and returns only the named runtime rows while still failing on missing required rows; stale-admin regression |
| 84 | GPT agentic rounds reused the full timeout for every API and tool call | One monotonic deadline now governs all GPT API rounds and bash tools, with remaining time recomputed before each operation; multi-round deadline regression |
| 85 | Git process failures escaped the typed verifier boundary | Git launch and timeout failures become `VerificationError`, allowing reconciliation to defer one item and continue; timeout regression |
| 86 | A successful friend accept could disappear before its first read-back | The relationship Delegation is persisted before the mutation; an interrupted read-back leaves a verifying contract for scheduled authority recovery; accept-timeout regression |
| 87 | An uncertain welcome message had no scheduled reconciliation path | Every welcome receipt is projected under its stable action key, and the reconciler repairs missing projections from `verified_external_actions`; uncertain-welcome regression |
| 88 | A friend receipt projection failure could permanently lose control-plane state | The pre-mutation relationship projection remains repairable through the registered EigenFlux friend verifier even if the final matched projection fails; interruption regression |
| 89 | L3 could verify a release from evidence collected before deployment | Source coverage now carries an observation timestamp and must be strictly newer than `shipped_at`; same-pass and stale-audit evidence is deferred; post-release observation regressions |
| 90 | One deferred authority error caused the global reconciliation task to trip its circuit | Item errors stay visible in structured output while the pre-script exits successfully; only an unhandled process-level failure makes the task nonzero; scheduler regression |
| 91 | A stale uncertain EigenFlux message automatically replayed the write | Existing `attempting` or `verifying` actions only re-read authority and never resend; a new external action requires an explicit repeat token; stale-idempotency regression |
| 92 | EigenFlux preinstall overwrote Jarvis's verified-send safety contract with an upstream raw-ID example | The tracked overlay is deterministically composed after every upstream sync, replaces the unsafe direct-send example, fails closed when missing or malformed, and is checked against the live skill; overlay and skill-contract regressions |

The eleventh independent review checked cancellation, L3 absence evidence,
Taskline release continuity, scheduler recovery, and terminal projections. Its
six findings were reproduced and closed:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 93 | Cancelling an auxiliary tool-capable call left its private model process group running | SIGTERM/SIGINT now terminates the active model session with TERM/KILL before the router exits; signal-handler regression |
| 94 | More than 50 open conversation findings could falsely prove that the omitted finding disappeared | The bounded ingest remains 50, but truncation marks conversation coverage incomplete and blocks absence-based outcome verification; 51-finding regression |
| 95 | Taskline deployment proof evaluated stale optional runtime registrations | Engineering contracts explicitly require only resident `bot` and `heartbeat-loop` registrations; contract-policy regression |
| 96 | Failed Tier-0 observations waited their full 6-24 hour interval | Delegation reconciliation, L3 observation, log maintenance, and provider canaries have explicit bounded retry delays; scheduler-contract regression |
| 97 | Linking a Codex/Claude execution pointer could replace an already-bound release SHA with `pending` | Context linking adopts the existing Delegation without revising outcome or verification policy; release-preservation regression |
| 98 | A terminal Delegation projection failure left linked Matter, Intent, or Handoff state stale forever | Every projection failure enters a durable SQLite queue and the reconciler retries active and terminal rows until all projections converge; terminal-recovery regression |

The twelfth independent review focused on authority boundaries, crash
recovery, release evidence completeness, and operational metrics. Its eight
findings were reproduced and closed:

| # | Finding | Resolution and regression evidence |
|---|---|---|
| 99 | A canary explanation containing the expected marker could be treated as a healthy provider | Claude-compatible and OpenAI probes now require the exact trimmed marker; explanatory-output and sticky-fallback regressions |
| 100 | Rejecting an EigenFlux friend request unnecessarily depended on a successful friends-list read | Reject executes directly from the server request ID without the accept-only identity or relationship preflight; no-readback reject regression |
| 101 | Friend acceptance could be marked executed before the remote mutation, leaving a crash gap that no scheduler could resume | A pending connector step is durably reserved before the mutation, claimed only at execution, and resumed by the reconciler after lease expiry; crashes before the mutation and after remote commit both recover without duplicate acceptance |
| 102 | Terminal failure could rewrite post-mutation verification steps as retryable failures | Failure is rejected while any current step is verifying or awaiting external authority, preserving read-only recovery and preventing duplicate writes; terminal-transition regression |
| 103 | A required GitHub Check Run on a later API page could be omitted from release evidence | The gate now aggregates every paginated Check Run page before evaluating required contexts and app identities; later-page regression |
| 104 | Classic GitHub commit statuses could not satisfy a required legacy status context | Successful classic commit statuses are merged with Check Run evidence while app-bound checks remain identity-specific; classic-status regression |
| 105 | L3 watched the schema-protected duplicate idempotency-key count instead of the observed duplicate external mutation metric | The daily observer now emits a critical signal from `duplicate_external_mutations`; signal regression |
| 106 | The qualifying-evidence metric counted stale contracts, expired evidence, and untrusted or wrong-authority receipts | Metrics now apply the evaluator's current-version, required-step, trust, verifier, authority, strength, match, and expiry rules; evaluator-parity regression |

Generic `COMMENTED` reviews do not count as approval. Exact-SHA evidence is
mandatory, so a review submitted before the final push cannot authorize a
later revision.

## 5. Engineering Loops

| Layer | Contract | Implementation | Exit evidence |
|---|---|---|---|
| L1 | One Agent completes spec, dev, test, review, merge, deploy | current-state docs, localtest skill/script, release gate | focused/full tests, independent review, CI, merged PR, runtime smoke |
| L2 | Agents consume a dependency queue without duplicate ownership | external Taskline service, wrapper, component check, bridge, claims, leases, worktrees, stop reasons | Taskline bridge tests and this release task |
| L3 | Real feedback creates only worthwhile accepted work | signal/proposal store, dedup, human acceptance, Taskline enqueue, post-release observation | iteration-loop tests and observation records |

## 6. Provider Chain

| Position | Contract | Current code status |
|---|---|---|
| Primary | Official Claude account | Bounded canary; spend-limit trips fallback gate |
| Backup 1 | First relay | Bounded canary and live routing |
| Backup 2 | Independent second relay | Fully supported; disabled until a second credential is configured |
| GPT agentic | Tool-capable final fallback | Bounded canary; actual response model recorded |

Canary state stores only provider labels, model labels, timestamps, latency, and
sanitized categories. Tokens, authorization headers, and private response
content are neither persisted nor placed in process arguments.

## 7. Deliberate Non-Goals

The following are decisions, not unfinished promises:

- Telegram, Slack, email, or a native app without an identified adopter;
- a second personal task inbox beside Item/Matter/Intent;
- automatic shadow promotion without reviewed production evidence;
- real external mutations created solely for test coverage;
- enabling relay backup 2 without an owner-provided independent credential;
- treating Taskline engineering tasks as personal Intents.

## 8. Required Release Evidence

Each production release must carry:

- full local test result (`2083 passed` for this candidate);
- public-repository hygiene and secret scan;
- independent review and all comments resolved;
- required CI checks;
- merged commit on protected `main`;
- release-gated restart;
- component health, delivery smoke, desktop/mobile browser smoke;
- provider read-only canary;
- post-release L3 observation.
