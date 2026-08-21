# Repository Scorecard

Assessment date: 2026-08-21

This is a reproducible current-state scorecard. Historical incidents remain in
git history and the changelog; they do not stay here as if they were the live
verdict. Scores combine source evidence with the revision actually running.

## Scores

| Dimension | Score | Current evidence |
|---|---:|---|
| Code and tests | 9.0/10 | 3,100+ tests, strict write isolation, shell checks, import-cycle budget, public-repository hygiene, provider continuity scenarios, and a new non-growing maintainability-debt gate in local and protected CI. |
| Product effect | 7.5/10 | Lark is the single mobile surface; unified delivery, long-card continuation, cross-product memory, routines, EigenFlux, attention budgets, and model fallback all have executable contracts. Real usefulness, recall precision, and notification value still require ongoing production observation. |
| Architecture maintainability | 7.8/10 | Cross-session, Memorial storage/rendering/transport, model control, and delivery have explicit boundaries. Four orchestration modules remain large, but their file and longest-function baselines now cannot grow unnoticed. |
| Release and runtime consistency | 9.2/10 | Merged-PR/CI/review authority, clean-worktree checks, full resident-version verification, component health, delivery smoke, and the exact released SHA are joined into one durable post-release receipt. A failed check cannot persist a success receipt. |
| **Overall** | **8.4/10** | Rounded mean of the four dimensions. |

## What Changed

The previous scorecard was an incident snapshot: it described an unmerged
release branch, a dirty production checkout, and runtime drift from 2026-08-16.
Those facts were useful then and false now. Keeping them as a current verdict
made the repository look permanently broken after the incident was closed.

This scorecard now rests on repeatable controls:

- `scripts/maintainability_budget.py` accepts today's large-module debt but
  rejects any increase in file size or longest-function size. Refactoring can
  lower the checked-in limits; feature work cannot silently raise them.
- `core.deploy receipt` verifies the release-gate SHA, every registered
  runtime, all critical components, and unified-delivery smoke before writing
  one SQLite receipt.
- `restart.sh` writes that receipt only after a governed deploy or an authorized
  same-revision runtime restart has completed all checks.
- `core.deploy receipt-latest` gives the next human or Agent one durable answer
  to "what was released, under which authority, and what proved it healthy?"

## Reproduce

Run these from the repository root:

```bash
./scripts/localtest.sh
python3 scripts/maintainability_budget.py
python3 -m core.components
python3 -m core.deploy verify
python3 -m core.deploy receipt-latest
python3 -m core.provider_health status
```

The first five commands are repository and release evidence. Provider health
is reported separately because an upstream account limit or relay outage is a
real operational degradation, but not proof of code/runtime drift.

## Residual Debt

1. `core.memorial`, `core.intentions`, `core.heartbeat`, and
   `core.delegations` remain large orchestration modules. The budget stops
   growth; small behavior-preserving extractions must now ratchet it downward.
2. Product quality cannot be established from test count. Cross-session recall
   precision, useful EigenFlux signals, routine completion, ignored cards, and
   attention cost remain L3 production metrics.
3. Owner calendar/docs/mail/task operations still have a human OAuth boundary.
   Bot delivery is deliberately independent and must not impersonate it.
4. Claude account limits and relay timeouts are external availability facts.
   The model control plane must keep reporting them honestly while routing to
   a verified healthy fallback.

## Next Threshold

Reaching 9/10 overall requires evidence, not another scoring edit: reduce the
four checked modules below their budgets, publish stable branch-coverage
baselines for their critical workflows, and show sustained production gains in
recall usefulness, routine closure, and low-noise delivery.
