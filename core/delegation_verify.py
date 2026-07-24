"""Authoritative read-back verifiers for Delegation steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.delegations import (
    DelegationError,
    DelegationStore,
    step_verification_policy,
)


@dataclass(frozen=True, slots=True)
class Verification:
    matched: bool
    authority: str
    resource_locator: str
    evidence_type: str
    strength: str
    expected_summary: str
    observed_summary: str
    observed_digest: str
    metadata: dict[str, Any]


class VerificationError(DelegationError):
    """The authority could not be read safely."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _subset(expected: Any, observed: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(observed, dict) and all(
            key in observed and _subset(value, observed[key])
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(observed, list) and all(
            any(_subset(wanted, actual) for actual in observed) for wanted in expected
        )
    return expected == observed


def _summary(value: Any, *, limit: int = 450) -> str:
    text = _canonical(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _run_json(
    command: list[str],
    *,
    cwd: Path,
    runner: Runner,
    timeout: int = 30,
) -> dict[str, Any]:
    try:
        result = runner(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise VerificationError(f"{command[0]} readback failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "readback failed").strip()
        raise VerificationError(detail[:300])
    try:
        value = json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VerificationError("authority returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise VerificationError("authority returned a non-object response")
    if value.get("code") not in (None, 0, "0"):
        raise VerificationError(str(value.get("msg") or "authority rejected readback")[:300])
    return value


class VerifierRegistry:
    """Run named verifiers with read-only, deterministic comparisons."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        db_path: str | Path | None = None,
        runner: Runner = subprocess.run,
    ):
        self.root = Path(
            root
            or os.environ.get("JARVIS_DIR")
            or Path(__file__).resolve().parent.parent
        ).resolve()
        self.db_path = Path(
            db_path
            or os.environ.get("JARVIS_DB_PATH")
            or self.root / "data" / "jarvis.db"
        )
        self.runner = runner
        self._registry = {
            "local_file": self._local_file,
            "git_commit": self._git_commit,
            "git_remote": self._git_remote,
            "runtime_deploy": self._runtime_deploy,
            "delivery": self._delivery,
            "eigenflux_message": self._eigenflux_message,
            "eigenflux_friend": self._eigenflux_friend,
            "lark_message": self._lark_message,
            "lark_calendar": self._lark_calendar,
            "lark_doc": self._lark_doc,
        }

    def verify(
        self,
        verifier: str,
        expected: dict[str, Any],
        policy: dict[str, Any],
    ) -> Verification:
        handler = self._registry.get(str(verifier))
        if handler is None:
            raise VerificationError(f"unknown verifier: {verifier}")
        return handler(expected, policy)

    def _result(
        self,
        *,
        authority: str,
        locator: str,
        expected: Any,
        observed: Any,
        metadata: dict[str, Any] | None = None,
        strength: str = "strong",
        evidence_type: str = "authoritative_readback",
    ) -> Verification:
        return Verification(
            matched=_subset(expected, observed),
            authority=authority,
            resource_locator=locator,
            evidence_type=evidence_type,
            strength=strength,
            expected_summary=_summary(expected),
            observed_summary=_summary(observed),
            observed_digest=_digest(observed),
            metadata=metadata or {},
        )

    def _repo(self, policy: dict[str, Any]) -> Path:
        path = Path(str(policy.get("repo") or self.root)).resolve()
        try:
            path.relative_to(self.root.parent)
        except ValueError as exc:
            raise VerificationError("repository path is outside the Jarvis workspace") from exc
        if not (path / ".git").exists():
            raise VerificationError("repository does not contain .git")
        return path

    def _git(self, repo: Path, *args: str) -> str:
        result = self.runner(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            raise VerificationError((result.stderr or "git readback failed").strip()[:300])
        return result.stdout.strip()

    def _local_file(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        raw = str(policy.get("path") or expected.get("path") or "")
        path = (self.root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise VerificationError("file verifier path is outside repository") from exc
        observed: dict[str, Any] = {"path": str(relative), "exists": path.is_file()}
        if path.is_file():
            data = path.read_bytes()
            observed["sha256"] = hashlib.sha256(data).hexdigest()
            observed["size"] = len(data)
        return self._result(
            authority="filesystem",
            locator=f"file:{relative}",
            expected=expected,
            observed=observed,
        )

    def _git_commit(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        repo = self._repo(policy)
        sha = str(policy.get("sha") or expected.get("sha") or "HEAD")
        resolved = self._git(repo, "rev-parse", f"{sha}^{{commit}}")
        paths = [
            line
            for line in self._git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", resolved
            ).splitlines()
            if line
        ]
        observed = {"sha": resolved, "paths": paths}
        return self._result(
            authority="git_object_database",
            locator=f"git:{repo.name}@{resolved}",
            expected=expected,
            observed=observed,
        )

    def _git_remote(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        repo = self._repo(policy)
        remote = str(policy.get("remote") or "origin")
        ref = str(policy.get("ref") or "refs/heads/main")
        if not re_safe_ref(ref) or not re_safe_ref(remote):
            raise VerificationError("unsafe git remote or ref")
        output = self._git(repo, "ls-remote", remote, ref)
        sha = output.split()[0] if output else ""
        observed = {"remote": remote, "ref": ref, "sha": sha}
        return self._result(
            authority="git_remote",
            locator=f"git-remote:{remote}:{ref}",
            expected=expected,
            observed=observed,
        )

    def _runtime_deploy(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        from core.components import check_components
        from core.deploy import revision_contains, verify_runtime

        required = [
            str(value) for value in policy.get("required_components", []) if value
        ]
        runtime = verify_runtime(
            root=self.root, db_path=self.db_path, required=required
        )
        component_rows = check_components(root=self.root)
        unhealthy = [
            str(row.get("name") or row.get("component") or "")
            for row in component_rows
            if isinstance(row, dict) and not row.get("ok", False)
        ]
        release_sha = str(policy.get("release_sha") or "").lower()
        resident_sha = str(runtime.get("git_head") or "").lower()
        contains_release = False
        if release_sha:
            try:
                contains_release = revision_contains(
                    release_sha,
                    resident_sha,
                    root=self.root,
                    runner=self.runner,
                )
            except (ValueError, RuntimeError) as exc:
                raise VerificationError(str(exc)) from exc
        observed = {
            "release_sha": release_sha if contains_release else "",
            "git_head": resident_sha,
            "runtime_ok": bool(runtime.get("ok")),
            "components_ok": not unhealthy,
            "unhealthy_components": unhealthy,
        }
        return self._result(
            authority="jarvis_runtime",
            locator=f"runtime:{observed['git_head']}",
            expected=expected,
            observed=observed,
            metadata={"runtime_issue_count": len(runtime.get("issues", []))},
        )

    def _delivery(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        from core.delivery import DeliveryPipeline

        delivery_id = str(policy.get("delivery_id") or expected.get("delivery_id") or "")
        if not delivery_id:
            raise VerificationError("delivery_id is required")
        row = DeliveryPipeline(self.root, db_path=self.db_path).get(delivery_id)
        if row is None:
            raise VerificationError("delivery receipt was not found")
        observed = {
            "delivery_id": row.get("id"),
            "state": row.get("state"),
            "channel": row.get("route_channel"),
            "message_id": row.get("message_id"),
            "memorial_id": row.get("memorial_id"),
        }
        return self._result(
            authority="delivery_store",
            locator=f"delivery:{delivery_id}",
            expected=expected,
            observed=observed,
        )

    def _db_row(self, query: str, values: tuple[Any, ...]) -> dict[str, Any]:
        try:
            db = sqlite3.connect(str(self.db_path), timeout=5)
            db.row_factory = sqlite3.Row
            row = db.execute(query, values).fetchone()
        except sqlite3.Error as exc:
            raise VerificationError(f"local authority read failed: {exc}") from exc
        finally:
            if "db" in locals():
                db.close()
        if row is None:
            raise VerificationError("authority row was not found")
        return dict(row)

    def _eigenflux_message(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        from core.eigenflux_messages import CliFailure, EigenFluxMessenger

        action_key = str(policy.get("idempotency_key") or "")
        msg_id = str(policy.get("msg_id") or expected.get("msg_id") or "")
        if action_key:
            row = self._db_row(
                "SELECT * FROM verified_external_actions WHERE idempotency_key=?",
                (action_key,),
            )
        elif msg_id:
            row = self._db_row(
                "SELECT * FROM verified_external_actions WHERE msg_id=?", (msg_id,)
            )
        else:
            raise VerificationError("message id or idempotency key is required")
        if action_key and str(row.get("state") or "") != "verified":
            try:
                EigenFluxMessenger(
                    root=self.root,
                    db_path=self.db_path,
                    runner=self.runner,
                ).reconcile_action(action_key)
            except CliFailure as exc:
                raise VerificationError(
                    f"EigenFlux history readback failed: {exc}"
                ) from exc
            row = self._db_row(
                "SELECT * FROM verified_external_actions WHERE idempotency_key=?",
                (action_key,),
            )
        observed = {
            key: row.get(key)
            for key in ("state", "target_id", "conv_id", "msg_id", "payload_hash")
        }
        return self._result(
            authority="eigenflux_message_history",
            locator=f"eigenflux-message:{observed.get('msg_id', '')}",
            expected=expected,
            observed=observed,
        )

    def _eigenflux_friend(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        target_id = str(policy.get("agent_id") or expected.get("agent_id") or "")
        if not target_id:
            raise VerificationError("agent_id is required")
        cursor = ""
        seen: set[str] = set()
        friend = None
        for _ in range(20):
            command = [
                "eigenflux",
                "relation",
                "friends",
                "--limit",
                "100",
            ]
            if cursor:
                command.extend(["--cursor", cursor])
            command.extend(["-f", "json", "--no-interactive"])
            payload = _run_json(
                command,
                cwd=self.root,
                runner=self.runner,
            )
            data = payload.get("data") if isinstance(
                payload.get("data"), dict
            ) else {}
            rows = payload.get("friends")
            if rows is None:
                rows = data.get("friends")
            friend = next(
                (
                    row
                    for row in (rows or [])
                    if isinstance(row, dict)
                    and str(row.get("agent_id") or "") == target_id
                ),
                None,
            )
            if friend is not None:
                break
            next_cursor = str(
                data.get("next_cursor")
                or data.get("nextCursor")
                or payload.get("next_cursor")
                or payload.get("nextCursor")
                or ""
            ).strip()
            if not next_cursor:
                break
            if next_cursor in seen:
                raise VerificationError(
                    "friend readback pagination cursor repeated"
                )
            seen.add(next_cursor)
            cursor = next_cursor
        else:
            raise VerificationError(
                "friend readback pagination exceeded 20 pages"
            )
        observed = {
            "agent_id": target_id,
            "relationship": "friend" if friend else "absent",
        }
        if friend:
            observed["agent_name"] = str(friend.get("agent_name") or "")
        return self._result(
            authority="eigenflux_relationship_service",
            locator=f"eigenflux-friend:{target_id}",
            expected=expected,
            observed=observed,
        )

    def _lark_api(
        self, path: str, expected: dict[str, Any], authority: str, locator: str
    ) -> Verification:
        if not path.startswith("/open-apis/") or any(
            marker in path for marker in ("\n", "\r", "?", "#")
        ):
            raise VerificationError("unsafe Lark readback path")
        payload = _run_json(
            ["lark-cli", "api", "GET", path],
            cwd=self.root,
            runner=self.runner,
        )
        observed = payload.get("data", payload)
        return self._result(
            authority=authority,
            locator=locator,
            expected=expected,
            observed=observed,
        )

    def _lark_message(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        message_id = str(policy.get("message_id") or expected.get("message_id") or "")
        if not re_safe_ref(message_id):
            raise VerificationError("safe message_id is required")
        return self._lark_api(
            f"/open-apis/im/v1/messages/{message_id}",
            expected,
            "lark_message_service",
            f"lark-message:{message_id}",
        )

    def _lark_calendar(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        calendar_id = str(policy.get("calendar_id") or "")
        event_id = str(policy.get("event_id") or expected.get("event_id") or "")
        if not re_safe_ref(calendar_id) or not re_safe_ref(event_id):
            raise VerificationError("safe calendar_id and event_id are required")
        return self._lark_api(
            f"/open-apis/calendar/v4/calendars/{calendar_id}/events/{event_id}",
            expected,
            "lark_calendar_service",
            f"lark-event:{calendar_id}:{event_id}",
        )

    def _lark_doc(
        self, expected: dict[str, Any], policy: dict[str, Any]
    ) -> Verification:
        document_id = str(policy.get("document_id") or expected.get("document_id") or "")
        if not re_safe_ref(document_id):
            raise VerificationError("safe document_id is required")
        return self._lark_api(
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
            expected,
            "lark_document_service",
            f"lark-doc:{document_id}",
        )


def re_safe_ref(value: str) -> bool:
    return bool(value and len(value) <= 500 and all(
        char.isalnum() or char in "._:/@+-" for char in value
    ))


def verify_step(
    delegation_id: str,
    step_id: str,
    *,
    store: DelegationStore | None = None,
    registry: VerifierRegistry | None = None,
    resume_external: bool = False,
) -> dict[str, Any]:
    store = store or DelegationStore()
    detail = store.get(delegation_id)
    step = next((row for row in detail["steps"] if row["id"] == step_id), None)
    if step is None:
        raise VerificationError("step is not in the current contract")
    policy = step_verification_policy(
        detail["verification_policy"], str(step["kind"])
    )
    verifier = str(policy.get("verifier") or step["kind"])
    registry = registry or VerifierRegistry(root=store.root, db_path=store.db_path)
    result = registry.verify(
        verifier, dict(detail["expected_postcondition"]), policy
    )
    evidence = store.record_evidence(
        delegation_id,
        step_id,
        expected_version=detail["contract_version"],
        evidence_type=result.evidence_type,
        strength=result.strength,
        authority=result.authority,
        resource_locator=result.resource_locator,
        observed_digest=result.observed_digest,
        expected_summary=result.expected_summary,
        observed_summary=result.observed_summary,
        matched=result.matched,
        privacy_class=detail["privacy_class"],
        metadata=result.metadata,
        actor_id=verifier,
        resume_external=resume_external,
    )
    return {
        "matched": result.matched,
        "evidence_id": evidence["id"],
        "delegation": store.get(delegation_id),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one Delegation step")
    parser.add_argument("delegation_id")
    parser.add_argument("step_id")
    args = parser.parse_args(argv)
    try:
        result = verify_step(args.delegation_id, args.step_id)
    except DelegationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["matched"] else 3


if __name__ == "__main__":
    sys.exit(main())
