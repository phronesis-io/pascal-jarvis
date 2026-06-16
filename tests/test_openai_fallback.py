from core import openai_fallback as of


def test_build_payload_marks_toolless_fallback():
    payload = of.build_payload("System rules", "hello", "gpt-test", 123)

    assert payload["model"] == "gpt-test"
    assert payload["input"] == "hello"
    assert payload["max_output_tokens"] == 123
    assert "OpenAI fallback" in payload["instructions"]
    assert "no local tools" in payload["instructions"]
    assert "System rules" in payload["instructions"]


def test_extract_text_prefers_output_text():
    assert of.extract_text({"output_text": "hi"}) == "hi"


def test_extract_text_from_responses_output_blocks():
    response = {
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "first"},
                    {"type": "output_text", "text": "second"},
                ],
            }
        ]
    }

    assert of.extract_text(response) == "first\nsecond"


def test_main_requires_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert of.main([]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_main_success_with_stubbed_call(monkeypatch, capsys, tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("be kind", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_USER_AGENT", "JarvisTest/1.0")
    monkeypatch.setattr("sys.stdin", type("In", (), {"read": lambda self: "hello"})())
    seen = {}

    def fake_call(payload, api_key, base_url, timeout, user_agent=""):
        seen["user_agent"] = user_agent
        return {"output_text": "reply"}

    monkeypatch.setattr(of, "call_openai", fake_call)

    assert of.main(["--system-prompt-file", str(prompt)]) == 0
    assert capsys.readouterr().out == "reply"
    assert seen["user_agent"] == "JarvisTest/1.0"
