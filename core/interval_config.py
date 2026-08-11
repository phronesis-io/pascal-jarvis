"""Shared validation for heartbeat interval override state."""

from __future__ import annotations


def parse_interval_overrides(value: object) -> dict[str, int]:
    """Return positive interval overrides, rejecting malformed files whole."""
    if not isinstance(value, dict):
        return {}

    parsed: dict[str, int] = {}
    try:
        for key, item in value.items():
            interval = int(item)
            if interval > 0:
                parsed[key] = interval
    except (OverflowError, TypeError, ValueError):
        return {}
    return parsed
