### Jarvis verified friend messages (required in this repository)

When this skill runs inside Pascal Jarvis and the owner names a person or
agent, **do not copy a numeric `agent_id` from model context into
`eigenflux msg send --receiver-id`**. Use the local verified gateway:

```bash
python3 -m core.eigenflux_messages send \
  --recipient "EXACT FRIEND NAME OR REMARK" \
  --content "YOUR MESSAGE"
```

For a long body, write it to a temporary file and use `--content-file`.
The gateway resolves the current server-side friend record, rejects ambiguous
or numeric model-supplied targets, reserves an idempotency key, sends once,
and reads the conversation history back. Report completion only when the
command exits 0 and prints `已核验发送`; `发送结果仍在核验` is not completion
and must not be retried manually. If the owner explicitly says to send the
same content again, pass a stable `--repeat-token` for that new request.

The post-turn action form is available when needed:
`[ACTION:eigenflux_message|recipient=<exact name or remark>|content_b64=<UTF-8 base64>]`.
Do not write a success claim around the marker; its deterministic receipt is
appended after execution.
