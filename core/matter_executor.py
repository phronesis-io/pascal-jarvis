"""Matter-aware launch and completion capture for Claude Code and Codex."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.matter_context import write_context_bundle
from core.matters import add_event, get_matter, link_entity
from core.work_sessions import discover_sessions, extract_text


def prepare_handoff(matter_id: str, provider: str,
                    actor: str = "executor") -> dict:
    """Write a bounded context bundle and return the canonical launch command."""
    provider = str(provider).lower()
    if provider not in {"claude", "codex"}:
        raise ValueError("provider must be claude or codex")
    matter = get_matter(matter_id, include_links=False, include_events=False)
    if matter is None:
        raise KeyError(f"matter not found: {matter_id}")
    context_path = write_context_bundle(matter_id)
    command = f"./scripts/jarvis-matter launch {matter_id} {provider}"
    add_event(
        matter_id, "handoff_prepared", f"准备交接给 {provider}", actor=actor,
        payload={"provider": provider, "context_path": str(context_path),
                 "command": command},
    )
    return {
        "matter_id": matter_id,
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
    for raw in result.stdout.split(b"\0"):
        if len(raw) < 4:
            continue
        name = raw[3:].decode("utf-8", errors="replace")
        if " -> " in name:
            name = name.rsplit(" -> ", 1)[-1]
        files.add(name)
    return files


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
                      exit_code: int = 0, actor: str = "executor") -> dict:
    """Attach a completed work session, its final summary, and changed files."""
    if get_matter(matter_id, include_links=False, include_events=False) is None:
        raise KeyError(f"matter not found: {matter_id}")
    workspace = Path(workspace).expanduser().resolve()
    payload = {"provider": provider, "exit_code": int(exit_code),
               "workspace": str(workspace)}
    if session:
        metadata = {
            "workspace": session.get("workspace", str(workspace)),
            "model": session.get("model", ""),
            "started_at": session.get("started_at", ""),
            "updated_at": session.get("updated_at", ""),
            "path": session.get("path", ""),
        }
        link_entity(
            matter_id, "session", session["session_id"], provider=provider,
            title=session.get("title", ""), metadata=metadata, actor=actor,
        )
        payload.update({"session_id": session["session_id"],
                        "model": session.get("model", "")})
        final = _last_assistant(session.get("path", ""), provider)
    else:
        final = ""
    for name in sorted(changed_files or set()):
        path = workspace / name
        link_entity(
            matter_id, "artifact", str(path), provider="file", title=name,
            metadata={"workspace": str(workspace), "exists": path.exists(),
                      "source": provider}, actor=actor,
        )
    summary = final or (
        f"{provider} 会话已结束" if exit_code == 0 else f"{provider} 会话退出码 {exit_code}"
    )
    add_event(matter_id, "work_session_completed", summary[:1200], actor=provider,
              payload={**payload, "artifacts": sorted(changed_files or set())})
    try:
        from core.mobile_access import send_push
        send_push(f"{provider} 工作已结束", summary[:240],
                  url=f"/matters/{matter_id}", matter_id=matter_id)
    except Exception:
        pass
    return {"matter_id": matter_id, "session": session, "summary": summary,
            "artifacts": sorted(changed_files or set()), "exit_code": exit_code}


def launch(matter_id: str, provider: str, workspace: str | Path | None = None,
           prompt: str = "") -> int:
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
    context_path = write_context_bundle(matter_id)
    context = context_path.read_text(encoding="utf-8")
    task = str(prompt or matter.get("next_action") or "Continue this Matter to a concrete result.")
    opening = (
        f"You are continuing Jarvis Matter {matter_id}.\n\n{context}\n\n"
        f"Current task:\n{task}\n\n"
        "Keep the Matter ID in your final summary and name concrete artifacts changed."
    )
    before_sessions = {
        (s["provider"], s["session_id"])
        for s in discover_sessions(provider=provider, days=2, limit=30,
                                   include_matter_links=False)
    }
    before_files = _git_files(workspace)
    started = time.time()
    add_event(matter_id, "work_session_started", f"交接给 {provider}", actor="executor",
              payload={"workspace": str(workspace), "context_path": str(context_path)})
    if provider == "codex":
        command = [binary, "--cd", str(workspace), "--no-alt-screen", opening]
    else:
        command = [binary, "--name", f"Matter {matter_id}", opening]
    result = subprocess.run(command, cwd=workspace, check=False)
    session = _select_new_session(before_sessions, provider, workspace, started)
    # Do not claim files that were already dirty before the handoff. A launcher
    # cannot safely attribute those edits without hashing every worktree file.
    changed = _git_files(workspace) - before_files
    record_completion(matter_id, provider, session, workspace, changed, result.returncode)
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
    finish = sub.add_parser("finish")
    finish.add_argument("matter_id")
    finish.add_argument("provider", choices=("claude", "codex"))
    finish.add_argument("session_id")
    finish.add_argument("--workspace", default="")
    args = parser.parse_args(argv)
    if args.command == "context":
        print(write_context_bundle(args.matter_id, args.output or None))
        return 0
    if args.command == "launch":
        return launch(args.matter_id, args.provider, args.workspace or None, args.prompt)
    sessions = discover_sessions(provider=args.provider, days=90, limit=100,
                                 include_matter_links=False)
    session = next((s for s in sessions if s["session_id"] == args.session_id), None)
    if not session:
        parser.error(f"session not found: {args.session_id}")
    workspace = args.workspace or session.get("workspace") or os.getcwd()
    print(json.dumps(record_completion(args.matter_id, args.provider, session, workspace),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
