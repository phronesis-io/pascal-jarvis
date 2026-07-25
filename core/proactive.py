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
