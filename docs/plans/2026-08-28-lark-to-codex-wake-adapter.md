# PRD: Verified Lark-to-Codex Wake Adapter

- Status: implemented release candidate; production and mobile acceptance pending
- Date: 2026-08-28
- Product boundary: Codex frontstage, Jarvis backstage, Lark wake-up
- Acceptance connector: `jarvis-matters 0.4.0`

## Problem

The product contract says substantive work should continue in Codex, but a
Lark wake-up still asks Pascal to open Codex, create a task, and type an exact
Matter phrase. That is honest but needlessly manual. The historical
`/matter handoff codex` path also prepared a Matter Run immediately. If Pascal
did not open Codex at once, the unused handoff occupied a six-hour lease and
could make the Matter look busy before any work began.

A fabricated desktop deep link is not an acceptable shortcut. No supported
task URL has been established, and a link that works on one device but not the
other would recreate the pairing and dead-end failures of the retired mobile
gateway.

## Outcome

An explicit owner action in private Lark can prepare one real, named Codex task
for the current Matter. The task is visible in Codex, contains no model turn,
starts no Matter lease, and carries only a bounded trusted instruction naming
the Matter. The owner opens it and says “继续”; only then does the normal MCP
continuation acquire the Matter and load the Context Packet.

If task preparation or verification fails, Lark states that no task was
confirmed and supplies the stable manual continuation phrase. It never claims
that Codex opened, synced to mobile, or started work without evidence.

## User Journeys

### Private Lark to desktop Codex

1. Pascal is already in a Lark conversation bound to one active Matter.
2. He says `去 Codex`, `在 Codex 继续`, or uses `/matter handoff codex`.
3. Jarvis starts a short-lived official Codex app-server session and creates a
   durable, empty task in the best known workspace.
4. Jarvis names the task `继续：<Matter title>`, reads it back, proves it has
   zero turns, and links its real thread ID to the Matter.
5. Lark tells Pascal which task to open. It also retains the stable phrase as
   a fallback.
6. Pascal opens the task and sends the first message. The task calls
   `jarvis_matter_continue` with the trusted Matter ID and wake ID; Jarvis
   resolves the real thread, advances the wake receipt, and acquires here, not
   in step 3.

### Private Lark to mobile Codex

The same task is prepared. Mobile visibility remains an acceptance claim, not
an implementation claim. Until Pascal confirms that the task appears and
continues correctly on real mobile Remote, the Lark reply always includes the
manual phrase and never promises a one-tap mobile open.

### Repeated action

If the linked task still exists and has zero turns, Jarvis returns the same
task. Once the task has any turn, the next explicit handoff creates a fresh
task for the next bounded execution window. A per-Matter process lock closes
the double-tap race.

## Contract And Authority

- Supported API boundary: Codex app-server JSON-RPC `initialize`,
  `project/list`, `thread/start`, `thread/name/set`, `thread/read`, and
  cleanup-only `thread/delete`.
- Jarvis never reads or mutates Codex rollout files or its state database.
- `thread/start` receives an absolute workspace, an optional saved project ID,
  and a fixed developer instruction containing generated IDs only. Matter
  titles and external text never enter the instruction channel.
- The durable wake receipt is the Matter session link metadata plus timeline
  event. It records the real thread ID, connector version, protocol user agent,
  workspace, source reference, zero-turn verification, and the fact that no
  lease started.
- The first continuation presents the exact generated `wake_id` back to
  Jarvis. Jarvis resolves one matching prepared link, verifies the workspace,
  records the real thread ID on the Matter Run, and advances the link through
  `running -> released|failed`. It never guesses from a title.
- The lifecycle is explicit: `requested -> task_created -> linked` or
  `requested -> failed`. `python3 -m core.codex_wake --audit` reports an old
  request with no terminal state, an external thread with no Matter link, and
  any orphan whose cleanup was not verified.
- A wake receipt is not a Result Receipt and cannot complete a Matter.
- Lark command ownership remains private-owner-only. Group and untrusted
  traffic cannot create a local Codex task.

## Failure Semantics

- Start or read-back failure: delete any unlinked task and return the manual
  phrase.
- Link failure: delete the task so Codex and Matter cannot silently diverge;
  if deletion itself cannot be verified, record the orphan thread ID and raise
  a structured operations alert while still telling the owner only that task
  creation was not confirmed.
- Secondary timeline rendering failure after a committed link: keep the
  verified task usable and emit a structured warning; the link is already the
  durable receipt.
- Existing prepared task cannot be read: fail closed to the manual phrase and
  do not create a possible duplicate. If it is read successfully and now has
  turns, create a fresh bounded task for the next execution window.
- Closed or missing Matter: no task creation.

## Acceptance

Deterministic tests must prove:

1. zero model turns and zero Matter runs after preparation;
2. real thread ID/name/workspace read-back before success;
3. no Matter title or user content enters developer instructions;
4. repeated handoff reuses one unused task;
5. a used task produces a new bounded task;
6. partial creation cleans the orphan;
7. app-server errors do not expose server messages or credentials;
8. private Lark natural-language and explicit commands share one path;
9. existing Claude CLI handoff remains unchanged;
10. full repository validation and capability inventory stay green.
11. a stale half-created wake is visible to the read-only lifecycle audit.
12. exact wake consumption records the real Codex thread on the Matter Run;
13. release and abort advance the wake link, while missed projection is visible
    to health audit.

Production acceptance additionally requires real owner samples on both
desktop and mobile. Those samples, not tests or Agent prose, determine whether
the stable phrase may later be reduced or a verified navigation affordance can
be added.

## Non-Goals

- no fake `codex://` or HTTP deep link;
- no autonomous model turn merely because a wake-up arrived;
- no automatic Remote pairing or enabling remote control;
- no second Jarvis task list or mobile application;
- no reduction of Lark delivery before desktop/mobile acceptance;
- no inference that task creation means the Matter is complete.
