# Jarvis Agent Guide

This repository is both a public codebase and a live personal-agent runtime.
Treat correctness, privacy, and user attention as production constraints.

## Start Here

Read these current-state documents before changing behavior:

1. `PRODUCT.md` - who Jarvis serves and what outcomes matter.
2. `DOMAIN.md` - vocabulary and invariants.
3. `ARCHITECTURE.md` - process, module, and authority boundaries.
4. `DECISIONS.md` - easy-to-confuse ownership decisions and change routing.
5. `DESIGN.md` - interaction and surface rules.
6. `docs/prd_portfolio.md` - which historical PRDs are shipped, superseded,
   rejected, or active.
7. `docs/release_acceptance_2026-07-24.md` - the current requirement-to-evidence
   ledger.
8. `docs/engineering_health.md` - verified current debt and rejected audit
   false positives.

Then inspect the worktree:

```bash
git status --short --branch
python3 -m core.components
python3 scripts/capability_inventory.py --check-doc docs/capability_inventory.md
python3 scripts/import_graph.py core tasks --threshold 24 --limit 20
python3 scripts/import_graph.py core --max-direct-cycles 11
```

The capability inventory is the evidence-backed list of supported runtime
surfaces. Update it with the generator whenever a component, heartbeat task,
CLI, admin route, or Lark command changes; never delete a capability
from a broad cleanup without an explicit retirement and migration review.

The import graph is a review signal for broad or self-improve rounds, not a
hard quality verdict. Compare the high-adjacency list before and after the
change; explain growth instead of mechanically hiding central authorities.
The direct two-module cycle budget is a hard regression gate: existing debt is
reviewed explicitly, and new cycles do not enter unnoticed.

Never discard changes you did not create. A dirty live-runtime worktree is
normal; stage only your own files or hunks.

## Change Lifecycle

For behavior changes:

1. Reconstruct the real failure from logs, ledgers, and authoritative APIs.
2. Write or update the product contract when the behavior is not already
   specified.
3. Claim executable engineering work through Taskline when L2 is available;
   use a separate worktree for concurrent agents.
4. Add the regression test that fails for the observed incident.
5. Implement at the narrowest shared boundary that prevents the failure class.
6. Run focused tests, then the full suite.
7. Review the diff for privacy, false completion claims, duplicate side
   effects, and stale documentation.
8. Commit and push a focused change through a real PR. Prefer an independent
   review of the final PR head. In a repository configured for zero required
   approvals and no code-owner or last-push review rule, an explicit
   admin-owner release decision may substitute only when it is bound to the
   merged SHA and records a reason.
9. For resident-runtime changes, restart and run deploy verification plus
   smoke tests. Code on disk is not deployed until the live process is proven
   to run that revision.

The repo-local validation skill is
`.agents/skills/jarvis-localtest/SKILL.md`. The one-command test entry is:

```bash
./scripts/localtest.sh
```

Use `./scripts/localtest.sh --runtime` only on the production machine after a
restart.

## Authority Rules

- A model response is a proposal, never completion evidence.
- Delegation terminal state comes only from `core.delegations` after matching
  verifier evidence; linked objects are projections, not competing authorities.
- External mutations complete only after deterministic code verifies an
  authoritative read-back or receipt.
- Never copy a numeric EigenFlux agent ID from model context. Use
  `python3 -m core.eigenflux_messages`.
- Delivery state comes from `core.delivery` and SQLite, not producer prose.
- Runtime health comes from `components.yaml` and `core.components`.
- Current calendar truth comes from the calendar sync artifact/API, not old
  conversation text.
- Item, Matter, Intent, Delivery, and Handoff have separate responsibilities;
  do not create a second state machine that overlaps them.

## Privacy

Personal names, contacts, schedules, IDs, interests, credentials, and private
content belong in gitignored `jarvis.yaml`, `sources.yaml`, `data/`, or memory.
Tracked code, docs, prompts, and tests use synthetic fixtures.

Before committing:

```bash
python3 -m pytest tests/test_public_repo_hygiene.py -q
git diff --cached
```

## Tests

- Python tests use `tmp_path` and injected clocks/runners.
- No test may read or write the production `data/`, memory, Lark, or
  EigenFlux account.
- Every external-action path needs tests for ambiguity, idempotency,
  interrupted execution, failed read-back, and honest user-visible status.
- Shell changes must pass `bash -n`; `bot.sh` must also pass the CI shellcheck
  policy.

## Runtime Surfaces

- `:3456` - local Admin operations console.
- `:3457` - retired (2026-08-21); the NiceGUI dashboard no longer runs.
  Archive duty moved to the morning-anchor batch line and the Admin
  console; the code archive is git history.
- `:3458` - retired (2026-08-11, REQ-120); the mobile gateway and its
  Tailscale funnel no longer run.
- `components.yaml` - single source of truth for supervised components.
- `data/jarvis.db` - shared SQLite WAL state store.
- `:8787` - optional local Taskline engineering sidecar.

Do not expose a new network surface or external side effect without an
explicit authority, security, and rollback design.
