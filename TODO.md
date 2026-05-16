# Jarvis Roadmap

## Done (recent)

- Calendar read/write (create, update, delete Lark events from conversation)
- Daily rhythm system (morning plan, activity log, evening reflect, free-time nudge)
- EigenFlux two-stage pipeline (feed triage → deep research)
- Feed item enrichment (full content + URL fetched in pre-script)
- PID lock for bot.sh (prevent duplicate instances)
- Image message handling (download + read via Claude)
- Session backup script with read-only protection
- Phronesis group chat monitoring
- Repos sync + self-diagnostic tasks
- Docs overhaul: architecture diagram, task development guide, example memory templates
- Remove hardcoded personal info from HEARTBEAT.md eigenflux prompts

## P1 — Hardcoded paths

- Several task scripts have hardcoded /Users/pascal paths (phronesis_monitor_pre.sh, self_diagnostic_pre.sh, repos_sync_pre.sh, backup_sessions.sh) — should use $JARVIS_DIR or config
- phronesis_monitor_pre.sh has hardcoded chat_id and user open_id
- content-recommend prompt has hardcoded taste profile — should read from memory

## P2 — Open source readiness

- Admin wizard for plugin setup (Lark + EigenFlux in-browser)
- Progressive onboarding (first-visit guided flow)

## P3 — Architecture improvements

- Heartbeat parallel tasks (non-pipeline tasks can run concurrently)
- Extract inline Python from bot.sh into proper scripts
- EigenFlux stream handler configurability (enable/disable, model selection)
- Session rotation notification (optional Lark notice)
- Memory change notification (approve/reject before pickup)

## Not planned (and why)

| Idea | Why not |
|---|---|
| Webhook external triggers | 10s polling heartbeat is sufficient; webhook adds complexity |
| Mobile admin UI | Lark IS the mobile interface; admin is for desktop config |
| Export/import | Git is the backup mechanism; memory files are plain markdown |
| Multi-user support | This is a personal agent — one user, one bot, one config |
| Database backend | Flat files (JSONL + markdown) are simpler, debuggable, git-friendly |
