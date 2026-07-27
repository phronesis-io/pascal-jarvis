"""Tests for core.mobile_access — device pairing and token validation."""

import sqlite3

import pytest

from core.mobile_access import (
    create_pair_code,
    consume_pair_code,
    validate_device_token,
    _normalize_pair_code,
)


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


def test_pair_code_round_trip(_isolated_db):
    result = create_pair_code("test phone", 15)
    assert "code" in result
    code = result["code"]
    consumed = consume_pair_code(code)
    assert consumed is not None
    assert "token" in consumed or "device_token" in consumed


def test_pair_code_single_use(_isolated_db):
    result = create_pair_code("test phone", 15)
    code = result["code"]
    consume_pair_code(code)
    assert consume_pair_code(code) is None


def test_validate_device_token_valid(_isolated_db):
    result = create_pair_code("test phone", 15)
    consumed = consume_pair_code(result["code"])
    token = consumed.get("token") or consumed.get("device_token")
    device = validate_device_token(token, touch=True)
    assert device is not None
    assert device["label"] == "test phone"


def test_validate_device_token_invalid(_isolated_db):
    assert validate_device_token("bogus-token-12345", touch=False) is None


def test_normalize_pair_code_strips_dashes():
    assert _normalize_pair_code("AB-CD-EF") == "ABCDEF"
    assert _normalize_pair_code("abcdef") == "ABCDEF"
