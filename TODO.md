# Jarvis Roadmap

> **Current release: v1.0.0 (2026-06-15)** — first formal tag. REQ-01~77 all
> shipped & tested (779 passing). See `CHANGELOG.md` for the release summary and
> `docs/prd_interaction_v3.md` for the latest wave.

## Done (2026-06-15 wave — REQ-59~77, v1.0.0 — see docs/prd_interaction_v3.md)

- No-nag intents: outbox dedup keyed on closure-ask root, closure-of-closure
  guard, stale external-closure expiry, breach-shown-once (REQ-59/60)
- Clean funnel: hourly-cron self-reports excluded, silent sources don't log
  "sent", last_error stops masquerading as state; ops/self-mon alerts off chat;
  engagement attribution via quote-reply join not 60-min proximity (REQ-61~63)
- Reply-based closure classifier, negation-aware, via='reply' (REQ-64)
- Protected-doc write guard: read-back counts + multiplicity block-diff,
  destructive-overwrite reject (REQ-65)
- Self-monitoring from live JSONL/state/DB + liveness assertion (REQ-67)
- Calendar→intent idempotent upsert + prep-after-event drop (REQ-68);
  carry/bring reminders anchored to morning-before-leave (REQ-70)
- Structured dated facts in hot memory (REQ-71); per-tier memory sub-budgets
  with borrow-headroom + truncation alarm (REQ-73)
- Behavioral rules: no false truncation/blame, link self-check, continuation
  discipline, evidence-over-narrative (REQ-69/72/74)
- Event-gated free-time-nudge / content-recommend (REQ-75)
- Graceful model fallback opus→sonnet→haiku on model/spend error (REQ-77)

## Done (recent)

- Background jobs — long tasks run in independent Claude sessions with start/finish cards (`jobs` / `job output <id>` / `cancel <id>`)
- NiceGUI dashboard (port 3457) — home, tasks, bookmarks, intentions, thinking stream, agent calendar, settings; SQLite-backed
- 30-day calendar window (7d detailed + 8-30d compact, NBA schedule API)
- Philosophical task system (praxis/poiesis capture → commit → decay)
- Weekly review task
- Cross-session context sync (digest from other Claude Code projects)
- Engagement analysis with self-tuning recommendations
- Calendar read/write (create, update, delete Lark events from conversation)
- Daily rhythm system (morning plan, activity log, evening reflect, free-time nudge)
- EigenFlux two-stage pipeline (feed triage → deep research)
- Feed item enrichment (full content + URL fetched in pre-script)
- Guardian daemon with stuck-process detection and auto-restart
- Image message handling (download + read via Claude)
- Session backup script with read-only protection
- Docs overhaul: architecture diagram, task development guide, example memory templates

## Done (2026-06-13 wave — see docs/prd_system_iteration_v2.md)

- Intent closure end-to-end: manifest ack, bounded retry + breach cards,
  cron catch-up + dow fix, closure buttons, lifecycle telemetry (REQ-30~35)
- Scheduler honesty: parse failure = failure, scoped force triggers,
  cancel_job containment, single-consumer restart (REQ-36~38, 42)
- Self-monitoring: components.yaml manifest, unmuted self-diagnostic alerts,
  real backups (memory+DB), truth watermarks (REQ-39~41, 51)
- Data hygiene: hourly GC (jobs/views/log trims), 7-generation sched_events,
  dead-file cleanup (REQ-49); phronesis identity moved to jarvis.yaml (REQ-50)

## P1 — Residual

- content-recommend prompt has hardcoded taste profile — should read from memory
- repos-sync pre-script still exceeds the 60s cap (REQ-52: move git pulls to a
  background job; pre reads last job product)
- Engagement page + Ops/log explorer on the dashboard (REQ-54/55)
- Host sleep modeling (REQ-56)

## P2 — Self-evolution

- Engagement tracking → auto-adjust checkin frequency and content mix
- Prompt A/B testing framework (propose → approve → measure)
- Implementation intentions auto-generation for calendar events
- Mental contrasting integration in daily plan

## P3 — Architecture improvements

- Heartbeat parallel tasks (non-pipeline tasks can run concurrently)
- Extract inline Python from bot.sh into proper scripts
- EigenFlux stream handler configurability (enable/disable, model selection)
- Memory change notification (approve/reject before pickup)
- Plugin hot-reload (detect file changes, reload without restart)

## Not planned (and why)

| Idea | Why not |
|---|---|
| Webhook external triggers | 10s polling heartbeat is sufficient; webhook adds complexity |
| Mobile admin UI | Lark IS the mobile interface; admin is for desktop config |
| Export/import | Git is the backup mechanism; memory files are plain markdown |
| Multi-user support | This is a personal agent — one user, one bot, one config |
| Database backend for memory | Memory stays flat files (JSONL + markdown) — simpler, debuggable, git-friendly. (The dashboard keeps its own SQLite store for bookmarks/cached views; core agent state does not.) |
