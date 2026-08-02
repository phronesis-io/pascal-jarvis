# PRD — Checkin as a companion that learns from the interaction

Status: **implemented 2026-08-03** — `core/companion.py`, `tests/test_companion.py`
Date: 2026-08-02
Owner statement: *"the product is an idea, is that you can check in with me like an
old friend… a friend who cares about me, help me, remind me, guide me. The friend
can reorg and improve itself during my interaction."*

## 1. Why now

Checkin is not one of 36 heartbeat tasks. It is the product. It has been
**silent for 10 days** (last card 2026-07-23) while reporting perfect health:
`last_status: ok`, 708 total runs, "last success" the same evening this was
written.

That silence is not an accident. It is the designed behavior of the 7/21
rewrite, which says so in the prompt:

> If neither exists in the pre-script context: HEARTBEAT_OK. That is the
> EXPECTED outcome most of the time. Silence is the default, not failure.

## 2. What actually went wrong

The 7/21 rewrite was a correction to a real complaint — Pascal named 「乱联系」
four times. But it conflated two different things:

- contacting him **for no reason** (the real problem), and
- contacting him **without a task** (what got banned).

The gate now requires every card to carry `NEW INFORMATION or an ANSWERABLE ASK`,
and explicitly bans observations about his state. That is a correct rule for an
assistant and the wrong rule for a friend. A friend has no agenda item; they
noticed something and said it. The prompt's own banned example —

> 「台上那种绷」

— is lifted from one of the cards that best embodies the product idea.

### The deeper defect: the only feedback loop is a human rewriting the prompt

Pascal complains → a developer hand-edits `HEARTBEAT.md` → the pendulum
overshoots → 10 days of nothing → he complains again. Nothing measured whether
the correction was right. This is the exact failure `core/attention_roi.py` was
built to end for other sources, and `checkin` sits in its `PROTECTED_SOURCES`
exemption list, so it learns nothing.

### The instrument is too blunt to learn from anyway

Trailing ledger, all 23 checkin cards ever created:

| signal | count |
|---|---|
| card acknowledged (`已阅` / `知道了`) | 22 |
| card led to a real conversation | **3** |
| card lapsed unread | 1 |

Everything is read — checkin is not spam. But `知道了` is emitted both by "that
was good" and by "noted, go away", and nothing distinguishes them. The existing
ROI governor counts that tap as `engaged`, so even if checkin were governed, it
would be optimizing against a signal that cannot express displeasure.

**A user whose only channel for "stop doing that" is getting annoyed enough to
say it four times does not have a feedback loop. He has an escalation path.**

## 3. Design

Four mechanisms. (1) is a prerequisite for the rest and is the only one that
changes behavior on its own.

### 3.1 Give the ack a gradient (prerequisite)

Replace the single acknowledgment with a three-way that still costs one tap:

| option | meaning | signal |
|---|---|---|
| 「说说」 | wants to talk about it | strong positive |
| 「知道了」 | seen, fine, no reply needed | neutral |
| 「这类不必」 | this *kind* wasn't worth the interruption | negative |

The third option is the whole point: a cheap, in-band way to say 「乱联系」that
does not require Pascal to complain to a developer. It names the *kind*, not the
card, so one tap teaches the system something general.

### 3.2 Give every checkin a declared kind

Each card declares one:

- `followup` — an open thread of his with a concrete next step
- `standing` — a reminder he explicitly asked for (康复 etc.)
- `notice` — an observation about pattern, rhythm, or state (the friend voice)
- `guide` — a suggestion or nudge forward

The kind is stored on the card and is the unit of learning. Today the ledger
records only `source=checkin`, so all four collapse into one undifferentiated
blob and nothing can be learned about which register works.

### 3.3 Replace the binary gate with a per-kind budget, floor, and ceiling

**Deviation from this section, decided during implementation.** The plan was to
extend `core/attention_roi.py` to key on `(source, kind)` and drop `checkin`
from its `PROTECTED_SOURCES`. That turned out to be the wrong seam: that module
decides which *lane* a source occupies (decision vs notice), and checkin is
already a notice, so governing it there would only have added it to a
"noisy notices" report. The question here is a different one — how often one
source *speaks*, and in which register.

So cadence lives in `core/companion.py`, keyed on kind, and `attention_roi`
keeps owning lanes, keyed on attention class. They read different tables and
neither consults the other, which satisfies the same constraint the original
plan was reaching for (no second governor over the same decision) without
overloading one module with two unrelated policies. `checkin` stays in
`PROTECTED_SOURCES`: its lane should never be demoted by engagement.

What *is* reused is the reasoning, deliberately mirrored: the same
`WINDOW_DAYS`, a `MIN_SAMPLE` below which a rate is noise, and the rule that
demotion is a hypothesis rather than a sentence.

- Each kind holds a **daily allowance** that moves with its trailing score,
  within hard bounds. Scoring uses the 3.1 gradient: 「说说」 strongly positive,
  「这类不必」 strongly negative, 「知道了」 weakly positive (read, not resented).
- **Floor.** If nothing has been said for N waking hours, the
  highest-scoring kind gets one slot. *Silence stops being free.* This is the
  direct fix for the 10-day gap: the system must be able to notice its own
  muteness.
- **Ceiling.** A per-day cap so a healthy score cannot turn into noise.
- A kind whose score collapses decays toward its floor but **never to zero** —
  demotion is a hypothesis, and a kind at zero can never earn its way back.

### 3.4 Make silence a first-class, alarmable state

`HEARTBEAT_OK` from checkin must be recorded as a *decision not to speak*, with
its reason, rather than counted as an ordinary success. Ten days of declining to
speak is then visible as an anomaly instead of 708 green runs.

This is the same defect class as the batch-cap starvation fixed in #30: a
component that says nothing and calls it fine.

### 3.5 Learn from what he says, not only what he taps

When a card leads to a conversation, the post-hook records one line about what
he actually engaged with, attached to that kind. This is the richest available
signal and currently it is discarded entirely. This is the concrete meaning of
*"the friend can reorg and improve itself during my interaction"*: the
interaction is the training signal, not a later audit.

## 4. What is deliberately kept

Every ban that came from a real incident stays:

- never assert his location or activity from the calendar (7/17: told him he was
  at 世博展览馆 when he was not)
- no 「你好吗」/「最近怎么样」/status-check questions
- no health, habit, or productivity lecturing
- no re-touching a theme used recently, by meaning rather than string match
- under 60 words, Chinese, no response obligation

These are why it stopped being annoying. What is removed is only the
requirement that a card carry information or an ask, and the declaration that
silence is the expected default.

## 5. Open question for the owner

The evidence does not settle the taste question. The observational cards
(`notice`) are exactly the register the 7/21 rewrite blamed, and 已阅 taps cannot
tell us whether they landed or were dismissed politely. 3.1 exists precisely to
start collecting that evidence honestly.

**Proposed sequencing:** ship 3.1 + 3.2 + 3.4 first — instrument the signal,
label the kinds, make silence visible — and run for a week before letting 3.3
move any allowance. Do not let a governor act on a signal that has not been
validated.

**What was actually built (owner said "build all"):** all five, in one change.
The sequencing concern is handled by `MIN_SAMPLE` rather than by a delayed
release — no kind's allowance moves until it has 6 cards of evidence, so the
governor is inert for roughly the first week regardless. Every kind starts at
`ALLOWANCE_BASE`, and the floor guarantees none can be silenced before it has
been measured.

## 7. Verified behaviour

- Card renders the full gradient: 「知道了」/「这类不必」/「💬 聊聊这个」.
- `KIND` and `THEMES` contract lines are stripped from the body; the kind
  reaches the ledger via `__jarvis_context` and reads back.
- Loop closes in both directions with no prompt edit: 8×「这类不必」 drives
  `guide` to score −1.00 and allowance 1/day (never 0); 8× conversations drive
  it to +1.00 and 4/day.
- `HEARTBEAT_OK` writes a `silent` row with its reason instead of returning 0.
- `components.yaml → companion-voice` watches `data/companion_last_spoke`, so
  a mute checkin now goes red like any other dead component.

## 6. Non-goals

- Not a second scheduler or a second state machine over attention. Firing stays
  on the heartbeat; delivery stays in `core.delivery`; governance extends
  `core.attention_roi` rather than competing with it.
- Not a mood or sentiment model.
- No new network surface or external side effect.
