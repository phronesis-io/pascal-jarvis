# Pascal Jarvis — Final Project Conclusion

- Closure date: 2026-09-01
- Repository status: archived and unmaintained
- Code archive point before this notice:
  [`bd09e19050f4`](https://github.com/phronesis-io/pascal-jarvis/commit/bd09e19050f430bc205d836a4fff3a9af771084b)
- Final behavior change:
  [PR #146](https://github.com/phronesis-io/pascal-jarvis/pull/146)
- Final pre-archive `main` verification:
  [GitHub Actions run 33373078482](https://github.com/phronesis-io/pascal-jarvis/actions/runs/33373078482)

## Decision

Pascal Jarvis is complete as an experiment and closed as a maintained
software project.

The project answered its central product question: the interactive frontstage
should be Codex on desktop and mobile; durable continuity, authority, timed
work, and verified closure belong in a quieter backstage; Git/GitHub should
remain the code-evidence plane; and Lark should be a bounded wake-up and
native-integration channel rather than another long-form workspace.

The repository proved that these boundaries can be implemented with durable
Items, Matters, Intents, Context Packets, Result Receipts, verified external
actions, attention governance, cross-session memory, provider-neutral model
routing, and release receipts. It also proved the cost of carrying all of
those concerns in one resident personal-agent harness: at the final audited
state the repository had 186 active capabilities, thousands of tests, several
large orchestration modules, and significant operational and compatibility
surface.

The final judgment is therefore not to keep expanding Pascal Jarvis as a
standalone personal-agent product. Its useful ideas should survive as smaller,
platform-native, demand-led components or in a new project with a newly
validated scope. This repository should not be revived in place merely to
continue its historical roadmap.

## What Was Closed

At the archive point:

- `main` ended at `bd09e19050f4`; the protected `main` test workflow completed
  successfully after the final behavior merge;
- the final delivery wave made Codex the frontstage and Jarvis the continuity
  layer, hardened compiled memory and interruption policy, and fixed the last
  PGC outage-probe failure class;
- GitHub had no open pull requests or issues;
- unattended code mutation had already been retired; new code work required an
  owner-started Codex or Claude Code task and the normal GitHub release gates;
- the old Jarvis dashboard, mobile gateway, and Jarvis-owned Tailscale surface
  had already been retired.

The final repository state is a source and design archive, not a supported
distribution.

## Explicit Non-Claims and Unfinished Evidence

Archiving does not turn pending acceptance gates into completed work:

- the planned 20 desktop and 20 mobile Codex journeys were not completed, so
  the full Lark-to-Codex product migration was never production-proven;
- Verified Delegation automatic promotion did not obtain its required
  multi-week, multi-connector reviewed production sample;
- known maintainability debt remains in large orchestration modules and the
  reviewed import-cycle baseline;
- the latest tagged release predates the archive point; the final `main`
  revision is historical source state, not a new supported release;
- this notice does not assert that any private deployed instance is still
  running or healthy. Repository archival does not stop a resident process,
  migrate private data, revoke credentials, or decommission external services.

These are not backlog items. No maintainer is assigned to complete them.

## Maintenance and Support Policy

From 2026-09-01:

- no bug fixes, dependency updates, security patches, compatibility work, or
  production support are planned;
- issues and pull requests are not accepted or monitored;
- no availability, data-migration, or security guarantee is provided;
- existing deployments should be treated as self-managed, unpatched software
  and should be disabled or migrated deliberately;
- anyone reusing the code should fork it, review the license, rotate all
  environment-specific credentials, re-audit dependencies and permissions,
  and validate every production assumption from scratch.

Private runtime state, credentials, personal memory, and external service
configuration were intentionally excluded from this public repository and are
not part of the archive.

## Historical Reading Order

For readers studying the work rather than deploying it:

1. [PRODUCT.md](PRODUCT.md) — final product boundary;
2. [DOMAIN.md](DOMAIN.md) — durable concepts and invariants;
3. [ARCHITECTURE.md](ARCHITECTURE.md) — final runtime and authority design;
4. [DECISIONS.md](DECISIONS.md) — accepted architecture decisions;
5. [docs/prd_portfolio.md](docs/prd_portfolio.md) — shipped, superseded,
   rejected, and incomplete product claims;
6. [docs/codex_frontstage_completion_audit.md](docs/codex_frontstage_completion_audit.md)
   — the separation between implementation, deployment, and real-use evidence;
7. [docs/engineering_health.md](docs/engineering_health.md) — verified debt
   and audit decisions.

Git history remains the authoritative record of how the project reached this
state.
