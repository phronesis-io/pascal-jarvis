import json

from core.prompt_experiments import choose_variant, inject_variant


def test_choose_variant_missing_or_bad_config_is_noop(tmp_path):
    assert choose_variant(tmp_path, "checkin") is None
    system = tmp_path / "system"
    system.mkdir()
    (system / "prompt_experiments.json").write_text("{bad json")
    assert choose_variant(tmp_path, "checkin") is None


def test_choose_variant_uses_enabled_task_experiment(tmp_path):
    system = tmp_path / "system"
    system.mkdir()
    (system / "prompt_experiments.json").write_text(json.dumps({
        "experiments": [
            {
                "id": "other",
                "task": "content-recommend",
                "enabled": True,
                "variants": [{"id": "x", "instruction": "wrong"}],
            },
            {
                "id": "checkin-v1",
                "task": "checkin",
                "enabled": True,
                "variants": [{"id": "choice", "instruction": "Use choices."}],
            },
        ]
    }))

    variant = choose_variant(tmp_path, "checkin", now=1_779_000_000)

    assert variant is not None
    assert variant.experiment_id == "checkin-v1"
    assert variant.variant_id == "choice"
    assert variant.instruction == "Use choices."


def test_inject_variant_is_soft_and_hidden_from_user():
    system = inject_variant("Base prompt.", None)
    assert system == "Base prompt."

    class _Variant:
        experiment_id = "exp"
        variant_id = "v"
        instruction = "Ask one concrete question."

    prompt = inject_variant("Base prompt.", _Variant())
    assert "Base prompt." in prompt
    assert "[Prompt experiment]" in prompt
    assert "Do not mention the experiment to the owner" in prompt
    assert "Ask one concrete question." in prompt
