"""Keep provider credentials inside Jarvis-controlled execution boundaries."""

from __future__ import annotations

import os
from collections.abc import Mapping


MODEL_CREDENTIAL_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_BACKUP_AUTH_TOKEN",
    "CLAUDE_BACKUP2_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "OPENAI_API_KEY_CONFIG",
})


def without_model_credentials(
    env: Mapping[str, str] | None = None,
    *,
    keep: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Copy an environment without credentials unrelated to its child."""
    clean = dict(os.environ if env is None else env)
    for name in MODEL_CREDENTIAL_ENV_NAMES - keep:
        clean.pop(name, None)
    return clean
