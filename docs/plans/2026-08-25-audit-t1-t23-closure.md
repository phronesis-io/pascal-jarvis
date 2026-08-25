# Audit T1-T23 Closure, 2026-08-25

## Scope And Evidence

This document reconciles every item in
`~/Desktop/jarvis/AUDIT_TODO_2026-08-25.md` against the isolated implementation
branch. It distinguishes code completion from evidence that can exist only
after protected merge, Owner-authorized deployment, and real runtime traffic.

Status terms:

- **code-complete**: implementation and focused regression coverage exist.
- **runtime-pending**: code is complete, but the audit's production metric
  cannot be truthfully measured before release.
- **design-closed**: the literal suggestion was rejected because it would
  violate a stronger correctness or interruption policy; the underlying
  problem is closed another way.
- **config-dependent**: the path is implemented and testable, but a live
  provider or private setting must exist before it can pass a canary.

No row below is deployment evidence. This branch has not been merged,
restarted, or presented as production-complete.

## Line-By-Line Reconciliation

| Item | What it protects | Resolution and evidence | Status |
|---|---|---|---|
| T1 | Stop every heartbeat task from spending Opus | `model:` is parsed and routed. Feed/friend/mail triage use GPT; structured operational tasks use Sonnet; owner voice and memory mutation retain Opus. Relays preserve an explicit lower tier. | **code-complete, runtime-pending**: observe one day of provider/model distribution and sample ten feed cards. |
| T2 | Keep the reusable prompt prefix cacheable | Stable memory precedes volatile state; indexed warm memory is the default; cache usage ratios are queryable; resumed sessions omit transcript-derived material already held by the provider session. | **code-complete, runtime-pending**: second live resumed call must show the expected cache read/write ratio. |
| T3 | Prevent timeout retry storms and fake health probes | One logical call has one wall-clock budget; production calls never act as probes; safe no-tool work advances through Primary, Backup1, Backup2, then GPT within the remainder; ambiguous tool timeouts fail closed; recovery probes remain in `provider-canary`. Explicit Sonnet/Haiku task tiers stay at that tier across relays. | **code-complete, config-dependent**: run real read-only canaries for every configured route after release. |
| T4 | Keep internal metric ids and HTTP codes off cards | Shared display names plus a deterministic post-hook scrub translate probe ids and replace transport codes with plain language. | **code-complete**. |
| T5 | Make the morning archive line readable | Exact duplicate titles collapse to `xN`; middle ellipsis preserves the subject and distinguishing suffix; quiet flush gives each source at most one slot. | **code-complete**. |
| T6 | Detect detached self-improvement sessions that produced nothing | The session now persists acquire/run/release receipts, reconciles missing release, retries on a bounded clock, records usage, and reports health only after repeated failures. A worker must pass a final run-id/status/lease admission check before model execution; terminal receipts are write-once; an expired live worker is fenced by command identity before TERM/KILL with exit confirmation. A reused PID is never signalled, and an unavailable identity probe retains the exclusive lease instead of guessing. | **code-complete**. |
| T7 | Put one deterministic plain-language gate before operational cards | Component/task names, provider jargon, framing, and long mobile text pass through shared display/safety helpers; deterministic producers have regression coverage. | **code-complete**. |
| T8 | Stop paying for the same style contract in every task | Common card prose rules remain once in the system prompt; task prompts retain only domain rules and output schemas. Prompt text fell from 50,680 to below 30,000 characters, guarded below 35,000. | **code-complete**: reduction exceeds 30%. |
| T9 | Close five small recurring wastes | Obsolete recovery incidents are filtered before delivery; EigenFlux publish requires new material; relay routing no longer promotes Haiku/Sonnet to Opus; impossible WebSearch language was removed; deploy smoke is excluded from engagement rates. | **code-complete**. |
| T10 | Stop rebuilding roughly 190k tokens on every Lark message | Resumed conversations reuse a stable system prompt and no longer append current message focus, recent transcript, compact output, or current minute to the reusable prefix. Message timestamps stay in the message body. Initial, queue/rotation, and backup-provider prompt rebuilds all recompute and preserve resume detection. | **code-complete, runtime-pending**: verify live resume cache creation/read metrics. |
| T11 | Avoid unnecessary session churn after background promotion | Rotation remains because two concurrent writers must not resume one provider transcript. The cost is removed by T10's compact stable resume prompt; error copy no longer exposes internal job/session ids. | **design-closed**: raising the threshold or sharing a transcript would trade cost for corruption/latency. |
| T12 | Keep private material out of outward-facing prompts | `eigenflux-publish` and profile work declare outbound memory purpose and run isolated without tools. Outbound memory is an allowlist containing only curated `hot/group_context.md`; todos, sessions, warm notes, timeline, mail and DMs are absent. Publish material comes from Git evidence rather than private auto-memory, and the deterministic post-hook remains the sole effect boundary. | **code-complete**. |
| T13 | Give untrusted triage useful context without private memory | A bounded sanitized `triage_profile` config is injected into EigenFlux feed/friends and mail. Emails, phones, URLs, and credential-like values are rejected; full memory remains withheld. | **code-complete, config-dependent**: an empty profile deliberately means no inferred personal tie. |
| T14 | Stop Guardian from killing healthy work during provider overload | Provider degradation opens observation/verification rather than whole-stack recovery; listener/process probes are tri-state; regressions replay overload and unknown-probe states. | **code-complete, runtime-pending**. |
| T15 | Stop decisions losing to FYI traffic or reopening as new cards | One daily decision slot is reserved; decisions blocked by either the global cap or their source cap move to the next send day; an intent gets a stable Matter/dedup identity; a live `先都放着` receipt suppresses re-asking; flush is one card per source. Nondecision overflow remains terminal so old FYI traffic cannot become a permanent backlog. | **code-complete, runtime-pending**: one week must show zero terminal decision cap drops and at most one open decision per Matter. |
| T16 | Keep Memorial and delivery truth aligned | Every queue flush projects delivered/suppressed state and real Lark message id back to the Memorial ledger. | **code-complete**. |
| T17 | Stop claiming FYI cards are free web-only output | The prompt states that a notice is a real Lark card and consumes attention budget. Retired web language is absent. | **code-complete**. |
| T18 | Stop spending a model call whose self-diagnostic prose was discarded | `self-diagnostic` is Tier 0; deterministic pre/post logic owns diagnosis and user wording. | **code-complete**. |
| T19 | Prevent heartbeat batch scaffolding from reaching check-in cards | `strip_task_framing` recognizes `HEARTBEAT - N card(s)` before title extraction, including the Unicode dash form. | **code-complete**. |
| T20 | Stop memory maintenance damaging or lying about memory | Weekly digest preserves the rolling daily log; tidy measures production index and reference full modes separately; exact allowlisted stale operational claims are migrated; due intents retrieve bounded verbatim warm evidence. | **code-complete**: stale runtime claims migrate on the first post-release tidy. |
| T21 | Remove task-level waste and future test failures | Dead standalone content recommendation and `personal-site` are retired; hard-coded NBA downloading is removed; intention check is 3m; option length is enforced once at 14 characters; memory tidy is change-gated; check-in budget gates before calendar network; prompt/Tier-0 lists are synchronized; date literals and delivered-card clocks are deterministic. | **code-complete**. A global one-message-per-day cross-session cap was tested then rejected because it swallowed different PRs and failed-delivery retries; semantic dedup remains. Friend requests retain 10m polling because it is an active real-time feature. |
| T22 | Stop Guardian restart loops and invisible recovery state | No self-mtime reload; failed `ps` is unknown; listener recovery targets only its sidecar; startup/wake grace covers critical probes; recurring incident dedup is 24h; only Jarvis-owned live session locks (or their short acquire window) block whole-stack restart, so stale lock files cannot hide a dead heartbeat; bot receives graceful SIGTERM before bounded cleanup; repeated DEGRADED logs emit only on state change; external dead-man withholds green when brain-dead. | **code-complete, runtime-pending**. New recovery chatter/card types were rejected under the current low-noise policy; receipts and private logs retain attribution instead. |
| T23 | Bound operational state and protect private files | Daemon logs retain 30 generations; log maintenance runs allowlisted mode repair, 90/180-day SQLite detail retention, temp cleanup, and memory Git auto-gc; no raw payload is added to state; no active-writer VACUUM is attempted; external dead-man checks brain and delivery transport. | **code-complete, config-dependent** for live provider canaries. |

## Removed Surfaces

- Standalone content recommendation heartbeat: default-disabled and had no
  production run for 14 days. External recommendation products remain the
  appropriate discovery surface.
- Calendar-owned NBA/Cavaliers fetch: hard-coded personal taste plus a large
  periodic network download did not belong in calendar synchronization.
- `personal-site`: previously retired because its JSON output had no consumer.
- Tailscale/mobile gateway and NiceGUI remain retired; Lark plus Admin `:3456`
  are the supported surfaces.

## Release Acceptance Still Required

After a protected PR is merged and the Owner authorizes release:

1. Pass main CI and the independent review/release gate.
2. Restart through the governed deploy path and verify exact resident SHA.
3. Run component, delivery, Lark bot transport, read-only provider, EigenFlux,
   and Admin smoke checks.
4. Verify live resumed-prompt cache metrics and one-day task model distribution.
5. Observe one week for decision cap drops, duplicate Matters, Guardian action,
   EigenFlux material gating, and memory-tidy change gating.

Until those checks exist, the truthful state is **implemented and locally
verified**, not **online and proven**.

## Adversarial Closure

The final review replayed the shared failure boundaries rather than only the
new happy paths. It found and closed four gaps: source-level decision caps now
defer like global caps; stale session-lock files cannot suppress Guardian
recovery; detached self-improvement workers cannot execute after ownership has
changed or overwrite an existing receipt; and all three `bot.sh` prompt-build
paths preserve resume detection. Each case has a dedicated regression test.
