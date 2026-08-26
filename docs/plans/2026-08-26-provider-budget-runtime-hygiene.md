# Provider Budget and Runtime Hygiene Closure

## Evidence

- A production heartbeat call failed on the official provider after 8 seconds,
  then a Claude-compatible relay consumed the remaining 600-second task budget.
- The documented single-wall-clock contract was not true in code: the OpenAI
  reserve was added after the task timeout instead of being reserved inside it.
- Repeated transient OpenAI network failures could remain in an escalating
  cooldown even after the same HTTP canary recovered.
- Daemon rotation created `daemon.log.1` in the repository while only the base
  log name was ignored.

## Contract

1. One logical model call never exceeds its caller-provided wall-clock budget.
2. Replay-safe calls reserve bounded slots for configured downstream routes.
3. Every Claude-compatible relay attempt is capped independently (120 seconds
   by default), including tool-capable calls.
4. A tool-capable timeout remains ambiguous and is never replayed elsewhere.
5. A later OpenAI canary may clear only an older `network_error`; it cannot
   clear timeouts, quota/auth failures, or a concurrent real-request failure.
6. Provider failures on non-heavy isolated work never open that business
   task's circuit. Context overflow and heavy-task failures remain task-owned.
7. All daemon log generations remain local operational evidence and are ignored
   by Git.

## Acceptance

- A 600-second replay-safe chain with Primary, Backup1, Backup2 and OpenAI uses
  maximum attempt slots of 240, 120, 120 and 120 seconds.
- A tool-capable call routed directly to a stalled relay fails closed after at
  most 120 seconds and performs no cross-provider replay.
- Provider-state concurrency tests prove a newer real failure wins over a
  canary result.
- GPT, untrusted and no-tools provider outages retain fast task retry without
  opening a multi-hour per-task circuit.
- Repository hygiene tests require `daemon.log.*` to be ignored.
