# Jarvis Roadmap

- Updated: 2026-08-17
- Tagged release: v1.8.2
- Current source status: see `docs/current_system.md`
- Product status: expansion frozen

Historical feature waves and requirement numbers live in `CHANGELOG.md`,
`docs/prd_portfolio.md`, and `docs/release_acceptance_2026-07-24.md`. This file
contains only work that is still actionable under the current product freeze.

## P0 - Finish The Current Release

- Record a trusted exact-SHA review or owner receipt for merged `main`.
- Verify merged-main required checks.
- Pass `python3 -m core.release_gate` without weakening its policy.
- Run the governed full restart and prove bot/heartbeat revision, critical
  components, Lark delivery, provider canary, local Admin/Dashboard smoke, and
  post-release L3 observation.
- Confirm an active Routine survives a model infrastructure failure as
  `deferred`, retries once, and does not duplicate its Lark Item.

## P1 - Runtime Evidence

- Make real delivery and provider-load health as visible as tiny canaries:
  success ratio, oldest due envelope, terminal-failure streak, last real
  receipt, route failure reason, and resident revision.
- Measure cross-session retrieval quality: useful retrieval, stale fact,
  duplicate fact, missed decision, and rejected context.
- Keep self-healing internal. Notify the owner only for exhausted recovery or
  a genuine owner action.
- Verify restore of the private session/data backup, not only creation and
  checksums.

## P2 - Behavior-Preserving Debt Retirement

- Establish reproducible line and branch coverage for `core.memorial` and
  `core.intentions`.
- Add characterization tests around their longest workflows.
- Extract lifecycle-owned slices behind compatibility facades in small PRs.
- Reduce reviewed direct import cycles and monitor adjacency growth.
- Continue routing every runtime write through injected private roots so tests
  and resident processes cannot dirty the public repository.

## Frozen Or Retired

The following are not backlog while the product freeze is in force:

- new web/mobile product surfaces, Tailscale, pairing codes, or Web Push;
- new Routine product features or another proactive notification lane;
- Telegram, Slack, or email delivery without a committed adopter;
- a second personal task/inbox system beside Item, Matter, Intent, and Routine;
- automatic promotion of inferred prose into external actions;
- a home-grown Taskline clone;
- broad plugin or source expansion without a named blind spot and privacy
  evidence.

An item leaves this section only through an explicit owner thaw plus updated
product, authority, privacy, migration, and retirement decisions.
