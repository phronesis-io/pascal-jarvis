# EigenFlux Preinstall Governed Sync

## Problem

`eigenflux-preinstall` is a production heartbeat task, but it mirrored upstream
Skill files directly into the running `pascal-jarvis` checkout. On 2026-08-28
that changed three tracked files while Jarvis was live. The source-integrity
watchdog correctly detected drift and blocked child respawns until a governed
deploy. A background compatibility check had therefore weakened runtime
self-healing even though the Skill changes themselves were valid.

The same path also let upstream prose become active prompt behavior before code
review, tests, Owner authorization, and release evidence.

## Product Contract

EigenFlux preinstallation remains a first-class Jarvis capability:

- Production continuously fetches, compares, validates, and reports upstream
  Skill and CLI drift.
- A heartbeat observation never modifies tracked source or silently changes the
  assistant contract.
- Skill updates become active only after an isolated worktree applies them,
  tests pass, a PR is reviewed, the Owner authorizes release, and Jarvis is
  deployed from the resulting main commit.
- Runtime state and parity backlogs remain writable because they are untracked
  operational evidence, not executable source.

## Implementation

- Default `tasks/eigenflux_preinstall_pre.sh` to detect-only mode.
- Keep overlay rendering, add/update comparison, provenance-gated retirement
  planning, CLI checks, live probes, and parity reporting.
- Add `EIGENFLUX_PREINSTALL_APPLY=1` for an explicit maintainer run in an
  isolated worktree.
- Refuse apply mode on `main`/`master`, a detached checkout, or any checkout
  whose `.bot.pid` identifies a live Jarvis process.
- Add dry-run retirement planning so upstream deletions are visible without
  removing deployed files.
- Accept Git worktrees as source repositories in every repository gate.
- Treat transport-level `EOF`/timeout/TLS failures in the live feedback probe
  as inconclusive; explicit API/shape rejection still fails verification.
- Do not advance the stored upstream Skill SHA while a detected change remains
  unreleased; later checks must not confuse observed state with deployed state.
- Adopt the current upstream dashboard-link contract in this governed PR:
  automated/delayed reports use the stable dashboard URL, while one-time login
  links are minted only during a live response and expire after about 15 minutes.

## Acceptance

1. A default preinstall run reports candidate add/update/remove operations and
   leaves the destination byte-for-byte unchanged.
2. Dry-run retirement returns the same proven retirement plan as apply mode but
   removes nothing.
3. Explicit apply mode still produces an upstream-plus-overlay mirror.
4. Pending Skill drift does not advance the deployed upstream SHA.
5. Automated EigenFlux content cannot carry a delayed one-time login code.
6. Focused tests, shell syntax checks, the full repository suite, CI, review,
   Owner release, deploy verification, and post-deploy component checks pass.

## Non-goals

- Treating campus-network or VPN `EOF` failures as authentication failures.
- Restarting all of Jarvis because an idle WebSocket has not emitted a message.
- Auto-merging upstream prompt changes.
