# Changelog

All notable changes to Pascal Jarvis. This project tracks requirements as
`REQ-NN` across PRD cycles in `docs/` (prd_interaction_quality v1, REQ-01~29;
prd_system_iteration_v2, REQ-30~58; prd_interaction_v3, REQ-59~77;
prd_interaction_v4, REQ-78~90).

## [1.1.0] — 2026-07-02 — delivery reliability (v4, REQ-78~90)

Theme shift from "interaction annoyances" (v3) to **"promised actions that
silently never happen"**: a missed ¥66k credit-card reminder, the
conversation audit dead for 13 days unnoticed, a 57% intention-check failure
rate with zero diagnosable errors, and a 15-row Prep:请假 create/cancel churn.
Five parallel data-mining passes over the real interaction record + a
current-state verification pass + three independent red-team reviews
(necessity/evidence/risk). 960 tests passing (was 898 + 4 time-of-day flakes).

### Delivery reliability
- **Skip digest** (REQ-78 pt.1): stall-skipped cron occurrences surface as ONE
  breach-queue digest card (inherits BREACH_MAX_SHOWS=1, consumed-state-first
  idempotency) + a self-diagnostic 24h counter. Billing-class refire lands
  after a one-week shadow.
- **Shared-call failure no longer trips innocent circuits** (REQ-79.1): the
  `if not raw:` branch mirrors the parse_failed shared counter; ≥3 consecutive
  failed shared calls back off 5min→60min (Tier0 keeps running). Replayed
  against the real 7/1 and 7/2 outage batches: zero false trips.
- **Failed events carry error excerpts** (REQ-80): first error line,
  secret-redacted, on every failed/parse_failed task_finish; log rotation
  deepened 3→8 generations. Diagnosed the 7/2 DNS outage in seconds.
- **Zombie-task sweep** (REQ-81.1/.3): five tasks with 16 days of zero
  executions retired (eigenflux-messages/-research, memory-monthly,
  task-triage, harness-evolve) + hardcoded-roster guard test; card-callback
  success logging + dormant sidecar deleted.

### Self-monitoring blind spots
- **Conversation audit on its own launchd cron** (REQ-82): daily 04:20, pure
  regex (zero LLM), file_age 48h freshness alarm via components.yaml — the
  audit can never again die silently for 13 days.
- **Calendar failure ≠ empty agenda** (REQ-83): fetch failures keep the last
  good snapshot with a "data as of X" annotation (auto-cleared on recovery,
  proven live during the 7/2 DNS outage) + an `--as user` token probe in
  self-diagnostic.
- **Daily-plan card build removed** (REQ-84): was assembled daily and
  discarded daily by SILENT_TASKS since 6/12; PLAN_LOG (the real consumer)
  stays.

### Interaction friction
- **Prep:请假 churn eradicated** (REQ-85): multi-day events key on their TRUE
  start day via the calendar_event_mapping sidecar (key format unchanged,
  zero migration, legacy-key resurrection guard); all-day status blocks
  (请假/婚假/OOO) produce nothing at all; date-prep expiry now leaves a trace
  event.
- **Closure edge cases** (REQ-90): context-category intents close their
  closure axis (na + closed_at) instead of dangling forever; cron rows refuse
  closure_question; done-with-empty-result coerces to na — and the ✅ button
  now carries a result (the latent source of that very bug).
- **free-time-nudge retired** (REQ-89): 11 sends, zero real engagement.
- **Shadow instrumentation** (REQ-88/86): write-claim audit reconciles
  "已记录" claims against actual write-surface changes (log-only, promotion
  gated on <5% false positives); direct-reply journal attribution logged
  shadow-only.

Deferred on their own acceptance gates: REQ-78 billing refire (~7/9),
REQ-81.2 memory-task root-cause fixes (~7/9), REQ-79.2 parse-clamp (~7/9),
REQ-88 promotion (~7/14), REQ-86 write-enable (post-shadow).

## [1.0.0] — 2026-06-15 — first formal release

The first tagged release. Consolidates three PRD cycles of audit-driven
hardening of Pascal's resident Lark/飞书 assistant into one stable, fully
tested (779 passing), self-monitored, deployed system. Every requirement is
grounded in the real interaction record, not speculation.

### Intent / closure (the proactive core)
- **Closed-loop intents** (REQ-30~35): inflight-manifest execution ack (the
  LLM authors content, never state), bounded retry + a breach apology card on
  exhaustion, cron catch-up + standard-cron dow fix, closure-axis spawn on all
  terminal moments + awaiting TTL, lifecycle telemetry. Fixed the audited 50%
  silent-death rate of one-shot intents.
- **Reply-based closure** (REQ-64): a negation-aware classifier closes a loop
  from Pascal's chat reply (做了/没做/不用追) — no Feishu button backend
  needed; ambiguous replies defer to the LLM; only single-root, awaiting
  intents auto-close.
- **No nagging** (REQ-59/60, breach max=1): breach apology shown once not 3×;
  outbox dedup keyed on the closure-ask root kills reworded duplicate cards;
  closure-of-closure spawning blocked; external closures expire >2 days stale.
- **Calendar idempotency** (REQ-68): (date,title,role) is an at-most-one-row
  invariant across all statuses; a prep that would fire after its event is
  dropped (no more 11-row dinner churn).
- **Carry reminders** (REQ-70): "things to bring" fire in the morning before
  first leave (clamped, never after the event, expires after the fire time) —
  the umbrella-for-a-noon-clinic miss is fixed.

### Reliability / self-monitoring
- **components.yaml manifest** (REQ-40): single source of truth for "what
  should be running", consumed by daemon / self-diagnostic / doctor /
  restart --status. `python3 -m core.components` one-shot health.
- **Self-monitoring from live data** (REQ-67): `core/selfmon.py` computes
  noise-card count, same-intent re-fires, closure-overdue, crashes, silent log
  failures + a liveness assertion, from the live JSONL/state/DB (bounded +
  cached); dashboard panel.
- **Unmuted alarms** (REQ-39): self-diagnostic alerts via a deterministic post
  path (osascript fallback) instead of the silenced LLM summary; ops/circuit
  events stay off Pascal's chat (REQ-62).
- **Real backups** (REQ-41): memory dirs + WAL-safe DB + state, with a
  freshness check. **Restart hardening** (REQ-42): single-consumer restart,
  daemon hot-reload + deploy guard, watchdog covers ef-stream/admin,
  heartbeat-loop singleton lock.
- **Truth watermarks** (REQ-51): starvation reads last_success, not the
  synthetic last_run, so a 100%-failing pre-script can't look healthy.
- **Two-channel alerts** (REQ-58) + **graceful model fallback** (REQ-77):
  opus→sonnet→haiku on a model-unavailable/spend-limit error instead of an
  empty death-loop (Fable never in the chain).

### Memory
- **Per-tier budgets** (REQ-73): each tier has a reserved floor; the full
  payload loads when under the global cap (no throwing away headroom);
  truncation is observable; stale warm files demote to archive.
- **Structured dated facts** (REQ-71): `hot/structured_facts.md` + get/set_fact
  (sanitized, atomic) injected top-priority so load-bearing dates stop getting
  lost across sessions.
- **Trust** (REQ-65): `core/doc_guard.py` verifies protected-doc writes by
  multiplicity-aware block-diff + independent read-back counts — completion
  claims come from the live doc, never generation-side counts; destructive
  overwrites are rejected.

### UX / responsiveness / noise
- **Perceived latency** (responsiveness policy): first activity feedback within
  ~6s + a one-time "thinking" ack during long opus replies (the model is the
  wait, not the pipeline — diagnosed from real logs).
- **Engagement attribution** (REQ-63): one response per sent, quote-reply
  join, flock-serialized — fixed the impossible 107% per-source rates.
- **Event-gated nudges** (REQ-75): free-time-nudge stays silent unless there's
  real content; content-recommend standalone push gated off.
- **Behavioral rules** (REQ-69/72/74): no false truncation/external blame,
  Lark-unrenderable link self-check, continuation discipline (don't re-fetch +
  re-diagnose the same artifact every turn), evidence-over-narrative reports.

### Dashboard / Admin
- Dashboard (:3457) rebuilt honest (REQ-43~46): all 7 pages 200, task-health
  board, intent funnel, live home feed; Admin (:3456) honest actuators + ops
  depth + security trio (REQ-47/48).

### Model
- Pinned to **Opus 4.8** end-to-end (`main_model: opus`, never inherit a
  possibly-banned account default; Fable severed).

### Tests
- 779 passing. Every REQ shipped with regression tests; each wave
  adversarially red-teamed before release.

[1.0.0]: https://github.com/phronesis-io/pascal-jarvis/releases/tag/v1.0.0
