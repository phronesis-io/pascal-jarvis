# PRD — Card delivery closure: reachable surfaces, honest taps, durable approvals

> **Current status (2026-08-17): partially superseded.** Honest tap outcomes
> and durable approvals remain current. The web/phone desk routing below is a
> historical failure record: Lark is now the sole delivery and decision
> surface, and ambient exhaust is ledger-only. See
> [`prd_portfolio.md`](prd_portfolio.md) and
> [`prd_2026_08_11_signal_over_noise.md`](prd_2026_08_11_signal_over_noise.md).

Status: implemented 2026-08-03
Owner complaint (2026-08-03, verbatim intent): 飞书收到的消息很少很少了；卡片系统经常打不开；
有些选项点了以后也没有后续。

## 1. Reconstructed failures (from ledgers, not guesses)

### F1 — Lark went near-silent on 2026-07-24

Cards actually sent to Lark per day: 69 (7/22) → 50 (7/23) → **1 (7/24)**, and
1–7/day since. Cause: commit `1bf3eaa` (7/23 16:37) routed decisions to a
phone/web "desk" (`REVIEW_PHONE`) and notices to `web_only`, keeping Lark for
alerts, urgent, chat-bound, and Lark-native-action cards only.

The design is defensible **only if the desk is reachable**. It never was:

- `_has_paired_phone_subscription()` → **False**. No phone has ever paired.
  `phone_ready` cards notify nobody, nowhere.
- The desk URL is a LAN address / tailnet URL behind device auth **plus a
  custom CA** (`trust_required: true`). The owner reports he often cannot open
  it.

So the change rerouted ~20 cards/day from a chat the user reads to a surface
that does not ring and frequently does not open. This violates the standing
死路 rule (2026-07-29): a destination the user cannot reach is an availability
failure, not a polish item.

### F2 — Taps whose outcome was a lie or a shrug

All 42 consequential taps ever made were audited against upstream truth:

- `发（确认广播）` on 7/24 21:26 failed with
  `[Errno 2] No such file or directory: 'eigenflux'` — the card-callback
  process lacks `~/.local/bin` on PATH (the known launchd PATH gotcha). The
  result string promised 「内容仍保留待重试」 — **nothing retries approved
  drafts**; the file aged out into `expired/`. An explicit approval was
  silently lost.
- One 7/22 publish failed on a transient network error with the same
  未兑现的重试 promise.
- `Intent not found or already closed` was returned five times for
  做了/不用追了 taps — a no-op — while the toast said 「已批：✓」. The user
  was told success when nothing happened.
- 23 record-only taps (做了/还没做 with `action: None`) behaved as designed
  (ledger + next-conversation context injection), but since 7/24 the decided
  card lived on the unreachable desk, so nothing *visible* ever changed.

### F3 — Measurement trap (recorded for the next auditor)

A first pass claimed 42/42 taps had no follow-up. Wrong: ledger timestamps are
minute-granular, so a same-minute `action_result` was filtered out by a
`ts > tap_ts` comparison. The honest join is by id, not by time. 19 of 42 did
have results; the failures above are the real ones.

## 2. Contracts

### C1 — No card routes to an unreachable surface

`desk_reachable()` (a paired phone push subscription exists) gates the desk:

- A decision that would infer `REVIEW_PHONE` degrades to `REVIEW_LARK` when
  the desk is unreachable. Hard `REVIEW_LARK` reasons are unchanged.
- A notice pushes to Lark when the desk is unreachable — **except ambient
  sources** (`AMBIENT_SOURCES`: cross-session-sync, eigenflux-feed-triage,
  metrics-digest, phronesis-monitor, repos-sync), which stay web-first: they
  are monitoring exhaust, the morning escrow docket re-surfaces anything that
  needs a human, and pushing them would recreate the 7/22 card storm.
- Web storage is unchanged — the desk remains the durable archive either way.
- When a phone pairs, routing reverts to the 7/23 design with no code change.

### C2 — Every tap yields an honest, visible outcome

- The behavioural rule: **a no-op or failed action must not produce a success
  toast.** The toast says what actually happened; the decided card already
  carries the result line. Genuinely-successful taps keep the ✓.
- The current detection (`_ACTION_NOOP_RE` matching handler prose) is a
  **temporary bridge, not the contract** — handlers destroy the ok/no-op
  boolean into strings (`core/actions.py` `_do_intent_close`) and the regex
  re-derives it downstream. The right fix is a structured
  `ActionResult(status, message)` from `_execute_action`; the prose patterns
  must not be treated as canonical or grown.

### C3 — An explicit approval is durable until executed or explicitly dead

- The eigenflux binary is resolved absolutely (`shutil.which`, then
  `~/.local/bin/eigenflux`) — the callback process's PATH can no longer turn
  an approval into an Errno 2.
- A failed approved publish stamps the draft (`approved_epoch`, `attempts`,
  `last_error`) and stays pending.
- `reconcile_pending_drafts` (already runs on every eigenflux-publish cycle)
  **retries approved drafts deterministically** — no model in the loop — up to
  `APPROVED_MAX_ATTEMPTS`, with expiry measured from `approved_epoch` so an
  approval near the 48h line gets a full retry window.
- A draft that exhausts retries is moved to `expired/`, its card is lapsed
  with a reason naming the last error, and the failure is no longer silent.

## 3. Out of scope, said out loud

- The 17 drafts already in `expired/` (7/21–7/23 content) are stale for a
  broadcast network and are written off, reported to the owner — not
  auto-republished.
- Closing the record-only followup loop end-to-end (taps feeding intent
  state) is the intent-closure redesign's territory; this PRD only guarantees
  the tap's outcome is visible and honest.
- Fixing the desk's own reachability (pairing UX, CA trust) is separate work;
  this PRD stops pretending it is reachable before that lands.
