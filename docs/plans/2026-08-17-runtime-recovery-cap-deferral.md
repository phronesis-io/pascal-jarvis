# Runtime Recovery Cap Deferral

Date: 2026-08-17
Status: implemented after PR #86 runtime verification
Scope: narrow reliability follow-up

## Finding

PR #86 correctly reconciled 271 historical terminal failures into 266 obsolete
or resolved suppressions and five then-valid EigenFlux replays. The first replay
was delivered, but the remaining four hit the already-full global daily budget.
The ordinary proactive policy made cap overflow terminal, so those four rows
became `suppressed` rather than waiting for the next budget window.

That policy is correct for newly generated proactive output: carrying today's
overflow into tomorrow would create a backlog. It is wrong for a recovery
envelope whose source Item has already passed the unresolved/unexpired gate.

## Contract

1. A recovery receipt grants no new budget or quiet-hour exemption; the
   envelope keeps its existing attention class.
2. A recovery envelope blocked by a daily cap remains queued until the next
   local budget window instead of becoming terminal.
3. Reconciliation repairs cap-suppressed recovery rows produced by PR #86
   without incrementing their one allowed replay count.
4. Recovery persists its computed expiry. `flush_due` suppresses the envelope
   if it becomes stale while waiting, before any transport call.
5. Ordinary proactive overflow remains terminal; this behavior applies only to
   envelopes carrying the explicit recovery deferral marker.

## Production-Data Drill

A WAL-safe copy of the post-#86 production database and the complete Memorial
ledger was exercised with the follow-up code. All four cap-suppressed rows were
requeued. At the current full daily budget, all four deferred without a send.
At the next awake budget window, three notices exceeded their 24-hour TTL and
closed; the one still-current notice delivered. The production database was not
modified by the drill.

## Verification

- Regression: recovery at a full global cap queues to the next local day and
  delivers after the cap resets.
- Regression: an upgrade repairs a recovery row already suppressed by cap.
- Regression: an unreceipted producer cannot opt into recovery deferral.
- Regression: a recovery row expiring while deferred closes without transport.
- Focused delivery, quiet-hour, observability, and import-graph gate:
  `109 passed`.
- Complete local gate: `3133 passed`; shell syntax, shellcheck, capability
  inventory, and diff checks passed. Protected CI remains required before
  release.
- The production-data drill was repeated after receipt validation was tightened:
  four rows deferred with zero current-window sends; three expired and one
  delivered at the next awake window.
