"""Deterministic artifact and external-effect evidence for Matter Runs."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


QUALIFYING_STRENGTHS = {"strong", "corroborated", "user_attested"}


class EvidenceValidationError(RuntimeError):
    """A claimed run result cannot be proven by the allowed authorities."""


def _verified_git_deletion(workspace: Path, relative: Path) -> bool:
    if not (workspace / ".git").exists():
        return False
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--", relative.as_posix()],
            cwd=workspace,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(
        result.returncode == 0
        and result.stdout
        and b"D" in result.stdout[:2]
    )


def verify_artifacts(
    workspace_value: str | Path, artifacts: list[str] | None
) -> list[dict[str, Any]]:
    """Hash present files or prove tracked deletions inside one workspace."""
    workspace = Path(workspace_value).resolve()
    verified = []
    for raw in artifacts or []:
        if len(verified) >= 100:
            raise EvidenceValidationError("artifact list exceeds 100 files")
        candidate = Path(str(raw))
        path = (
            (workspace / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
        try:
            relative = path.relative_to(workspace)
        except ValueError as exc:
            raise EvidenceValidationError(
                "artifact is outside the run workspace"
            ) from exc
        if not path.is_file() and _verified_git_deletion(workspace, relative):
            verified.append({
                "path": relative.as_posix(),
                "state": "deleted",
                "sha256": "",
                "size": 0,
            })
            continue
        if not path.is_file():
            raise EvidenceValidationError(f"artifact does not exist: {relative}")
        data = path.read_bytes()
        verified.append({
            "path": relative.as_posix(),
            "state": "present",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        })
    return sorted(verified, key=lambda item: item["path"])


def verify_effects(
    *,
    workspace: str | Path,
    matter_id: str,
    effects: list[dict[str, str]] | None,
    epoch: float,
) -> list[dict[str, str]]:
    """Resolve effect references against current trusted Delegation evidence."""
    if not effects:
        return []
    from core.db import _db_path
    from core.delegations import DelegationError, DelegationStore

    store = DelegationStore(db_path=_db_path(), root=workspace)
    verified = []
    for ref in effects:
        if not isinstance(ref, dict):
            raise EvidenceValidationError(
                "effect must reference Delegation evidence"
            )
        delegation_id = str(ref.get("delegation_id") or "")
        evidence_id = str(ref.get("evidence_id") or "")
        if not delegation_id or not evidence_id:
            raise EvidenceValidationError("effect evidence reference is incomplete")
        try:
            detail = store.get(delegation_id)
        except (DelegationError, KeyError) as exc:
            raise EvidenceValidationError("effect evidence was not found") from exc
        if str(detail.get("matter_id") or "") != str(matter_id):
            raise EvidenceValidationError(
                "effect evidence belongs to another Matter"
            )
        evidence = next(
            (
                item for item in detail.get("evidence", [])
                if item.get("id") == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise EvidenceValidationError("effect evidence was not found")
        step = next(
            (
                item for item in detail.get("steps", [])
                if item.get("id") == evidence.get("step_id")
            ),
            None,
        )
        current = int(evidence.get("contract_version") or 0) == int(
            detail.get("contract_version") or 0
        )
        unexpired = (
            evidence.get("expires_at") is None
            or float(evidence["expires_at"]) > epoch
        )
        qualifies = bool(
            current
            and unexpired
            and evidence.get("matched")
            and evidence.get("trusted")
            and evidence.get("strength") in QUALIFYING_STRENGTHS
            and step
            and step.get("status") == "completed"
        )
        if not qualifies:
            raise EvidenceValidationError("effect evidence is not qualifying")
        verified.append({
            "delegation_id": delegation_id,
            "evidence_id": evidence_id,
            "authority": str(evidence.get("authority") or ""),
            "resource_locator": str(evidence.get("resource_locator") or ""),
            "observed_digest": str(evidence.get("observed_digest") or ""),
        })
    return sorted(
        verified, key=lambda item: (item["delegation_id"], item["evidence_id"])
    )
