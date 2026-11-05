"""The explicit test backend must be ephemeral and expose no external tools."""

import json
from pathlib import Path
import subprocess

import pytest

pytest.importorskip("eigenflux_sdk")
from core import db as jarvis_db
from plugins.eigenflux.sdk_codex_model import DISABLED_FEATURES, run_codex_model


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DB_PATH", str(tmp_path / "data" / "jarvis.db"))
    monkeypatch.setattr(jarvis_db, "DB_PATH", tmp_path / "data" / "jarvis.db")
    monkeypatch.setattr(jarvis_db, "_connection", None)
    monkeypatch.setattr("core.model_control.resolve_codex_bin", lambda: "/fixture/codex")
    monkeypatch.setattr("plugins.eigenflux.sdk_codex_model.resolve_codex_bin", lambda: "/fixture/codex")
    yield tmp_path
    if jarvis_db._connection is not None:
        jarvis_db._connection.close()


def test_explicit_backend_uses_new_text_only_process(root):
    def runner(args, **kwargs):
        assert "--ephemeral" in args and "resume" not in args
        assert "--ignore-user-config" in args and "--ignore-rules" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert kwargs["cwd"] == str(root / "analysis-sandbox")
        for feature in DISABLED_FEATURES:
            assert any(args[i:i + 2] == ["--disable", feature] for i in range(len(args)))
        assert "mcp_servers={}" in args and 'web_search="disabled"' in args
        Path(args[args.index("-o") + 1]).write_text('{"answer":"synthetic"}')
        return subprocess.CompletedProcess(args, 0, json.dumps({"type": "item.completed", "item": {"type": "agent_message"}}), "")
    result = run_codex_model("Synthetic owner-requested analysis", root=root, task_id="sdk:fixture", runner=runner)
    assert result.status == "succeeded" and result.call_id
    assert not list((root / "analysis-sandbox").iterdir())


@pytest.mark.parametrize("kind", ["command_execution", "mcp_tool_call", "file_change", "future_tool"])
def test_tool_event_cannot_be_reported_as_success(root, kind):
    def runner(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_text('synthetic answer')
        return subprocess.CompletedProcess(args, 0, json.dumps({"type": "item.completed", "item": {"type": kind}}), "")
    result = run_codex_model("Synthetic analysis", root=root, task_id="sdk:fixture", runner=runner)
    assert result.status != "succeeded"


def test_transport_failure_does_not_claim_success(root):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "synthetic failure")
    result = run_codex_model("Synthetic analysis", root=root, task_id="sdk:fixture", runner=runner)
    assert result.status != "succeeded"
