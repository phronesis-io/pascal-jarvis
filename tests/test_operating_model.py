from __future__ import annotations

from core.operating_model import operating_model


def test_operating_model_answers_why_jarvis_exists_beyond_codex():
    model = operating_model()

    assert model["schema"] == "jarvis.operating-model.v1"
    assert model["default_entry"]["surface"] == "codex"
    reasons = {item["id"] for item in model["jarvis_is_needed_when"]}
    assert reasons == {
        "durable_continuity",
        "time_trigger",
        "material_external_change",
        "entrusted_async_result",
        "authority_and_closure",
        "retained_companion_rhythm",
    }
    assert model["quiet_is_healthy"] is True
    assert model["engagement_is_not_a_goal"] is True
    assert model["retained_rhythm_policy"] == {
        "configuration": "private jarvis.yaml retained_rhythms",
        "default": "disabled",
        "maximum_enabled": 2,
        "silence_creates_debt": False,
    }
    assert len(model["proactive_message_goals"]) == 5
    assert any("维持存在感" in item
               for item in model["jarvis_must_not_interrupt_for"])
    assert len(model["message_gate"]) == 5
    assert model["message_contract_fields"] == [
        "owner_need",
        "work_receipt",
        "why_now",
        "owner_action",
        "silence_cost",
    ]


def test_operating_model_callers_cannot_mutate_the_shared_contract():
    first = operating_model()
    first["default_entry"]["surface"] = "lark"
    first["jarvis_is_needed_when"].clear()

    second = operating_model()
    assert second["default_entry"]["surface"] == "codex"
    assert len(second["jarvis_is_needed_when"]) == 6
