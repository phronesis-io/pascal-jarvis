# PRD: Cross-Device Continuity

- Date: 2026-07-24
- Status: Implemented
- Owner: Pascal
- Product principle: changing devices must move attention, not duplicate work.

## 1. Decision

Desktop and phone are complementary Jarvis work surfaces:

- phone owns discovery, quick decisions, short reading, and sending complex
  work back to the computer;
- desktop owns comparison, full context, artifact inspection, and launching
  Claude Code or Codex;
- Lark remains the conversation and immediate-attention surface;
- Memorial remains the one visible Item and Matter remains its optional durable
  topic.

Cross-device collaboration is represented by a **Handoff**. A Handoff points
to an existing Item or Matter and says where the next interaction should
continue. It never copies the object, forks its state, or creates a second
inbox.

## 2. Problems

The unified Items desk solved duplicate state, but not every continuity cost:

1. A phone Push could open the general Items list instead of the exact object.
2. A long Item was clipped in the list with no focused full-text route.
3. Pascal could decide that an Item needed a computer, but there was no durable
   way to leave it waiting for the desktop.
4. The desktop could not place one Item onto the phone without sending an
   improvised Lark message.
5. Normal phone screen lock, network switching, or PWA suspension could close a
   WebSocket and produce alarming stack traces even though no state was lost.
6. A decision completed on one surface could leave a stale “continue this”
   affordance on another surface.

## 3. North-Star Flow

### Phone to desktop

1. Pascal sees an Item on the phone.
2. If it is a quick decision, he decides it there.
3. If it needs comparison or execution, he taps `电脑继续`.
4. The desktop Items page shows one durable handoff at the top.
5. Opening it goes to the exact Item; completing the Item closes the handoff.

### Desktop to phone

1. Pascal finds an Item that is better handled away from the desk.
2. He taps `发到手机`.
3. Jarvis stores the handoff before attempting Push.
4. A successful Push opens `/items/{memorial_id}` directly.
5. If Push is unavailable, the handoff remains visible the next time the PWA
   opens. Notification failure never loses the work.

### Concurrent surfaces

1. The phone and desktop may both show the same Item.
2. The first decision appends the canonical Memorial event and executes its
   action.
3. A later tap receives the existing decision and cannot execute twice.
4. Every open or claimed handoff for that Item becomes completed.
5. Any delivered Lark card is updated to the same terminal state.

### Phone suspension and network change

1. iOS may suspend the PWA or replace its network route.
2. Either WebSocket direction may close first.
3. The gateway cancels the other relay direction and treats expected connection
   reset as a normal disconnect.
4. On reconnect, NiceGUI rehydrates from SQLite and the Memorial ledger.

## 4. Data Contract

`surface_handoffs` is additive SQLite state:

```text
id
entity_type          memorial | matter
entity_id
matter_id
from_surface         desktop | mobile
to_surface           desktop | mobile
status               open | claimed | completed | cancelled
title
note
created_by           local or paired device id
created_epoch
claimed_epoch
completed_epoch
delivery_id          optional Push DeliveryEnvelope
metadata
```

Only one active handoff may target the same entity and surface. Repeated taps
reuse it. The object itself remains authoritative; a handoff is navigation and
attention state only.

## 5. Product Rules

- A handoff must cross surfaces; desktop-to-desktop and mobile-to-mobile are
  rejected.
- Creating the durable row happens before Push.
- Handoff calls use one connection per operation and always close it. Active
  creation runs under `BEGIN IMMEDIATE` plus the unique active-handoff index,
  so concurrent desktop/mobile requests converge on one row.
- Push uses `core.delivery`, including audit, retry, model metadata, and exact
  URL payload.
- A missing Push subscription does not erase the handoff.
- Opening a focused Item marks its delivered copies read.
- Deciding or externally resolving an Item completes all of its active
  handoffs.
- Claiming is idempotent and does not lock the other surface out.
- The list view stays bounded; full body and context belong on the focused
  route.
- There is no automatic desktop app launch. The visible handoff preserves user
  agency and avoids stealing focus.

## 6. Surface Design

### Items list

- An unframed `跨端接力` band appears only when the current surface has active
  handoffs.
- Each Item has a focused-detail affordance.
- Desktop cards offer `发到手机`.
- Phone cards offer `电脑继续`.

### Item detail

- Stable route: `/items/{memorial_id}`.
- Full title, full body, topic, timer, review surface, source links, and current
  decision state.
- Decision buttons operate on the same Memorial.
- `去飞书聊` continues the same context.
- One cross-device command moves the next interaction without copying state.

### Matter detail

- The same command moves a longer-running Matter to the other device.
- A phone Push opens `/matters/{matter_id}` directly.
- Moving the Matter to `done` or `archived` completes its active handoffs.

### Mobile navigation

- The bottom dock has exactly three equal tracks for its three destinations.
- Safe-area padding remains intact.
- No empty fourth column or dead thumb target.

## 7. API

- `GET /api/handoffs?target_surface=&status=`
- `POST /api/handoffs`
- `POST /api/handoffs/{id}/claim`
- `POST /api/handoffs/{id}/complete`

All writes use the existing JSON/origin guard. The mobile gateway supplies the
paired device identity through `X-Jarvis-Device`; local desktop requests use
`local`. The API derives `from_surface` from that trusted request identity and
rejects a conflicting value in the JSON body. Claim and dismiss operations are
also restricted to the authenticated target surface.

## 8. Failure Semantics

| Failure | User-visible result |
|---|---|
| Push subscriber missing/offline | handoff remains on phone desk |
| duplicate handoff tap | existing active handoff is reused |
| other surface already decided | existing decision is shown; no action rerun |
| Item no longer exists | API/detail route returns a clear not-found state |
| phone WebSocket resets | quiet reconnect; no daemon-style error card |
| Lark card update fails | canonical web state remains; error is logged |

## 9. Metrics

- handoffs created by direction;
- median time from handoff creation to claim and completion;
- active handoffs older than 24 hours;
- Push delivery success for desktop-to-phone handoffs;
- duplicate handoff suppression;
- expected versus unexpected mobile WebSocket disconnects;
- Item detail opens leading to read or acted state.

## 10. Acceptance

1. Repeated handoff creation is idempotent.
2. The target surface lists open and claimed handoffs newest first.
3. A desktop-to-phone handoff records one Push envelope with the exact Item or
   Matter URL.
4. A focused Item marks its deliveries read.
5. A decision or terminal Matter update completes every active handoff for its
   canonical object.
6. Web and Lark decisions remain idempotent and converge on one card state.
7. Expected WebSocket resets do not escape as request errors.
8. `/items` and `/items/{id}` render without horizontal overflow at 390px and
   desktop widths.
9. The mobile dock has three tracks for three destinations.
10. The full repository test suite and deploy verification pass.

## 11. Non-Goals

- synchronizing raw Claude/Codex transcripts to the phone;
- building an embedded web chat;
- launching desktop applications without an explicit user action;
- caching private Item API payloads for offline browsing;
- creating separate phone and desktop copies of an Item.
