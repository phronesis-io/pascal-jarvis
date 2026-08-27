# Codex Frontstage, Jarvis Backstage

- Status: current product and integration contract
- Adopted: 2026-08-27
- Applies to: Codex desktop/mobile, Jarvis, Lark, Claude Code, model runtime

## The Product In One Sentence

Pascal starts and steers substantive work in Codex; Jarvis quietly preserves
what must survive the current task: Matter identity, settled decisions, time,
authority, evidence, and the exact next action.

Codex is not a provider hidden behind Jarvis. Jarvis is not a chat product
competing with Codex. A model is not either product.

```text
Pascal
  -> Codex task (interactive work, tools, review, approval)
       -> Jarvis Matter (durable continuity and verified closure)
            -> Lark/calendar/EigenFlux/native effects when needed
       -> Model Runtime (replaceable model/provider route)

Jarvis time/signal trigger
  -> ledger first
  -> bounded Lark wake-up only when time or judgment cannot wait
  -> continue the substantive Matter in Codex
```

## Which Path To Use

| Pascal's situation | Start here | Jarvis involvement | Why |
|---|---|---|---|
| Ask, translate, brainstorm, inspect a file, or finish one bounded outcome now | New or existing Codex task | None by default | Codex already owns the interaction and has enough context. |
| Work will cross a day, task, device, repo, or executor | Codex task | Search/create one Matter, acquire a run, release with a receipt | The outcome needs identity beyond one task. |
| Continue something discussed in Lark or Claude Code | New clean Codex task | Open the existing Matter and load its compiled Context Packet | Old transcripts are evidence, not the prompt. |
| Perform a consequential external action | Codex task | Matter + Delegation evidence | The UI can request approval; Jarvis verifies the effect and prevents false completion. |
| Use a Feishu document, calendar, contact, group, or approval | Usually Codex with the native Lark tool | Add a Matter only if continuity or follow-up is needed | Native tools should remain native; Jarvis must not proxy everything. |
| Receive a deadline, outage, safety issue, or decision that cannot wait | Lark wake-up | Jarvis owns trigger, dedupe, attention budget, and Item state | This is interruption, not a long work surface. |
| Run recurring observation or organization while Pascal is absent | Jarvis Routine/Intent | Record evidence and remain quiet unless the threshold says to wake | This is the backstage's unique asynchronous value. |
| Diagnose the resident system | Admin `:3456` or Codex engineering task | Jarvis exposes component and receipt truth | Ops is not a daily personal workflow. |

## Normal Desktop Journey

1. Pascal opens the relevant project in Codex and starts one task for one
   outcome. A distinct outcome gets a new task.
2. Codex answers or works normally. It does not create durable state merely
   because a conversation exists.
3. When the work needs continuity, Codex searches Jarvis Matters. It reuses an
   existing recognizable outcome or creates one only after the durable need is
   clear.
4. Codex acquires the Matter. Jarvis returns a bounded, traceable Context
   Packet containing current consensus, decisions, pointers, authority, and
   next action. Raw chat histories are not dumped into the prompt.
5. Codex does the interactive work with its native tools and approvals. Jarvis
   stays out of the conversation unless asked for durable state or an effect.
6. Codex releases the run with file hashes and authoritative effect evidence.
   Its narrative is stored as an unverified report, not completion truth.
7. Jarvis reconciles the receipt. Matter closure remains a separate verified
   transition; a process exit or confident sentence cannot close it.

When Pascal refers to a decision from another product, Codex first searches
compiled memory. An active claim includes its exact source reference and may be
used without replaying the whole transcript. Assistant-authored claims remain
candidates. Contradictory facts disappear from ordinary context until Pascal
chooses one; claim review is never inferred from silence or model prose.

## Normal Mobile Journey

1. Pascal uses Codex mobile/Remote as the primary work surface. The Mac must be
   online for computer-backed work; Jarvis does not restore Tailscale, pairing
   codes, `:3458`, or a private mobile web app.
2. For a distinct outcome, Pascal starts a clean task. For an existing outcome,
   Codex binds the task to the same Matter and receives the same provider-
   neutral Context Packet as desktop.
3. Long analysis, diffs, artifacts, approvals, and test evidence stay in
   Codex. Lark does not receive a clipped duplicate.
4. A Lark wake-up remains independently understandable until an exact Codex
   continuation link has been proven in production. It names the result or
   decision and the Matter; it never promises a broken deep link.

## When Pascal Talks Directly To Jarvis

Direct Jarvis conversation is an exception, not the main journey:

- quick capture while Codex is not open;
- an urgent/time-bound reply to a Jarvis wake-up;
- a native Lark action whose participants are already in that conversation;
- outage fallback when the Codex frontstage is unavailable.

Jarvis should convert durable substance into Matter state and let the next
clean Codex task continue it. It should not grow one permanent chat context.

## What Each Layer Owns

| Layer | Owns | Must not own |
|---|---|---|
| Codex | task/session UX, mobile Remote, tools, diffs, approvals, long results | durable Matter truth, provider policy, unverified completion |
| Jarvis | Matter/Item/Intent, memory compilation, time, attention, authority, evidence, reconciliation | cloned chat/editor/mobile UI, raw model transcript as truth |
| Lark | bounded wake-up and native communication/calendar/document workflows | long analysis, second task inbox, duplicate Matter state |
| Model Runtime | capability/trust/cost/health-based route and observed model | product state, permission, closure semantics |
| Claude Code | optional execution session for tasks where it is useful | product continuity or exclusive model ownership |

## Model And Package Usage

Pascal should not need to open several provider billing pages. The Model
Runtime is the single read surface for the route that actually answered,
available quota or spend signals, reset time, recent throttling, predicted
exhaustion, and the fallback route currently available. Codex desktop/mobile
is the normal query surface. Lark receives only a bounded warning when a route
is likely to run out soon, has been rate-limited, or no healthy fallback
remains. Unknown provider data must be labeled unknown; a small canary, a
configured token, or a successful login is not proof of usable production
quota.

## Replacement And Retirement Rules

- Use Codex native tasks, Remote, `/goal`, scheduling, approvals, tools, and
  local memory when they solve the local interaction well.
- Keep Jarvis only where it adds cross-product authority, offline/asynchronous
  value, multi-executor continuity, attention policy, or verified closure.
- Retire a duplicate only after its replacement has real desktop and mobile
  evidence for discovery, continuation, action, and closure.
- Never migrate by changing a document alone. Until acceptance passes, the old
  reachable path remains.

## Progress Standard

The north-star is **useful closed Matters per 10 minutes of Pascal attention**.
Guardrails:

- median re-explanation needed to resume a Matter;
- false-completion rate and unverified external-effect claims;
- duplicate Matter and duplicate decision rate;
- Lark interruptions per day and repeated asks per Matter;
- lease residue, recovery count, and terminal runs without receipts;
- desktop/mobile continuation success;
- time from accepted decision to verified effect;
- percentage of Jarvis messages that contain completed useful work.

Phase-1 acceptance requires 20 real desktop and 20 real mobile continuations.
For each sample, record Matter discovery, packet correctness, task completion,
receipt validity, duplicate side effects, and whether Pascal had to re-explain
settled context. Only then may the corresponding Lark interaction be reduced.
The reviewer records these observations with
`python3 -m core.frontstage_acceptance record`; the read-only report is
included in `jarvis_frontstage_health`. The MCP surface intentionally exposes
no tool that can self-approve a sample.
