"""Matter-aware launch and completion capture for Claude Code and Codex."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from core.matter_context import build_context_bundle, write_context_bundle
from core.matter_run_audit import audit_matter_runs
from core.matter_runs import (
    MatterRunConflict,
    abort_run,
    acquire_run,
    bind_context_packet,
    get_run,
    list_runs,
    mark_run_running,
    recover_expired_runs,
    release_run,
    renew_run,
)
from core.matters import add_event, get_matter
from core.work_sessions import discover_sessions, extract_text


def _new_run_context(matter_id: str, provider: str, workspace: Path,
                     task: str = "") -> tuple[dict, Path]:
    run = acquire_run(
        matter_id,
        executor=provider,
        task=task,
        workspace=workspace,
        lease_seconds=21600,
    )
    try:
        bundle = build_context_bundle(matter_id, run=run)
        context_path = write_context_bundle(matter_id, run=run)
        run = bind_context_packet(
            run["id"],
            packet_id=bundle["packet_id"],
            context_digest=bundle["digest"],
            context_path=context_path,
        )
    except Exception as exc:
        abort_run(run["id"], error=f"context_packet_failed:{type(exc).__name__}")
        raise
    return run, context_path


def prepare_handoff(matter_id: str, provider: str,
                    actor: str = "executor",
                    workspace: str | Path | None = None,
                    task: str = "") -> dict:
    """Write a bounded context bundle and return the canonical launch command."""
    provider = str(provider).lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    matter = get_matter(matter_id, include_links=False, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    workspace_path = Path(workspace or os.getcwd()).expanduser().resolve()
    desired_task = task or matter.get("next_action", "")
    try:
        run, context_path = _new_run_context(
            matter_id, provider, workspace_path,
            desired_task,
        )
    except MatterRunConflict:
        reusable = next((
            item for item in list_runs(matter_id=matter_id, limit=5)
            if item["status"] == "acquired"
            and item["executor"] == provider
            and item["task"] == desired_task
            and Path(item["workspace"]).resolve() == workspace_path
            and item["context_digest"]
            and Path(item["context_path"]).is_file()
            and Path(item["context_path"]).with_suffix(".json").is_file()
            and float(item["lease_expires_epoch"]) > time.time()
        ), None)
        if reusable is None:
            raise
        run = reusable
        context_path = Path(run["context_path"])
    command = (
        f"./scripts/jarvis-matter launch {matter_id} {provider} "
        f"--run-id {run['id']}"
    )
    add_event(
        matter_id, "handoff_prepared", f"准备交接给 {provider}", actor=actor,
        payload={"provider": provider, "context_path": str(context_path),
                 "command": command, "run_id": run["id"],
                 "context_digest": run["context_digest"]},
    )
    return {
        "matter_id": matter_id,
        "run_id": run["id"],
        "provider": provider,
        "context_path": str(context_path),
        "command": command,
    }


def _git_files(workspace: Path) -> set[str]:
    if not (workspace / ".git").exists():
        return set()
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"], cwd=workspace,
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    files = set()
    records = result.stdout.split(b"\0")
    index = 0
    while index < len(records):
        raw = records[index]
        index += 1
        if len(raw) < 4:
            continue
        status = raw[:2]
        name = raw[3:].decode("utf-8", errors="replace")
        files.add(name)
        # Porcelain v1 -z emits renames/copies as `XY destination\0source\0`.
        # The source record has no status prefix and must not be parsed as a
        # second file (the old parser turned `old.txt` into the ghost `.txt`).
        if (b"R" in status or b"C" in status) and index < len(records):
            index += 1
    return files


def _keep_lease_alive(run_id: str, stop: threading.Event) -> None:
    """Renew long foreground sessions without extending abandoned handoffs."""
    while not stop.wait(60):
        try:
            renew_run(run_id, lease_seconds=21600)
        except Exception as exc:
            from core.log import log
            log(
                "matter-runtime",
                "lease_renewal_failed",
                level="error",
                run_id=run_id,
                error_type=type(exc).__name__,
            )
            return


def _last_assistant(path: str | Path, provider: str) -> str:
    transcript = Path(path)
    if not transcript.exists():
        return ""
    latest = ""
    try:
        with transcript.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if provider == "claude" and row.get("type") == "assistant":
                    message = row.get("message") or {}
                    candidate = extract_text(message.get("content"))
                    if candidate:
                        latest = candidate
                elif provider == "codex" and row.get("type") == "event_msg":
                    payload = row.get("payload") or {}
                    if payload.get("type") == "agent_message":
                        candidate = str(payload.get("message") or "").strip()
                        if candidate:
                            latest = candidate
    except OSError:
        return ""
    return latest[:4000]


def _select_new_session(before: set[tuple[str, str]], provider: str,
                        workspace: Path, started: float) -> dict | None:
    candidates = discover_sessions(provider=provider, days=2, limit=30,
                                   include_matter_links=False)
    resolved = str(workspace.resolve())
    for session in candidates:
        key = (session["provider"], session["session_id"])
        if key in before:
            continue
        session_workspace = str(session.get("workspace") or "")
        if session_workspace:
            try:
                if str(Path(session_workspace).expanduser().resolve()) != resolved:
                    continue
            except OSError:
                continue
        try:
            if Path(session.get("path", "")).stat().st_mtime + 2 < started:
                continue
        except OSError:
            continue
        return session
    return None


def record_completion(matter_id: str, provider: str, session: dict | None,
                      workspace: str | Path, changed_files: set[str] | None = None,
                      exit_code: int = 0, actor: str = "executor",
                      run_id: str = "") -> dict:
    """Release an execution window; never infer Matter completion from prose."""
    if get_matter(matter_id, include_links=False, include_events=False) is None:
        raise KeyError(f"matter not found: {matter_id}")
    workspace = Path(workspace).expanduser().resolve()
    if run_id:
        run = get_run(run_id)
        if run is None:
            raise KeyError(f"matter run not found: {run_id}")
        if run["matter_id"] != matter_id or run["executor"] != provider:
            raise MatterRunConflict("run does not match the requested Matter/provider")
    else:
        run, _ = _new_run_context(
            matter_id, provider, workspace, "记录已有执行会话结果"
        )
        run_id = run["id"]
    if session:
        run = mark_run_running(
            run_id,
            session_id=session["session_id"],
            model=session.get("model", ""),
        )
        final = _last_assistant(session.get("path", ""), provider)
    else:
        run = mark_run_running(run_id)
        final = ""
    narrative = final or (
        f"{provider} 会话已结束" if exit_code == 0 else f"{provider} 会话退出码 {exit_code}"
    )
    receipt = release_run(
        run_id,
        context_generation=int(run["context_generation"]),
        context_digest=str(run["context_digest"]),
        narrative=narrative,
        exit_code=exit_code,
        artifacts=sorted(changed_files or set()),
    )
    add_event(
        matter_id,
        "work_session_completed",
        "执行会话已结束并生成 Result Receipt",
        actor=provider,
        payload={
            "provider": provider,
            "exit_code": int(exit_code),
            "workspace": str(workspace),
            "session_id": (session or {}).get("session_id", ""),
            "model": (session or {}).get("model", ""),
            "artifacts": sorted(changed_files or set()),
            "run_id": run_id,
            "receipt_id": receipt["receipt_id"],
            "receipt_digest": receipt["digest"],
            "matter_completed": False,
        },
    )
    return {"matter_id": matter_id, "run_id": run_id, "session": session,
            "summary": narrative, "narrative_trust": "unverified_model_report",
            "artifacts": sorted(changed_files or set()), "exit_code": exit_code,
            "receipt": receipt}


def launch(matter_id: str, provider: str, workspace: str | Path | None = None,
           prompt: str = "", run_id: str = "") -> int:
    provider = str(provider).lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    matter = get_matter(matter_id, include_links=False, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    workspace = Path(workspace or os.getcwd()).expanduser().resolve()
    binary = shutil.which(provider)
    if not binary:
        raise FileNotFoundError(f"{provider} CLI not found")
    if run_id:
        run = get_run(run_id)
        if run is None:
            raise KeyError(f"matter run not found: {run_id}")
        if run["matter_id"] != matter_id or run["executor"] != provider:
            raise MatterRunConflict("run does not match the requested Matter/provider")
        if Path(run["workspace"]).resolve() != workspace:
            raise MatterRunConflict("run workspace does not match launch workspace")
        context_path = Path(run["context_path"])
        if not run["context_digest"] or not context_path.is_file():
            raise MatterRunConflict("run has no readable bound Context Packet")
        if prompt and str(prompt) != str(run.get("task") or ""):
            raise MatterRunConflict("prepared run task does not match launch prompt")
        task = str(
            run.get("task")
            or matter.get("next_action")
            or "Continue this Matter to a concrete result."
        )
    else:
        task = str(
            prompt
            or matter.get("next_action")
            or "Continue this Matter to a concrete result."
        )
        run, context_path = _new_run_context(matter_id, provider, workspace, task)
        run_id = run["id"]
    context = context_path.read_text(encoding="utf-8")
    opening = (
        f"You are continuing Jarvis Matter {matter_id} in run {run_id}.\n\n"
        f"{context}\n\n"
        f"Current task:\n{task}\n\n"
        "Keep the Matter and run IDs in your final summary. Name concrete artifacts "
        "changed. Your narrative is not completion evidence; the launcher will verify "
        "artifacts and create a Result Receipt."
    )
    before_sessions = {
        (s["provider"], s["session_id"])
        for s in discover_sessions(provider=provider, days=2, limit=30,
                                   include_matter_links=False)
    }
    before_files = _git_files(workspace)
    started = time.time()
    mark_run_running(run_id)
    add_event(matter_id, "work_session_started", f"交接给 {provider}", actor="executor",
              payload={"workspace": str(workspace), "context_path": str(context_path),
                       "run_id": run_id, "context_digest": run["context_digest"]})
    if provider == "codex":
        command = [binary, "--cd", str(workspace), "--no-alt-screen", opening]
    else:
        command = [binary, "--name", f"Matter {matter_id}", opening]
    lease_stop = threading.Event()
    lease_thread = threading.Thread(
        target=_keep_lease_alive,
        args=(run_id, lease_stop),
        name=f"matter-lease-{run_id[-8:]}",
        daemon=True,
    )
    lease_thread.start()
    try:
        result = subprocess.run(command, cwd=workspace, check=False)
    except BaseException as exc:
        abort_run(run_id, error=f"provider_process_failed:{type(exc).__name__}")
        raise
    finally:
        lease_stop.set()
        lease_thread.join(timeout=2)
    session = _select_new_session(before_sessions, provider, workspace, started)
    # Do not claim files that were already dirty before the handoff. A launcher
    # cannot safely attribute those edits without hashing every worktree file.
    changed = _git_files(workspace) - before_files
    record_completion(
        matter_id, provider, session, workspace, changed, result.returncode,
        run_id=run_id,
    )
    return int(result.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.matter_executor")
    sub = parser.add_subparsers(dest="command", required=True)
    context = sub.add_parser("context")
    context.add_argument("matter_id")
    context.add_argument("--output", default="")
    run = sub.add_parser("launch")
    run.add_argument("matter_id")
    run.add_argument("provider", choices=("claude", "codex"))
    run.add_argument("--workspace", default="")
    run.add_argument("--prompt", default="")
    run.add_argument("--run-id", default="")
    finish = sub.add_parser("finish")
    finish.add_argument("matter_id")
    finish.add_argument("provider", choices=("claude", "codex"))
    finish.add_argument("session_id")
    finish.add_argument("--workspace", default="")
    finish.add_argument("--run-id", default="")
    status = sub.add_parser("run-status")
    status.add_argument("run_id")
    audit = sub.add_parser("audit")
    audit.add_argument("--recover-expired", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "context":
        print(write_context_bundle(args.matter_id, args.output or None))
        return 0
    if args.command == "launch":
        return launch(
            args.matter_id, args.provider, args.workspace or None, args.prompt,
            args.run_id,
        )
    if args.command == "run-status":
        result = get_run(args.run_id)
        if result is None:
            parser.error(f"matter run not found: {args.run_id}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "audit":
        recovered = recover_expired_runs() if args.recover_expired else []
        result = audit_matter_runs()
        result["recovered_run_ids"] = recovered
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["healthy"] else 3
    sessions = discover_sessions(provider=args.provider, days=90, limit=100,
                                 include_matter_links=False)
    session = next((s for s in sessions if s["session_id"] == args.session_id), None)
    if not session:
        parser.error(f"session not found: {args.session_id}")
    workspace = args.workspace or session.get("workspace") or os.getcwd()
    print(json.dumps(record_completion(
        args.matter_id, args.provider, session, workspace, run_id=args.run_id,
    ),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
