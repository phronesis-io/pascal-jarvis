"""Shared validation and policy for heartbeat interval override state."""

from __future__ import annotations


# Engagement is evidence about user-facing content, not infrastructure health.
# These tasks must keep the cadence declared in HEARTBEAT.md even when their
# outputs are intentionally silent or rarely clicked. Keep this shared so the
# scheduler, watermarks, and tuning writer cannot disagree.
ENGAGEMENT_TUNING_PROTECTED_TASKS = frozenset({
    "activity-log",
    "calendar-sync",
    "cross-session-sync",
    "daily-plan",
    "delegation-reconcile",
    "eigenflux-friends",
    "eigenflux-inbox-reconcile",
    "eigenflux-preinstall",
    "intention-check",
    "iteration-observe",
    "log-maintenance",
    "memorial-escrow",
    "memory-hourly",
    "provider-canary",
    "routine-run",
    "self-diagnostic",
    "thinking-review",
})


def parse_interval_overrides(value: object) -> dict[str, int]:
    """Return valid user-content overrides, rejecting protected task keys."""
    if not isinstance(value, dict):
        return {}

    parsed: dict[str, int] = {}
    try:
        for key, item in value.items():
            interval = int(item)
            if interval > 0 and key not in ENGAGEMENT_TUNING_PROTECTED_TASKS:
                parsed[key] = interval
    except (OverflowError, TypeError, ValueError):
        return {}
    return parsed


def resolve_effective_interval(
    name: str,
    task_interval: object,
    legacy_interval: object = 0,
    overrides: dict | None = None,
) -> int:
    """Resolve one cadence consistently across scheduler and diagnostics."""
    values = (
        (task_interval,)
        if name in ENGAGEMENT_TUNING_PROTECTED_TASKS
        else ((overrides or {}).get(name), legacy_interval, task_interval)
    )
    for value in values:
        try:
            interval = int(value or 0)
        except (OverflowError, TypeError, ValueError):
            continue
        if interval > 0:
            return interval
    return 0
