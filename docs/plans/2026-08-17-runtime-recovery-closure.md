# Runtime Recovery Closure

Date: 2026-08-17
Status: implemented, awaiting governed review/release
Scope: reliability only; product feature expansion remains frozen

## Incident Evidence

The production ledger recorded 271 terminal delivery failures between August
14 and August 17. Of those, 216 were Guardian messages without durable incident
identity. The same Lark transport carried both the original messages and the
Guardian warnings, so the warning path failed with the path it was describing.
After transport recovery, no terminal envelope was reconciled or replayed.

Provider evidence had a second split-brain failure: a tiny backup canary kept
the route green while 59 production-sized requests each timed out at 600
seconds. The persisted row did not distinguish canary evidence from a real
request, and the timeout cooldown was shorter than the scheduler interval.

The owner-user Lark path also reported Keychain access failures as if the OAuth
token had expired, asking Pascal to authorize again even when the token was
valid in an interactive shell.

## Product Contract

1. Alerts have a stable incident identity even when their producer omits one.
2. A terminal delivery failure is not described as queued.
3. A verified transport success reconciles terminal failures. It requeues only
   unresolved, unexpired work and retains the original envelope/idempotency
   identity.
4. Guardian, routine, check-in, morning-anchor, intention-check, and calendar
   sync output is regenerated work and is never replayed after an outage.
5. Replayed work gets one recovery attempt. Resolved, expired, superseded, or
   exhausted work becomes an audited suppression.
6. A real provider request is stronger evidence than a successful tiny canary.
   Canary evidence remains visible but cannot erase a production timeout.
7. Real-request failures create an explicit, escalating cooldown. A timeout
   starts at 30 minutes and repeated failures expand to two and eight hours,
   capped at twelve hours. A real success resets the streak.
8. Tool-capable calls remain fail-closed within the same request after an
   ambiguous timeout: replaying them could duplicate side effects. The
   provider is cooled so the next request selects a different eligible route.
9. Owner OAuth expiry and background Keychain denial are different states.
   Only verified auth expiry offers the authorization action.
10. The external dead-man receives a success ping only while both the local
    stack and the Lark delivery transport are healthy. Three consecutive
    transport failures withhold the ping so the external service can alert.

## Boundaries

- `core.delivery` owns alert identity, recovery reconciliation, replay policy,
  and transport health derived from real attempts.
- `core.provider_health` owns canary and real-request evidence without merging
  their authority.
- `core.model_control` consumes persisted cooldowns when building route plans.
- `daemon.py` turns local health into external dead-man success pings and
  presents terminal failures in human language.
- User-scoped calendar/docs/mail/task access remains an OAuth boundary; bot
  delivery cannot impersonate or replace it.

## Verification

- Provider tests prove real timeout evidence survives a later green canary,
  repeated failures escalate cooldown, and real success resets it.
- Delivery tests prove stable alert deduplication, valid recovery replay,
  terminal dead-letter reconciliation, stale suppression, and non-replay of
  regenerated work.
- Guardian tests prove incident identity, transport-aware dead-man withholding,
  and human terminal-failure copy without raw credential errors.
- Calendar/self-diagnostic tests prove a Keychain context failure preserves the
  last snapshot and does not offer reauthorization.
- Full repository validation and CI remain required before release.

## Configuration-Dependent Remainder

Code cannot create the external monitoring account or invent its tokenized ping
URL. The operator must place a private HTTPS endpoint in
`JARVIS_DEADMAN_URL` or the gitignored `jarvis.yaml`, set
`ops.deadman.enabled: true`, and configure the service to alert after at least
45 minutes without a ping. The URL must never enter Git, logs, status output,
or a public issue.
