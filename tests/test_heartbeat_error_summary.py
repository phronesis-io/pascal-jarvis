import json
from core.heartbeat_provider import error_summary


def test_cli_envelope_surfaces_the_cause_not_the_counters():
    """The real 2026-08-27 shape: cause last, ~500 chars of counters first."""
    payload = {
        "is_error": True, "duration_api_ms": 0, "num_turns": 1,
        "stop_reason": "stop_sequence", "session_id": "x" * 36,
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0,
                  "padding": "0" * 600},
        "subtype": "error_during_execution",
        "result": "Claude usage limit reached. Resets at 4pm.",
    }
    out = error_summary(json.dumps(payload))
    assert "usage limit reached" in out
    assert "error_during_execution" in out
    assert "padding" not in out


def test_plain_stderr_keeps_head_and_tail():
    text = "first line\n" + "x" * 4000 + "\nlast line"
    out = error_summary(text, limit=200)
    assert out.startswith("first line")
    assert out.endswith("last line")
    assert "chars omitted" in out


def test_short_text_is_untouched():
    assert error_summary("boom") == "boom"


def test_envelope_without_a_cause_falls_back_to_text():
    out = error_summary(json.dumps({"is_error": True, "usage": {}}), limit=50)
    assert "is_error" in out
