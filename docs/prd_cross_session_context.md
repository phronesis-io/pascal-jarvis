# Cross-Session Context for the Main Agent

## Problem

Pascal works in Lark, Claude Code, and Codex, but the main Jarvis conversation
previously learned only from Claude transcripts outside the Jarvis workspace.
Codex was absent, same-workspace Claude sessions were deliberately excluded,
and the only bridge depended on a successful 10-minute heartbeat summary.
Listing session metadata on the dashboard did not give the main agent the
decisions, blockers, or next steps from those conversations.

## Outcome

The next owner-private Jarvis turn understands recent work discussed in either
Claude Code or Codex without asking Pascal to repeat it. Important context also
survives as a durable digest after native provider sessions age out. Relevant
older work can be retrieved from a private local index without loading the full
archive into every prompt.

## Requirements

1. Discover top-level, human-driven Claude Code and Codex sessions across
   workspaces, including the Jarvis workspace.
2. Exclude Jarvis-owned provider sessions, headless Codex executions,
   canaries, subagents, reasoning, tool inputs, and tool outputs. Preserve
   owner-visible Codex Desktop tasks even if a provider version labels them
   `source=exec`.
3. Redact credential-shaped material before text reaches a prompt, digest, or
   diagnostic output.
4. Inject a bounded recent projection only into the owner's private prompt.
   Group and external conversations never receive it.
5. Keep the existing heartbeat digest, upgraded to both providers and an
   atomic per-file watermark. Unchanged sessions emit nothing, and watermark
   entries outside the same scan window are pruned. If an old transcript later
   becomes active again, a bounded global scan cursor must baseline its
   historical turns so only newly appended turns are emitted.
6. Treat transcript text as untrusted historical context. Mutable facts such
   as PR, deploy, task, or calendar state still require authoritative checks.
7. Provider transcript formats may fail independently; a bad or drifting file
   must degrade to missing context, never break the main conversation.
8. Build a private, gitignored, WAL-backed SQLite index in bounded batches.
   Persist only redacted visible user/assistant turns and hashed source paths;
   transcripts remain the rebuildable source of truth.
9. Retrieve older turns only when they match the current owner request. Generic
   continuation text must not surface a historical archive, and historical
   context never enters a shared chat or a named Matter.

## Acceptance

- A new Claude Code turn appears in immediate context and incremental digest.
- A new Codex Desktop/CLI turn appears through the same contract.
- A second unchanged scan emits nothing; an appended turn emits only the new
  turn plus a marked context tail.
- A transcript can age out of the watermark state and later reactivate without
  replaying its historical turns; its newly appended turn is still emitted.
- Managed Claude sessions, Codex fallback/canary `exec` sessions, and Codex
  subagents never appear.
- Secret fixtures are replaced with `[redacted]`.
- Owner prompts include the projection; group prompts do not.
- Historical Claude Code and Codex fixtures are indexed incrementally, survive
  process restarts, update after append, disappear after source deletion or
  later managed-session ownership, and are queryable by topic.
- Identical turns in different sessions do not collide; credentials, tool
  payloads, and provider failure text are absent from the index and projection.
- Full tests, independent review, protected CI, release gate, restart, and a
  production read-only context canary all pass.

## Non-Goals

- Copying full transcripts into SQLite, Matter, memory, or the phone.
- Sharing cross-session context with groups or other users.
- Treating remembered provider prose as completion or current-state evidence.
- Unifying native provider thread IDs or replacing provider-native history.
