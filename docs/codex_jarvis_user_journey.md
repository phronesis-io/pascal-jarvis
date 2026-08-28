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
  -> explicit owner handoff may prepare one verified empty Codex task
  -> first owner message continues the substantive Matter in Codex
```

## Which Path To Use

| Pascal's situation | Start here | Jarvis involvement | Why |
|---|---|---|---|
| Ask, translate, brainstorm, inspect a file, or finish one bounded outcome now | New or existing Codex task | None by default | Codex already owns the interaction and has enough context. |
| Work will cross a day, task, device, repo, or executor | Codex task | Search/create one Matter, acquire a run, release with a receipt | The outcome needs identity beyond one task. |
| Continue something discussed in Lark or Claude Code | New clean Codex task | Open the existing Matter and load its compiled Context Packet | Old transcripts are evidence, not the prompt. |
| Perform a consequential external action | Codex task | Matter + Delegation evidence | The UI can request approval; Jarvis verifies the effect and prevents false completion. |
| Change code, inspect a diff, review a PR, or watch CI | Codex plus native Git/GitHub | Link the commit, PR, or verified artifact only when the outcome must survive this task | Git owns version and review evidence; it is not a second Matter, inbox, or memory store. |
| Use a Feishu document, calendar, contact, group, or approval | Usually Codex with the native Lark tool | Add a Matter only if continuity or follow-up is needed | Native tools should remain native; Jarvis must not proxy everything. |
| Receive a deadline, outage, safety issue, or decision that cannot wait | Lark wake-up | Jarvis owns trigger, dedupe, attention budget, and Item state | This is interruption, not a long work surface. |
| Run recurring observation or organization while Pascal is absent | Jarvis Routine/Intent | Record evidence and remain quiet unless the threshold says to wake | This is the backstage's unique asynchronous value. |
| Ask what was accomplished or what to do next | Codex task | Read the bounded Matter result review | The answer uses confirmed outcomes and next actions, not Agent activity or raw history. |
| Diagnose the resident system | Admin `:3456` or Codex engineering task | Jarvis exposes component and receipt truth | Ops is not a daily personal workflow. |

## Normal Desktop Journey

1. Pascal opens the relevant project in Codex and starts one task for one
   outcome. A distinct outcome gets a new task.
2. Codex answers or works normally. It does not create durable state merely
   because a conversation exists.
3. When the work needs continuity, Codex uses one natural continuation call.
   One match proceeds; zero matches stays unbound; several matches produce one
   short clarification. The user never operates the underlying search/acquire
   protocol. Codex creates a Matter only after the durable need is clear.
4. The continuation call acquires the Matter. Jarvis returns a bounded, traceable Context
   Packet containing current consensus, decisions, pointers, authority, and
   next action. Raw chat histories are not dumped into the prompt.
   If a Codex task still carries a Session link to a terminal Matter, the
   continuation moves that link forward and records both sides of the move.
   It never steals a Session from another active Matter: that conflict stops
   before a run is created and the user starts a separate task or closes the
   earlier outcome.
5. Codex does the interactive work with its native tools and approvals. Jarvis
   stays out of the conversation unless asked for durable state or an effect.
6. Code work uses the repository's ordinary branch, commit, PR, review, and CI
   flow. Git/GitHub remains the authority for source history and collaborative
   review. Jarvis may retain a bounded `git`/`github` artifact link or verified
   file digest, but it never mirrors the repository into Matter state.
7. Codex releases the run with file hashes and authoritative effect evidence.
   Its narrative is stored as an unverified report, not completion truth.
8. Jarvis reconciles the receipt. Matter closure remains a separate owner-
   confirmed transition; a process exit or confident sentence cannot close
   it. Once Pascal explicitly confirms completion, one idempotent closure
   retires linked Intents, Items, and Handoffs before the Matter reaches done.
   Live runs, Jobs, and Delegations continue to block closure.

When Pascal refers to a decision from another product, Codex first searches
compiled memory. An active claim includes its exact source reference and may be
used without replaying the whole transcript. Assistant-authored claims remain
candidates. Contradictory facts disappear from ordinary context until Pascal
chooses one; claim review is never inferred from silence or model prose.

When Pascal asks “what did we finish?” or “what next?”, Codex reads
`jarvis_matter_review`. Confirmed closures, released work awaiting his decision,
blocked/waiting Matters, and bounded next actions remain separate. The weekly
Lark review is only a low-noise rendering of this same read model, not another
inbox and not a model-authored performance report.

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
   continuation link has been proven in production. An explicit `去 Codex`
   action may prepare and name a real empty task through Codex app-server, but
   the message still gives one stable phrase until mobile visibility is proven.
   Task preparation performs no model turn and takes no Matter lease; it never
   promises a broken deep link. The first real message presents the trusted
   wake ID through MCP, so the run records the real task rather than guessing
   from a title.

## When Pascal Talks Directly To Jarvis

Direct Jarvis conversation is an exception, not the main journey:

- quick capture while Codex is not open;
- an urgent/time-bound reply to a Jarvis wake-up;
- a native Lark action whose participants are already in that conversation;
- outage fallback when the Codex frontstage is unavailable.

When the private conversation is already bound to an active Matter, `去 Codex`,
`在 Codex 继续`, and `/matter handoff codex` use the same deterministic wake
path. A repeated action reuses the linked task only while it still has zero
turns. If creation or read-back is uncertain, Jarvis says it did not confirm a
task and returns the stable continuation phrase. Once work begins, the wake
receipt moves from prepared to running and then to the run's verified terminal
state.

Jarvis should convert durable substance into Matter state and let the next
clean Codex task continue it. It should not grow one permanent chat context.

## When Jarvis Talks First

Jarvis talks first only when the value comes from arriving without Pascal
having to remember to ask: a deadline, a material external change, an
explicitly retained companion rhythm, or the verified result of work he
already entrusted. It may also ask for judgment or authority after it has
finished every reversible step itself.

It stays quiet when it merely ran, retried, self-healed, found a health fact,
or produced material Pascal can retrieve later in Codex without loss. Such
work remains traceable in the ledger or the Matter review. Every proactive
Item declares its owner need, completed-work receipt, and why-now evidence;
missing evidence never falls through as raw prose. Silence never approves,
rejects, closes, or validates anything, and it never causes a second ask.

Recurring companion contact is explicit private configuration, not an inferred
preference. `jarvis.yaml retained_rhythms` defaults `checkin`, `daily_reflect`,
and `exercise_week` to false; only exact boolean `true` enables one, and more
than two enabled rhythms fails all three closed. Both pre- and post-hooks check
the subscription so a manual or stale model result cannot bypass it. User-
created Routines remain separate explicit requests. Time since Jarvis last
spoke is analytics, never a reason to speak.

## What Each Layer Owns

| Layer | Owns | Must not own |
|---|---|---|
| Codex | task/session UX, mobile Remote, tools, diffs, approvals, long results | durable Matter truth, provider policy, unverified completion |
| Jarvis | Matter/Item/Intent, memory compilation, time, attention, authority, evidence, reconciliation | cloned chat/editor/mobile UI, raw model transcript as truth |
| Git/GitHub | source history, branches, diffs, commits, PR review, CI and merge evidence | personal memory, Matter lifecycle, reminders, model policy |
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

The repository now implements this contract through `core.model_usage`:

- Codex desktop/mobile can call `jarvis_model_status`; owner Lark can use
  `/usage` or a direct natural-language quota question.
- The signed-in Codex app-server supplies exact used percentage and reset time.
- Claude CLI supplies account type only. MICU/relay/API routes remain unknown
  until they expose a supported, credential-free balance endpoint.
- Numeric observations are retained privately for 45 days. A forecast is
  shown only after two observations in the same reset window, at least five
  minutes apart, demonstrate positive consumption.
- An hourly model-free Tier-0 task refreshes the view. It interrupts only
  on a new `>=90%`, exhausted, predicted-before-reset, or real-request account
  limit episode; repetition is silent and recovery rearms the warning.

The runtime does not redeem reset credits, buy capacity, or open billing pages.
Those actions are consequential and remain explicit owner decisions.

Setup prepares the Codex surface: it installs MCP 2.x, registers the repo
marketplace, installs the current `jarvis-matters` plugin, and exercises the
stdio handshake. Governed release is read-only: it checks the installed plugin
and handshake but never installs packages or changes Codex configuration. A
degraded optional frontstage cannot block resident Jarvis recovery; it remains
an explicit health warning for setup to repair.

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
After an eligible released run, Codex may show one optional, one-line question:
`顺` records success; `找错事项 / 背景不对 / 没做完 / 有重复动作 / 需要重讲`
record exact failure dimensions. The prompt claim is durable, so ignoring it
never causes a second ask. The MCP accepts only Pascal's exact published label,
stores his original words, and cannot submit arbitrary scores or a reviewer
identity. Agent praise, silence, tests, and Result Receipts never count as
acceptance. The prompt records its connector version, so a delayed reply can
never qualify a later implementation. Operators retain the equivalent CLI for
audit and recovery; the read-only aggregate remains in
`jarvis_frontstage_health`.
