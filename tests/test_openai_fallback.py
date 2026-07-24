import io
import shlex
import signal
import subprocess
import time

from core import openai_fallback as of


def test_build_payload_includes_system_prompt():
    payload = of.build_payload("System rules", "hello", "gpt-test", 123)

    assert payload["model"] == "gpt-test"
    assert payload["input"] == "hello"
    assert payload["max_output_tokens"] == 123
    assert "fallback" in payload["instructions"]
    assert "System rules" in payload["instructions"]
    assert "No local tools are available" in payload["instructions"]


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


def test_main_no_tools_mode(monkeypatch, capsys, tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("be kind", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_USER_AGENT", "JarvisTest/1.0")
    monkeypatch.setattr("sys.stdin", type("In", (), {"read": lambda self: "hello"})())
    seen = {}

    def fake_call(payload, api_key, base_url, timeout, user_agent=""):
        seen["user_agent"] = user_agent
        return {"output_text": "reply"}

    monkeypatch.setattr(of, "_api_call", fake_call)

    assert of.main(["--system-prompt-file", str(prompt), "--no-tools"]) == 0
    assert capsys.readouterr().out == "reply"
    assert seen["user_agent"] == "JarvisTest/1.0"


def test_main_agentic_mode_no_tool_calls(monkeypatch, capsys):
    """When model returns text without tool calls, output it directly."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("sys.stdin", type("In", (), {"read": lambda self: "hi"})())

    def fake_call(payload, api_key, base_url, timeout, user_agent=""):
        return {"output_text": "direct reply", "output": []}

    monkeypatch.setattr(of, "_api_call", fake_call)

    assert of.main([]) == 0
    assert capsys.readouterr().out == "direct reply"


def test_agentic_tool_loop(monkeypatch):
    """Tool calls are executed and results fed back until text response."""
    call_count = [0]
    payloads = []

    def fake_call(payload, api_key, base_url, timeout, user_agent=""):
        payloads.append(payload)
        call_count[0] += 1
        if call_count[0] == 1:
            return {
                "id": "resp_1",
                "output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": '{"command": "echo hello"}',
                }],
            }
        return {"output_text": "done after tool", "output": []}

    monkeypatch.setattr(of, "_api_call", fake_call)

    result = of.run_agentic("sys", "do it", "gpt-test", 4096,
                            "sk-test", "http://fake", 30)
    assert result == "done after tool"
    assert call_count[0] == 2
    continuation = payloads[1]
    assert "previous_response_id" not in continuation
    assert continuation["input"][0] == {"role": "user", "content": "do it"}
    assert continuation["input"][1]["type"] == "function_call"
    assert continuation["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "hello\n",
    }


def test_agentic_rounds_share_one_deadline(monkeypatch):
    clock = [100.0]
    api_timeouts = []
    tool_timeouts = []

    def fake_call(payload, api_key, base_url, timeout, user_agent=""):
        api_timeouts.append(timeout)
        clock[0] += 3
        if len(api_timeouts) == 1:
            return {
                "output": [{
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "bash",
                    "arguments": '{"command":"echo hi"}',
                }]
            }
        return {"output_text": "done", "output": []}

    def fake_tool(name, arguments, *, timeout=None, **_kwargs):
        tool_timeouts.append(timeout)
        clock[0] += 4
        return "ok"

    monkeypatch.setattr(of.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(of, "_api_call", fake_call)
    monkeypatch.setattr(of, "execute_tool", fake_tool)

    result = of.run_agentic(
        "sys", "do it", "gpt-test", 4096,
        "sk-test", "http://fake", 10,
    )

    assert result == "done"
    assert api_timeouts == [10, 3]
    assert tool_timeouts == [7]


def test_execute_tool_bash(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    result = of.execute_tool("bash", {"command": "echo works"})
    assert "works" in result


def test_execute_tool_bash_exposes_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    result = of.execute_tool(
        "bash", {"command": "printf 'usage text'; exit 2"})

    assert "usage text" in result
    assert "(exit 2)" in result


def test_execute_tool_timeout_kills_descendant_process_group(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    marker = tmp_path / "descendant-finished"
    command = (
        "python3 -c "
        + shlex.quote(
            "import pathlib,time;"
            "time.sleep(0.5);"
            f"pathlib.Path({str(marker)!r}).write_text('alive')"
        )
        + " & wait"
    )

    result = of.execute_tool("bash", {"command": command}, timeout=0.1)
    time.sleep(0.7)

    assert "timed out" in result
    assert not marker.exists()


def test_execute_tool_cancellation_kills_descendant_process_group(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    marker = tmp_path / "cancelled-descendant-finished"
    checks = [False, True]
    command = (
        "python3 -c "
        + shlex.quote(
            "import pathlib,time;"
            "time.sleep(0.5);"
            f"pathlib.Path({str(marker)!r}).write_text('alive')"
        )
        + " & wait"
    )

    result = of.execute_tool(
        "bash",
        {"command": command},
        timeout=2,
        cancelled=lambda: checks.pop(0) if checks else True,
    )
    time.sleep(0.7)

    assert "cancelled" in result
    assert not marker.exists()


def test_cli_signal_during_tool_spawn_reaps_new_process_group(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("sys.stdin", io.StringIO("owner task"))
    calls = [0]

    def fake_api(*_args, **_kwargs):
        calls[0] += 1
        return {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-1",
                    "name": "bash",
                    "arguments": '{"command":"sleep 30"}',
                }
            ]
        }

    real_popen = subprocess.Popen
    spawned = []

    def signal_during_spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return process

    monkeypatch.setattr(of, "_api_call", fake_api)
    monkeypatch.setattr(of.subprocess, "Popen", signal_during_spawn)

    assert of.main([]) == 128 + signal.SIGTERM
    assert calls[0] == 1
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


def test_execute_tool_file_read_write(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    of.execute_tool("file_write", {"path": "test.txt", "content": "hello"})
    result = of.execute_tool("file_read", {"path": "test.txt"})
    assert "hello" in result


def test_execute_tool_unknown():
    result = of.execute_tool("nonexistent", {})
    assert "unknown tool" in result
