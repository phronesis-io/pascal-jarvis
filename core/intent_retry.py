"""Infrastructure retry timing without changing an Intent's real cadence."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable


INFRA_RETRY_DELAY = timedelta(minutes=15)


def deferred_until(now: datetime) -> str:
    return (now + INFRA_RETRY_DELAY).isoformat(timespec="seconds")


def is_deferred(
    value: object,
    now: datetime,
    coerce: Callable[[datetime], datetime],
) -> bool:
    raw = str(value or "")
    if not raw:
        return False
    try:
        return now < coerce(datetime.fromisoformat(raw))
    except (TypeError, ValueError):
        return False
