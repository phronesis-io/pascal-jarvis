import io
import json

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
