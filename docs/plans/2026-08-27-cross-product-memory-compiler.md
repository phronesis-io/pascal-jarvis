# Cross-Product Memory Compiler

- Status: implemented in repository; release and live replay pending
- Scope: Codex, Claude Code, owner-private Lark, Matters, Context Packets
- Supersedes: rolling cross-session digest as a prompt-memory mechanism

## Outcome

Pascal can start a clean Codex task without re-explaining a settled decision.
Jarvis remembers the smallest still-valid claim, where it came from, which
Matter it belongs to, and whether a newer statement superseded or contradicted
it. It does not remember a model's confidence as truth.

## Source And Authority

| Source | Can be indexed | Can become active automatically | Completion authority |
|---|---:|---:|---:|
| Owner turn in interactive Codex/Claude Code | yes, redacted | yes, as owner-asserted memory | no |
| Owner-private Lark turn explicitly marked eligible | yes, redacted | yes, as owner-asserted memory | no |
| Assistant turn | yes, redacted | no; candidate only | no |
| Group/non-owner Lark turn | no | no | no |
| Matter/Delegation authoritative state | referenced separately | already authoritative in its domain | only its domain verifier |

Raw provider transcripts remain the audit source. Applied compiler batches
erase their temporary payload. Durable rows retain source digest, exact bounded
quote, source reference, claim lifecycle, and Matter scope.

## Compile Protocol

1. `core.cross_session_index` indexes visible redacted turns from owner-operated
   Codex and Claude Code sessions.
2. `core.matter_bridge.record_turn` marks only verified owner-private Lark
   turns as memory eligible. Existing ambiguous rows remain ineligible.
3. `core.memory_compiler prepare` creates one replayable batch. Codex/Claude and
   Lark each receive a fair quota so one busy source cannot starve the other.
4. The heartbeat model may extract at most three claims per source. Every claim
   needs an exact quote, stable key, supported kind, and the source's existing
   Matter ID. Every source must be claimed or explicitly ignored.
5. `core.memory_compiler apply` validates the envelope and atomically records a
   receipt. Invalid or incomplete output leaves the batch pending for retry.
6. A self-contained user-authored claim becomes active. Context-dependent
   acknowledgements such as "搞吧", "写进 blog 吧", or "go ahead" are marked
   `owner_context_candidate`: an exact quote proves the words but cannot prove
   the model-expanded referent from a neighboring turn. Assistant-authored
   claims also remain candidates. Concrete directives such as "发布 PR #130 吧"
   remain owner assertions because their object is present. Core re-evaluates
   both the complete source turn and the selected quote: questions are never
   assertions, a question cannot become a fact by dropping its question mark,
   and quoting only "好的" from a longer sentence cannot borrow the rest of
   that sentence's authority.
   Decisions, preferences, and todos supersede older owner statements with the
   same key. Conflicting facts, constraints, or artifacts suspend both values
   until explicit human review.

## Recall Contract

- Named Matter Context Packets contain only active claims attached to that
  exact Matter, with claim IDs and source references.
- Unbound owner chat receives query-relevant active claims only.
- Raw turns, assistant candidates, superseded claims, rejected claims, and both
  sides of an open conflict are excluded from default prompts.
- Explicit Codex tools can search compiled claims and review a candidate or
  conflict. Explicit audit search of raw redacted history remains available via
  `python3 -m core.cross_session search`.
- Group and non-owner prompts receive neither compiled private memory nor raw
  transcript search results.

## Attention Policy

Ordinary compilation is silent. A deterministic ambient item is created only
when a new contradiction is found; even then the conflicting values stay out
of prompts. The old model-authored `user_message`, PR-status prose filters,
sent-cache, and rolling cross-session digest injection are retired.

## Acceptance

1. Codex, Claude Code, and eligible owner-Lark fixtures reach one compiled
   store; group Lark fixtures never enter a batch.
2. Exact-quote forgery, omitted sources, inferred Matter IDs, and more than
   three claims per source fail closed without consuming the batch.
3. Context-dependent owner acknowledgements remain candidates; short,
   self-contained constraints and concrete directives still activate. A later
   explicit owner statement promotes an equal candidate and reconciles the
   value it replaces. Upgrade repair demotes affected legacy claims and
   restores any value they displaced.
4. Assistant completion prose remains a candidate and is absent from a Matter
   Context Packet.
5. A newer owner decision supersedes the prior one; the old value has zero
   default-prompt appearances.
6. Conflicting facts suspend both values until a named reviewer chooses or
   rejects one.
7. Every active claim has at least one source receipt; no applied batch retains
   its transcript payload; no key has two active values.
8. Focused replay, full local tests, protected CI, independent review, release
   gate, deploy verification, and post-release live replay all pass.

## Non-Goals

- Replacing Codex native task-local memory or provider transcripts.
- Inferring a Matter from topical similarity without an existing durable link.
- Treating a remembered sentence as proof of deployment, delivery, calendar,
  document, payment, or any other external effect.
- Sending routine memory updates to Lark.
