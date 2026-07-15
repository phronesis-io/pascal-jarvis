"""Perception source adapters (docs/prd_perception_ingestion.md §5.3).

Each adapter module implements:

    def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]

- cfg: the source's `collect:` block from sources.yaml
- state: that source's persisted adapter_state (incremental cursor, not a
  full snapshot)
- returns (signals, new_state); NEVER raises — on failure return ([], state)
  with state['error_type'] set to auth|network|timeout|rate_limit|crash
- idempotent: same state in → same signals out; overlap after restart is
  reconciled by the global seen-store

Optionally, an adapter may also implement:

    def validate_cfg(cfg: dict) -> list[str]   # [] = valid

called by the runtime before collect(); non-empty errors skip the source
for that pass and surface in the collect summary notes.

The runtime discovers adapters by dynamic import of sources.<type> — there
is no hardcoded registry; adding a new source TYPE is one new module here,
adding another source of an existing type is one sources.yaml block.
Full contract spec: sources/README.md.
"""
