# Isolated SDK integration

Opt-in acceptance adapter; the resident CLI plugin and heartbeat are unchanged.
Install the private binary wheel in a dedicated virtualenv. Do not add the SDK
source, wheel, application credential or runtime data to this repository.

```sh
python -m plugins.eigenflux.sdk_runner \
  --root /private/acceptance \
  --credential-file /private/acceptance/app.credential \
  --context-file /private/acceptance/context.json \
  --agent-id isolated-agent --once --seconds 300
```

- Context is an explicit file under `--root`, containing `summary` or `fields`.
- Feed and its batch payload commit to the isolated Jarvis bookmark store with
  an atomic receipt. Bookmark consumption itself does not call a model.
- Restart/retry resolves the receipt by batch ID; duplicate items create one bookmark.
- Polling defaults to one hour. SDK scheduling and controls remain authoritative.
- Auto-update is off for this private test build. No recurring service is installed.
- `status.json` is diagnostic; `snapshot.json` reports durable local consumption.
- Normal tests use fixtures. Hosted acceptance is manual and uses a dedicated
  approved application, Agent and private root, never the personal CLI identity.
- This archived repository remains archived. Local acceptance is not a resident
  rollout or public SDK release.

Private-wheel tests: `python -m pytest tests/test_eigenflux_sdk_adapter.py`.
The full repository gate is `./scripts/localtest.sh`.

## Optional reasoning and test notifications

```sh
python -m plugins.eigenflux.sdk_reasoning --root /private/acceptance \
  --source-file /approved/project-context.md --backend codex --seconds 3600
```

Only explicitly selected text documents are read, bounded to 4000 characters;
their hashes are stored privately. No memory directory or transcript is scanned.
The default backend uses Jarvis auxiliary routing. Explicit `codex` uses a new
ephemeral, read-only owner-requested process with shell, MCP, apps, plugins,
memory and multi-Agent features disabled; resident routing is unchanged.
Select an account-supported model with `codex.fallback_model` in the private
root's `jarvis.yaml`; no global Codex or Jarvis setting is changed.
Feed is untrusted data. Output must cite supplied item IDs and pass a strict
schema. Jarvis Model Runtime receipts prove inference, separate from SDK delivery.
Model calls can repeat after interruption; durable analyses and deterministic
test-inbox receipts prevent repeated notifications after a completed analysis.
Notifications terminate only in `test-notifications/*.json` under the private
root. This adapter has no production notification transport or recipient setting.

Acceptance passed with real project documents and owner task constraints, two
hosted batches separated by a natural hour, two verified analyses and two local
test notifications. No server clock or quota timestamp was changed.

TODO: optional personal-memory sources, external test-channel transport and a
resident deployment. The current host needs no existing Jarvis installation.
