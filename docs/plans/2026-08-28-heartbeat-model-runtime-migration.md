# Heartbeat Model Runtime Migration

**Status:** implemented and locally verified; stacked review/release pending
**Parent:** `2026-08-28-provider-neutral-model-runtime.md`
**Date:** 2026-08-28

## Outcome

Every model-backed heartbeat task now executes through the shared
provider-neutral Model Runtime. Heartbeat still owns scheduling, task prompts,
private-memory selection, post-hooks, and task state. It no longer owns a
second provider selector, fallback loop, total-time budget, or provider-health
policy.

## Runtime Flow

```text
HEARTBEAT.md task(s)
  -> HeartbeatRunner task framing and stable task ID
  -> route-specific prompt builder
  -> core.heartbeat_model provider adapters
  -> core.model_runtime route/deadline/replay policy
  -> durable model call + attempt receipts
  -> deterministic heartbeat post-hook and task state
```

Solo calls are attributed as `heartbeat:<task>`. A shared batch uses
`heartbeat:batch:<sorted task names>`, so changing process or provider cannot
erase task ownership. Explicit GPT tasks narrow execution to OpenAI; ordinary
heartbeat tasks retain primary, two Claude-compatible relays, then OpenAI.

## Safety Decisions

1. A no-tools task may cross providers after a timeout; a tool-capable call
   stops when effects may have begun. Explicit account/model/auth/rate/overload
   admission rejection is safe to fail over because no model round ran.
2. Primary and relay credentials are isolated per spawned process.
3. Primary prompts use the cacheable full-memory profile. Fallback prompts are
   rebuilt with the constrained memory budget and are never reused as primary
   cache blocks.
4. The runtime receipt persists task, route, model, status, reason, timing and
   cost evidence, but never prompt text, credentials, or raw provider output.
5. Heartbeat keeps one redacted transient error summary for diagnosis while
   the durable receipt stores only a bounded reason code.
6. An ambiguous network failure is not replayable, yet still updates provider
   health so later calls avoid a broken route.
7. Generic `opus` maps to each relay's configured model alias; explicit cheap
   tiers remain portable.

## Deleted Paths

The former `HeartbeatRunner._legacy_claude_call`, direct OpenAI fallback
method, and their private provider timeout/environment helpers were deleted.
Tests now exercise the actual Model Runtime adapters and health snapshots,
not implementation-specific fallback hooks.

## Acceptance Evidence

- route narrowing rejects unknown and duplicate routes;
- primary-to-relay and relay-to-GPT failover use route-specific prompts;
- tool-capable timeout/nonzero failures do not replay;
- text-only timeout retains a final bounded GPT slot;
- health cooldown can skip primary or a relay without a production probe;
- provider/model/usage and scheduler receipts name the actual responder;
- solo and batch task IDs are stable;
- transient diagnostics remain useful and redact credential-shaped text;
- focused heartbeat, provider, continuity, component, and auxiliary suites
  pass before full repository validation.

## Release Order

This branch is stacked on the provider-neutral runtime foundation (PR #133).
It must be rebased after its parent lands, pass full CI and independent review,
then use the normal Owner authorization and governed deployment path. It does
not authorize or imply a production restart by itself.
