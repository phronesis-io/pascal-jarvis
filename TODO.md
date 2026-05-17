# Jarvis Roadmap

## Done (recent)

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

## P1 — Hardcoded paths

- Several task scripts have hardcoded /Users/pascal paths (phronesis_monitor_pre.sh, self_diagnostic_pre.sh, repos_sync_pre.sh, backup_sessions.sh) — should use $JARVIS_DIR or config
- phronesis_monitor_pre.sh has hardcoded chat_id and user open_id
- content-recommend prompt has hardcoded taste profile — should read from memory

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
| Database backend | Flat files (JSONL + markdown) are simpler, debuggable, git-friendly |
