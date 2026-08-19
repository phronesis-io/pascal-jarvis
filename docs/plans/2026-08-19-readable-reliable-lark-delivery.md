# Readable, Reliable Lark Delivery

## Problem

Jarvis deliberately caps a Memorial card at 900 characters and 14 lines so a
phone is not buried under walls of text. The full body remains in the ledger,
but the only visible affordance was `聊聊这个`; very long bodies then required
repeated manual `继续发` replies. The data path was recoverable, yet the reading
path was hidden and laborious.

The same release also closes two adjacent reliability gaps already reviewed in
draft PRs: EigenFlux private messages need polling reconciliation beside the
WebSocket, and proactive cards need evidence that Jarvis completed useful work
before asking for attention.

## Product Contract

1. A clipped Memorial says that more text exists and displays `查看全文`.
2. One authenticated owner tap sends every remaining chunk in the current Lark
   conversation. No additional reply is required.
3. Each chunk has a stable delivery dedup key and advances the continuation
   offset only after confirmed delivery.
4. An interruption leaves the offset unchanged. The card reports the
   interruption, and the next tap resumes at that offset.
5. Decisions, `聊聊这个`, and `看不懂` remain available while or after the full
   text is sent.
6. The legacy `继续发` command remains as a fallback for old cards and partial
   deployments.
7. Proactive model output without a concrete work receipt is withheld.
8. EigenFlux treats WebSocket as the low-latency path and a five-minute poll +
   cache reconciliation as the no-loss path, deduplicated by canonical message
   receipt.

## Engineering Boundaries

- `core.memorial` owns clipping, continuation offsets, and card rendering;
  `core.memorial_reader` owns the bounded background transfer and chunk
  receipts behind the stable `memorial.read_full` facade.
- `scripts/lark_event_sidecar.py` authenticates the operator and only dispatches
  the framework action.
- `core.delivery` remains the sole transport authority; full-text chunks do not
  bypass its retry, dedup, audit, or provider/model receipts.
- The dashboard and retired mobile gateway receive no new product behavior.

## Acceptance

- Short cards do not show `查看全文`.
- Clipped cards show it as the primary reading action.
- A long body reaches its final paragraph after one tap.
- Automatic chunks never ask the user to type `继续发`.
- A failed chunk is retried from the last confirmed offset without repeating
  earlier content.
- Duplicate callbacks do not create parallel send jobs.
- Owner authentication is still required for the callback.
- EigenFlux ingress, work-receipt, Memorial, sidecar, delivery, safety, and full
  repository tests pass before release.
