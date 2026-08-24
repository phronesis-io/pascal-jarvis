"""Intent model-envelope contract shared by prompts and reconciliation."""

from __future__ import annotations


ENVELOPE_SCHEMA_DOC = (
    '{"intents": {"<intent_id>": {"response": "<text>", '
    '"action": "notify|silent|chain|failed", "closure": {'
    '"parent": "<parent_id>", "outcome": "done|recorded|na", '
    '"result": "<one line>"}}}}'
)


def validate_envelope(
    data: object,
    expected_ids: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """Return covered IDs, missing IDs and bounded structural errors."""
    if not isinstance(data, dict):
        return [], list(expected_ids), ["envelope is not a dict"]
    intents = data.get("intents")
    if not isinstance(intents, dict):
        return [], list(expected_ids), ["envelope has no 'intents' dict"]
    errors = [
        f"{intent_id}: slot is not a dict"
        for intent_id, slot in intents.items()
        if not isinstance(slot, dict)
    ]
    covered = [
        str(intent_id)
        for intent_id, slot in intents.items()
        if isinstance(slot, dict)
    ]
    return covered, [item for item in expected_ids if item not in covered], errors
