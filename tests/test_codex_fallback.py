"""Codex CLI fallback and conversation continuity."""

from __future__ import annotations

import json
import io
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import dashboard.db as db_module
from core import codex_fallback as cf


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_resolve_codex_bin_accepts_explicit_binary(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    assert cf.resolve_codex_bin(str(binary)) == str(binary)


def test_new_command_uses_workspace_review_without_sandbox_bypass(tmp_path):
    command = cf.build_command(
        "/opt/codex", model="gpt-test", work_dir=tmp_path,
        output_file=tmp_path / "answer.txt", thread_id="", allow_tools=True,
    )

    assert command[:2] == ["/opt/codex", "exec"]
    assert "--approve-for-me" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("-C") + 1] == str(tmp_path)


def test_resume_command_inherits_the_original_sandbox(tmp_path):
    command = cf.build_command(
        "/opt/codex", model="gpt-test", work_dir=tmp_path,
        output_file=tmp_path / "answer.txt", thread_id="thread-1",
        allow_tools=True,
    )

    assert command[:3] == ["/opt/codex", "exec", "resume"]
    assert command[-2:] == ["thread-1", "-"]
    assert "--approve-for-me" not in command
    assert "-C" not in command


def test_parse_thread_id_ignores_non_json_noise():
    output = "\n".join([
        "advisory warning",
        json.dumps({"type": "thread.started", "thread_id": "thread-7"}),
        json.dumps({"type": "turn.completed"}),
    ])

    assert cf.parse_thread_id(output) == "thread-7"


def test_run_reuses_thread_for_the_same_lark_conversation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cf, "resolve_codex_bin", lambda configured="": "/opt/codex")
    monkeypatch.setattr(cf, "ensure_codex_authenticated", lambda binary: None)

    def invoke(**kwargs):
        calls.append(kwargs)
        return cf.CliResult(
            text=f"answer-{len(calls)}",
            thread_id=kwargs.get("thread_id") or "thread-persisted",
        )

    monkeypatch.setattr(cf, "invoke_codex", invoke)
    common = dict(
        conv_key="ou_owner", system_prompt="system", model="gpt-test",
        timeout=10, work_dir=tmp_path, binary="/opt/codex",
    )

    assert cf.run_fallback(content="first", **common) == "answer-1"
    assert cf.run_fallback(content="second", **common) == "answer-2"
    assert calls[0]["thread_id"] == ""
    assert calls[1]["thread_id"] == "thread-persisted"
    assert "second" in calls[1]["prompt"]


def test_same_matter_codex_thread_is_serialized_across_transports(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cf, "resolve_codex_bin", lambda configured="": "/opt/codex")
    monkeypatch.setattr(cf, "ensure_codex_authenticated", lambda binary: None)
    guard = threading.Lock()
    active = 0
    maximum = 0
    seen_threads = []

    def invoke(**kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
            seen_threads.append(kwargs["thread_id"])
            call_number = len(seen_threads)
        time.sleep(0.08)
        with guard:
            active -= 1
        return cf.CliResult(
            text="ok", thread_id=kwargs["thread_id"] or f"matter-thread-{call_number}")

    monkeypatch.setattr(cf, "invoke_codex", invoke)

    def run(conv_key):
        return cf.run_fallback(
            content="continue", conv_key=conv_key, context_key="matter:shared",
            system_prompt="context", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ("lark-owner", "mobile-owner"))) == ["ok", "ok"]

    assert maximum == 1
    assert seen_threads == ["", "matter-thread-1"]


def test_missing_saved_thread_is_rebuilt_once(tmp_path, monkeypatch):
    cf.save_session("ou_owner", "stale-thread", "gpt-test", str(tmp_path))
    calls = []
    monkeypatch.setattr(cf, "resolve_codex_bin", lambda configured="": "/opt/codex")
    monkeypatch.setattr(cf, "ensure_codex_authenticated", lambda binary: None)

    def invoke(**kwargs):
        calls.append(kwargs.get("thread_id"))
        if kwargs.get("thread_id"):
            raise cf.StaleSessionError("thread not found")
        return cf.CliResult(text="recovered", thread_id="fresh-thread")

    monkeypatch.setattr(cf, "invoke_codex", invoke)

    answer = cf.run_fallback(
        content="hello", conv_key="ou_owner", system_prompt="system",
        model="gpt-test", timeout=10, work_dir=tmp_path,
        binary="/opt/codex",
    )

    assert answer == "recovered"
    assert calls == ["stale-thread", ""]
    assert cf.load_session("ou_owner")["thread_id"] == "fresh-thread"


def test_uncertain_failure_is_never_replayed_in_a_fresh_thread(
        tmp_path, monkeypatch):
    cf.save_session("ou_owner", "existing-thread", "gpt-test", str(tmp_path))
    calls = []
    monkeypatch.setattr(cf, "resolve_codex_bin", lambda configured="": "/opt/codex")
    monkeypatch.setattr(cf, "ensure_codex_authenticated", lambda binary: None)

    def invoke(**kwargs):
        calls.append(kwargs.get("thread_id"))
        raise cf.CodexFallbackError("connection closed after execution")

    monkeypatch.setattr(cf, "invoke_codex", invoke)

    with pytest.raises(cf.CodexFallbackError):
        cf.run_fallback(
            content="mutating request", conv_key="ou_owner",
            system_prompt="system", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex",
        )

    assert calls == ["existing-thread"]


def test_invoke_reads_final_file_and_thread_event(tmp_path, monkeypatch):
    class FakeProcess:
        pid = 12345
        returncode = 0

        def communicate(self, input, timeout):
            output_path = Path(seen["command"][seen["command"].index("-o") + 1])
            output_path.write_text("CODEX ANSWER", encoding="utf-8")
            return (
                json.dumps({"type": "thread.started", "thread_id": "thread-9"}),
                "",
            )

    seen = {}

    def popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)

    result = cf.invoke_codex(
        prompt="hello", thread_id="", model="gpt-test", timeout=10,
        work_dir=tmp_path, binary="/opt/codex", allow_tools=True,
    )

    assert result == cf.CliResult(text="CODEX ANSWER", thread_id="thread-9")
    assert seen["kwargs"]["start_new_session"] is True


def test_login_preflight_classifies_missing_login_as_safe_unavailability(
        monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, stdout="Not logged in\n", stderr=""
        ),
    )

    with pytest.raises(cf.CodexUnavailableError, match="not logged in"):
        cf.ensure_codex_authenticated("/opt/codex")


def test_preturn_401_is_safe_but_post_tool_401_is_uncertain():
    safe_stdout = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "error", "message": "401 Unauthorized"}),
        json.dumps({"type": "turn.failed", "error": {"message": "401"}}),
    ])
    unsafe_stdout = "\n".join([
        safe_stdout,
        json.dumps({
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "touch proof"},
        }),
    ])

    assert cf._is_preturn_auth_failure(safe_stdout, "401 Unauthorized") is True
    assert cf._is_preturn_auth_failure(unsafe_stdout, "401 Unauthorized") is False


def test_invoke_classifies_preturn_auth_failure_as_unavailable(
        tmp_path, monkeypatch):
    class FakeProcess:
        pid = 12345
        returncode = 1

        def communicate(self, input, timeout):
            return (
                json.dumps({"type": "turn.failed", "error": {}}),
                "401 Unauthorized: Missing bearer or basic authentication",
            )

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(cf.CodexUnavailableError):
        cf.invoke_codex(
            prompt="hello", thread_id="", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex", allow_tools=True,
        )


def test_cli_main_refuses_empty_conversation_key(capsys):
    assert cf.main(["--conv-key", "", "--work-dir", "/tmp"]) == 2
    assert "conv-key" in capsys.readouterr().err


def test_cli_exit_codes_distinguish_safe_unavailability_from_uncertain_failure(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello"))
    monkeypatch.setattr(
        cf, "run_fallback",
        lambda **kwargs: (_ for _ in ()).throw(
            cf.CodexUnavailableError("not installed")),
    )
    assert cf.main([
        "--conv-key", "ou_owner", "--work-dir", str(tmp_path),
    ]) == 75

    monkeypatch.setattr(sys, "stdin", io.StringIO("hello"))
    monkeypatch.setattr(
        cf, "run_fallback",
        lambda **kwargs: (_ for _ in ()).throw(
            cf.CodexFallbackError("interrupted after tool use")),
    )
    assert cf.main([
        "--conv-key", "ou_owner", "--work-dir", str(tmp_path),
    ]) == 74


def test_usage_limit_before_any_executable_item_is_safe_unavailability(
        tmp_path, monkeypatch):
    class FakeProcess:
        returncode = 1

        def communicate(self, input, timeout):
            return (
                "\n".join([
                    json.dumps({
                        "type": "thread.started", "thread_id": "thread-limit",
                    }),
                    json.dumps({"type": "turn.started"}),
                    json.dumps({
                        "type": "error",
                        "message": "You've hit your usage limit. Try again Aug 18.",
                    }),
                    json.dumps({
                        "type": "turn.failed",
                        "error": {
                            "message": "You've hit your usage limit. Try again Aug 18."
                        },
                    }),
                ]),
                "WARN rollout state discrepancy: falling_back",
            )

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(cf.CodexUnavailableError, match="usage limit"):
        cf.invoke_codex(
            prompt="hello", thread_id="", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex", allow_tools=True,
        )


def test_usage_limit_after_executable_item_remains_uncertain(
        tmp_path, monkeypatch):
    class FakeProcess:
        returncode = 1

        def communicate(self, input, timeout):
            return (
                "\n".join([
                    json.dumps({
                        "type": "thread.started", "thread_id": "thread-limit",
                    }),
                    json.dumps({"type": "turn.started"}),
                    json.dumps({
                        "type": "item.completed",
                        "item": {
                            "type": "command_execution",
                            "command": "touch /tmp/example",
                        },
                    }),
                    json.dumps({
                        "type": "turn.failed",
                        "error": {"message": "You've hit your usage limit."},
                    }),
                ]),
                "",
            )

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(cf.CodexFallbackError, match="usage limit") as exc:
        cf.invoke_codex(
            prompt="hello", thread_id="", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex", allow_tools=True,
        )
    assert not isinstance(exc.value, cf.CodexUnavailableError)


@pytest.mark.parametrize("stdout", [
    "",
    "not-json",
    json.dumps({"type": "turn.started"}) + "\ntruncated-event",
])
def test_usage_limit_without_complete_structured_terminal_evidence_is_uncertain(
        tmp_path, monkeypatch, stdout):
    class FakeProcess:
        returncode = 1

        def communicate(self, input, timeout):
            return stdout, "You've hit your usage limit. Try again Aug 18."

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(cf.CodexFallbackError, match="usage limit") as exc:
        cf.invoke_codex(
            prompt="hello", thread_id="", model="gpt-test", timeout=10,
            work_dir=tmp_path, binary="/opt/codex", allow_tools=True,
        )
    assert not isinstance(exc.value, cf.CodexUnavailableError)


def test_terminal_error_parser_ignores_non_object_json_events():
    stdout = "\n".join([
        json.dumps(["not", "an", "event"]),
        json.dumps({
            "type": "turn.failed",
            "error": {"message": "You've hit your usage limit."},
        }),
    ])

    assert cf.parse_terminal_error(stdout) == "You've hit your usage limit."
