# Lark Identity Transport Separation

**Status:** implementation complete; release verification pending

## Problem

Jarvis used `lark-cli` for both application-bot messages and owner-identity
APIs. The CLI encrypted both profiles behind one macOS Keychain master key.
When an automation sandbox lost Keychain access, calendar sync correctly
failed, but bot replies, Memorial cards, EigenFlux realtime messages, and even
bot open-id discovery failed with it. One credential boundary therefore took
down unrelated product capabilities and filled the durable delivery queue.

## Product Outcome

Pascal must still receive and answer Jarvis messages when personal OAuth is
expired or temporarily inaccessible. Personal calendar/docs/mail/task actions
must say they are unavailable and wait for reauthorization; they must neither
block bot communication nor silently run as the bot.

## Identity Contract

| Identity | Credential | Capabilities | Failure behavior |
|---|---|---|---|
| application bot | private app id/secret, short-lived tenant token | replies, cards, proactive alerts, EigenFlux delivery, bot metadata | retry through unified delivery; require message-id receipt |
| owner user | user OAuth token | calendar, docs, mail, tasks and other personal data/actions | capability-specific degraded state; no bot substitution |

The tenant token is memory-only. Secrets and tokens never enter Git, SQLite,
logs, command arguments, or user-visible diagnostics.

## Flow

```text
producer -> core.delivery policy -> core.lark_bot_transport
         -> tenant token (memory cache) -> Lark message API
         -> provider message_id -> delivery receipt

owner calendar/docs action -> lark-cli --as user -> user OAuth gate
                            -> verified result or explicit degradation
```

Legacy installations without an app secret may continue using the old
bot-only CLI path. A configured production installation prefers OpenAPI and
does not touch Keychain for bot sends.

## Acceptance

1. A Keychain/user-token failure cannot block bot text, card, reply, Memorial,
   self-diagnostic, or EigenFlux stream delivery.
2. Every successful bot send carries a real Lark `message_id`; a 2xx response
   without that receipt is a failed attempt.
3. Invalid cards, missing/ambiguous targets, HTTP errors, timeouts, and rejected
   API responses fail closed with payload-free reason codes.
4. The app secret and tenant token are absent from logs, result objects, the
   repository, and durable delivery state.
5. Bot metadata discovery uses the same identity boundary, preserving group
   mention routing during user OAuth outages.
6. User APIs remain unavailable until their own OAuth is healthy; Jarvis never
   claims calendar/docs recovery from a bot-message canary.
7. Focused tests, full tests, protected CI/review, governed deploy, real bot
   send/read-back, EigenFlux delivery, and user-OAuth degraded-state canaries
   are recorded before the release is complete.

## Rollout

After merge, restart the governed runtime, send one low-noise private canary,
verify its message id in the delivery ledger, and deliver one queued unique
EigenFlux item. Do not replay duplicate Guardian alerts. Keep user OAuth marked
degraded until its own read-only calendar canary succeeds.
