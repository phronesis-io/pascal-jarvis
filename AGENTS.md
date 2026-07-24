# Jarvis Agent Guide

This repository is both a public codebase and a live personal-agent runtime.
Treat correctness, privacy, and user attention as production constraints.

## Start Here

Read these current-state documents before changing behavior:

1. `PRODUCT.md` - who Jarvis serves and what outcomes matter.
2. `DOMAIN.md` - vocabulary and invariants.
3. `ARCHITECTURE.md` - process, module, and authority boundaries.
4. `DESIGN.md` - interaction and surface rules.
5. `docs/prd_portfolio.md` - which historical PRDs are shipped, superseded,
   rejected, or active.

Then inspect the worktree:

```bash
git status --short --branch
python3 -m core.components
```

Never discard changes you did not create. A dirty live-runtime worktree is
normal; stage only your own files or hunks.

## Change Lifecycle

For behavior changes:

1. Reconstruct the real failure from logs, ledgers, and authoritative APIs.
2. Write or update the product contract when the behavior is not already
   specified.
3. Add the regression test that fails for the observed incident.
4. Implement at the narrowest shared boundary that prevents the failure class.
5. Run focused tests, then the full suite.
6. Review the diff for privacy, false completion claims, duplicate side
   effects, and stale documentation.
7. Commit and push a focused change.
8. For resident-runtime changes, restart and run deploy verification plus
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
- `:3457` - NiceGUI dashboard.
- `:3458` - authenticated mobile gateway; it may proxy the dashboard, never
  Admin.
- `components.yaml` - single source of truth for supervised components.
- `data/jarvis.db` - shared SQLite WAL state store.

Do not expose a new network surface or external side effect without an
explicit authority, security, and rollback design.
