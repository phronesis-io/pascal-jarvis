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
| Phase 0 gate | Automatic capture needs production quality evidence | shadow labels and `phase1_ready` calculation | threshold tests; production gate remains closed until 50 reviewed samples qualify |

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

- full local test result (`1987 passed` for this candidate);
- public-repository hygiene and secret scan;
- independent review and all comments resolved;
- required CI checks;
- merged commit on protected `main`;
- release-gated restart;
- component health, delivery smoke, desktop/mobile browser smoke;
- provider read-only canary;
- post-release L3 observation.
