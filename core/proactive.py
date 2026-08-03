"""Bounded proactive reach for durable Jarvis signals.

Memorial remains the source of truth. This module only decides whether a
durable web notice also deserves a phone reach attempt through Delivery.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Callable

from core.delivery import DeliveryEnvelope, DeliveryResult, deliver

PROACTIVE_SIGNAL_SOURCES = {"eigenflux-feed-triage"}
PROACTIVE_DAILY_CAP = 2


def _compact(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _has_paired_phone_subscription(
    root: str | Path | None = None,
) -> bool:
    from core.mobile_access import push_subscription_status
    return bool(
        push_subscription_status(paired_only=True, root=root)["enabled"])


_DESK_CACHE_TTL_S = 30.0
_desk_cache: tuple[float, bool] | None = None


def desk_reachable(root: str | Path | None = None) -> bool:
    """Can the phone/web desk actually reach the user right now?

    The 7/23 routing change sent decisions to a phone desk and notices to a
    web archive — correct only if that surface can ring the user. It never
    could: no phone ever paired, so Lark dropped from ~60 cards/day to 1-7
    while `phone_ready` cards notified nobody (2026-08-03 audit). Routing must
    ask this question, not assume the answer.

    Fails CLOSED to unreachable: if the check itself errors, cards route to
    Lark. Delivering to a chat the user reads is the safe failure; delivering
    to a desk that may not exist is the 死路 this exists to prevent. Cached
    briefly because delivery paths call it per card.
    """
    global _desk_cache
    import time as _time
    now = _time.monotonic()
    if root is None and _desk_cache is not None:
        stamped, value = _desk_cache
        if now - stamped < _DESK_CACHE_TTL_S:
            return value
    try:
        value = _has_paired_phone_subscription(root)
    except Exception:
        value = False
    if root is None:
        _desk_cache = (now, value)
    return value


def maybe_push_signal(
    state: dict,
    *,
    root: str | Path | None = None,
    subscription_checker: Callable[[], bool] | None = None,
    deliverer: Callable[..., DeliveryResult] | None = None,
) -> dict:
    """Reach a paired phone for selected signals without weakening web storage.

    Missing permission/subscription is a clean skip: the Memorial has already
    been stored and remains discoverable. Delivery owns quiet hours, durable
    retry, deduplication, and the shared daily cap.
    """
    source = str(state.get("source") or "")
    if source not in PROACTIVE_SIGNAL_SOURCES:
        return {
            "eligible": False,
            "accepted": False,
            "reason": "source_not_selected",
        }

    checker = subscription_checker or (
        lambda: _has_paired_phone_subscription(root))
    if not checker():
        return {
            "eligible": True,
            "accepted": False,
            "reason": "no_paired_phone_subscription",
        }

    memorial_id = str(state.get("id") or "")
    title = _compact(state.get("title") or "EigenFlux 新信号", 80)
    body = _compact(state.get("body") or title, 240)
    envelope = DeliveryEnvelope(
        source="proactive-eigenflux",
        kind="push",
        payload={
            "title": f"Jarvis · {title}",
            "text": body,
            "url": f"/items/{memorial_id}" if memorial_id else "/signals",
        },
        attention="notice",
        requested_channel="push",
        memorial_id=memorial_id,
        matter_id=str(state.get("matter_id") or ""),
        dedup_key=f"proactive-push:{memorial_id}" if memorial_id else "",
        throttle_key="proactive:eigenflux",
        metadata={
            "metric_daily_cap": PROACTIVE_DAILY_CAP,
            "paired_only": True,
            "optional_no_subscriber": True,
            "reach_policy": "selected_signal",
            "dedup_text": f"{title}\n{body}",
        },
    )
    sender = deliverer or deliver
    result = sender(envelope, root=root)
    payload = asdict(result)
    payload.update(
        eligible=True,
        accepted=bool(
            result.accepted
            and result.state in {"queued", "delivered", "read", "acted"}
        ),
    )
    return payload
