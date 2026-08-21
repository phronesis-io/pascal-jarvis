"""The retired phone web ingress cannot quietly become active again."""

from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_retired_device_auth_has_no_active_module_or_import():
    assert not (ROOT / "core" / "mobile_access.py").exists()
    for relative in ("core/memorial.py",):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "core.mobile_access" not in text
        assert "validate_device_token" not in text


def test_legacy_mobile_tables_remain_for_non_destructive_retention():
    from core.db import get_db

    names = {
        row[0] for row in get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "mobile_devices",
        "mobile_pair_codes",
        "mobile_access_audit",
        "matter_push_subscriptions",
    } <= names
