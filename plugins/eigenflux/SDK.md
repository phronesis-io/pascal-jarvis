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
- Feed is committed to the isolated Jarvis bookmark store with an atomic batch
  receipt. Jarvis bookmark search can consume it without Lark or model calls.
- Restart/retry resolves the receipt by batch ID; duplicate items create one bookmark.
- Polling defaults to one hour. SDK scheduling and controls remain authoritative.
- Auto-update is off for this private test build. No recurring service is installed.
- `status.json` is diagnostic; `snapshot.json` reports durable local consumption.
- Normal tests use fixtures. Hosted acceptance is manual and uses a dedicated
  approved application, Agent and private root, never the personal CLI identity.
- This archived repository remains archived. Local acceptance is not a resident
  rollout, public SDK release, live Agent reasoning, or notification acceptance.

Private-wheel tests: `python -m pytest tests/test_eigenflux_sdk_adapter.py`.
The full repository gate is `./scripts/localtest.sh`.

TODO: approved real-memory context selection, model-facing Feed consumption,
notification acceptance, and a second natural hourly pull. These are separate
from durable bookmark consumption and server-side hourly admission validation.
