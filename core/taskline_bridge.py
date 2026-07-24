"""Jarvis adapter for external Taskline claims and isolated worktrees."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


class TasklineBridgeError(RuntimeError):
    """Taskline or worktree setup failed."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


class TasklineBridge:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        project: str = "pascal-jarvis",
        runner: Runner = subprocess.run,
        db_path: str | Path | None = None,
    ):
        self.root = Path(
            root
            or os.environ.get("JARVIS_DIR")
            or Path(__file__).resolve().parent.parent
        ).resolve()
        self.project = project
        self.runner = runner
        self.db_path = Path(
            db_path
            or os.environ.get("JARVIS_DB_PATH")
            or self.root / "data" / "jarvis.db"
        )
        self.worktree_root = Path(
            os.environ.get(
                "JARVIS_WORKTREE_ROOT",
                str(Path.home() / ".local" / "share" / "jarvis" / "worktrees"),
            )
        )

    def _run(self, command: list[str], *, timeout: int = 30) -> dict[str, Any]:
        try:
            result = self.runner(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "TASKLINE_PROJECT": self.project},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TasklineBridgeError(f"{command[0]} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise TasklineBridgeError(detail[:500])
        try:
            value = json.loads(result.stdout or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TasklineBridgeError("Taskline returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise TasklineBridgeError("Taskline returned a non-object")
        return value

    def status(self) -> dict[str, Any]:
        status = self._run(["taskline", "status"])
        if not status.get("healthy"):
            raise TasklineBridgeError("Taskline server is not healthy")
        if not status.get("registered"):
            raise TasklineBridgeError("this workspace is not registered")
        return status

    def claim_next(
        self, *, lease: str = "2h", labels: list[str] | None = None
    ) -> dict[str, Any]:
        self.status()
        command = [
            "taskline",
            "task",
            "next",
            "--project",
            self.project,
            "--claim",
            "--lease",
            lease,
        ]
        for label in labels or []:
            command.extend(["--label", label])
        task = self._run(command)
        if not task.get("id"):
            return {"available": False, "reason": "queue_empty_or_blocked"}
        delegation_id = self._engineering_delegation(task)
        return {
            "available": True,
            "task": task,
            "delegation_id": delegation_id,
        }

    def _engineering_delegation(self, task: dict[str, Any]) -> str:
        """Project one claimed engineering task into the common control plane."""
        from core.delegations import DelegationStore

        task_id = str(task.get("id") or "")
        if not task_id:
            raise TasklineBridgeError("Taskline task has no id")
        store = DelegationStore(root=self.root, db_path=self.db_path)
        release_sha = self._task_release_sha(task)
        expected = {
            "git_head": release_sha or f"pending:{task_id}",
            "runtime_ok": True,
            "components_ok": True,
        }
        verification_policy = {
            "verifier": "runtime_deploy",
            "taskline_project": self.project,
            "taskline_id": task_id,
            "release_sha": release_sha,
        }
        delegation, _ = store.create(
            principal_id="owner",
            source="taskline",
            source_ref=task_id,
            title=str(task.get("title") or "Engineering task")[:300],
            operation="engineering_change",
            request_summary=str(task.get("description") or "")[:1000],
            target_type="repository",
            target_id=self.root.name,
            target_label=self.root.name,
            expected_postcondition=expected,
            authority="jarvis_runtime",
            verification_policy=verification_policy,
            privacy_class="internal",
            capture_mode="explicit",
            authorized=True,
        )
        detail = store.get(delegation["id"])
        if (
            detail["expected_postcondition"] != expected
            or detail["verification_policy"] != verification_policy
        ):
            delegation = store.revise_contract(
                delegation["id"],
                expected_version=detail["contract_version"],
                expected_postcondition=expected,
                verification_policy=verification_policy,
                actor_id="taskline-bridge",
            )
            detail = store.get(delegation["id"])
        if not detail["steps"]:
            store.add_step(
                delegation["id"],
                expected_version=int(delegation["contract_version"]),
                sequence=1,
                kind="runtime_deploy",
                executor="release",
            )
        store.link(
            delegation["id"],
            "taskline_task",
            task_id,
            relation="tracks",
        )
        return str(delegation["id"])

    @staticmethod
    def _task_release_sha(task: dict[str, Any]) -> str:
        """Return only an immutable full commit SHA supplied by Taskline."""
        candidates = [
            task.get("release_sha"),
            task.get("merge_sha"),
            task.get("commit_sha"),
        ]
        metadata = task.get("metadata")
        if isinstance(metadata, dict):
            candidates.extend(
                metadata.get(key)
                for key in ("release_sha", "merge_sha", "commit_sha")
            )
        for candidate in candidates:
            value = str(candidate or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", value):
                return value
        return ""

    @staticmethod
    def _pull_url(task: dict[str, Any]) -> str:
        for link in task.get("links") or []:
            if not isinstance(link, dict):
                continue
            value = str(link.get("url") or "").strip()
            if re.fullmatch(
                r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
                value,
            ):
                return value
        return ""

    def refresh_release(self, task_id: str) -> dict[str, Any]:
        """Bind a completed Taskline task to its immutable merged revision."""
        from core.delegations import DelegationConflict, DelegationStore

        task = self._run(["taskline", "task", "get", task_id])
        if str(task.get("state") or "") != "done":
            delegation_id = self._engineering_delegation(task)
            return DelegationStore(
                root=self.root, db_path=self.db_path
            ).get(delegation_id)
        pull_url = self._pull_url(task)
        if not pull_url:
            raise TasklineBridgeError("completed task has no GitHub PR link")
        pull = self._run(
            [
                "gh",
                "pr",
                "view",
                pull_url,
                "--json",
                "mergedAt,mergeCommit",
            ]
        )
        release_sha = str(
            (pull.get("mergeCommit") or {}).get("oid") or ""
        ).lower()
        if (
            not pull.get("mergedAt")
            or not re.fullmatch(r"[0-9a-f]{40}", release_sha)
        ):
            raise TasklineBridgeError("Taskline PR is not merged")
        delegation_id = self._engineering_delegation(
            {**task, "release_sha": release_sha}
        )
        store = DelegationStore(root=self.root, db_path=self.db_path)
        detail = store.get(delegation_id)
        step = detail["steps"][0]
        if step["status"] == "pending":
            try:
                store.claim_step(
                    delegation_id,
                    step["id"],
                    expected_version=detail["contract_version"],
                    owner="taskline-release",
                    lease_seconds=120,
                )
                store.record_attempt(
                    delegation_id,
                    step["id"],
                    expected_version=detail["contract_version"],
                    owner="taskline-release",
                    succeeded=True,
                    artifact_locator=pull_url,
                )
            except DelegationConflict:
                pass
        return store.get(delegation_id)

    def link_execution_context(
        self,
        task: dict[str, Any],
        *,
        provider: str = "",
        session_id: str = "",
        job_id: str = "",
        workspace: str = "",
        branch: str = "",
    ) -> dict[str, Any]:
        """Attach execution pointers, never transcript bodies, to Delegation."""
        from core.delegations import DelegationStore

        delegation_id = self._engineering_delegation(task)
        store = DelegationStore(root=self.root, db_path=self.db_path)
        if provider and session_id:
            provider_ref = re.sub(
                r"[^A-Za-z0-9_.:/@+-]", "-", provider
            )[:80]
            session_ref = re.sub(
                r"[^A-Za-z0-9_.:/@+-]", "-", session_id
            )[:200]
            store.link(
                delegation_id,
                "session",
                f"{provider_ref}:{session_ref}",
                relation="executed_by",
            )
        if job_id:
            store.link(
                delegation_id,
                "job",
                job_id,
                relation="executed_by",
            )
        if workspace:
            digest = hashlib.sha256(
                str(Path(workspace).resolve()).encode("utf-8")
            ).hexdigest()[:20]
            store.link(
                delegation_id,
                "workspace",
                f"sha256:{digest}",
                relation="executed_in",
            )
        if branch:
            safe_branch = re.sub(r"[^A-Za-z0-9_.:/@+-]", "-", branch)[:200]
            store.link(
                delegation_id,
                "git_branch",
                safe_branch,
                relation="implemented_on",
            )
        return store.get(delegation_id)

    def heartbeat(self, task_id: str, *, lease: str = "2h") -> dict[str, Any]:
        return self._run(
            [
                "taskline",
                "task",
                "heartbeat",
                task_id,
                "--lease",
                lease,
            ]
        )

    @staticmethod
    def _slug(value: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return slug[:32] or "task"

    def prepare_worktree(
        self,
        task: dict[str, Any],
        *,
        base: str = "origin/main",
    ) -> dict[str, str]:
        task_id = str(task.get("id") or "")
        if not re.fullmatch(r"[A-Za-z0-9-]{8,100}", task_id):
            raise TasklineBridgeError("task id is unsafe")
        title = str(task.get("title") or "task")
        branch = f"agent/{self._slug(title)}-{task_id[:8]}"
        path = (self.worktree_root / task_id[:8]).resolve()
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        created = False
        if path.exists():
            result = self.runner(
                ["git", "worktree", "list", "--porcelain"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                raise TasklineBridgeError("cannot inspect existing worktrees")
            entries = result.stdout.split("\n\n")
            expected_branch = f"branch refs/heads/{branch}"
            registered = any(
                f"worktree {path}" in entry and expected_branch in entry
                for entry in entries
            )
            if not registered:
                raise TasklineBridgeError(
                    "existing workspace is not the expected registered worktree"
                )
        else:
            branch_exists = self.runner(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=10,
            ).returncode == 0
            command = ["git", "worktree", "add"]
            if branch_exists:
                command.extend([str(path), branch])
            else:
                command.extend(["-b", branch, str(path), base])
            result = self.runner(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                raise TasklineBridgeError(
                    (
                        result.stderr
                        or result.stdout
                        or "worktree setup failed"
                    ).strip()[:500]
                )
            created = True
        self._run(
            [
                "taskline",
                "task",
                "update",
                task_id,
                "--append-description",
                f"Isolated workspace: {path} ({branch})",
            ]
        )
        self.link_execution_context(
            task,
            workspace=str(path),
            branch=branch,
        )
        return {
            "path": str(path),
            "branch": branch,
            "created": "true" if created else "false",
        }

    def queue_state(self) -> dict[str, Any]:
        self.status()
        active = self._run(
            [
                "taskline",
                "task",
                "list",
                "--project",
                self.project,
                "--state",
                "start,spec,dev,test,review",
            ]
        )
        tasks = active.get("tasks", active.get("items", []))
        preview = self._run(
            ["taskline", "task", "next", "--project", self.project]
        )
        executable = bool(preview.get("id"))
        return {
            "active": len(tasks) if isinstance(tasks, list) else 0,
            "executable": executable,
            "stop_reason": (
                ""
                if executable
                else (
                    "queue_blocked"
                    if isinstance(tasks, list) and tasks
                    else "queue_empty"
                )
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis Taskline bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    claim = sub.add_parser("claim")
    claim.add_argument("--lease", default="2h")
    claim.add_argument("--label", action="append", default=[])
    claim.add_argument("--worktree", action="store_true")
    sub.add_parser("queue-state")
    context = sub.add_parser("link-context")
    context.add_argument("--task-id", required=True)
    context.add_argument("--title", default="Engineering task")
    context.add_argument("--provider", default="")
    context.add_argument("--session-id", default="")
    context.add_argument("--job-id", default="")
    context.add_argument("--workspace", default="")
    context.add_argument("--branch", default="")
    args = parser.parse_args(argv)
    bridge = TasklineBridge()
    try:
        if args.command == "status":
            result = bridge.status()
        elif args.command == "queue-state":
            result = bridge.queue_state()
        elif args.command == "link-context":
            result = bridge.link_execution_context(
                {"id": args.task_id, "title": args.title},
                provider=args.provider,
                session_id=args.session_id,
                job_id=args.job_id,
                workspace=args.workspace,
                branch=args.branch,
            )
        else:
            result = bridge.claim_next(lease=args.lease, labels=args.label)
            if args.worktree and result["available"]:
                result["worktree"] = bridge.prepare_worktree(result["task"])
    except TasklineBridgeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
