"""Regression gates for failure visibility on user-facing hot paths."""

from __future__ import annotations

import ast
from pathlib import Path

from core import attention_roi, delivery, memorial


ROOT = Path(__file__).resolve().parents[1]
HOT_PATHS = (
    "core/conversation_audit.py",
    "core/delivery.py",
    "core/memorial.py",
    "core/memorial_transport.py",
)
LOG_CALLS = {"_ops_log", "log"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def test_broad_hot_path_catches_are_visible_or_reraised():
    """A broad catch may fail open, but it may not hide the failure.

    ``_ops_log`` itself is deliberately fail-open: observability cannot abort
    delivery. Every other ``except Exception`` in these high-volume modules
    must either re-raise or emit structured operational evidence.
    """
    silent: list[str] = []
    for relative in HOT_PATHS:
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            if function.name == "_ops_log":
                continue
            for handler in (
                node for node in ast.walk(function)
                if isinstance(node, ast.ExceptHandler)
            ):
                if not isinstance(handler.type, ast.Name):
                    continue
                if handler.type.id != "Exception":
                    continue
                reraises = any(isinstance(node, ast.Raise)
                               for node in ast.walk(handler))
                logs = any(
                    isinstance(node, ast.Call) and _call_name(node) in LOG_CALLS
                    for node in ast.walk(handler)
                )
                if not reraises and not logs:
                    silent.append(f"{relative}:{handler.lineno}:{function.name}")

    assert silent == [], "silent broad catches:\n" + "\n".join(silent)


def test_attention_policy_failure_is_logged_before_fail_open(monkeypatch):
    events: list[tuple[str, dict]] = []

    def fail(*_args, **_kwargs):
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr(attention_roi, "class_for", fail)
    monkeypatch.setattr(
        memorial,
        "_ops_log",
        lambda message, **fields: events.append((message, fields)),
    )

    assert memorial._governed("routine:stretch", "notice") == "notice"
    assert events == [(
        "attention_policy_unavailable",
        {
            "level": "warn",
            "source": "routine:stretch",
            "error_type": "RuntimeError",
        },
    )]


def test_transport_exception_is_logged_without_payload(monkeypatch, tmp_path):
    events: list[tuple[str, dict]] = []

    def fail(*_args, **_kwargs):
        raise OSError("private payload must not enter logs")

    monkeypatch.setattr(delivery.subprocess, "run", fail)
    monkeypatch.setattr(
        delivery,
        "_ops_log",
        lambda message, **fields: events.append((message, fields)),
    )
    envelope = delivery.DeliveryEnvelope(
        source="test",
        payload={"text": "private payload must not enter logs"},
        chat_id="oc_test",
    ).normalized()

    result = delivery._default_transport(tmp_path)(envelope, "lark")

    assert result.ok is False
    assert events == [(
        "lark_transport_exception",
        {
            "level": "error",
            "delivery_id": envelope.id,
            "channel": "lark",
            "error_type": "OSError",
        },
    )]
    assert "private payload" not in repr(events)
