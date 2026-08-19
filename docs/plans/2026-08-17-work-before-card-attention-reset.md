# Work Before Card and Attention Reset

## Problem

Jarvis had 212 live Memorial rows but no outstanding decisions. Most were old
informational cards, including 114 cross-session ledger entries and repeated
exercise prompts. Routines could turn a model suggestion into a card before
Jarvis completed the available research. This converted internal work into
Pascal's attention debt.

## Product Decision

1. A proactive card is a receipt for completed preparation, not a forwarded
   to-do.
2. Jarvis completes all reversible, authorized work before asking.
3. Only an irreversible choice, missing authority, or real information gap is
   eligible for a decision card.
4. Every new visible card says what was already completed.
5. Informational cards leave the live queue after 24 hours. 留中 preserves the
   append-only history; cleanup never deletes the event.
6. Real calendar history is not cleanup material. Only proven duplicates or
   invalid future entries may be changed.

## Implementation

- `core.memorial` parses one top-level `WORKED:` directive per proactive card,
  stores it as `work_receipt`, and renders it before the body.
- `core.heartbeat_loop` enables the receipt requirement. Missing receipts
  return no payload and create no Memorial.
- Native card producers attach a structured receipt before adoption. Strict
  adoption rejects a missing receipt instead of manufacturing a generic one.
- Deterministic card producers pass explicit receipts describing their
  completed read, reconciliation, or validation step.
- `core.routines` requires `work_receipt` for `propose` and `act`. A missing
  receipt ends the run as `withheld`; no action executes and no card is sent.
- Notice escrow is 24 hours. Decisions retain their separate 48-hour review
  and four-day hard-lapse contracts.

## One-Time Production Cleanup

- A verified private backup was created before mutation.
- Real Lark calendar history and future events were read-only audited; no
  duplicate future events were found and none were deleted.
- 198 expired or recovered Memorials and 8 same-day noise rows were filed as
  留中 with explicit reasons.
- `起来动动`, `每日松身`, and `联调测试` were archived with run history intact.
- `力线周复盘` and `协议史精读` were paused until this gate is deployed, then may
  be resumed as completed-analysis Routines.
- Six recent external signals remain pending for deliberate processing: four
  EigenFlux messages, one mail item, and one feed item.

## Acceptance

- Missing `WORKED:` at the heartbeat boundary creates no ledger row and no
  Lark payload.
- A valid receipt is stored, rendered, and absent from the ordinary body.
- Every active native-card post-hook supplies its own structured receipt;
  legacy native cards without one are withheld at the proactive boundary.
- Quoted or fenced `WORKED:` examples cannot satisfy the gate.
- A Routine without `work_receipt` executes no action and sends no card.
- Every production `memorial.create` call declares `work_receipt`.
- Notice rows older than 24 hours are eligible for 留中.
- Existing old cards without receipts remain readable and tappable.
