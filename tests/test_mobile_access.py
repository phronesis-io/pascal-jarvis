"""Tests for core.mobile_access — legacy device-token validation.

Pairing is retired (REQ-120): no code path mints new tokens, so these tests
seed a ``mobile_devices`` row directly, exactly like the legacy data the
validator still has to honor (and refuse once revoked).
"""

import hashlib

import pytest

from core.mobile_access import validate_device_token, web_desk_url


@pytest.fixture
def _isolated_db(tmp_path, monkeypatch):
    """Point mobile_access at an isolated temp DB with full schema."""
    import dashboard.db as _db_mod
    db_path = tmp_path / "jarvis.db"
    monkeypatch.setattr(_db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(_db_mod, "_connection", None)
    _db_mod.get_db()
    yield db_path
    _db_mod._connection = None


def _seed_device(device_id: str = "dev_legacy", secret: str = "s3cret",
                 label: str = "test phone", revoked_at: str | None = None) -> str:
    import dashboard.db as _db_mod
    db = _db_mod.get_db()
    db.execute(
        "INSERT INTO mobile_devices (id,label,token_hash,created_at,"
        "last_seen_at,revoked_at) VALUES (?,?,?,?,?,?)",
        (device_id, label,
         hashlib.sha256(secret.encode("utf-8")).hexdigest(),
         "2026-07-25T10:00:00", "2026-07-25T10:00:00", revoked_at),
    )
    db.commit()
    return f"{device_id}.{secret}"


def test_validate_device_token_valid(_isolated_db):
    token = _seed_device()
    device = validate_device_token(token, touch=True)
    assert device is not None
    assert device["label"] == "test phone"
    assert "token_hash" not in device


def test_validate_device_token_invalid(_isolated_db):
    _seed_device()
    assert validate_device_token("bogus-token-12345", touch=False) is None
    assert validate_device_token("dev_legacy.wrong-secret", touch=False) is None


def test_validate_device_token_refuses_revoked_device(_isolated_db):
    token = _seed_device(device_id="dev_gone", revoked_at="2026-08-01T00:00:00")
    assert validate_device_token(token, touch=False) is None


def test_web_desk_url_requires_an_explicit_https_public_url(monkeypatch):
    """No funnel exists anymore (REQ-120): only mobile.public_url counts."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        "core.config.Config",
        lambda *a, **k: SimpleNamespace(
            get=lambda key, default="": "https://desk.example"),
    )
    assert web_desk_url("/items") == "https://desk.example/items"
