# Quarantined Heartbeat Tasks

These hooks are retained as historical implementation material but are not
scheduled by `HEARTBEAT.md` and are excluded from CI shell discovery.

They were removed from the active task roster after the v4 task audit found
that they were either superseded or had produced no useful executions during
the observation window. Moving a hook back into `tasks/` requires:

1. A current product reason and an active `HEARTBEAT.md` entry.
2. Tests restored under `tests/`.
3. A local runtime smoke that proves the hook is not noisy or silently dead.

Active reusable cores such as `core/eigenflux_messages.py` remain outside this
directory. The former `tasks/harness_apply.py` surface is deleted and recorded
in the capability inventory's retirement ledger; Git history is its archive.
