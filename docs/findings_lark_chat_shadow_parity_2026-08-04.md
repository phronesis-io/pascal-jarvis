# lark_chat shadow parity — verdict, evidence, and two real defects

Date: 2026-08-04 (night deep work)
Scope: perception source `phronesis` (type `lark_chat`) vs the legacy
`phronesis-monitor` heartbeat task. Read-only investigation; **no code changed**.

---

## 0. First: my acceptance criterion was self-contradictory, and that is why
   I "could not find where the shadow lands" two nights in a row

The criterion I wrote into `open_threads` was:

> 判据 = `inbox_team.md` 里出现由 lark_chat 路径（**非 phronesis**）采到的团队消息

That criterion can never be satisfied, because **`phronesis` is the source *id*
and `lark_chat` is that source's *type*** (`sources.yaml`):

```yaml
    - id: phronesis
      type: lark_chat
      label: "Phronesis 团队群 (shadow until T2 parity)"
      perceive: { buffer: inbox_team.md }
```

`core/perception.py::_write_inbox` writes the header as
`### <event_id> | <source_id> | <who> | <ts> | <sensitivity> | buffer`.
So **every `| phronesis |` line in `inbox_team.md` IS the lark_chat shadow's
output.** It has been in my heartbeat context on every single beat. The word
"phronesis" names two different things — the legacy monitor task and the shadow
source id — and I read the id as "the old path".

Landing path, for the record:
- buffer: `memory/system/inbox_team.md`
- ledgers: `memory/system/perception_seen.jsonl` (action=deliver/dedup),
  `memory/system/perception_delivery.jsonl` (action=buffer)
- cursor: `perception_state.json` → `phronesis.adapter_state.last_ts`

## 1. Window start was also wrong

I recorded "观察窗从 8/3 10:26 重新起算". The `%z` fix actually landed
**2026-07-29 00:27** (`bd8c6de fix(perception): a source can fail every pass for
13 days and look enabled`). Shadow deliveries per day since:

| day | buffered |
|---|---|
| 07-29 | 54 |
| 07-30 | 56 |
| 07-31 | 22 |
| 08-01 | 0 |
| 08-02 | 0 |
| 08-03 | 11 |
| 08-04 | 6 |

The two zero days are **verified real, not a silent failure**: a direct
`lark-cli im +chat-messages-list` over 08-01 and 08-02 returns `n=0` — the team
group had no messages that weekend.

## 2. Capture parity: 17/17, zero misses

Independent read of the group via lark-cli for 08-03 (11 messages) and 08-04
(6 messages), then each `message_id` looked up in `perception_seen.jsonl`:
**every one is present with `action=deliver`.** No drops, no dedup false-positives.

The shadow is also strictly wider than the legacy monitor:
`tasks/phronesis_monitor_pre.sh` exits early outside 09:00–22:59; the shadow
runs 24/7 at a 10m interval.

## 3. ⚠️ But two defects make the shadow's output wrong in the fields I quote

### Defect A — every timestamp is *collection* time, not *send* time

`lark-cli` renders `create_time` as a **local formatted string**, not epoch ms:

```json
{"create_time": "2026-08-04 17:03", "message_id": "om_x100b682ee2b...", ...}
```

`sources/lark_chat.py`:

```python
        try:
            ts = float(msg.get("create_time", 0)) / 1000.0 or now
        except (TypeError, ValueError):
            ts = now          # ← taken on EVERY message, always
```

`float("2026-08-04 17:03")` raises `ValueError`, so `ts = now` on every single
signal. Measured drift:

| day | real send time | inbox_team.md stamp | drift |
|---|---|---|---|
| 08-03 | 11:59 → 18:37 | 12:07 … 18:43 | +2 … +14 min |
| 08-04 | 16:02, 16:03, 16:03, 16:04, 16:15, 17:03 | **all 18:27:03** | +84 … +145 min |

Three consequences, in descending severity:
1. **The team inbox lies about when things were said, and that is the field I
   quote back to Pascal.** In tonight's hourly note I cited a teammate's
   message (paraphrased here: a suggestion about the friend-request flow) as
   sent at 18:27; it was actually sent at **16:02**.
2. **After any downtime the drift is unbounded** — a backlog collected on
   recovery gets stamped with the recovery moment. Today's +145 min is the mild
   version; a multi-day outage would stamp days-old messages as "now".
3. **It silently disables the page_full cursor guard.** The code comment above
   `next_cursor` says advancing unconditionally to `end_iso` "silently and
   permanently dropped everything past the first page in a busy chat" — but with
   `ts = now`, `last_msg_iso` *is* ~`end_iso`, so that guard is dead code today.

### Defect B — the sender's name is thrown away

lark-cli returns `sender.name` (e.g. `"同事C"` — synthetic placeholder, real
names stay out of the repo), the adapter keeps only the open_id:

```python
            "actor": {"raw": sender, "resolved": ""},
```

`_write_inbox` uses `resolved or raw`, so `inbox_team.md` shows a bare
`ou_…` open_id instead of the sender's name. `warm/team.md` does not
map every id, so summarizing the team group means guessing who spoke — a direct
hallucination surface (REQ-78 class).

### Defect C (minor) — plain-text messages truncated at 500 chars

`_extract_text` falls through to `return str(raw)[:500]` for plain text (the
common case here — content is a bare string, not JSON), while the signal schema
allows `body[:2048]`. Long team messages lose their tail before the buffer ever
sees them.

## 4. Proposed patch (~15 lines, one file, NOT applied)

```python
def _parse_create_time(raw, fallback: float) -> float:
    """lark-cli renders create_time as a LOCAL 'YYYY-MM-DD HH:MM' string, not
    epoch ms. float() on it raised ValueError, so every signal silently took
    the `now` fallback and the team inbox recorded collection time as send
    time (2026-08-04: six messages sent 16:02-17:03 all stamped 18:27)."""
    if raw is None:
        return fallback
    s = str(raw).strip()
    if s.isdigit():
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return fallback
```

- A: `ts = _parse_create_time(msg.get("create_time"), now)` (replaces the
  try/except). Minute-precision cursors may re-fetch the boundary minute; the
  seen-store already dedups that.
- B: `sender_obj = msg.get("sender") or {}` … `"actor": {"raw": sender,
  "resolved": sender_obj.get("name", "")}`.
- C: `return str(raw)` in `_extract_text`'s fallback (callers already slice).

Tests to add: a fixture message with `create_time="2026-08-04 17:03"` asserting
the signal ts is 17:03 (not now) and `actor.resolved == "同事C"` (synthetic
fixture name).

## 5. Retirement question: **do NOT retire `phronesis-monitor` yet**

sources.yaml's own plan says wave 2 = "通用 chat-digest 任务（必须带跨周期
flagged 记忆）" then retire the monitor. **That task does not exist** — no
`chat-digest` in `HEARTBEAT.md`, no `tasks/chat_digest_*`.

What is proven tonight is capture parity only. The monitor still owns the
*judgment + delivery* leg (LLM decides whether Pascal should know, with 24h
cross-cycle flagged memory). Retiring it now would leave team messages sitting
in a buffer with nothing deciding whether any of them needs him.

## 6. Honest gaps

- The six 08-04 messages existed at the 16:56 perception-collect run but were
  only buffered at 18:27. **I did not establish why.** Most likely the lark-cli
  call failed right after wake-from-sleep (the day had six Feishu disconnects),
  and the cursor correctly did not advance — zero loss either way. Not asserted
  as fact.
- Parity was checked on 08-03/08-04 (17 messages). 07-29 → 07-31 (132 messages)
  was not re-verified against the API; those days are outside lark-cli's
  practical re-fetch window for this check.
