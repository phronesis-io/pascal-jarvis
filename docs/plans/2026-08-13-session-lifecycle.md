# Jarvis Logical Session Lifecycle PRD

Status: Released on 2026-08-13 at `cce89d4f5b326a278455a8cf6ca2ab8339a1a9f3`
Date: 2026-08-13
Owner: Pascal / Jarvis

## Problem

Jarvis currently maps one Lark conversation directly to an automatically
rotating Claude transcript. The owner cannot name a topic, open a fresh work
context, return to an earlier topic, or deliberately reset short-term context.
The production owner conversation has consequently accumulated hundreds of
physical provider sessions while `matter_bindings` remains unused.

Adding only a `/session new` command would be unsafe. Compact summaries,
cross-provider turns, and Codex threads are currently keyed by the Lark
conversation, so switching topics would still carry the previous topic into
the next provider prompt.

## Product Contract

Jarvis exposes a **logical session** to Pascal. A logical session is backed by
one Matter and survives provider changes, device changes, and physical context
rotation. Claude sessions and Codex threads are replaceable execution windows;
their identifiers are never part of the user-facing interaction.

Supported private-Lark commands and natural-language equivalents:

- `/session new <title>`: create and enter a new logical session.
- `/session switch <title|matter-id>`: resume an existing logical session.
- `/session current`: show the current logical session and next action.
- `/session list`: show resumable logical sessions.
- `/session reset`: discard ephemeral projected turns and compact context while
  retaining the Matter's durable goal, decisions, links, and artifacts.
- `/session close [outcome]`: close the Matter, subject to existing follow-up
  guards, leave it, and start with a fresh unbound provider window.
- `/session leave`: leave the current logical session without closing it.
- `/session help`: concise command reference.

Existing `/matter` commands remain compatible. Session management is private
owner functionality and is refused in group conversations.

## Context Model

1. `conv_key` identifies the transport conversation and owns routing preference.
2. `matter:<matter_id>` identifies a bound logical context.
3. `conversation:<conv_key>` identifies an unbound logical context.
4. Claude's `session_id` and Codex's `thread_id` are physical provider state.

Compact summaries, recent cross-provider turns, and Codex threads use the
logical context key. Manual logical transitions rotate Claude immediately.
Returning to a Matter restores only that Matter's bounded context. Reset clears
only the selected logical context's derived compact/turn projection and Codex
thread; raw provider logs remain intact.

## Safety And Concurrency

- A transition is refused while the current provider session lock is active.
  Pascal can stop the running turn first, then retry the transition.
- Once a long turn is promoted into a context-scoped background job, it no
  longer blocks switching; its result can only return to the captured session.
- The message dispatch path captures Matter and logical context before spawning
  a provider. The eventual reply is recorded against that captured context even
  if a later command changes the current binding.
- Group prompts never receive bound private Matter context.
- Reset advances a logical generation atomically. Late receipts and queued
  background/card context from an earlier generation cannot repopulate it.
- A Codex thread has one cross-transport lock per logical session.
- Database evolution is additive and migrates old turns to their unbound
  conversation context.
- Logical transitions do not send messages, create calendar events, or mutate
  external services.

## Acceptance Criteria

1. New, switch/resume, current, list, reset, close, leave, and help work through
   deterministic private-Lark commands and documented Chinese aliases.
2. New/switch/close/leave/reset produce a fresh Claude physical session.
3. Matter A turns and compacts never appear in Matter B or unbound prompts.
4. Claude-to-Codex fallback within a Matter resumes that Matter's Codex thread;
   another Matter receives a different thread.
5. Reset preserves Matter durable context but removes its recent projected
   turns, compact summary, and Codex thread.
6. A delayed assistant receipt is written to the context captured at dispatch,
   not whichever Matter is bound later.
7. Group session commands and group Matter prompt projection fail closed.
8. Existing `/matter` and provider-continuity behavior remains compatible.
9. Full local tests, independent adversarial review, PR CI, merged-main CI,
   governed deploy, component health, and production read-only smoke all pass.

## Non-goals

- Deleting raw Claude or Codex transcript files.
- Letting an LLM infer or silently switch logical sessions.
- Exposing provider session IDs in Lark or the mobile UI.
- Replacing Matter with a second competing project/task model.

## Verification

- Strict repository gate: 2,977 tests passed, including shell syntax and the
  CI-parity ShellCheck policy.
- Fault injection covers database rollback, migration crash re-entry, delayed
  old-generation writes, queued context switches, and a deterministic command
  process exiting after a simulated side effect.
- Independent adversarial re-review found no remaining P0, P1, or P2 finding
  in the locally verified worktree.
- PR #74 and merged-main CI passed. The protected release gate verified the
  final PR head, merge SHA, required GitHub check, branch protection, and the
  post-merge admin-owner release decision.
- The governed restart deployed the merge SHA to every resident component.
  Runtime localtest, component/deploy checks, unified delivery smoke, desktop
  and 390x844 mobile browser smoke, bounded provider canaries, and the
  post-release L3 observation all passed.
- Claude primary remained quota-limited during the release canary; the Claude
  relay, read-only Codex fallback, and GPT API fallback all answered. Backup2
  remains intentionally disabled and unconfigured.
