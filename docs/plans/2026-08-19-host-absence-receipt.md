# Host Absence: Honest Measurement and a Receipt

Date: 2026-08-19
Status: implemented, awaiting governed review/release
Scope: reliability and honesty only; the host's sleep behaviour is not changed

## Incident Evidence

Between 08-17 21:12 and 08-19 13:02 the MacBook sat closed on battery. The
production ledger for those 39 hours:

| signal | before | during |
| --- | --- | --- |
| `Beat sent` per hour | 29 | 3 in 39 hours |
| memorial `create` per day | 76-78 | 2 |
| `task_spawn` per day | 386-505 | 11, then 9 |
| intents fired per day | 44-64 | 0, then 2 |

One heartbeat cycle spanned 12 hours because a DarkWake window advances it by
seconds. `core.presence` — the sentinel written for exactly this shape — was
over its floor and stayed unread, because the process that would have raised
it was asleep with everything else.

Nothing was broken. The host was not there, and:

1. `daemon.py` detected it **38 times**, correctly, totalling 39.4h. Every
   observation ended in `post-wake grace, NOT restarting` or `would alert but
   in post-wake grace`. Both are right about restarts and component pages and
   neither says the system was gone.
2. `core.heartbeat_loop` measured the overshoot of its own 10s nap only, so
   sleep that happened during a model call was invisible: it booked 0.7h of
   the same 39h into `sched_events`.
3. The single alert that did reach Pascal (08-18 02:16) blamed ef-stream for
   being "not running". The process was alive the whole time; it was the only
   staleness check without a post-wake grace, so it took the blame for the
   host.
4. Two intents expired unfired, including the 08-18 16:00 meeting prep, and
   the owner learned all of this by requesting an audit two days later.

`caffeinate -s` is asserted by a launchd job and holds only on AC power (its
own man page); clamshell sleep ignores assertions entirely. `components.yaml`
already documents that as deliberate (REQ-56, battery policy untouched). It is
not a defect to repair — it is an exposure nobody was told about.

## Relationship to PR #89

PR #89 landed on the same incident hours earlier, from an unattended
self-improve round. It is kept, with three deliberate replacements:

- Its `_post_wake_grace` is a bounded *excuse* and its own author documented
  the hole: on a laptop that naps hourly the window re-arms almost
  continuously, so a component that is genuinely wedged can hide behind a hold
  that never lapses. Recorded sleep answers the same question quantitatively,
  and where an episode exists the timer no longer overrides it. Grace stays as
  the fallback for windows with no recorded sleep.
- Its `_sleep_gap_seconds` arithmetic moved into `core.hostclock.gap_from`;
  the loop keeps its call site and its tests. One meter, two call sites.
- Its morning-anchor absence footer is removed. Absence now has exactly one
  surface — the receipt on the wake, which arrives while the gap is still the
  thing on his mind and names what it cost, instead of a vaguer line the next
  morning. `core.presence`'s sentinel keeps its fix: a sleeping host is no
  longer read as a broken delivery chain.

## Product Contract

1. Host sleep is measured wherever it happens in a tick, including inside a
   600s model call, and a long model call is never mislabeled as sleep.
2. A staleness check ages against time the host was actually up. A component
   that could not run because the machine was off is not reported as stale.
3. Sleeping is not a fault and never pages. An overnight lid-close produces no
   card.
4. Absence overlapping the owner's active hours (the shared non-quiet window)
   by more than three hours produces exactly one notice card on the next
   confirmed wake — one per episode, however many DarkWake windows fragmented
   it.
5. That card is a receipt, not an alarm: it states the span, what the absence
   actually cost from the scheduler's own ledger, that a closed lid on battery
   is not a malfunction, and that nothing is required of the reader.
6. Reporting waits for a confirmed wake. A card emitted inside a DarkWake
   window would understate the absence and queue behind the next sleep.
7. No mechanism here claims to keep the host awake, and none reports an
   absence while it is happening. Only the external dead-man can do that.

## Boundaries

- `core.hostclock` owns the measurement (wall-vs-monotonic drift) and the
  recorded episode log. The Guardian daemon is its only writer.
- `core.absence` owns episode aggregation, the reporting policy, and the card
  text. It reads the scheduler's ledger; it does not ask a model what happened.
- `daemon.py` observes each tick and emits. Its existing wake grace, restart
  budget, and brain-health suppression are untouched.
- `core.components` consumes awake-age; it does not measure sleep itself.
- `core.attention_policy` remains the single definition of the owner's active
  hours.

## Verification

- Meter tests prove a 600s model call is not sleep, that sleep *inside* such a
  call is caught, that sub-threshold jitter is ignored, and that a monotonic
  restart does not add the previous uptime to the absence.
- Absence tests prove an overnight lid-close is silent, a working day is one
  card, twelve DarkWake-fragmented gaps stay one episode, a confirmed wake
  splits episodes, nothing is emitted before the wake is confirmed, and the
  card names the expired intents.
- Component tests prove a stamp older than its limit passes when the host slept
  through the window and still fails when the host was up.
- Daemon tests prove the episode is recorded and reported exactly once, and
  that a failure in this path cannot take down the watchdog loop.
- Full repository validation: 3178 passed (including PR #89's suites).

## Configuration-Dependent Remainder

Unchanged from 2026-08-17 and now demonstrated: `ops.deadman` is still
disabled, and it is the only mechanism that could have told Pascal *during*
the 39 hours rather than after them. Code cannot create the external
monitoring account or invent its ping URL.
