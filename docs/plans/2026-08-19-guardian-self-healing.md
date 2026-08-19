# Guardian Self-Healing Incident Closure

## Problem

Guardian historically mixed observation, delivery, and escalation semantics.
Queued or duplicate alerts were called failures and retried forever; first
probe failures reached Pascal before the owning watchdog had time to recover;
brain-health and self-diagnostic checks explicitly said they would not repair;
legacy dead-letter evidence was truncated before a notice was accepted. A
separate external incident also routed private Mac absence into a monitoring
group.

## Product Contract

1. Guardian works before it speaks: observe, request one bounded owner repair,
   verify, then notify only if the same incident remains.
2. Sleep, wake grace, deploy windows, offline state, and active coding sessions
   are expected states, not component failures.
3. A notification is a receipt of work already attempted. It says what remains
   wrong and does not ask Pascal to run routine operator commands.
4. Guardian is an independent process path, not an independent Lark channel.
   The external dead-man is the only whole-machine/out-of-band detector.
5. Runtime and personal health data is `owner_private`. No group/public
   fallback is permitted; uncertain routing fails closed.

## Engineering Contract

- Child recovery requires exact command identity and ancestry under the live
  repo-owned `bot.sh`. Dashboard recovery uses the exact launchd label.
- Component probes require two red observations separated by recovery grace.
- Brain-dead heartbeat and stale self-diagnostic recycle the scheduler once,
  persist the repair timestamp, and allow a real task-cycle recovery window.
- Delivery receipts are `confirmed`, `covered`, `pending`, or `lost`. Pending
  work never raises a banner; lost work does. Stable incident keys deduplicate
  wording changes.
- Legacy dead letters are read under a lock and removed only after every notice
  is accepted by the durable pipeline; concurrent appends are preserved.

## Acceptance Evidence

- Unit tests cover queued/attempting/covered/lost receipts and private metadata.
- Process tests prove a matching foreign process and a near-match command are
  not killed.
- Probe tests prove first failure repairs silently and persistent failure pages.
- Scheduler tests prove first brain/diagnostic failure repairs and only a
  post-grace failure pages.
- Dead-letter tests prove genuine loss preserves evidence and durable queueing
  consumes the legacy copy.
- Release requires full local tests, CI, merged-main gate, restart, component
  health, deploy verification, and read-only delivery/dead-man checks.

## Non-Goals

Guardian does not wake a sleeping or powered-off Mac, repair an external model
provider, or turn Lark into an out-of-band channel. It also never restarts an
interactive Claude Code or Codex process.
