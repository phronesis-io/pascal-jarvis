# Owner Chat Model Runtime Migration

**Status:** implemented locally; independent review and release pending  
**Parent:** `2026-08-28-provider-neutral-model-runtime.md`  
**Date:** 2026-08-28

## Product Problem

Owner-private Lark was still a separate model router. Shell selected Claude,
relay, Codex, and OpenAI; each route had different timeout, cancellation,
health, and replay behavior. That made three recurring failures possible:

1. a tiny provider canary looked healthy while a real owner turn hung;
2. a failed tool-capable turn could be replayed by the next shell branch;
3. the user-facing provider label had no durable call receipt behind it.

The migration must remove the duplicate router without turning Model Runtime
into a product brain. Lark remains a bounded transport, not the long-term
interactive frontstage.

## User Journey

1. Pascal sends a private message to Jarvis in Lark.
2. `bot.sh` authenticates the owner, resolves the conversation/Matter, obtains
   the session lock, and computes one provider preference plus one account-gate
   fact.
3. One killable `core.owner_chat_model` worker receives the prompt through
   stdin and the system prompt through a private temporary file.
4. Model Runtime selects an eligible route using real-request health, one total
   deadline, tool trust, and upstream configuration.
5. A proven pre-execution rejection may continue to another route. Any
   tool-capable failure with unknown effects stops as `ambiguous` and is never
   replayed.
6. The worker returns a bounded envelope with answer, call ID, actual route,
   actual model, terminal status, and sanitized reason.
7. `bot.sh` retains responsibility for progress, background promotion,
   cancellation, action processing, reliable delivery, conversation history,
   and Matter timeline projection.

## Ownership Boundary

| Concern | Owner |
|---|---|
| owner/group trust classification | `bot.sh` |
| conversation lock and auto-promotion | `bot.sh` |
| task/Matter identity | connector plus Matter bridge |
| route order, deadline, replay safety | Model Runtime |
| Claude/Codex/OpenAI process behavior | owner-chat adapters |
| actual provider/model receipt | Model Runtime |
| product completion and external reconciliation | owning product connector |
| reply/card delivery | `bot.sh` and unified delivery |

## Safety Invariants

1. Only an authenticated owner P2P turn enters this tool-capable adapter.
2. Group and non-owner traffic stays on the restricted no-private-tools path.
3. Preference and primary gate are acquired once by shell and passed as facts;
   the runtime wrapper cannot consume a second probe lease.
4. User content is stdin; private system context uses a mode-0600 temporary
   file. Neither appears in provider argv, logs, or runtime receipts, and each
   prompt file is removed after its attempt.
5. Prompts and credentials never enter SQLite receipts or public envelopes.
6. One shell attempt launches one runtime wrapper; shell has no Codex replay
   helper and cannot start a second provider after an ambiguous result.
7. Claude, Codex, OpenAI, and their tool descendants share a killable process
   holder. A cancellation during process spawn closes the new group before the
   wrapper exits.
8. A cancelled effectful call is reconciliable `ambiguous` evidence, not proof
   that no effect happened.
9. The reduced backup prompt is rebuilt inside the wrapper from the same
   bounded prompt compiler and never silently replaces the primary context.
10. Provider and model labels shown after delivery come from the same terminal
    runtime receipt as the answer.

## Acceptance

- Claude account limit to Codex succeeds through the production handler and
  returns the same logical context to later sessions.
- Claude limit plus proven pre-turn Codex unavailability reaches a local fake
  OpenAI Responses server and delivers through Lark.
- ambiguous, cancelled, and post-tool failures do not replay;
- later health routing skips a known failed primary and all-cooling fails
  closed;
- existing group/non-owner trust tests remain green;
- cancellation reaps the active provider/tool process group;
- capability inventory, public-repo hygiene, architecture gates, full local
  suite, protected CI, independent review, deploy verification, and runtime
  receipts pass before production release.

## Release State

Implementation and local tests are not production evidence. This candidate is
stacked behind the earlier Codex-frontstage and Model Runtime PRs. No merge,
restart, or production claim is allowed without the exact Owner authorization,
protected main CI, deploy verification, and a post-release owner-turn receipt.
