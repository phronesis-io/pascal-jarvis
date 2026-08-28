# Attention And Owner-Copy Audit, Round 19

**Status:** code-complete, release pending  
**Scope:** audit findings T43-T49  
**Product rule:** Jarvis may ask for attention only after it has done the work
it can do, and it must describe the remaining owner decision without exposing
internal execution machinery.

## Findings Closed

| Finding | Change | Acceptance evidence |
|---|---|---|
| T43 stale docket | The morning docket includes only delivered, unresolved decision Items. A linked terminal Intent retires its old card. | Never-delivered and terminal-linked regressions in `test_memorial_escrow.py`. |
| T44 raw provider/Guardian errors | The disabled-organization provider string is blocked. Guardian now creates an actionable Item with acknowledgement, conversation exit, work receipt, stable incident identity, and non-recursive failure handling. | Safety and daemon regressions, including same-day acknowledgement dedup and dead-letter recursion prevention. |
| T45 duplicate morning narration | An intention response beginning with yesterday's main-line summary completes internally but does not create a second morning card. | Two Chinese punctuation variants replayed in `test_intentions_post.py`. |
| T46 fake engagement | Deploy smoke ends at verified delivery and never records an owner action. | Delivery row remains `delivered`, with no `acted_epoch`. |
| T47 repeated usage episode | Quota alerts preserve one route/window episode across read failures and reset rollover; the owner route order uses stable human names. | Model-usage task episode regressions. |
| T48 excessive Codex exposure | Closed in Round 18 by field whitelisting and source-aware transcript exclusion. | Codex frontstage and memory source privacy tests. |
| T49 internal copy | Quota, weekly review, Matter closure, and Codex continuation copy use human times and names, bounded conclusions, and no internal IDs or raw run titles. | Model usage, weekly review, and Matter continuity regressions. |

## Message Contract

Every proactive card must answer, in order:

1. What changed in the outside world or durable system state?
2. What did Jarvis already do and verify?
3. Why does this need Pascal now rather than a background retry or Codex task?
4. What is the smallest decision or acknowledgement still required?

If question 3 has no strong answer, the event remains backstage. Delivery,
health checks, retries, model routing, and internal identifiers are evidence,
not reasons to interrupt the owner.

## Release Boundary

This round changes repository code only. It does not merge, deploy, restart,
or send a Lark message. Those effects still require review, CI, exact-SHA
release evidence, and explicit Owner authority for the final release commit.
