"""Legacy device-token validation and the optional public web-desk URL.

The mobile gateway (`:3458`), its Tailscale funnel, device pairing, Web Push,
and the access audit were retired on 2026-08-11 (REQ-120): Lark is the only
delivery surface. What remains here are the two pieces with live callers:

- ``validate_device_token`` — the dashboard's `_owner_guard` still accepts a
  Bearer token from an already-paired device row. No new tokens can be minted
  (pairing is retired); rows in ``mobile_devices`` keep working until revoked.
- ``web_desk_url`` — Lark card buttons that link to the web desk resolve the
  base URL here. There is no funnel anymore, so only an explicitly configured
  ``mobile.public_url`` (https) yields a URL; "" means "render no button".

The SQLite tables (``mobile_devices``, ``matter_push_subscriptions``,
``mobile_pair_codes``, ``mobile_access_audit``) are data, not code — they are
intentionally left in place.
"""

from __future__ import annotations

import hashlib
import hmac

from core.timeutil import now_local, now_local_str


def _db():
    from dashboard.db import get_db
    return get_db()


def _now() -> str:
    return now_local_str("%Y-%m-%dT%H:%M:%S")


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def validate_device_token(token: str, touch: bool = True) -> dict | None:
    try:
        device_id, secret = str(token).split(".", 1)
    except ValueError:
        return None
    row = _db().execute(
        "SELECT * FROM mobile_devices WHERE id = ? AND revoked_at IS NULL",
        (device_id,),
    ).fetchone()
    if not row or not hmac.compare_digest(str(row["token_hash"]), _hash(secret)):
        return None
    item = dict(row)
    if touch:
        should_touch = True
        try:
            from datetime import datetime
            last_seen = datetime.fromisoformat(row["last_seen_at"] or "")
            should_touch = (now_local().replace(tzinfo=None)
                            - last_seen.replace(tzinfo=None)).total_seconds() >= 300
        except (TypeError, ValueError):
            pass
        if should_touch:
            _db().execute("UPDATE mobile_devices SET last_seen_at = ? WHERE id = ?",
                          (_now(), device_id))
            _db().commit()
    item.pop("token_hash", None)
    return item


def web_desk_url(path: str = "/items") -> str:
    """Absolute, phone-reachable URL for a web-desk page — or "" if there is none.

    A Lark card cannot follow a relative path and cannot reach localhost from a
    phone, so a card button that wants to send the user to the web desk needs
    this. With the Tailscale funnel retired (REQ-120) the only source of a
    reachable base is an explicit ``mobile.public_url`` config entry; the old
    tailnet lookup is gone because an online tailnet node with nothing served
    would have produced a URL that answers nothing — a dead button.

    Returning "" is a real answer and callers must honour it by rendering NO
    button at all. A button that goes nowhere is worse than an absent one —
    it spends the user's tap and their trust to tell them nothing.
    """
    path = "/" + str(path or "").lstrip("/")
    try:
        from core.config import Config
        base = str(Config().get("mobile.public_url", "") or "").rstrip("/")
    except Exception:
        base = ""
    if not base.startswith("https://"):
        return ""
    return base + path
