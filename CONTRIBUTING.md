# Contributing to Pascal Jarvis

Thanks for helping improve Jarvis! This repo uses a simple collaboration model:

> **Everyone works on their own branch. Only Pascal merges to `main`.**

## The workflow

1. **Branch off `main`** — one branch per person (or per feature), named:
   ```
   dev/<your-name>              # your personal long-lived branch
   dev/<your-name>/<topic>      # optional: one branch per feature
   ```
   Always start from the latest `main`:
   ```bash
   git fetch origin
   git checkout -b dev/<your-name> origin/main
   ```

2. **Make your changes and run the tests locally** before pushing:
   ```bash
   ./setup.sh            # once, or after dependency changes
   ./scripts/localtest.sh
   ```
   All tests must pass. New behavior needs new tests — this repo's rule of thumb
   is that every bug fix ships with the regression test that would have caught it.

3. **Open a Pull Request targeting `main`.**
   Direct pushes are not a release path. Required CI must be green and review
   evidence must satisfy `core.release_gate`; an explicit owner release
   decision is accepted only when it is recorded after merge, bound to the
   exact merged SHA, and includes a reason. CI runs automatically on every PR.

4. **Pascal reviews and merges.** Keep PRs focused — one topic per PR merges
   faster than a grab-bag.

5. **Deploy code through the governed full restart.** A merge is not a
   deployment. After merged-main checks and review evidence are available,
   run `./restart.sh`, then verify runtime revision, critical components,
   delivery/provider smoke, and post-release observation. `--runtime` is only
   for configuration/state changes on the same already-deployed revision.

Product expansion is frozen as of 2026-08-17. Reliability, privacy, tests,
documentation, evidence, and behavior-preserving debt retirement remain open.
A new surface, notification lane, workflow, or authority needs an explicit
owner thaw and updated current-state contracts before code.

## Personal data is config, not code

Jarvis is a personal agent — but the *person* must never be hardcoded. The rule:

> **Anything user-specific (interests, schedules, contacts, mailboxes, banks,
> keywords) lives in gitignored per-user files, never in tracked code, prompts,
> tests, or docs.**

Established homes for per-user data:
- `jarvis.yaml` / `sources.yaml` (gitignored) — config, credentials, IDs
- `data/` (fully gitignored) — personalization files read at runtime, e.g.
  `data/checkin_personal.sh` (recurring-appointment prep),
  `data/checkin_topics_personal.txt` (topic keywords)
- your memory directory (outside the repo) — profile, contacts, projects

Test fixtures must be synthetic (`user_1998@163.com`, `alice@example.org`, fake
UIDs and IDs). `tests/test_public_repo_hygiene.py` enforces the obvious shapes
(real mailboxes, full-length Lark IDs) and will fail your PR if they sneak in.
If your feature needs a user-specific value, add a config key or a `data/` file
with a documented fallback — see how `tasks/checkin_pre.sh` does it.

## What NOT to commit

This repo doubles as a live runtime directory on the machines that run it, so
be strict:

- **No secrets** — tokens, app secrets, webhook URLs, cookies, API keys.
- **No runtime state** — `*.log`, `*.jsonl` ledgers, `*.pid`,
  `heartbeat_state.json`, `active_sessions.json`, `jobs/`, `data/`, etc.
  These are gitignored; never force-add them.
- **No personal data** — see the section above.

If `git status` shows a file you didn't create, leave it alone — it's runtime
state.

## Code conventions

- Python 3.10+ (CI runs 3.12). Follow the style of the file you're editing.
- Tests live in `tests/`, named `test_<module>.py`. Tests must be hermetic:
  never read or write the real data dir — use `tmp_path` and monkeypatch
  (see existing tests for the pattern), and never depend on wall-clock time —
  pass `now=` explicitly (a wall-clock fixture once turned CI red for days).
  On a machine running the production bot, the protected-file guard forgives
  writes the live bot may have made, and prints a "mutations forgiven" summary
  when it does. **A run with that summary is not release evidence** — stop the
  bot and re-run with `JARVIS_TEST_STRICT_GUARD=1` to get CI's strictness
  locally (2026-07-27: a PR quoted a local pass while CI was red).
- Shell scripts must pass `bash -n` and `shellcheck -s bash -S error`
  (CI enforces this for `bot.sh`).
- Task scripts follow the pre/post convention documented in README
  ("Writing Custom Tasks"). Pre-scripts must honor **empty stdout = skip**:
  if your task's data source is unavailable on a machine, print nothing and
  exit 0 — don't make every install burn a Claude call to say "nothing to do".

## Getting your environment running

Read `docs/current_system.md`, then follow README Quick Start +
`docs/INSTALL.md`. You can develop and run the full
test suite without any Lark/EigenFlux credentials — plugins are optional and
tests are self-contained.

## Questions / design discussions

Open a GitHub issue, or raise it in the team chat before building something
large — cheaper to align first.
