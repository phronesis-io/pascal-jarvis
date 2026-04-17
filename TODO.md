# Jarvis Roadmap

Improvements beyond the current release, organized by priority.

## P1 — One-click plugin setup in Admin

### Lark setup wizard (in browser)
- "Connect Lark" button in Admin → runs `lark-cli config init --new` in background
- Shows the auth URL in a modal → user clicks → browser auth → done
- Auto-fills `jarvis.yaml` with the user's `open_id`
- Status: 🟢 Connected / 🔴 Not configured

### EigenFlux setup wizard (in browser)
- "Connect EigenFlux" button → email input → runs `eigenflux auth login`
- Shows "Check email for OTP" → input field → runs `eigenflux auth verify`
- Profile editor: name + bio → submits via CLI
- Status indicator with token expiry warning

### Progressive onboarding
- First visit to Admin → guided flow: name → language → connect Lark? → connect EigenFlux? → done
- Replaces the current "edit jarvis.yaml manually" step

## P2 — Async & Performance

### Feed URL pre-fetch
- When heartbeat runs `eigenflux-feed-triage`, also call `eigenflux feed get` for each item
- Cache full content + URL locally so Claude doesn't need to fetch on-demand during conversation
- Trade-off: more API calls upfront vs faster response when user asks "what was that article?"

### Heartbeat parallel tasks
- Non-pipeline tasks (feed-triage, checkin, messages) can run concurrently
- Pipeline tasks (hourly → daily → weekly → monthly) remain sequential
- Reduces total cycle time from `N * claude_time` to `max(claude_time) + pipeline_time`

### Session rotation notification
- When a session rotates, optionally notify user on Lark: "Context rotated to session #N"
- Configurable: `notifications.session_rotation: true` in jarvis.yaml

### Memory change notification
- When `memory_consolidate` queues UPDATE directives, push a summary to Lark
- User can approve/reject before next session picks them up

## P3 — Router & Capability Awareness

### Lark command router
Beyond `loop` / `heartbeat`, recognize more shortcuts:
- `/status` — heartbeat task status + last errors (what Admin Heartbeat tab shows)
- `/memory` — latest 3 memory entries (quick summary)
- `/feed` — force-pull EigenFlux feed
- `/publish <text>` — broadcast to EigenFlux
- `/profile` — show current EigenFlux profile
- These are pure convenience — same info available in Admin UI

### Capability boundary declaration
- In system prompt, explicitly list what the bot CAN and CANNOT do
- Reduces hallucinated promises ("I'll set a reminder" when no reminder system exists)
- Update as capabilities grow

### Request classification
- Detect long-running requests ("write me a research report") vs quick ones ("what time is it")
- Route to different timeout / model settings
- Consider: long tasks → background agent, short tasks → inline

## Not planned (and why)

| Idea | Why not |
|---|---|
| Webhook external triggers | 10s polling heartbeat is sufficient; webhook adds complexity |
| Mobile admin UI | Lark IS the mobile interface; admin is for desktop config |
| Export/import | Git is the backup mechanism; memory files are plain markdown |
| Multi-user support | This is a personal agent — one user, one bot, one config |
| Database backend | Flat files (JSONL + markdown) are simpler, debuggable, git-friendly |
