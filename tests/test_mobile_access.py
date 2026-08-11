"""Tests for core.mobile_access — legacy device-token validation.

Pairing is retired (REQ-120): no code path mints new tokens, so these tests
seed a ``mobile_devices`` row through the shared conftest helper, exactly
like the legacy data the validator still has to honor (and refuse once
revoked).
"""

import pytest

from core.mobile_access import validate_device_token, web_desk_url
from tests.conftest import seed_legacy_device


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


def test_validate_device_token_valid(_isolated_db):
    token = seed_legacy_device(label="test phone")
    device = validate_device_token(token)
    assert device is not None
    assert device["label"] == "test phone"
    assert "token_hash" not in device


def test_validate_device_token_invalid(_isolated_db):
    seed_legacy_device()
    assert validate_device_token("bogus-token-12345") is None
    assert validate_device_token("dev_legacy.wrong-secret") is None


def test_validate_device_token_refuses_revoked_device(_isolated_db):
    token = seed_legacy_device(
        device_id="dev_gone", revoked_at="2026-08-01T00:00:00")
    assert validate_device_token(token) is None


def test_validate_device_token_is_a_pure_read(_isolated_db):
    """REQ-120: the last-seen touch machinery is gone — validating must not
    write anything back to the device row."""
    import dashboard.db as _db_mod
    token = seed_legacy_device()
    before = dict(_db_mod.get_db().execute(
        "SELECT * FROM mobile_devices WHERE id='dev_legacy'").fetchone())
    assert validate_device_token(token) is not None
    after = dict(_db_mod.get_db().execute(
        "SELECT * FROM mobile_devices WHERE id='dev_legacy'").fetchone())
    assert after == before


def test_web_desk_url_requires_an_explicit_https_public_url(monkeypatch):
    """No funnel exists anymore (REQ-120): only mobile.public_url counts."""
    from types import SimpleNamespace
    monkeypatch.setattr(
        "core.config.Config",
        lambda *a, **k: SimpleNamespace(
            get=lambda key, default="": "https://desk.example"),
    )
    assert web_desk_url("/items") == "https://desk.example/items"
