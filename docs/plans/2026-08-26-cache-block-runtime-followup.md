# Prompt Cache Block Runtime Follow-up, 2026-08-26

## Why T2/T10 Reopened

The first audit fix moved volatile memory and the minute timestamp toward the
end of the system prompt. Production evidence showed that this did not meet the
acceptance contract:

- repeated `intention-check` calls still wrote about 80k cache tokens each;
- their cache reads remained about 20k, the Claude Code base/tool prefix;
- cross-session batches sometimes read more, but still wrote another roughly
  80k system block.

The invalid assumption was that a stable byte prefix inside one text block is
independently reusable. Provider caching hashes content blocks and cumulative
prefixes at cache breakpoints. A timestamp or live memory suffix changes the
same system block, so the earlier cache entry does not match.

## Corrective Design

1. Lark stores one exact private system snapshot per provider session. The
   cache key includes the session, chat/context/Matter boundary, provider memory
   budget, and a digest of prompt/memory assembly code.
2. Heartbeat keeps an exact one-hour in-process snapshot for each primary
   trust/tool profile. Full-memory, outbound, relay, and GPT paths rebuild.
3. Current time moves to the user request. Lark already prefixes each message;
   heartbeat appends the same authoritative timestamp to task DATA.
4. Primary heartbeat snapshots do not reorder memory from each task's changing
   focus text. Constrained fallback routes retain relevance ordering.
5. Private snapshots are ignored runtime data, directory mode 0700, files and
   locks mode 0600, and capped at 128 entries.

## Acceptance

- Unit tests prove two calls in one session receive byte-identical system
  prompts after memory/time changes, while a new session receives fresh memory.
- Unit tests prove two compatible Heartbeat calls load memory once, reuse the
  exact system block, and carry different timestamps in their user requests.
- After release, two compatible calls within five minutes must show the second
  call shifting the custom system block from cache creation to cache read.
- A live resumed Lark pair must show the same behavior without losing current
  time, route identity, action handling, or private-context isolation.

Until the last two runtime checks pass, T2/T10 remain runtime-pending.
