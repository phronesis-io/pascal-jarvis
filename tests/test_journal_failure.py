"""Regression tests for core/journal.py failure-path observability.

The 7/7 loud-failure fix (f4a9ece) shipped _warn referencing sys.stderr
without importing sys: every failure branch raised NameError out of
append_entry's "NEVER raised" contract, and both callers swallow exceptions
(journal_capture / daily_reflect_post) — re-creating the 6/20 17-day silent
journal failure the fix existed to close. These tests drive append_entry
end-to-end with lark-cli mocked out (never invoke the real binary — a bogus
token would still hit live Feishu auth) and pin BOTH halves of the contract:
returns False AND the FAILED line actually reaches stderr.
"""

import json
import types

import pytest

import core.journal as journal


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def fake_token(monkeypatch):
    """Never read the real jarvis.yaml doc token."""
    monkeypatch.setattr(journal, "_doc_token", lambda: "docxFAKETOKEN")


def _patch_run(monkeypatch, proc=None, exc=None):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "lark-cli"
        if exc is not None:
            raise exc
        return proc
    # Shim only journal's view of subprocess — the real module stays intact
    # for conftest guards and other tests in the same process.
    monkeypatch.setattr(journal, "subprocess", types.SimpleNamespace(run=fake_run))


def test_nonzero_exit_returns_false_and_warns_on_stderr(fake_token, monkeypatch, capsys):
    _patch_run(monkeypatch, proc=_FakeProc(returncode=1, stderr="token expired"))
    assert journal.append_entry("今日复盘正文") is False
    err = capsys.readouterr().err
    assert "[journal] append FAILED" in err
    assert "exited 1" in err


def test_garbage_stdout_returns_false_and_warns(fake_token, monkeypatch, capsys):
    _patch_run(monkeypatch, proc=_FakeProc(returncode=0, stdout="not json at all"))
    assert journal.append_entry("今日复盘正文") is False
    assert "[journal] append FAILED" in capsys.readouterr().err


def test_rejected_response_returns_false_and_warns(fake_token, monkeypatch, capsys):
    body = json.dumps({"ok": False, "error": {"code": 99991663}})
    _patch_run(monkeypatch, proc=_FakeProc(returncode=0, stdout=body))
    assert journal.append_entry("今日复盘正文") is False
    assert "[journal] append FAILED" in capsys.readouterr().err


def test_subprocess_exception_returns_false_and_warns(fake_token, monkeypatch, capsys):
    _patch_run(monkeypatch, exc=OSError("lark-cli not found"))
    assert journal.append_entry("今日复盘正文") is False
    assert "[journal] append FAILED" in capsys.readouterr().err


def test_success_path_returns_true_and_stays_quiet(fake_token, monkeypatch, capsys):
    # lark-cli may prefix non-JSON status lines before the payload.
    body = "updating doc...\n" + json.dumps({"ok": True})
    _patch_run(monkeypatch, proc=_FakeProc(returncode=0, stdout=body))
    assert journal.append_entry("今日复盘正文") is True
    assert capsys.readouterr().err == ""
