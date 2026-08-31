"""Fail-closed PR/CI/review gate for production restarts."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote


class ReleaseGateError(RuntimeError):
    """The revision has not met production release evidence requirements."""


class ReleaseGateUnavailable(ReleaseGateError):
    """Live remote evidence could not be read because the network is down."""


Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
CACHE_SCHEMA = "jarvis.release-gate-cache.v1"
_NETWORK_FAILURE_MARKERS = (
    "could not resolve host",
    "failed to connect",
    "network is unreachable",
    "connection refused",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "i/o timeout",
    "tls handshake timeout",
    "temporary failure in name resolution",
    "no such host",
    "context deadline exceeded",
)


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


def _owner_release_reason(body: str, sha: str) -> str:
    marker = f"RELEASE-GATE: OWNER-APPROVED {sha}".upper()
    lines = [line.strip() for line in str(body or "").splitlines()]
    if not any(line.upper() == marker for line in lines):
        return ""
    for line in lines:
        prefix, separator, value = line.partition(":")
        if separator and prefix.strip().upper() == "REASON":
            reason = value.strip()
            if len(reason) >= 12:
                return reason
    return ""


def _github_timestamp_after(candidate: Any, baseline: Any) -> bool:
    try:
        candidate_time = datetime.fromisoformat(
            str(candidate).replace("Z", "+00:00")
        )
        baseline_time = datetime.fromisoformat(
            str(baseline).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    return candidate_time > baseline_time


_TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
_TRUSTED_PERMISSIONS = {"admin", "maintain", "write", "triage"}


class ReleaseGate:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        runner: Runner = subprocess.run,
        cache_path: str | Path | None = None,
        cache_max_age_seconds: int = DEFAULT_CACHE_MAX_AGE_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.runner = runner
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self.cache_max_age_seconds = max(1, int(cache_max_age_seconds))
        self.now = now

    @staticmethod
    def _remote_command(command: list[str]) -> bool:
        return bool(
            command
            and (
                command[0] == "gh"
                or command[:2] == ["git", "fetch"]
            )
        )

    @classmethod
    def _network_failure(cls, command: list[str], detail: str) -> bool:
        text = str(detail or "").lower()
        return cls._remote_command(command) and any(
            marker in text for marker in _NETWORK_FAILURE_MARKERS
        )

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
        except subprocess.TimeoutExpired as exc:
            if self._remote_command(command):
                raise ReleaseGateUnavailable(
                    "live release evidence is temporarily unavailable"
                ) from exc
            raise ReleaseGateError(f"{command[0]} timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseGateError(f"{command[0]} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            if self._network_failure(command, detail):
                raise ReleaseGateUnavailable(
                    "live release evidence is temporarily unavailable"
                )
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

    def _run_check_runs(self, endpoint: str) -> list[dict[str, Any]]:
        pages = self._run(
            ["gh", "api", "--paginate", "--slurp", endpoint],
            json_output=True,
        )
        if isinstance(pages, dict):
            pages = [pages]
        if not isinstance(pages, list):
            raise ReleaseGateError("check-runs response is invalid")
        runs: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            values = page.get("check_runs", [])
            if isinstance(values, list):
                runs.extend(row for row in values if isinstance(row, dict))
        return runs

    def _trusted_actor(
        self,
        record: dict[str, Any],
        repo: str,
        cache: dict[str, bool],
    ) -> bool:
        """Accept attestations only from repository-controlled identities."""
        actor = str((record.get("user") or {}).get("login") or "")
        if not actor:
            return False
        association = str(record.get("author_association") or "").upper()
        if association in _TRUSTED_ASSOCIATIONS:
            return True
        if actor in cache:
            return cache[actor]
        try:
            permission = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/collaborators/{quote(actor, safe='')}/permission",
                ],
                json_output=True,
            )
        except ReleaseGateError:
            cache[actor] = False
            return False
        level = str(permission.get("permission") or "").lower()
        role = str(permission.get("role_name") or "").lower()
        cache[actor] = bool(
            level in _TRUSTED_PERMISSIONS or role in _TRUSTED_PERMISSIONS
        )
        return cache[actor]

    def _admin_actor(
        self,
        record: dict[str, Any],
        repo: str,
        cache: dict[str, bool],
    ) -> bool:
        """Require authoritative admin permission for an owner release decision."""
        actor = str((record.get("user") or {}).get("login") or "")
        if not actor:
            return False
        if actor in cache:
            return cache[actor]
        try:
            permission = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/collaborators/{quote(actor, safe='')}/permission",
                ],
                json_output=True,
            )
        except ReleaseGateError:
            cache[actor] = False
            return False
        level = str(permission.get("permission") or "").lower()
        role = str(permission.get("role_name") or "").lower()
        cache[actor] = level == "admin" or role == "admin"
        return cache[actor]

    def _local_release_identity(self, *, fetch: bool) -> dict[str, str]:
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
        return {"repo": repo, "sha": sha}

    def verify(self, *, fetch: bool = True) -> dict[str, Any]:
        identity = self._local_release_identity(fetch=fetch)
        repo = identity["repo"]
        sha = identity["sha"]

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
        head_sha = str((merged.get("head") or {}).get("sha") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
            raise ReleaseGateError("merged PR is missing its final head revision")
        review_policy = protection.get("required_pull_request_reviews") or {}
        required_review_count = int(
            review_policy.get("required_approving_review_count") or 0
        )
        owner_release_allowed = bool(
            required_review_count == 0
            and not review_policy.get("require_code_owner_reviews")
            and not review_policy.get("require_last_push_approval")
        )

        runs = self._run_check_runs(
            f"repos/{repo}/commits/{sha}/check-runs"
        )
        combined_status = self._run(
            ["gh", "api", f"repos/{repo}/commits/{sha}/status"],
            json_output=True,
        )
        statuses = (
            combined_status.get("statuses", [])
            if isinstance(combined_status, dict)
            else []
        )
        configured_checks = [
            value
            for value in checks_policy.get("checks", [])
            if isinstance(value, dict) and value.get("context")
        ]
        configured_names = {
            str(value["context"]) for value in configured_checks
        }
        required_checks: set[tuple[str, int | None]] = {
            (
                str(value["context"]),
                (
                    int(value["app_id"])
                    if value.get("app_id") not in (None, -1)
                    else None
                ),
            )
            for value in configured_checks
        }
        required_checks.update(
            (str(value), None)
            for value in checks_policy.get("contexts", [])
            if value and str(value) not in configured_names
        )
        if not required_checks:
            raise ReleaseGateError(
                "main branch protection defines no required checks"
            )
        successful_checks = {
            (
                str(run.get("name") or ""),
                int((run.get("app") or {}).get("id"))
                if (run.get("app") or {}).get("id") is not None
                else None,
            )
            for run in runs
            if (
                run.get("status") == "completed"
                and str(run.get("conclusion") or "")
                in {"success", "neutral", "skipped"}
            )
        }
        successful_checks.update(
            (str(status.get("context") or ""), None)
            for status in statuses
            if (
                isinstance(status, dict)
                and str(status.get("state") or "") == "success"
            )
        )
        missing = [
            (context, app_id)
            for context, app_id in required_checks
            if (
                (context, app_id) not in successful_checks
                and not (
                    app_id is None
                    and any(
                        name == context
                        for name, _candidate_app in successful_checks
                    )
                )
            )
        ]
        if missing:
            raise ReleaseGateError(
                "required checks are not successful: "
                + ", ".join(
                    context if app_id is None else f"{context}@app:{app_id}"
                    for context, app_id in missing
                )
            )

        reviews = self._run_paginated(
            f"repos/{repo}/pulls/{number}/reviews"
        )
        comments = self._run_paginated(
            f"repos/{repo}/issues/{number}/comments"
        )
        evidence = []
        trust_cache: dict[str, bool] = {}
        admin_cache: dict[str, bool] = {}
        for review in reviews if isinstance(reviews, list) else []:
            reviewer = str((review.get("user") or {}).get("login") or "")
            state = str(review.get("state") or "").upper()
            body = str(review.get("body") or "")
            reviewed_sha = str(review.get("commit_id") or "")
            if (
                reviewer
                and reviewer != author
                and state == "APPROVED"
                and reviewed_sha == head_sha
                and self._trusted_actor(review, repo, trust_cache)
            ):
                evidence.append(f"review:{reviewer}:{state}")
            elif (
                reviewer
                and reviewer != author
                and state == "COMMENTED"
                and (
                    _has_pass_attestation(body, head_sha)
                    or _has_pass_attestation(body, sha)
                )
                and self._trusted_actor(review, repo, trust_cache)
            ):
                evidence.append(f"attestation:{reviewer}")
        owner_decisions = []
        for comment in comments if isinstance(comments, list) else []:
            reviewer = str((comment.get("user") or {}).get("login") or "")
            body = str(comment.get("body") or "").strip()
            if (
                reviewer
                and reviewer != author
                and (
                    _has_pass_attestation(body, head_sha)
                    or _has_pass_attestation(body, sha)
                )
                and self._trusted_actor(comment, repo, trust_cache)
            ):
                evidence.append(f"attestation:{reviewer}")
            reason = _owner_release_reason(body, sha)
            if (
                owner_release_allowed
                and reviewer == author
                and reason
                and _github_timestamp_after(
                    comment.get("created_at"),
                    merged.get("merged_at"),
                )
                and self._admin_actor(comment, repo, admin_cache)
            ):
                owner_decisions.append(
                    {
                        "actor": reviewer,
                        "reason": reason,
                    }
                )
        if not evidence and not owner_decisions:
            raise ReleaseGateError(
                "merged PR has no independent review evidence "
                "or valid owner release decision"
            )
        return {
            "ok": True,
            "repo": repo,
            "sha": sha,
            "pr_head_sha": head_sha,
            "pr": number,
            "required_checks": sorted(
                context if app_id is None else f"{context}@app:{app_id}"
                for context, app_id in required_checks
            ),
            "review_evidence": sorted(set(evidence)),
            "owner_release_decisions": owner_decisions,
            "approval_mode": (
                "independent_review" if evidence else "owner_release_decision"
            ),
            "branch_protection": {
                "admin_bypass": False,
                "strict_checks": True,
                "pull_request_required": True,
                "conversation_resolution": True,
                "required_approving_reviews": required_review_count,
                "owner_release_allowed": owner_release_allowed,
            },
        }

    def _write_cache(self, result: dict[str, Any], verified_epoch: float) -> bool:
        if self.cache_path is None:
            return False
        path = self.cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = {
                "schema": CACHE_SCHEMA,
                "verified_epoch": float(verified_epoch),
                "result": result,
            }
            fd, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            finally:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass
            return True
        except OSError:
            return False

    def _cached_result(self, identity: dict[str, str]) -> dict[str, Any]:
        path = self.cache_path
        if path is None or path.is_symlink():
            raise ReleaseGateError("no trustworthy cached release evidence")
        try:
            stat = path.stat()
            if hasattr(os, "getuid") and stat.st_uid != os.getuid():
                raise ReleaseGateError("cached release evidence has another owner")
            if stat.st_mode & 0o077:
                raise ReleaseGateError("cached release evidence permissions are unsafe")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ReleaseGateError:
            raise
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseGateError("cached release evidence is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
            raise ReleaseGateError("cached release evidence schema is invalid")
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ReleaseGateError("cached release evidence did not pass")
        if (
            str(result.get("repo") or "") != identity["repo"]
            or str(result.get("sha") or "") != identity["sha"]
        ):
            raise ReleaseGateError("cached release evidence is for another revision")
        required_checks = result.get("required_checks")
        branch = result.get("branch_protection")
        if not isinstance(required_checks, list) or not required_checks:
            raise ReleaseGateError("cached release evidence has no required checks")
        if not isinstance(branch, dict):
            raise ReleaseGateError("cached branch-protection evidence is invalid")
        if not all(
            branch.get(key) is expected
            for key, expected in (
                ("admin_bypass", False),
                ("strict_checks", True),
                ("pull_request_required", True),
                ("conversation_resolution", True),
            )
        ):
            raise ReleaseGateError("cached branch-protection evidence is incomplete")
        approval_mode = str(result.get("approval_mode") or "")
        if approval_mode == "independent_review":
            approval_present = bool(result.get("review_evidence"))
        elif approval_mode == "owner_release_decision":
            approval_present = bool(result.get("owner_release_decisions"))
        else:
            approval_present = False
        if not approval_present:
            raise ReleaseGateError("cached release approval evidence is incomplete")
        try:
            verified_epoch = float(payload["verified_epoch"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseGateError("cached release evidence has no timestamp") from exc
        age = float(self.now()) - verified_epoch
        if age < 0 or age > self.cache_max_age_seconds:
            raise ReleaseGateError("cached release evidence is stale")
        return {
            **result,
            "evidence_source": "cached_live_verification",
            "stale": True,
            "live_verified_epoch": verified_epoch,
            "cache_age_seconds": round(age, 3),
        }

    def verify_resilient(self, *, fetch: bool = True) -> dict[str, Any]:
        """Use a fresh exact-SHA live receipt only for transient network loss."""
        try:
            result = self.verify(fetch=fetch)
        except ReleaseGateUnavailable:
            identity = self._local_release_identity(fetch=False)
            return self._cached_result(identity)
        verified_epoch = float(self.now())
        live = {
            **result,
            "evidence_source": "github_live",
            "stale": False,
            "live_verified_epoch": verified_epoch,
        }
        live["cache_persisted"] = self._write_cache(live, verified_epoch)
        return live


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis production release gate")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)
    try:
        cache_path = os.environ.get(
            "JARVIS_RELEASE_GATE_CACHE",
            str(Path.home() / ".jarvis" / "release-gate-cache.json"),
        )
        result = ReleaseGate(cache_path=cache_path).verify_resilient(
            fetch=not args.no_fetch
        )
    except ReleaseGateError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
