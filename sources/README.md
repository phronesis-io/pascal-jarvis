# Perception source adapters — the connector contract

One source TYPE = one module `sources/<type>.py`. One source INSTANCE = one
block in gitignored `sources.yaml` (tracked template: `sources.example.yaml`).
The runtime (`core/perception.py`) discovers adapters by dynamic import of
`sources.<type>` — there is no registry to edit. Adding a new kind of input
channel is one new module here; adding another instance of an existing kind
is one YAML block.

## Required: collect()

```python
def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]
```

- `cfg`: the source's `collect:` block from sources.yaml (adapter-specific).
- `state`: this source's persisted `adapter_state` — an incremental CURSOR
  (mtimes, last ids, one previous snapshot), never an unbounded log.
- Returns `(signals, new_state)`.
- **NEVER raises.** On failure return `([], state)` with
  `state["error_type"]` set to one of: `auth | network | timeout |
  rate_limit | crash`. The runtime tracks error_count and surfaces repeat
  offenders in the collect summary.
- Idempotent: same state in → same signals out. Overlap after a restart is
  fine — the runtime seen-store dedups on `(source_id, event_id)`, so
  `event_id` MUST be stable for the same underlying event.

## Signal dict

Required: `event_id`, `ts` (ISO-8601 local), `title`, `summary` (falls back
to title), `actor` (`{"raw": "", "resolved": ""}` if none).
Optional: `body` (inbox shows the first 2048 chars), `url`, `payload`
(dict), `sensitivity` (else the source/default value), `content_hash` (else
`sha256(title[:80] + body[:100])`).
`source_id` / `source_type` are stamped by the runtime.

## Optional: validate_cfg()

```python
def validate_cfg(cfg: dict) -> list[str]   # [] = valid
```

Called by the runtime before `collect()`. Non-empty → the source is skipped
this pass and the errors appear in the collect summary notes. Check required
keys and types only — do NOT probe the network here.

## Rules of the road

- **No personal data in tracked code.** Hosts, email addresses, chat ids,
  SQL against private schemas belong in `sources.yaml` / `data/` (both
  gitignored). Adapter code and `sources.example.yaml` ship with synthetic
  placeholders only — `tests/test_public_repo_hygiene.py` enforces this.
- First run must baseline silently where a backlog exists (see
  `file_watch.py`): enabling a new source never floods the inbox with
  pre-existing items.
- Cap signals per run (≤20) — a stuck cursor must not flood the inbox.
- Never echo secrets or the configured command/credentials into signal
  titles, bodies, or any file the adapter writes.

## Try a source before enabling it

```bash
# validate + trial-collect ALL sources (nothing is persisted: no state,
# no inbox, no seen-store; adapters skip external writes too)
python3 -m core.perception --dry-run

# just one source
python3 -m core.perception --dry-run --source my_new_source
```

Output shows per source: config validation errors, collect errors
(`error_type`), or the signal count with the first few titles. Adapters
with external write side-effects must check the `PERCEPTION_DRY_RUN` env
var (set during dry runs) and skip those writes.

## Bundled adapter types

| type | input |
|---|---|
| `file_watch` | new/changed files matching globs |
| `git_repo` | new commits across local repos |
| `lark_chat` | a Lark group chat |
| `lark_mail` | Lark mailbox new-mail metadata |
| `imap_mail` | generic IMAP-SSL mailbox metadata |
| `metrics_probe` | any command printing `{"metrics": {...}}` JSON — daily snapshot + threshold anomaly signals, feeds the `metrics-digest` heartbeat task (see module docstring for the full config shape) |
