import io
import json

import pytest

from core import delegation_cli
from core.delegations import DelegationStore


def _prepared(tmp_path, monkeypatch):
    db_path = tmp_path / "jarvis.db"
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    store = DelegationStore(root=tmp_path, db_path=db_path)
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="cli-evidence",
        title="Verify",
        operation="message_send",
        target_type="agent",
        target_id="agent-1",
        expected_postcondition={"state": "sent"},
        authority="message_service",
        verification_policy={"verifier": "synthetic"},
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="synthetic",
        executor="worker",
    )
    return delegation, step


def test_worker_cli_rejects_caller_supplied_evidence_claims(
    tmp_path, monkeypatch, capsys,
):
    delegation, step = _prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "step_id": step["id"],
                    "strength": "strong",
                    "authority": "made_up",
                    "matched": True,
                }
            )
        ),
    )

    result = delegation_cli.main(["evidence", delegation["id"]])

    assert result == 2
    assert "accepts only step_id" in capsys.readouterr().out


def test_worker_cli_routes_evidence_through_registered_verifier(
    tmp_path, monkeypatch, capsys,
):
    delegation, step = _prepared(tmp_path, monkeypatch)
    called = {}

    def verified(delegation_id, step_id, *, store):
        called.update(
            delegation_id=delegation_id,
            step_id=step_id,
            db_path=store.db_path,
        )
        return {"matched": False, "delegation": {"status": "verifying"}}

    monkeypatch.setattr("core.delegation_verify.verify_step", verified)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"step_id": step["id"]}))
    )

    result = delegation_cli.main(["evidence", delegation["id"]])

    assert result == 0
    assert called["delegation_id"] == delegation["id"]
    assert called["step_id"] == step["id"]
    assert json.loads(capsys.readouterr().out)["matched"] is False


def test_worker_cli_has_no_owner_confirmation_command():
    with pytest.raises(SystemExit):
        delegation_cli.main(["confirm", "dlg_unsafe"])


def test_worker_cli_cannot_self_authorize_create(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "jarvis.db"))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "principal_id": "owner",
                    "source": "worker",
                    "source_ref": "unsafe-create",
                    "title": "Publish",
                    "operation": "public_publish",
                    "risk_tier": 3,
                    "target_type": "feed",
                    "target_id": "public",
                    "authority": "feed",
                    "verification_policy": {"verifier": "feed"},
                    "authorized": True,
                }
            )
        ),
    )

    assert delegation_cli.main(["create"]) == 2
    assert "cannot create an owner-authorized" in capsys.readouterr().out


def test_worker_cli_cannot_resolve_owner_verification_recovery(
    tmp_path, monkeypatch, capsys,
):
    delegation, step = _prepared(tmp_path, monkeypatch)
    store = DelegationStore()
    store.claim_step(
        delegation["id"], step["id"], expected_version=1, owner="worker"
    )
    store.record_attempt(
        delegation["id"],
        step["id"],
        expected_version=1,
        owner="worker",
        succeeded=True,
    )
    store.mark_waiting(
        delegation["id"],
        expected_version=1,
        waiting_on="verification_recovery",
        needs_user=True,
        reason_code="verification_budget_exhausted",
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"expected_version": 1, "actor_id": "owner"})),
    )

    assert delegation_cli.main(["retry", delegation["id"]]) == 2
    assert "owner recovery decision" in capsys.readouterr().out
    assert store.get(delegation["id"])["status"] == "needs_user"
