# EigenFlux Ingress Recovery

## Problem

Pascal reported that EigenFlux had gone quiet. The account and API were
healthy, but the product contract was not:

- Private-message dedup read `item_id`; the native stream emits `msg_id`.
- The scheduled polling supplement had been quarantined, leaving WebSocket as
  a single ingress path.
- Eighteen EigenFlux-related Lark envelopes reached terminal failure during the
  Keychain incident. Dead-letter alerts were sent, but recovery never replayed
  eligible messages after the bot transport recovered.
- `ef-stream` counted a long-lived `connecting` process as healthy without a
  protocol or polling receipt.
- Feed visibility stamps advanced before Lark delivery, so failed cards spent
  the daily three-card budget and hid later useful signals.
- One auxiliary analysis included provider tool-trace text in a user card.

## Product Contract

1. WebSocket provides low latency; a five-minute deterministic poll and CLI
   cache scan provide no-loss reconciliation.
2. Both paths share `msg_id`, one cross-process ingestion lock, one seen set,
   and the same Memorial delivery boundary.
3. A private message is receipted only after durable local acceptance. Private
   messages bypass proactive-news interruption caps; feed items do not.
4. Automatic terminal recovery is fail-closed: only a known definitive
   no-send error is eligible, at most two old cards per cycle, with cooldown
   and retry ceilings. Lapsed, resolved, read, or acted cards never reopen.
5. Component health requires a recent successful polling receipt after startup
   grace. Quiet WebSocket process existence alone is not green.
6. Feed cooldown and daily count come from unified-delivery states. Terminal
   failures do not consume visibility.
7. Tool/provider traces are rejected from message analysis; the raw external
   message still reaches Pascal.

## Acceptance

- Native `msg_id` stream packet deduplicates across stream and poll.
- CLI cache can recover a message even when unread fetch returns empty.
- Known Keychain no-send terminal failure is redelivered after transport
  recovery; a closed Memorial is not.
- Poll failure creates degraded health without fabricating success.
- `connecting` is unhealthy after grace unless the polling path is fresh.
- Failed feed envelope does not spend cooldown or daily budget; delivered or
  retryable work does.
- No model tool trace reaches a Memorial body.
