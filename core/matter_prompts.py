"""Stable user-facing phrases shared by Matter entry points."""

from __future__ import annotations

from typing import Any


def continuation_prompt(matter: dict[str, Any]) -> str:
    """Return a resume phrase that does not depend on harness internals."""
    return f"继续 Jarvis 事项「{matter['title']}」"
