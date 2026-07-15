# PRD: Delivery Connectors — 输出信道对称化

Status: DRAFT — awaiting owner sign-off on scope (esp. §6 phasing)
Date: 2026-07-15

## 1. Problem

The input side is now pluggable (perception sources, `sources/README.md`),
but the OUTPUT side is welded to Lark: `core/heartbeat_loop.py` calls
`lark-cli` directly (`_lark_send_card` / `_lark_send_text`), the memorial
(奏折) system assumes Lark interactive-card JSON, and bot.sh's reply loop is
a Lark event consumer. For a multi-user product this is the single biggest
adoption blocker: a new install that doesn't use Lark gets a working brain
with no mouth.

## 2. Goals / non-goals

Goals:
- One `delivery` abstraction with per-install backend selection in
  `jarvis.yaml` (`delivery.backend: lark | telegram | slack | email | stdout`).
- The heartbeat/output pipeline talks ONLY to the abstraction; `lark-cli`
  never appears above the backend layer.
- Memorial cards degrade gracefully per backend capability (see §4) — the
  one-card-one-decision interaction survives, even where interactive cards
  don't exist.
- Zero behavior change for the existing Lark install (phase 1 is a pure
  extraction refactor gated by the full test suite).

Non-goals (this PRD):
- Inbound message loop portability (bot.sh conversation layer) — separate,
  bigger effort; heartbeat push output first.
- Multi-backend fan-out (send to Lark AND Telegram) — design leaves room,
  not implemented.

## 3. Design

New module `core/delivery/`:

```
core/delivery/__init__.py    # get_backend() from jarvis.yaml; registry by
                             # dynamic import, same pattern as sources/
core/delivery/base.py        # the contract (below)
core/delivery/lark.py        # extraction of today's _lark_send_card/_text
core/delivery/stdout.py      # trivial backend: print to log — CI / headless
```

Contract (mirrors the perception adapter philosophy: small,函数级, no ABC):

```python
CAPABILITIES: set[str]   # {"card", "buttons", "markdown", "text"}

def send_text(text: str, target: str) -> bool
def send_card(card: dict, target: str) -> bool
    # card = the NEUTRAL shape, not Lark JSON:
    # {header, body_md, buttons: [{text, action_id|url}], meta: {...}}
```

Key inversion: post-scripts and memorial stop emitting Lark JSON; they emit
the neutral card dict (a thin `core/card.py` shim keeps the current
`build_card()` signature and adapts, so post-scripts don't all change at
once). The backend renders neutral → native (Lark interactive card /
Telegram inline keyboard / Slack blocks / plain email).

The idle-sentinel gate and delivery ledger (`_note_delivery`,
`heartbeat_outbox.jsonl`, engagement log) live ABOVE the backend — they are
product behavior, not transport.

## 4. Memorial (奏折) capability degradation

| capability | rendering |
|---|---|
| card+buttons (Lark, Telegram, Slack) | native buttons for 批红 options |
| markdown only (email) | numbered options; reply "1"/"2" = 批红 |
| text only (SMS-like / stdout) | one-line summary + numbered options |

The memorial ledger keys on memorial_id, which is backend-agnostic already.

## 5. Config

```yaml
delivery:
  backend: lark          # default; missing key = lark (zero-migration)
  target: "<chat/user id in backend terms>"   # per-user, gitignored
  # backend-specific auth lives in secrets/, referenced here by path
```

`components.yaml` gains a feature-gated check per configured backend.

## 6. Phasing (each phase独立可回滚)

1. **Extract**: `core/delivery/lark.py` = move `_lark_send_card/_text`
   verbatim; heartbeat_loop calls through `get_backend()`. No neutral card
   yet — Lark JSON passes through. Tests: existing suite green + new
   backend-selection tests.
2. **Neutralize**: `build_card()` produces the neutral dict + Lark renderer;
   memorial renders through it. Golden tests: neutral→Lark output byte-equal
   to today's cards for the top card shapes.
3. **Second backend**: `stdout` (trivial, proves the seam) then `telegram`
   (first real one — bot API is the simplest full-featured target).
4. **Inbound loop** (separate PRD once 1-3 land).

## 7. Risks

- The Lark card JSON has accreted behavior (linkify, body cap, feedback
  note, button groups) — extraction must move it into the Lark RENDERER,
  not the neutral layer, or every backend inherits Lark quirks.
- Memorial retry queue stores rendered card_json today; after phase 2 it
  must store the NEUTRAL card (re-render on retry) or queued cards break on
  backend switch.
- bot.sh reply-context features ("Chat about this" seeding) are Lark-shaped;
  phase 1-3 keep them Lark-only, documented as such.

## 8. Effort

Phase 1 ≈ 1 day (mostly test surface). Phase 2 ≈ 2-3 days (golden tests).
Phase 3 stdout ≈ half day; telegram ≈ 2 days incl. auth + docs.
