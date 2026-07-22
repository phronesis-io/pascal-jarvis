"""Device pairing, revocation, access audit, and optional Web Push."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str


def _db():
    from dashboard.db import get_db
    return get_db()


def _now() -> str:
    return now_local_str("%Y-%m-%dT%H:%M:%S")


def _hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def create_pair_code(label: str = "手机", ttl_minutes: int = 15) -> dict:
    label = str(label or "手机").strip()[:80]
    code = secrets.token_urlsafe(12)
    now = now_local()
    expires = now + timedelta(minutes=max(1, min(int(ttl_minutes), 60)))
    db = _db()
    db.execute("DELETE FROM mobile_pair_codes WHERE expires_at < ? OR consumed_at IS NOT NULL",
               (now.strftime("%Y-%m-%dT%H:%M:%S"),))
    db.execute(
        "INSERT INTO mobile_pair_codes (code_hash, label, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (_hash(code), label, expires.strftime("%Y-%m-%dT%H:%M:%S"),
         now.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    db.commit()
    return {"code": code, "label": label,
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%S")}


def consume_pair_code(code: str) -> dict | None:
    now = _now()
    db = _db()
    row = db.execute(
        "SELECT * FROM mobile_pair_codes WHERE code_hash = ? "
        "AND consumed_at IS NULL AND expires_at >= ?",
        (_hash(code), now),
    ).fetchone()
    if not row:
        return None
    device_id = f"dev_{uuid.uuid4().hex[:12]}"
    secret = secrets.token_urlsafe(32)
    token = f"{device_id}.{secret}"
    try:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            "UPDATE mobile_pair_codes SET consumed_at = ? WHERE code_hash = ? "
            "AND consumed_at IS NULL", (now, _hash(code)),
        ).rowcount
        if changed != 1:
            db.rollback()
            return None
        db.execute(
            "INSERT INTO mobile_devices "
            "(id, label, token_hash, created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (device_id, row["label"], _hash(secret), now, now),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"device_id": device_id, "label": row["label"], "token": token}


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


def list_devices(include_revoked: bool = False) -> list[dict]:
    where = "" if include_revoked else "WHERE revoked_at IS NULL"
    rows = _db().execute(
        f"SELECT id, label, created_at, last_seen_at, revoked_at FROM mobile_devices "
        f"{where} ORDER BY created_at DESC"
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_device(device_id: str) -> bool:
    db = _db()
    changed = db.execute(
        "UPDATE mobile_devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now(), str(device_id)),
    ).rowcount
    db.execute("UPDATE matter_push_subscriptions SET enabled = 0 WHERE device_id = ?",
               (str(device_id),))
    db.commit()
    return changed == 1


def audit_access(device_id: str, remote_addr: str, method: str, path: str,
                 status: int, metadata: dict | None = None) -> None:
    db = _db()
    db.execute(
        "INSERT INTO mobile_access_audit "
        "(timestamp, device_id, remote_addr, method, path, status, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_now(), str(device_id), str(remote_addr)[:120], str(method)[:12],
         str(path)[:600], int(status), json.dumps(metadata or {}, ensure_ascii=False)),
    )
    db.execute(
        "DELETE FROM mobile_access_audit WHERE id NOT IN "
        "(SELECT id FROM mobile_access_audit ORDER BY id DESC LIMIT 10000)"
    )
    db.commit()


def recent_access(limit: int = 100) -> list[dict]:
    rows = _db().execute(
        "SELECT * FROM mobile_access_audit ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 500)),),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            item["metadata"] = {}
        items.append(item)
    return items


def _vapid_paths() -> tuple[Path, Path]:
    from core.config import Config
    directory = Config().jarvis_dir / "data" / "mobile"
    return directory / "vapid-private.pem", directory / "vapid-public.txt"


def vapid_public_key() -> str:
    private_path, public_path = _vapid_paths()
    if public_path.exists() and private_path.exists():
        return public_path.read_text(encoding="utf-8").strip()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private_path.parent.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    private_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    numbers = key.public_key().public_numbers()
    raw = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    public = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    public_path.write_text(public, encoding="utf-8")
    try:
        private_path.chmod(0o600)
    except OSError:
        pass
    return public


def register_push(device_id: str, subscription: dict) -> int:
    endpoint = str(subscription.get("endpoint") or "")
    keys = subscription.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "")
    auth = str(keys.get("auth") or "")
    if (not endpoint.startswith("https://") or not p256dh or not auth
            or len(endpoint) > 2048 or len(p256dh) > 512 or len(auth) > 512):
        raise ValueError("invalid push subscription")
    now = _now()
    db = _db()
    db.execute(
        """INSERT INTO matter_push_subscriptions
           (device_id, endpoint, p256dh, auth, enabled, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET device_id=excluded.device_id,
             p256dh=excluded.p256dh, auth=excluded.auth, enabled=1,
             updated_at=excluded.updated_at""",
        (device_id, endpoint, p256dh, auth, now, now),
    )
    db.commit()
    row = db.execute("SELECT id FROM matter_push_subscriptions WHERE endpoint = ?",
                     (endpoint,)).fetchone()
    return int(row[0])


def unregister_push(endpoint: str, device_id: str = "") -> bool:
    db = _db()
    if device_id:
        changed = db.execute(
            "UPDATE matter_push_subscriptions SET enabled=0 WHERE endpoint=? AND device_id=?",
            (str(endpoint), str(device_id)),
        ).rowcount
    else:
        changed = db.execute(
            "UPDATE matter_push_subscriptions SET enabled=0 WHERE endpoint=?",
            (str(endpoint),),
        ).rowcount
    db.commit()
    return bool(changed)


def send_push(title: str, body: str, url: str = "/matters",
              matter_id: str = "") -> dict:
    rows = _db().execute(
        "SELECT * FROM matter_push_subscriptions WHERE enabled = 1"
    ).fetchall()
    if not rows:
        return {"sent": 0, "failed": 0, "disabled": 0}
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return {"sent": 0, "failed": len(rows), "disabled": 0,
                "error": "pywebpush is not installed"}
    private_path, _ = _vapid_paths()
    vapid_public_key()
    payload = json.dumps({"title": str(title)[:100], "body": str(body)[:300],
                          "url": url, "matter_id": matter_id}, ensure_ascii=False)
    sent = failed = disabled = 0
    for row in rows:
        subscription = {"endpoint": row["endpoint"],
                        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
        try:
            webpush(subscription_info=subscription, data=payload,
                    vapid_private_key=str(private_path),
                    vapid_claims={"sub": "mailto:jarvis@localhost"}, timeout=10)
            sent += 1
        except WebPushException as exc:
            failed += 1
            response = getattr(exc, "response", None)
            if response is not None and response.status_code in {404, 410}:
                _db().execute(
                    "UPDATE matter_push_subscriptions SET enabled=0 WHERE id=?",
                    (row["id"],),
                )
                disabled += 1
        except Exception:
            failed += 1
    _db().commit()
    return {"sent": sent, "failed": failed, "disabled": disabled}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m core.mobile_access")
    sub = parser.add_subparsers(dest="command", required=True)
    pair = sub.add_parser("pair")
    pair.add_argument("--label", default="手机")
    pair.add_argument("--ttl", type=int, default=15)
    sub.add_parser("devices")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("device_id")
    sub.add_parser("vapid-key")
    args = parser.parse_args(argv)
    if args.command == "pair":
        result = create_pair_code(args.label, args.ttl)
    elif args.command == "devices":
        result = list_devices(include_revoked=True)
    elif args.command == "revoke":
        result = {"revoked": revoke_device(args.device_id)}
    else:
        result = {"public_key": vapid_public_key()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
