"""Fail-closed PR/CI/review gate for production restarts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


class ReleaseGateError(RuntimeError):
    """The revision has not met production release evidence requirements."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _repo_name(remote: str) -> str:
    match = re.search(
        r"(?:github\.com[:/])([^/\s]+/[^/\s]+?)(?:\.git)?$", remote.strip()
    )
    if not match:
        raise ReleaseGateError("origin is not a GitHub repository")
    return match.group(1)


def _has_pass_attestation(body: str, sha: str) -> bool:
    expected = f"REVIEW-GATE: PASS {sha}".upper()
    return any(
        line.strip().upper() == expected
        for line in str(body or "").splitlines()
    )


class ReleaseGate:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        runner: Runner = subprocess.run,
    ):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.runner = runner

    def _run(
        self, command: list[str], *, json_output: bool = False, timeout: int = 30
    ) -> Any:
        try:
            result = self.runner(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseGateError(f"{command[0]} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise ReleaseGateError(detail[:500])
        if not json_output:
            return result.stdout.strip()
        try:
            return json.loads(result.stdout or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseGateError(f"{command[0]} returned invalid JSON") from exc

    def _run_paginated(self, endpoint: str) -> list[dict[str, Any]]:
        pages = self._run(
            ["gh", "api", "--paginate", "--slurp", endpoint],
            json_output=True,
        )
        if not isinstance(pages, list):
            raise ReleaseGateError("paginated GitHub response is invalid")
        if pages and all(isinstance(page, list) for page in pages):
            return [
                row
                for page in pages
                for row in page
                if isinstance(row, dict)
            ]
        return [row for row in pages if isinstance(row, dict)]

    def verify(self, *, fetch: bool = True) -> dict[str, Any]:
        branch = self._run(["git", "branch", "--show-current"])
        if branch != "main":
            raise ReleaseGateError("production restart requires local main")
        sha = self._run(["git", "rev-parse", "HEAD"])
        if fetch:
            self._run(["git", "fetch", "--quiet", "origin", "main"], timeout=60)
        origin_sha = self._run(["git", "rev-parse", "origin/main"])
        if sha != origin_sha:
            raise ReleaseGateError("HEAD does not equal origin/main")
        dirty = self._run(
            ["git", "status", "--porcelain", "--untracked-files=all"]
        )
        if dirty:
            raise ReleaseGateError("worktree changes are not deployable")
        remote = self._run(["git", "remote", "get-url", "origin"])
        repo = _repo_name(remote)

        protection = self._run(
            ["gh", "api", f"repos/{repo}/branches/main/protection"],
            json_output=True,
        )
        if not protection.get("enforce_admins", {}).get("enabled"):
            raise ReleaseGateError("branch protection still allows admin bypass")
        checks_policy = protection.get("required_status_checks") or {}
        if not checks_policy.get("strict"):
            raise ReleaseGateError("required checks are not strict against main")
        if not protection.get("required_pull_request_reviews"):
            raise ReleaseGateError("main does not require a pull request")
        if not protection.get("required_conversation_resolution", {}).get("enabled"):
            raise ReleaseGateError("review conversation resolution is not required")

        pulls = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github+json",
                f"repos/{repo}/commits/{sha}/pulls",
            ],
            json_output=True,
        )
        if not isinstance(pulls, list):
            raise ReleaseGateError("associated PR response is invalid")
        merged = next(
            (
                pr
                for pr in pulls
                if pr.get("merged_at")
                and (pr.get("base") or {}).get("ref") == "main"
            ),
            None,
        )
        if merged is None:
            raise ReleaseGateError("HEAD is not backed by a merged PR to main")
        number = int(merged["number"])
        author = str((merged.get("user") or {}).get("login") or "")

        check_runs = self._run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/check-runs"],
            json_output=True,
        )
        runs = check_runs.get("check_runs", []) if isinstance(check_runs, dict) else []
        required_contexts = {
            str(value)
            for value in checks_policy.get("contexts", [])
            if value
        }
        conclusions = {
            str(run.get("name") or ""): str(run.get("conclusion") or "")
            for run in runs
            if run.get("status") == "completed"
        }
        missing = [
            context
            for context in required_contexts
            if conclusions.get(context) not in {"success", "neutral", "skipped"}
        ]
        if missing:
            raise ReleaseGateError(
                "required checks are not successful: " + ", ".join(missing)
            )

        reviews = self._run_paginated(
            f"repos/{repo}/pulls/{number}/reviews"
        )
        comments = self._run_paginated(
            f"repos/{repo}/issues/{number}/comments"
        )
        evidence = []
        for review in reviews if isinstance(reviews, list) else []:
            reviewer = str((review.get("user") or {}).get("login") or "")
            state = str(review.get("state") or "").upper()
            body = str(review.get("body") or "")
            reviewed_sha = str(review.get("commit_id") or "")
            if (
                reviewer
                and reviewer != author
                and state == "APPROVED"
                and reviewed_sha == sha
            ):
                evidence.append(f"review:{reviewer}:{state}")
            elif (
                reviewer
                and reviewer != author
                and state == "COMMENTED"
                and _has_pass_attestation(body, sha)
            ):
                evidence.append(f"attestation:{reviewer}")
        for comment in comments if isinstance(comments, list) else []:
            reviewer = str((comment.get("user") or {}).get("login") or "")
            body = str(comment.get("body") or "").strip()
            if (
                reviewer
                and reviewer != author
                and _has_pass_attestation(body, sha)
            ):
                evidence.append(f"attestation:{reviewer}")
        if not evidence:
            raise ReleaseGateError("merged PR has no independent review evidence")
        return {
            "ok": True,
            "repo": repo,
            "sha": sha,
            "pr": number,
            "required_checks": sorted(required_contexts),
            "review_evidence": sorted(set(evidence)),
            "branch_protection": {
                "admin_bypass": False,
                "strict_checks": True,
                "pull_request_required": True,
                "conversation_resolution": True,
            },
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis production release gate")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = ReleaseGate().verify(fetch=not args.no_fetch)
    except ReleaseGateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
