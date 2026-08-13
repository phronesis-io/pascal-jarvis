from __future__ import annotations

import json
from datetime import date

import pytest

from core.person_registry import (
    PersonAmbiguous,
    PersonNotFound,
    PersonRegistry,
    PersonRegistryInvalid,
)


def _write_registry(tmp_path, people):
    path = tmp_path / "data" / "person_registry.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"version": 1, "people": people}, ensure_ascii=False),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _partner():
    return {
        "person_id": "partner",
        "name": "Partner Name",
        "aliases": ["my partner", "家人"],
        "relationships": ["spouse"],
        "channels": {
            "lark": {
                "open_id": "ou_partner_verified",
                "chat_id": "oc_partner_verified",
                "name": "Partner Name",
                "verified_at": "2026-08-13",
            },
            "eigenflux": {
                "agent_id": "agent-partner",
                "agent_name": "Partner Agent",
                "verified_at": "2026-08-13",
            },
        },
        "boundaries": ["Work opinions remain independent."],
    }


def test_exact_relationship_resolves_person_and_channel(tmp_path):
    _write_registry(tmp_path, [_partner()])
    registry = PersonRegistry(root=tmp_path)

    person, lark = registry.resolve_channel(" MY-PARTNER ", "lark")

    assert person.person_id == "partner"
    assert lark["open_id"] == "ou_partner_verified"
    assert registry.resolve("spouse") == person
    with pytest.raises(PersonNotFound):
        registry.resolve("ou_partner_verified")


def test_prompt_projection_is_useful_but_hides_provider_ids(tmp_path):
    _write_registry(tmp_path, [_partner()])

    prompt = PersonRegistry(root=tmp_path).prompt_context()

    assert "Partner Name" in prompt
    assert "my partner" in prompt
    assert "Work opinions remain independent" in prompt
    assert "lark" in prompt and "eigenflux" in prompt
    assert "ou_partner_verified" not in prompt
    assert "agent-partner" not in prompt
    assert "oc_partner_verified" not in prompt


def test_missing_or_channel_less_person_fails_closed(tmp_path):
    person = _partner()
    person["channels"] = {}
    _write_registry(tmp_path, [person])
    registry = PersonRegistry(root=tmp_path)

    with pytest.raises(PersonNotFound, match="没有已验证"):
        registry.resolve_channel("spouse", "lark")
    with pytest.raises(PersonNotFound, match="没有"):
        registry.resolve("unknown")


def test_duplicate_relationship_alias_is_ambiguous(tmp_path):
    other = {
        "person_id": "other",
        "name": "Other Person",
        "aliases": ["my partner"],
        "channels": {},
    }
    _write_registry(tmp_path, [_partner(), other])

    with pytest.raises(PersonAmbiguous, match="绑定了多人"):
        PersonRegistry(root=tmp_path).people()


def test_duplicate_provider_identity_is_rejected(tmp_path):
    other = {
        "person_id": "other",
        "name": "Other Person",
        "channels": {
            "lark": {
                "open_id": "ou_partner_verified",
                "verified_at": "2026-08-13",
            },
        },
    }
    _write_registry(tmp_path, [_partner(), other])

    with pytest.raises(PersonRegistryInvalid, match="重复绑定"):
        PersonRegistry(root=tmp_path).people()


def test_declared_channel_requires_authoritative_identity(tmp_path):
    person = _partner()
    person["channels"]["lark"] = {"name": "Partner Name"}
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid, match="缺少 open_id"):
        PersonRegistry(root=tmp_path).people()


def test_private_registry_permissions_fail_closed(tmp_path):
    path = _write_registry(tmp_path, [_partner()])
    path.chmod(0o644)

    with pytest.raises(PersonRegistryInvalid, match="chmod 600"):
        PersonRegistry(root=tmp_path).people()


def test_unknown_fields_are_rejected_instead_of_hiding_secrets(tmp_path):
    person = _partner()
    person["token"] = "must-not-be-accepted"
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid, match="不支持的字段"):
        PersonRegistry(root=tmp_path).people()


@pytest.mark.parametrize(
    "bad_value",
    [
        "Never disclose ou_partner_verified",
        "ou_stale_provider_identity",
        "o u _ p a r t n e r _ v e r i f i e d",
        "中文ou_stale_provider_identity紧邻",
        "315009640322564096",
        "315 009 640 322 564 096",
    ],
)
def test_action_aliases_cannot_contain_provider_identity(tmp_path, bad_value):
    person = _partner()
    person["aliases"] = [bad_value]
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid, match="私密渠道身份"):
        PersonRegistry(root=tmp_path).people()


def test_prompt_boundary_cannot_reveal_provider_identity(tmp_path):
    person = _partner()
    person["boundaries"] = ["中文ou_partner_verified紧邻"]
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid, match="私密渠道身份"):
        PersonRegistry(root=tmp_path).people()


@pytest.mark.parametrize("verified_at", ["", "not-a-date", "2026-08-14"])
def test_channel_verification_date_is_required_and_not_future(tmp_path, verified_at):
    person = _partner()
    person["channels"]["lark"]["verified_at"] = verified_at
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid, match="verified_at"):
        PersonRegistry(root=tmp_path, today=date(2026, 8, 13)).people()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda person: person["channels"]["lark"].update(open_id="bad"),
        lambda person: person["channels"].update(secret={"token": "x"}),
        lambda person: person.update(person_id="Bad Person"),
    ],
)
def test_invalid_registry_never_becomes_action_authority(tmp_path, mutate):
    person = _partner()
    mutate(person)
    _write_registry(tmp_path, [person])

    with pytest.raises(PersonRegistryInvalid):
        PersonRegistry(root=tmp_path).people()


def test_missing_registry_is_an_empty_optional_configuration(tmp_path):
    registry = PersonRegistry(root=tmp_path)
    assert registry.people() == ()
    assert registry.prompt_context() == ""
