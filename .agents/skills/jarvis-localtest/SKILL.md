---
name: jarvis-localtest
description: Validate the owner Jarvis changes from focused tests through production runtime smoke checks.
metadata:
  internal: true
---

# Jarvis Local Test

Use this skill after any behavior or runtime change.

## 1. Preserve the worktree

Run `git status --short --branch`. Do not reset or stage files owned by another
session. Review the exact diff you intend to ship.

## 2. Focused verification

Run the smallest tests that exercise the changed contract first. A bug fix
must include the regression scenario, its interrupted/failure path, and
idempotency when it performs a side effect.

For shell changes:

```bash
bash -n bot.sh
bash -n path/to/changed-script.sh
```

## 3. Full local gate

```bash
./scripts/localtest.sh
```

This runs shell syntax, shellcheck when installed, the full pytest suite, and
public-repository hygiene.

## 4. Engineering task evidence

When Taskline is available:

```bash
./scripts/taskline.sh status
./scripts/taskline.sh task heartbeat <task-id> --lease 30m
```

Keep Spec, Dev Notes, Test Report, real PR, review, CI, and merge evidence on
the same Taskline task. An Agent saying "done" is not a stage exit condition.

## 5. Self-review

Inspect:

- authority/read-back for completion claims;
- duplicate mutation after retry or callback;
- wrong-target and ambiguity behavior;
- private data in tracked fixtures, docs, logs, and API results;
- compatibility with existing dirty worktree changes;
- current documentation and PRD portfolio status.

## 6. Production verification

Only on the production machine, after committing the intended revision:

```bash
./restart.sh --yes
./scripts/localtest.sh --runtime
```

Runtime mode intentionally skips pytest because live heartbeat processes
mutate runtime state that the strict test-write guard requires to remain
unchanged. It is a post-restart component/revision/smoke gate; the ordinary
full local gate and protected CI must already be green before deployment.

`restart.sh` first requires local `main`, `HEAD == origin/main`, a clean tracked
worktree, protected-main policy, a merged PR, successful required checks, and
release authority. Normal authority is an independent review bound to the
final PR head. When branch protection explicitly requires zero approvals and
has no code-owner or last-push review rule, an admin author may instead record
a merge-SHA-bound owner release decision and reason. The runtime gate then
requires component health, deploy verification, and smoke checks. For
resource-lifecycle changes, sample the live process more than once across real
scheduler activity; a clean restart alone does not prove a leak is fixed.

For configuration-only changes with no code deployment, use
`./restart.sh --runtime --yes`. It revalidates release authority, requires a
clean worktree, and verifies that the resident bot and heartbeat already match
`HEAD`; missing authority or revision drift fails closed and must return to the
governed release path above.

Do not perform a real message, calendar, document, or public mutation merely
for smoke testing. Use a read-only preflight unless the owner explicitly
authorized the test side effect.
