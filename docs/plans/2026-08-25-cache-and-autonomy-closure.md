# Prompt Cache And Autonomy Closure, 2026-08-25

## Decision

Keep the product surface frozen. This round corrects post-PR #112 runtime
claims and closes internal reliability/readability gaps without adding a new
client, card type, model tier, or notification channel.

## Evidence And Actions

### Main Lark prompts did not use indexed warm memory

`bot.sh` exported `JARVIS_WARM_MEMORY_MODE=index`, but only heartbeat passed
that mode into the memory loader. Owner Lark prompts still expanded the whole
warm tier. `build_system_prompt` now consumes the runtime mode, with an
explicit `full` override for callers that require verbatim warm text.

### Volatile state invalidated the stable cache prefix

Active intents and today's calendar appeared before stable identity and warm
guidance. Index-mode composition now orders stable facts/rules/profile and
protected guidance first, followed by volatile hot state, the knowledge map,
system state, timeline, recent context, and the current timestamp. The
small-context focus ranking remains in place: removing it would reduce backup
answer relevance and is not required for the normal all-fits cache path.

### Task-level model routing is an explicit policy

`HEARTBEAT.md` can request `opus`, `sonnet`, `haiku`, or `gpt`. GPT tasks run
as isolated OpenAI-compatible calls; mixed Claude batches select the strongest
declared Claude tier. Feed, friend-request, and mail triage use GPT; structured
summaries and operational interpretation use Sonnet; owner-facing writing and
memory mutation continue to inherit Opus. Requested lower tiers remain lower
tiers across relays instead of being silently promoted to an Opus backup.

### Timeout fallback is bounded by one logical-call budget

A production request is never reused as a provider-health probe. Recovery
probing belongs to the small `provider-canary` task. A logical call now has one
wall-clock budget shared by every provider and fallback attempt. Safe no-tool
calls may continue to GPT inside the remaining budget, while an ambiguous
tool-capable timeout fails closed rather than replaying unknown side effects.

### Internal names and morning titles leaked implementation

The `pgc_pulse` source now renders as `PGC 指标日报`. Morning batches collapse
identical titles into `xN`, keep both the beginning and distinguishing suffix
of long titles, and show an ellipsis instead of silently cutting text.

### Self-improvement was measured at the wrong boundary

The heartbeat pre-hook is intentionally empty because it only starts a
detached coding session. Empty scheduler output is therefore not a failure.
The detached session previously had no completion evidence, which was the
real blind spot. Each run now persists acquire/run/release evidence with no
raw output in state, reconciles a dead process without a release receipt,
retries failures on a bounded clock, and becomes a self-diagnostic warning
only after repeated failed closures. A final worker admission check rejects a
stale child before it can invoke a model, and release receipts are write-once.
The worker lease has a hard deadline; expiry verifies the command identity
before ending its process group, and PID reuse is reconciled without signalling
an unrelated process. Identity-probe uncertainty retains the lease, and a
failed TERM/KILL confirmation blocks any overlapping worker.

### Optional work now needs material evidence

EigenFlux publication runs only when recent local evidence has a new digest,
with one bounded retry after 24 hours; raw material is never persisted in the
gate state. Memory tidy similarly runs after the memory tree changes, plus one
daily staleness pass. The standalone content-recommendation heartbeat and the
calendar hook's hard-coded NBA download were retired: both were inactive or
unowned behavior rather than part of the frozen product surface.

## Explicit Non-Goals

- Automatic quality changes outside the Owner-approved task model table.
- Replaying a tool-capable request after an ambiguous timeout.
- Removing the session rotation at background promotion. It prevents two
  concurrent writers from resuming the same provider transcript; prompt-cache
  waste is fixed at the resumed-prompt boundary instead.
- Putting raw memory, prompts, model output, credentials, or private message
  bodies into tracked docs or lifecycle receipts.
- Publishing or deploying before focused tests, full local tests, review,
  protected CI, and owner release authorization pass.

## Acceptance

1. Production-default owner prompts contain a warm file map, not expanded
   reference bodies; explicit full mode remains backward compatible.
2. Stable identity/guidance precede volatile calendar/intention state in
   index mode, and current time is the final system-prompt line.
3. Small-budget focused retrieval, untrusted/group privacy, and no-tool memory
   behavior remain unchanged.
4. Repeated morning titles appear once with a count; long titles preserve a
   useful suffix and show explicit omission.
5. Known perception sources never expose internal identifiers in user text.
6. Every detached self-improvement run has a terminal receipt or is
   deterministically reconciled and retried; stale workers cannot execute or
   replace an earlier receipt.
7. Production task prompts remain at least 30% smaller than the 50,680-character
   audit baseline; the current contract is below 35,000 characters.
8. Runtime model/cache ratios and provider failover remain post-release
   acceptance evidence, never inferred from unit tests.
9. Initial, queue/rotation, and backup-provider prompt rebuilds all preserve
   provider-session resume detection.
