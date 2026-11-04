"""Explicit owner-requested acceptance backend; no resident routing changes."""

import json
from pathlib import Path
import tempfile

from core.aux_model import AuxiliaryModelResult, _invoke
from core.codex_fallback import build_command, resolve_codex_bin
from core.config import Config
from core.model_credentials import without_model_credentials
from core.model_runtime import AdapterResult, RuntimeRequest, execute


DISABLED_FEATURES = ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent", "memories", "sleep_tool")


def command(binary, model, sandbox, output):
    args = build_command(binary, model=model, work_dir=sandbox, output_file=output,
                         allow_tools=False, ephemeral=True)
    for feature in DISABLED_FEATURES:
        args.extend(["--disable", feature])
    args.extend(["-c", 'web_search="disabled"', "-c", "mcp_servers={}", "-c", "project_doc_max_bytes=0"])
    return args


def run_codex_model(prompt, *, root, system_prompt="", timeout=120, task_id="", runner=None, **unused):
    root = Path(root)
    sandbox = root / "analysis-sandbox"
    sandbox.mkdir(mode=0o700, exist_ok=True)

    def adapter(route, request, model, limit):
        output = None
        try:
            with tempfile.NamedTemporaryFile(dir=sandbox, delete=False) as stream:
                output = Path(stream.name)
            args = command(resolve_codex_bin(), model, sandbox, output)
            result = _invoke(args, request.system_prompt + "\n\n" + request.prompt,
                             timeout=limit, cwd=str(sandbox), env=without_model_credentials(),
                             runner=runner, process_holder=None, process_key="sdk-analysis", cancelled=None)
            # A provider's answer never proves that its execution stayed text-only.
            # Fail closed on every tool event, including unknown future item kinds.
            allowed = {"agent_message", "reasoning"}
            for line in result.stdout.splitlines():
                event = json.loads(line)
                if event.get("type", "").startswith("item."):
                    if event.get("item", {}).get("type") not in allowed:
                        return AdapterResult(status="rejected", reason="adapter_failure", effects_started=None)
            if result.returncode != 0:
                return AdapterResult(status="ambiguous_failure", reason="adapter_failure", effects_started=None)
            text = output.read_text()
            if not text.strip():
                return AdapterResult(status="ambiguous_failure", reason="empty_or_error_output", effects_started=False)
            return AdapterResult(status="succeeded", text=text, effects_started=False)
        except Exception:
            return AdapterResult(status="ambiguous_failure", reason="adapter_failure", effects_started=None)
        finally:
            if output is not None:
                output.unlink(missing_ok=True)

    # This is an explicit owner-requested private acceptance run, not automatic
    # untrusted auxiliary routing or a resumed Jarvis/Codex conversation.
    result = execute(RuntimeRequest(task_id=task_id, prompt=prompt, system_prompt=system_prompt,
                                   context="owner_chat", route_ids=("codex",), allow_tools=False,
                                   effect_authority="none", timeout_seconds=timeout, workspace=str(sandbox)),
                     {"codex_cli": adapter}, root=root, config=Config(root / "jarvis.yaml"))
    return AuxiliaryModelResult(text=result.text, provider="Codex CLI", model=result.observed_model or result.requested_model,
                                call_id=result.call_id, status=result.status, terminal_reason=result.terminal_reason)
