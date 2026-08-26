# Jarvis Full-System Audit, 2026-08-24

## Decision

Product scope remains frozen. This round hardens the existing Lark-first
assistant: faster provider continuity, quieter self-healing, human-facing
language, trustworthy runtime provenance, and evidence for every retained
capability. It does not add another client, dashboard, notification channel,
or project-management surface.

## Evidence Reviewed

- Production component, deploy, provider-health and self-monitor snapshots.
- Recent owner conversation, response timing and historical comprehension
  findings from the conversation-audit and cross-session memory stores.
- The generated capability inventory, all runtime entrypoints, heartbeat
  tasks, Lark commands, Admin routes, public-repository hygiene, dependency
  graph, maintainability budget and the full test suite.
- Current Git/PR/runtime revision alignment and resident process ages.

No private identifiers, credentials, message bodies or memory contents are
copied into this repository document.

## Findings And Actions

### P0: Provider overload waited instead of failing over

**Evidence.** Real Claude requests returned HTTP 529 after minutes, while tiny
canaries remained green. The classifier reduced 529 to `request_failed`.
Neither direct chat nor heartbeat considered it safe pre-execution failover,
so a working Codex/GPT route could sit idle.

**Action.** Classify 529/overloaded responses as `server_overloaded`, apply a
bounded provider cooldown, and use a separate pre-execution predicate. Replay
on another provider is allowed even for tool-capable requests because the
provider rejected the request before execution; same-provider model
degradation remains disabled. Direct chat and heartbeat now consult the same
real-request health route before every new call, so the next request skips a
known-cooling primary instead of paying for the same failure again. Once a
transient cooldown expires, primary gets one bounded real-request recovery
turn; another failure expands its cooldown instead of trapping every message.

### P0: Guardian restarted healthy infrastructure for task failures

**Evidence.** Guardian saw model timeouts and parse failures on individual
heartbeat tasks, killed the whole heartbeat loop, and the watchdog reloaded it
from the mutable checkout. This interrupted unrelated tasks and could not cure
an external provider outage.

**Action.** Task-level brain-health verdicts now open a verification window
without process restart. Provider fallback and scheduler retry own those
failures. Guardian still alerts after sustained, independently verified
failure and retains component-level recovery for a genuinely dead process.

### P0: Resident processes could load unreleased source

**Evidence.** Runtime fingerprinting covered `core/` but omitted `tasks/`,
runtime scripts, plugins and several launch surfaces. A watchdog child could
respawn after the checkout moved to a feature branch, producing multiple code
generations inside one product instance. The daemon also hot-reloaded itself
on file mtime alone.

**Action.** Runtime fingerprints and dirty-source checks now cover all active
execution surfaces, including Admin static assets. Normal startup requires
the `origin/main` revision and a clean runtime tree; a deliberate
local-development override is explicit.
Watchdog child respawns are blocked if HEAD or runtime source changes after
boot. Daemon mtime hot reload is retired; governed deploy performs the restart.

### P1: Jarvis exposed implementation instead of speaking naturally

**Evidence.** Recent Lark output included raw task names, `STARVED`, tool
narration, job IDs, CLI commands, empty `Intent` headings, and a mechanical
`已完成` prefix on every card. Direct requests could remain textually silent
for minutes before a technical background notice.

**Action.** A slow direct reply gets one natural progress line at 20 seconds
and moves to background at 90 seconds with no IDs or commands. Tool narration
is retired. Work receipts remain mandatory private evidence, but card bodies
state the useful result naturally instead of rendering boilerplate.

### P1: Self-diagnosis assigned internal work to the owner

**Evidence.** Scheduler starvation, delivery queues and component probes were
copied verbatim into Lark cards. L3 converted component-health signals into
product proposals asking the owner to enter engineering work into a queue.

**Action.** Automatically owned diagnostics are recorded in the shared alert
stamp and remain internal. Only expired personal Lark authorization produces
a plain-language card with a real authorization action. Component-health
signals remain L3 evidence but cannot create owner-facing proposals.

**Acceptance correction, 2026-08-26.** Runtime evidence found the old tests
still required Guardian to send live-component degradation and persistent
task-failure cards ending in “you do not need to act”. Those sends are now
removed: self-healing states remain internal, while a genuinely dead component
may notify only after two red probes and its bounded recovery remains
unsuccessful, so owner action is actually required.

### P1: Infrastructure failures caused Intent refire bursts

**Evidence.** The Intent execution lease correctly restored content attempts
after provider failure, but immediately returned overdue rows to the due set.
A one-minute scheduler could reclaim the same occurrence repeatedly.

**Action.** Add an independent 15-minute `retry_after` watermark. It delays
infrastructure replay without consuming content attempts or changing the
original date/cron/interval cadence.

### P1: Two retained EigenFlux tasks lacked direct inventory evidence

**Evidence.** The profile hook had behavioral tests and inbox reconciliation
had core tests, but neither test body named the active heartbeat task, so the
static capability audit correctly kept both at `fix`.

**Action.** Add executable wiring contracts from each heartbeat task to its
tested hook. No EigenFlux feature is retired in this round.

### P1: A retired Tailscale resident still ran outside the component manifest

**Evidence.** Product and architecture documents correctly retired the mobile
gateway and all Jarvis-owned Tailscale paths, but an old
`com.pascal.jarvis.tailscaled` KeepAlive job remained loaded on the production
Mac. It continued changing the userspace network/DNS state and writing logs,
while `components.yaml` could not see it.

**Action.** Both installation and governed restart now boot out and remove the
legacy Jarvis launch-agent definition. Personal Tailscale account/application
data is not deleted; Jarvis only removes the resident it formerly owned.

## Architecture Assessment

The repository has strong domain contracts, deterministic ledgers, extensive
regression coverage, public-repository hygiene and unusually good operational
evidence. Its main debt is concentration: `memorial.py`, `intentions.py`,
`heartbeat.py`, `delegations.py` and `heartbeat_loop.py` remain large, and the
import graph still has direct cycles. This round does not perform a broad
module split because the production failure paths above are higher value and a
large structural rewrite would increase release risk. The next engineering
cycle should extract by existing ownership boundaries, one module at a time,
while holding behavior with scenario tests.

## Acceptance

- A primary 529 reaches a healthy backup route, including tool-capable calls,
  without same-provider model degradation.
- Task-level model/parse failure never asks Guardian to kill heartbeat.
- Runtime verification detects changes in tasks, scripts and launch surfaces.
- Install and governed restart remove every retired Jarvis launchd resident,
  including its historical userspace Tailscale service.
- A bot cannot start from a non-main or dirty runtime tree by accident, and an
  old bot cannot respawn children from a changed checkout.
- A slow reply emits at most one natural progress line before a plain
  background handoff; normal messages expose no tool narration, job ID or log
  command.
- Missing work evidence still suppresses a proactive card; valid evidence is
  stored privately and no mechanical receipt is prepended.
- Internal self-diagnostic and component-health findings do not message the
  owner or create owner-facing proposals.
- Infrastructure-deferred Intents cannot refire before their retry watermark.
- Capability inventory, focused tests, full suite, CI, review gate, governed
  deploy, runtime verification and post-release effect checks all pass before
  completion is claimed.

## Explicit Non-Goals

- Reintroducing `:3457`, `:3458`, Tailscale or another mobile application.
- Adding new product features while the product layer is frozen.
- Weakening release review, fabricating review evidence, or deploying from a
  dirty/feature-branch checkout.
- Treating a green tiny canary as proof that production-sized requests work.
