"""Owner-private Lark execution through the provider-neutral Model Runtime.

The resident shell keeps conversation serialization, progress replies,
background promotion, and delivery. This module owns one model turn's route,
deadline, replay policy, and durable receipt. Group and non-owner traffic never
enters this module.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Config
from core.model_adapter_support import provider_health_rows
from core.model_control import model_routes
from core.model_fallback import clear as clear_primary_gate
from core.model_fallback import gate as primary_gate
from core.model_runtime import RuntimeRequest, execute
from core.owner_chat_adapters import (
    CodexRunner,
    Logger,
    OpenAIRunner,
    OwnerChatAdapters,
    Runner,
    process_runner,
    provider_label,
    terminate_process_group,
)
from core.runtime_provider import get_preference


@dataclass(frozen=True)
class OwnerChatModelResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    call_id: str = ""
    route_id: str = ""
    status: str = ""
    terminal_reason: str = ""
    error_detail: str = ""
    attempted_routes: tuple[str, ...] = ()

    def envelope(self) -> dict[str, Any]:
        return {
            "result": self.text,
            "subtype": "success" if self.status == "succeeded" else self.status,
            "runtime": {
                "schema": "jarvis.owner-chat-model.v1",
                "call_id": self.call_id,
                "route_id": self.route_id,
                "provider": self.provider,
                "model": self.model,
                "status": self.status,
                "terminal_reason": self.terminal_reason,
                "error_detail": self.error_detail,
                "attempted_routes": list(self.attempted_routes),
            },
        }


def run_owner_chat_model(
    content: str,
    *,
    task_id: str,
    conv_key: str,
    context_key: str,
    session_id: str,
    session_dir: str | Path,
    root: str | Path,
    work_dir: str | Path,
    system_prompt: str,
    backup_system_prompt: str = "",
    matter_id: str = "",
    claude_bin: str = "claude",
    requested_model: str = "",
    timeout: int = 6000,
    allow_tools: bool = True,
    preference: str | None = None,
    gate_state: str | None = None,
    runner: Runner | None = None,
    codex_runner: CodexRunner | None = None,
    openai_runner: OpenAIRunner | None = None,
    logger: Logger | None = None,
    config: Config | None = None,
    health_rows: list[dict[str, Any]] | None = None,
    process_holder: dict[str, Any] | None = None,
) -> OwnerChatModelResult:
    """Run one owner turn without allowing ambiguous cross-route replay."""
    base = Path(root).expanduser().resolve()
    work = Path(work_dir).expanduser().resolve()
    sessions = Path(session_dir).expanduser().resolve()
    config = config or Config(base / "jarvis.yaml")
    holder = process_holder if process_holder is not None else {}
    gate = gate_state or primary_gate(base)
    adapters = OwnerChatAdapters(
        base=base,
        work=work,
        sessions=sessions,
        session_id=session_id,
        conv_key=conv_key,
        context_key=context_key,
        system_prompt=system_prompt,
        backup_system_prompt=backup_system_prompt,
        claude_bin=claude_bin,
        runner=runner or process_runner(holder),
        process_holder=holder,
        codex_runner=codex_runner,
        openai_runner=openai_runner,
        logger=logger or (lambda *_args, **_kwargs: None),
    )
    runtime = execute(
        RuntimeRequest(
            task_id=task_id,
            matter_id=matter_id,
            prompt=content,
            system_prompt=system_prompt,
            context="owner_chat",
            requested_model=requested_model,
            preference=preference or get_preference(conv_key),
            gate_state=gate,
            effect_authority=("external" if allow_tools else "none"),
            allow_tools=allow_tools,
            timeout_seconds=timeout,
            workspace=str(work),
        ),
        adapters.mapping(),
        root=base,
        config=config,
        health_rows=(
            provider_health_rows(base) if health_rows is None else health_rows
        ),
    )
    if runtime.status == "succeeded" and runtime.route_id == "primary" \
            and gate != "primary":
        try:
            clear_primary_gate(base)
        except Exception:
            pass
    labels = {route.id: route.label for route in model_routes(config)}
    return OwnerChatModelResult(
        text=runtime.text,
        provider=(
            provider_label(runtime.route_id)
            or labels.get(runtime.route_id, "")
        ),
        model=runtime.observed_model or runtime.requested_model,
        call_id=runtime.call_id,
        route_id=runtime.route_id,
        status=runtime.status,
        terminal_reason=runtime.terminal_reason,
        error_detail=adapters.last_error_detail,
        attempted_routes=tuple(item.route_id for item in runtime.attempts),
    )


def _read_optional(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _build_backup_system_prompt(
    *,
    content: str,
    root: str,
    memory_dir: str,
    tracker: str,
    conv_key: str,
    session_dir: str,
    session_id: str,
    chat_type: str,
    context_key: str,
    matter_id: str,
    max_memory_chars: int,
) -> str:
    if not memory_dir or max_memory_chars <= 0:
        return ""
    from core.prompt import build_cached_system_prompt
    from core.timeutil import now_local_str

    return build_cached_system_prompt(
        cache_dir=str(Path(root) / "data" / "session_prompt_cache"),
        jarvis_dir=root,
        memory_dir=memory_dir,
        session_dir=session_dir,
        session_id=session_id,
        conv_key=conv_key,
        now_ts=now_local_str("%Y-%m-%d %H:%M %A"),
        tracker_path=tracker,
        chat_type=chat_type,
        max_memory_chars=max_memory_chars,
        context_key=context_key,
        matter_id=matter_id,
        focus_text=content,
        resume_existing=(Path(session_dir) / f"{session_id}.jsonl").is_file(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.owner_chat_model")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--conv-key", required=True)
    parser.add_argument("--context-key", default="")
    parser.add_argument("--matter-id", default="")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--system-prompt-file", required=True)
    parser.add_argument("--backup-system-prompt-file", default="")
    parser.add_argument("--memory-dir", default=os.environ.get("MEMORY_DIR", ""))
    parser.add_argument("--tracker", default="")
    parser.add_argument("--chat-type", default="p2p")
    parser.add_argument("--backup-max-memory-chars", type=int, default=int(
        os.environ.get("BACKUP_MAX_MEMORY_CHARS", "40000"),
    ))
    parser.add_argument("--model", default="")
    parser.add_argument("--preference", choices=("auto", "codex"), default="")
    parser.add_argument(
        "--gate-state", choices=("primary", "backup", "probe"), default="",
    )
    parser.add_argument("--timeout", type=int, default=int(os.environ.get(
        "OWNER_CHAT_MODEL_TIMEOUT", "6000",
    )))
    parser.add_argument("--work-dir", default=os.environ.get("WORK_DIR", "."))
    parser.add_argument("--root", default=os.environ.get("JARVIS_DIR", "."))
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--no-tools", action="store_true")
    args = parser.parse_args(argv)
    holder: dict[str, Any] = {}
    previous_handlers: dict[int, Any] = {}

    def stop(signum, _frame):
        holder["cancelled"] = signum
        terminate_process_group(holder.get("process"))
        if holder.get("process:spawning"):
            return
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[sig] = signal.signal(sig, stop)
    try:
        content = sys.stdin.read()
        backup_system_prompt = _read_optional(args.backup_system_prompt_file)
        if not backup_system_prompt:
            try:
                backup_system_prompt = _build_backup_system_prompt(
                    content=content,
                    root=args.root,
                    memory_dir=args.memory_dir,
                    tracker=args.tracker,
                    conv_key=args.conv_key,
                    session_dir=args.session_dir,
                    session_id=args.session_id,
                    chat_type=args.chat_type,
                    context_key=args.context_key,
                    matter_id=args.matter_id,
                    max_memory_chars=max(0, args.backup_max_memory_chars),
                )
            except Exception:
                backup_system_prompt = ""
        result = run_owner_chat_model(
            content,
            task_id=args.task_id,
            conv_key=args.conv_key,
            context_key=args.context_key,
            matter_id=args.matter_id,
            session_id=args.session_id,
            session_dir=args.session_dir,
            root=args.root,
            work_dir=args.work_dir,
            system_prompt=_read_optional(args.system_prompt_file),
            backup_system_prompt=backup_system_prompt,
            claude_bin=args.claude_bin,
            requested_model=args.model,
            preference=args.preference or None,
            gate_state=args.gate_state or None,
            timeout=max(1, args.timeout),
            allow_tools=not args.no_tools,
            process_holder=holder,
        )
        if holder.get("cancelled"):
            raise SystemExit(128 + int(holder["cancelled"]))
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(result.envelope(), ensure_ascii=False))
    if result.status == "succeeded":
        return 0
    if result.status == "cancelled":
        return 143
    if result.status == "ambiguous":
        return 74
    return 75


if __name__ == "__main__":
    raise SystemExit(main())
