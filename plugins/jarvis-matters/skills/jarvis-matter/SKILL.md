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

1. The user speaks naturally; do not teach or expose the Matter protocol. When
   the request clearly continues durable work, call `jarvis_matter_continue`
   with a concise identifying phrase. If it returns one match, proceed. If it
   is ambiguous, ask one short human question using the returned titles. If no
   match exists and the work deserves continuity, create it once and continue.
2. The one-step continuation acquires the Matter run before substantive work.
   Pass the real workspace and concise task outcome. Pass `desktop` or `mobile`
   when known. Keep its `run_id`, `context_generation`, and `context_digest`.
3. Treat the returned Context Packet as the current bounded contract. Follow
   pointers only when needed; never load unrelated private memory or raw
   transcripts.
4. Renew the lease during long work.
5. Release exactly once. List only files inside the workspace that now exist
   or are verifiably deleted. External effects need Delegation evidence IDs.
6. If execution cannot finish, abort the run so the next task is not blocked.
7. When Pascal explicitly says the named Matter is complete, release any live
   run first, then call `jarvis_matter_close` with the useful outcome and his
   exact confirmation words. This one transition retires linked reminders,
   Items, and Handoffs. A live run, Job, or Delegation remains a blocker.

## Remember

- Search compiled memory when Pascal refers to a settled decision, preference,
  commitment, or fact from another Codex task, Claude Code session, or Lark.
- Active compiled claims may enter a Context Packet. Raw transcripts and
  assistant-only candidates may not.
- Each claim must retain source references. Follow the raw source only for an
  explicit audit or when Pascal asks to inspect the original conversation.
- Use memory review only after Pascal explicitly confirms, chooses, or rejects
  the named claim in the current conversation. Never infer consent, self-review
  a claim, or invent a reviewer identity.

## Model Status

- Use `jarvis_model_status` when Pascal asks which model is active, how much
  package usage remains, when it resets, or whether fallback is usable.
- Treat only `quota_evidence=exact` windows as numeric allowance. Account
  login, a configured token, and a green canary do not prove remaining quota.
- Report unknown providers plainly. Never convert token counts into a made-up
  subscription percentage.

## Authority

- A Result Receipt closes the execution window, not the Matter.
- Do not claim an external action from prose, tool intent, or an unverified
  response.
- Do not infer Matter completion. Pascal may explicitly close it; Jarvis then
  reconciles linked state and records an authoritative closure receipt.
- A new Codex task is the normal boundary for a distinct outcome. Multiple
  tasks may contribute to one Matter, one run at a time.
