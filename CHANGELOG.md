# Changelog

All notable changes to Pascal Jarvis. This project tracks requirements as
`REQ-NN` across PRD cycles in `docs/` (prd_interaction_quality v1, REQ-01~29;
prd_system_iteration_v2, REQ-30~58; prd_interaction_v3, REQ-59~77;
prd_interaction_v4, REQ-78~90).

## [1.4.0] — 2026-07-14 — self-improvement wave: memory-tier starvation fix, honest ops signals, card quality (REQ-91~99)

Driven by a full self-audit plus the user's own complaints from 7/13-7/14
(intent CLI error, unreadable checkin card, incomplete calendar card,
recurring false stream alarm, personal data in tracked scripts). Four-angle
adversarial review caught 17 tightenings including 3 self-introduced
regressions. 1440 tests green (+25).

### Memory system tier no longer starved (REQ-91~94) — biggest functional fix
- Measured: the system tier loaded 63.8k chars against a 40k budget, so
  `inbox_private_mail` (ALL of mail-triage's output) and issue files were
  silently invisible to heartbeat for days. The per-file caps never summed
  against the budget — now arithmetically consistent (SYSTEM 56k, HOT 30k)
  with a constants-consistency test so future cap edits stay honest.
- Perception `_trim_inbox` rewritten: entry-boundary char-cap retention
  aligned with the loader caps (disk ≈ injected; the old 500-line rule kept
  3× what could ever load and cut mid-entry). Entries <48h are protected
  (the mail buffer is a WORK QUEUE — mail-triage drains ≤15/cycle; trimming
  a burst would silently lose untriaged mail); entries >7 days age out
  (restores the PRD §5.4 bound that was documented but never implemented).
  flock + size recheck against concurrent writers.
- memory-tidy auto-archives `system/*.md` with resolved-family frontmatter
  `status:` after 7 days (line-anchored YAML detection, case-insensitive).
- `tier_truncated` now feeds selfmon; warm-tier squeeze (by design) is
  marked `expected` and skipped.

### Honest ops signals (REQ-95/96)
- ef-stream: a connection that lived ≥10min before dropping resets the
  reconnect backoff (a quiet day used to ratchet to permanent 300s blind
  windows — observed failure #27, ~2h/day blind). 'Connection replaced' is
  exempt (two live sessions would ping-pong). Long-lived ZERO-output
  connections stay visible via a quiet-streak counter that escalates to
  warn every 6 consecutive (~3h) — an idle day and an up-but-mute server
  are protocol-indistinguishable, so neither is silently blessed.
- heartbeat: elected primary-probe failures during a tripped spend-limit
  gate are annotated in the log line and flagged `expected` — selfmon,
  admin console and the ops dashboard all skip expected entries. Only
  annotated when a backup path actually exists; a missing backup env keeps
  alarming (that's a real outage).
- self-diagnostic: "Stream NOT running" now checks the supervisor loop
  before alarming — sampling inside a reconnect/deploy window (the recurring
  false alarm, also seen on collaborator first install) reports a
  self-healing window instead.

### Interaction quality (REQ-97~99)
- `python3 -m core.intentions create` — agents can file intents from
  sessions (the 7/13 "CLI 报错" failure). argparse with a trigger_type
  whitelist (unknown types used to insert never-firing zombie rows),
  typed --priority, ISO-validated --expires-at.
- checkin: live activity evidence in the prompt (last-message recency,
  today's reply count, memorial interactions) with an explicit "missing
  signal ≠ idle" rule — no more "you seem idle" cards on strategy-work
  days. The post-hook unwraps a stray {"response","action"} JSON envelope
  (raw JSON used to reach the user's card verbatim) and honors
  action=silent.
- calendar change card: every line carries date+weekday, same-title
  add+remove pairs render as ONE "改期 old → new" line, overflow beyond the
  display cap is counted, never dropped.

### Privacy: personal data is config, not code
- content-recommend interest queries (the user's whole interest profile)
  → gitignored `data/content_queries_personal.txt` (neutral starter set
  when absent); intent-categorizer personal keywords →
  `data/category_keywords_personal.json`; identifying names in comments
  and test fixtures neutralized. Hygiene test blocks regressions.
  Per-user config table added to INSTALL.md (Phase 5.5).

## [1.3.1] — 2026-07-13 — first-install fixes: portable launchd, honest health checks

Everything a collaborator hit installing 1.3.0 on a fresh machine (their
day-one self-diagnostic card listed 17 warnings), plus the portability
commit that just missed the 1.3.0 tag:

### Included from post-1.3.0 main
- All hardcoded user paths eliminated (`core/claude_projects.py`); **launchd
  plists are now templates** — install via `scripts/launchd/install.sh`,
  never by copying plists (the 1.3.0 tag shipped them with absolute paths).
- HEARTBEAT per-task overlay (`data/heartbeat_overlay/<task>.md`, gitignored).

### First-install health-check honesty
- `components.yaml` entries declare preconditions
  (`requires_cmd`/`requires_file`/`requires_config`); unconfigured optional
  features (EigenFlux, sidecar, admin, launchd services) report `○ skipped`
  instead of alarming `[critical]` forever — now consistent with doctor.sh.
- Never-run tasks get a fresh-install grace (2× interval from
  `data/.install_stamp`, created by setup.sh, self-healing) — no more six
  "has NEVER run" warnings minutes after install, including self-diagnostic
  reporting itself mid-first-cycle.
- Real bug: a dead ef-stream printed a bare "⚠️ 0" (grep -c double-zero)
  instead of "Stream NOT running" — affected production too.
- Personal-site checks read `jarvis.yaml personal_site.repo_dir` instead of
  a hardcoded owner repo (subject-less "⚠️ Repo not found" on every
  non-owner install; hygiene test now bans owner usernames in tracked files).
- `self_diagnostic_post.py` emergency send passes `--as bot` (user-identity
  fallback failed on exactly the installs where the fallback matters).
- `eigenflux-preinstall` skips off the maintainer machine; calendar/EigenFlux
  diag sections gate on the feature being configured.
- setup.sh/INSTALL.md: doctor + launchd supervision steps, documented
  first-install expectations (`○ skipped` ≠ broken). 1415 tests.

## [1.3.0] — 2026-07-13 — internal release: decision-first UI + collaboration readiness

The first release cut for the multi-collaborator model (everyone works on
their own `dev/<name>` branch; Pascal alone merges to `main`). Three threads:

### Decision-first cards & dashboard
- Memorial presets reworded to real decisions（同意/暂不处理/不采纳 ·
  已阅/标为重点 · 做了/还没做/这次跳过）; cards support button *groups* (rows)
  so choices, source links, and the chat affordance stop crowding one row
  (`core/card.py button_groups`).
- Dashboard: new `/memorials` inbox page + home page rebuilt around a pending-
  decisions panel (`dashboard/pages/memorials.py`, `dashboard/uiutil.py`);
  attention ranking puts direct asks above ambient feed signals, and a corrupt
  ledger row can no longer blank the decision surface.
- EigenFlux feed cards: hard ceiling of 3 non-urgent cards/day on top of the
  90-min cooldown; feed titles must name the event (no more bare 行动/知会).
- Memorial follow-ups closed out: engagement accounting for direct sends and
  verdicts, ledgers included in session backups, single-intent closure
  questions ride native memorial cards (dual-intent kept on legacy cards).
- Card-body clipping no longer cuts through a markdown link.

### Privacy scrub (pre-collaboration audit, 2026-07-13)
- A 78-agent audit swept the tracked tree before inviting collaborators:
  personal data (health schedule, financial figures, a real mailbox, real
  contact/family names, address) removed from code, prompts, tests, and docs.
  Principle now documented in CONTRIBUTING.md: **user-specific content lives
  in gitignored per-user files** (`data/checkin_personal.sh`,
  `data/checkin_topics_personal.txt`, `jarvis.yaml`), never in tracked files.
- Test fixtures fully synthetic; `tests/test_public_repo_hygiene.py` now also
  greps tracked content for real-mailbox and full-length Lark-ID shapes.
- Stale one-shot scripts with embedded personal data deleted
  (`scripts/seed_intentions.py`, `scripts/migrate_intent_closure.py`,
  `docs/conversation_audit_2026-06-16.md`).
- NOTE: pre-1.3.0 git history still contains the scrubbed content; see the
  release notes for the private-repo / history-rewrite decision.

### Collaboration & CI
- CONTRIBUTING.md (branch-per-user model, per-user-data rule, conventions);
  `.github/CODEOWNERS` routes every PR to Pascal; branch protection on `main`
  (PR + green `test` check + code-owner review; no force pushes).
- CI installs `requirements.txt` (the old pyyaml-only env silently skipped the
  entire dashboard suite), adds pip cache, 15-min timeout, per-ref concurrency.
- `pgc_improvement_pre.sh` honors empty-stdout=skip when Pascal's PGC host is
  unreachable — non-Pascal installs no longer burn a daily 900s Claude call.
- Time-bomb test fixture fixed (checkin busy-filter now takes `now=`); CI on
  `main` had been red since 7/11 because the fixture's end date passed.

## [1.2.0] — 2026-07-11 — memorial cards + mobile resilience（奏折 + 移动韧性）

Two workstreams born from one day (7/10, Pascal's directive): (1) **memorial
(奏折) cards** — every proactive output facing Pascal becomes ONE card per
event with quick-verdict buttons plus a「聊聊这个」hand-off into conversation
(long truncated text pushes are dead); (2) **mobile resilience** — an
8-dimension audit through the carry-the-laptop lens (lid-close sleep, offline,
captive portals, timezone jumps, power loss) confirmed 8 P1s via adversarial
verification; all eight approved item-by-item and fixed. 1380 tests passing
(was 960). Every fix red-teamed; the memorial framework's DOA P0 (sidecar
couldn't import core.memorial in production) was caught by red team before
deploy.

### Memorial cards (奏折)
- `core/memorial.py`: create / decide / chat; `memorials.jsonl` event ledger
  (O_APPEND, fold-by-id); presets decision/fyi/followup; every card auto-gets
  「💬 聊聊这个」. Ledger-before-action: a crash mid-action can never double-
  execute on re-tap; decide is idempotent, cards replaced in place with the
  verdict (`✅ 已批：… · HH:MM`).
- Sidecar generic routing (`value.action == "memorial"`); legacy
  feedback/watchlater/intent_close untouched; all sends run off the event-loop
  thread (the ws connection that carries Pascal's messages never blocks).
- 「聊聊这个」: opener message + memorial context injected via
  `jobs/pending_merge.jsonl`, consumed by the next user message — live-proven
  in prod 7/11 17:46→17:54 (tap → "SLA 到底是什么?" answered in context).
- Delivery: memorial cards ride the heartbeat pipeline (quiet hours, batching,
  dedup all apply); send timeouts are NOT assumed delivered — cards persist in
  `memorial_queue.jsonl` and drain ≤6 per window (`MEMORIAL_FLUSH_MAX_CARDS`).
- Surfaces: proactive outputs auto-wrap at the delivery layer; mail-triage
  push emits memorials (`send=False`, rides the CARD: path); EigenFlux
  feed/PM cards rate-floored at one per 90 min (urgent bypass);
  `python3 -m core.memorial send|list` CLI for any task; HEARTBEAT.md §奏折.

### Mobile resilience (audit 2026-07-10 — 8/8 confirmed P1s fixed)
- **Timezone**: `core/timeutil` re-resolves /etc/localtime on a 60s TTL (was:
  cached at import — the running heartbeat sat 8h behind after
  Reykjavik→Shanghai until this release's restart).
- **Zombie connections**: sidecar disconnect watchdog (exit → supervisor
  respawn, only after a successful first connect), SDK logs moved off the
  NDJSON stdout pipe; ef-stream stall watchdog kills a silent-but-alive child.
- **Outbound loss**: chat replies retry with backoff then dead-letter
  (`reply_send_failed`); ef-stream send failures dead-letter instead of being
  marked seen with a fake "Delivered"; night-queue send timeouts keep the
  queue (retry floor 900s) instead of unlinking 40 entries on a lie.
- **Alerting honesty**: brain-death suppression is now ledgered — a wedge
  surviving ≥2 wake windows or 1h cumulative suppression pierces the post-wake
  grace (7/10's 17.5h silent wedge would page in window two); a 2s
  reachability probe treats offline as grace (kills the flight-day false
  BRAIN-DEAD); new dead-letter kinds carry human labels.
- **Power-loss durability**: heartbeat `load_state` archives a torn state file
  and reseeds instead of silently killing every task forever; `save_state`
  fsyncs before rename; daemon singleton validates pidfile process identity
  (PID reuse no longer deadlocks boot).
- **Escape hatches**: provider-gate probe cycles fall back to backup on ANY
  primary failure (was: model-shaped errors only); heartbeat `run_script`
  kills the whole process group on timeout; the test suite is isolated from
  the production heartbeat trigger.

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
