---
name: jarvis-matter
description: Continue durable work across Codex tasks, devices, Lark, Claude Code, or time by binding the current outcome to a Jarvis Matter. Use for multi-session work, tracked commitments, handoffs, or verified external effects; do not use for ordinary one-turn questions.
---

# Jarvis Matter

Codex is the interactive frontstage. Jarvis is the durable backstage. Keep the
conversation and execution in Codex; use Jarvis only for continuity that must
survive this task.

## Decide

A Matter is warranted when at least one is true:

- the work will continue in another task, device, product, or day;
- Pascal made a commitment or expects a later follow-up;
- the work has external effects that need durable evidence;
- several artifacts or executors belong to one recognizable outcome.

Do not create a Matter for an ordinary answer, disposable exploration, or a
task that can finish and be verified entirely in the current Codex task.

## Work

1. Search before creating. Reuse the same recognizable outcome.
2. Start a Matter run before substantive work. Pass the real workspace and a
   concise task outcome. Pass `desktop` or `mobile` as the surface when it is
   known. Keep its `run_id`, `context_generation`, and `context_digest`.
3. Treat the returned Context Packet as the current bounded contract. Follow
   pointers only when needed; never load unrelated private memory or raw
   transcripts.
4. Renew the lease during long work.
5. Release exactly once. List only files inside the workspace that now exist
   or are verifiably deleted. External effects need Delegation evidence IDs.
6. If execution cannot finish, abort the run so the next task is not blocked.

## Authority

- A Result Receipt closes the execution window, not the Matter.
- Do not claim an external action from prose, tool intent, or an unverified
  response.
- Do not mark a Matter done. Jarvis reconciles evidence and Pascal owns
  consequential closure decisions.
- A new Codex task is the normal boundary for a distinct outcome. Multiple
  tasks may contribute to one Matter, one run at a time.
