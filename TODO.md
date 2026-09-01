# Jarvis Roadmap (Historical)

> [!IMPORTANT]
> This is the final pre-archive roadmap snapshot, not an active backlog.
> Pascal Jarvis was archived and became unmaintained on 2026-09-01. Items below
> that were unfinished are not planned work. See
> [FINAL_STATUS.md](FINAL_STATUS.md).

> **Current release: v1.15.0 (2026-08-28)** — see `CHANGELOG.md` for the full
> release history. `docs/prd_portfolio.md` is the authority on which PRDs are
> shipped, superseded, rejected, or active; `docs/release_acceptance_2026-07-24.md`
> is the requirement-to-evidence ledger.

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
- Event-gated content-recommend; free-time-nudge later retired after the
  low-engagement audit (REQ-75/89)
- Graceful model fallback opus→sonnet→haiku on model/spend error (REQ-77)

## Done (2026-07-28 wave — Town-inspired: 用户自建例程 / 注意力 ROI / 邮件草稿)

- **Routines** — 用户在飞书一句话就能建长期例程（`core/routines.py`）：触发器复用
  Intent 的 next_fire_at 追赶原语（不另起调度器）、证据由确定性代码先采集、
  三档自主级别（observe/propose/act）由代码按**存量记录**闸死而非提示词、
  每次运行落一条审计。`act` 只放行内部可逆动作白名单，外部变更仍归 Delegation。
  面板 `/routines` 同时展示定义和审计流（observe 的产出只在这里可见）。
- **注意力 ROI 治理**（`core/attention_roi.py`）—— 用奏折台账实测的回应率决定
  某个来源还配不配占决策位；只降不升、不碰受保护来源、每次调整都发卡告知。
  接上 log-maintenance Tier-0。上线当天按真实数据**一个都没降**（决策位健康），
  只报告了 4 个已在最低档但基本没人看的来源 —— 这正是校准没有过激的证据。
- **邮件回复草稿**（`core/mail_draft.py`）—— mail-triage 从"只读"升级到"给真需要
  回的邮件备一版草稿"，语气来自 per-user 配置而非硬编码。**不含发信**：
  Jarvis 没有发信通道，按钮措辞里不存在"已发送"这个状态。
- 退役 `dashboard/heartbeat_bridge.py` + SQLite `scheduled_tasks` 动态任务路径：
  唯一能跑的 `notify` 是 cron Intent 的劣化重复，生产零行数据。

### 下一步（未做，需要单独设计）
- 邮件**发送**闭环：需要 authority / 安全 / 回滚设计 + 权威读回验证，
  按架构应走 Verified Delegation，不是给按钮加个 send。
- routine 证据源目前 8 种，Pascal 真用起来后按缺口补，不预先造。

## Done (recent)

- Intent closure acceleration — drained hard/external closures now generate
  bounded re-ask intents, and only rendered closure cards consume touch budget
- Prompt A/B framework — memory-backed prompt experiments inject approved variants into heartbeat task prompts, record exposure metadata in engagement logs, and surface variant performance in engagement-analyze
- Engagement self-evolution content mix — engagement-analyze now writes advisory `engagement_content_mix.md`; checkin pre-hook consumes it as steering context alongside guarded interval tuning
- Dashboard Engagement/Ops views REQ-54/55 — `/engagement` source ROI board and `/ops` log/event/queue explorer backed by live JSONL/state files (retired with the dashboard, 2026-08-21)
- Host sleep modeling REQ-56 — heartbeat emits `sleep_gap` events after long host pauses; daemon grants a short wake grace before treating heartbeat age as stale
- Repos-sync REQ-52 — slow git fetch/pull work moved to detached single-flight worker; pre-hook now only spawns worker and emits fresh worker product once, with contract tests
- Content-recommend curation reads safe taste/profile context from memory instead of relying on a stale hardcoded taste block
- Background jobs — long tasks run in independent Claude sessions with start/finish cards (`jobs` / `job output <id>` / `cancel <id>`)
- NiceGUI dashboard (port 3457) — home, tasks, bookmarks, intentions, thinking stream, agent calendar, settings; SQLite-backed (retired 2026-08-21; the SQLite layer lives on as core/db.py)
- 30-day calendar window (7d detailed + 8-30d compact, NBA schedule API)
- Philosophical task system (praxis/poiesis capture → commit → decay)
- Weekly review task
- Cross-session context sync (digest from other Claude Code projects)
- Engagement analysis with self-tuning recommendations
- Calendar read/write (create, update, delete Lark events from conversation)
- Daily rhythm system (morning plan, activity log, evening reflect, free-time nudge)
- EigenFlux feed triage; the zero-execution deep-research poller was later
  retired in favor of enriched feed data and the real-time message stream
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

- None currently tracked from the 2026-06-13 P1 list; next work should come from P2/self-evolution or a fresh audit finding.

## P2 — Self-evolution

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
| Database backend for memory | Memory stays flat files (JSONL + markdown) — simpler, debuggable, git-friendly. (The shared SQLite store — `core/db.py`, formerly the dashboard's — holds bookmarks/cached views; core agent state does not.) |
