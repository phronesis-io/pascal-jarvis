import json
import subprocess

import pytest

from core.delegation_verify import (
    Verification,
    VerificationError,
    VerifierRegistry,
    verify_step,
)
from core.delegations import DelegationStore


def _completed_attempt(tmp_path, verifier, expected, policy):
    store = DelegationStore(
        root=tmp_path, db_path=tmp_path / "jarvis.db", now=lambda: 1000
    )
    delegation, _ = store.create(
        principal_id="owner",
        source="test",
        source_ref="msg-1",
        title="Verify action",
        operation="test",
        target_type="object",
        target_id="one",
        expected_postcondition=expected,
        authority="test_authority",
        verification_policy={"verifier": verifier, **policy},
        authorized=True,
    )
    step = store.add_step(
        delegation["id"],
        expected_version=1,
        sequence=1,
        kind="verify",
        executor="test",
    )
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
    return store, delegation, step


def test_local_file_verifier_matches_digest_and_size(tmp_path):
    path = tmp_path / "report.md"
    path.write_text("done", encoding="utf-8")
    registry = VerifierRegistry(root=tmp_path, db_path=tmp_path / "db")
    import hashlib

    result = registry.verify(
        "local_file",
        {
            "path": "report.md",
            "exists": True,
            "sha256": hashlib.sha256(b"done").hexdigest(),
            "size": 4,
        },
        {"path": "report.md"},
    )
    assert result.matched is True
    assert result.authority == "filesystem"


def test_local_file_verifier_rejects_path_escape(tmp_path):
    registry = VerifierRegistry(root=tmp_path, db_path=tmp_path / "db")
    with pytest.raises(VerificationError, match="outside"):
        registry.verify("local_file", {"exists": True}, {"path": "../secret"})


def test_lark_message_verifier_uses_read_only_api_path(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "code": 0,
                    "data": {"message_id": "om_123", "receive_id": "ou_456"},
                }
            ),
            "",
        )

    result = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "db", runner=runner
    ).verify(
        "lark_message",
        {"message_id": "om_123", "receive_id": "ou_456"},
        {"message_id": "om_123"},
    )
    assert result.matched is True
    assert calls == [
        ["lark-cli", "api", "GET", "/open-apis/im/v1/messages/om_123"]
    ]


def test_lark_verifier_rejects_path_injection(tmp_path):
    registry = VerifierRegistry(root=tmp_path, db_path=tmp_path / "db")
    with pytest.raises(VerificationError, match="safe message_id"):
        registry.verify(
            "lark_message",
            {"message_id": "x"},
            {"message_id": "x?authorization=secret"},
        )


def test_delivery_verifier_maps_persisted_fields_to_contract_names(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(
        "core.delivery.DeliveryPipeline.get",
        lambda _self, _delivery_id: {
            "id": "dlv_123",
            "state": "delivered",
            "route_channel": "lark",
            "message_id": "om_123",
            "memorial_id": "mem_123",
        },
    )

    result = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "db"
    ).verify(
        "delivery",
        {
            "delivery_id": "dlv_123",
            "state": "delivered",
            "channel": "lark",
        },
        {"delivery_id": "dlv_123"},
    )

    assert result.matched is True
    assert '"delivery_id":"dlv_123"' in result.observed_summary
    assert '"channel":"lark"' in result.observed_summary


def test_delivery_verifier_defers_missing_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "core.delivery.DeliveryPipeline.get",
        lambda _self, _delivery_id: None,
    )

    with pytest.raises(VerificationError, match="receipt was not found"):
        VerifierRegistry(
            root=tmp_path, db_path=tmp_path / "db"
        ).verify(
            "delivery",
            {"delivery_id": "dlv_missing"},
            {"delivery_id": "dlv_missing"},
        )


def test_eigenflux_friend_verifier_reads_relationship(tmp_path):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "code": 0,
                    "friends": [{"agent_id": "agent-1", "agent_name": "Friend"}],
                }
            ),
            "",
        )

    result = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "db", runner=runner
    ).verify(
        "eigenflux_friend",
        {"agent_id": "agent-1", "relationship": "friend"},
        {"agent_id": "agent-1"},
    )
    assert result.matched is True
    assert result.resource_locator == "eigenflux-friend:agent-1"


def test_eigenflux_friend_verifier_paginates_relationships(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "--cursor" not in command:
            payload = {"code": 0, "friends": [], "next_cursor": "next"}
        else:
            payload = {
                "code": 0,
                "friends": [{"agent_id": "agent-2", "agent_name": "Friend"}],
            }
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    result = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "db", runner=runner
    ).verify(
        "eigenflux_friend",
        {"agent_id": "agent-2", "relationship": "friend"},
        {"agent_id": "agent-2"},
    )

    assert result.matched is True
    assert calls[1][calls[1].index("--cursor") + 1] == "next"


def test_eigenflux_friend_absence_is_mismatch(tmp_path):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"code": 0, "friends": []}), ""
        )

    result = VerifierRegistry(
        root=tmp_path, db_path=tmp_path / "db", runner=runner
    ).verify(
        "eigenflux_friend",
        {"agent_id": "agent-1", "relationship": "friend"},
        {"agent_id": "agent-1"},
    )
    assert result.matched is False


def test_runtime_deploy_accepts_resident_descendant(
    tmp_path, monkeypatch,
):
    release_sha = "a" * 40
    resident_sha = "b" * 40
    calls = []
    component_calls = []

    monkeypatch.setattr(
        "core.deploy.verify_runtime",
        lambda **_kwargs: {
            "ok": True,
            "git_head": resident_sha,
            "issues": [],
        },
    )
    def check_components(**kwargs):
        component_calls.append(kwargs)
        return [{"name": "bot", "ok": True}]

    monkeypatch.setattr("core.components.check_components", check_components)

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = VerifierRegistry(
        root=tmp_path,
        db_path=tmp_path / "db",
        runner=runner,
    ).verify(
        "runtime_deploy",
        {
            "release_sha": release_sha,
            "runtime_ok": True,
            "components_ok": True,
        },
        {"release_sha": release_sha},
    )

    assert result.matched is True
    assert calls == [
        [
            "git",
            "merge-base",
            "--is-ancestor",
            release_sha,
            resident_sha,
        ]
    ]
    assert f'"release_sha":"{release_sha}"' in result.observed_summary
    assert f'"git_head":"{resident_sha}"' in result.observed_summary
    assert component_calls == [{"critical_only": True, "root": tmp_path}]


def test_verify_step_records_evidence_and_completes(tmp_path):
    store, delegation, step = _completed_attempt(
        tmp_path,
        "synthetic",
        {"status": "done"},
        {},
    )

    class Registry:
        def verify(self, verifier, expected, policy):
            assert verifier == "synthetic"
            return Verification(
                matched=True,
                authority="test_authority",
                resource_locator="test:1",
                evidence_type="readback",
                strength="strong",
                expected_summary='{"status":"done"}',
                observed_summary='{"status":"done"}',
                observed_digest="sha256:" + "a" * 64,
                metadata={},
            )

    result = verify_step(
        delegation["id"], step["id"], store=store, registry=Registry()
    )
    assert result["matched"] is True
    assert result["delegation"]["status"] == "completed"


def test_step_specific_verifier_is_trusted_for_that_step(tmp_path):
    store, delegation, step = _completed_attempt(
        tmp_path,
        "default_verifier",
        {"status": "done"},
        {
            "steps": {
                "verify": {
                    "verifier": "step_verifier",
                    "authority": "test_authority",
                }
            }
        },
    )

    class Registry:
        def verify(self, verifier, expected, policy):
            assert verifier == "step_verifier"
            return Verification(
                matched=True,
                authority="test_authority",
                resource_locator="test:step",
                evidence_type="readback",
                strength="strong",
                expected_summary='{"status":"done"}',
                observed_summary='{"status":"done"}',
                observed_digest="sha256:" + "b" * 64,
                metadata={},
            )

    result = verify_step(
        delegation["id"], step["id"], store=store, registry=Registry()
    )

    assert result["delegation"]["status"] == "completed"
    assert result["delegation"]["evidence"][0]["trusted"] == 1
    assert result["delegation"]["evidence"][0]["verifier_id"] == "step_verifier"


def test_unknown_verifier_fails_closed(tmp_path):
    registry = VerifierRegistry(root=tmp_path, db_path=tmp_path / "db")
    with pytest.raises(VerificationError, match="unknown verifier"):
        registry.verify("model_says_done", {}, {})
