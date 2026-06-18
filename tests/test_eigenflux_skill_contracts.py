"""Integrity checks for Jarvis's preinstalled EigenFlux skills.

The eigenflux-preinstall heartbeat task mirrors these files into Jarvis, and
core.prompt.load_ef_skills() injects them into the assistant context. These
docs are therefore runtime behavior, not passive README text.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EF = ROOT / "plugins" / "eigenflux" / "skills"


def _read(path: str) -> str:
    return (EF / path).read_text(encoding="utf-8")


def test_broadcast_contract_mirrors_feed_runtime_triggers():
    contract = _read("ef-broadcast/references/contract.md")
    feed = _read("ef-broadcast/references/feed.md")

    # These are binding triggers: contract.md is injected with every feed poll,
    # while feed.md carries the full examples/procedure.
    for text in (contract, feed):
        assert "feed_delivery_preference" in text
        assert "dashboard_last_hinted" in text
        assert "profile_calibration_remaining" in text
        assert "profile_followup_last" in text
        assert "📡 Powered by EigenFlux" in text
    assert "recurring_publish" in contract

    assert "2 days" in contract
    assert "5 days" in contract
    assert "1 month" in contract
    assert "~2 days" in feed
    assert "~5 days" in feed
    assert "~1 month (cap)" in feed


def test_profile_config_matches_broadcast_followup_cadence():
    config = _read("ef-profile/references/config.md")

    assert "0→~2d" in config
    assert "1→~5d" in config
    assert "2→~1wk" in config
    assert "3→~2wk" in config
    assert "≥4→~1mo cap" in config
    assert "≥4→~2mo" not in config


def test_preinstalled_skill_docs_do_not_reference_missing_local_sync_script():
    feed = _read("ef-broadcast/references/feed.md")

    assert "scripts/common/sync-feed-contract.sh" not in feed
    assert "Jarvis installs both files verbatim" in feed


def test_trading_expiry_is_consistent_across_skill_and_reference_docs():
    skill = _read("ef-trading/SKILL.md")
    orders = _read("ef-trading/references/orders.md")

    for text in (skill, orders):
        assert "Expiry closes the order" in text or "expired" in text
        assert "No payment" in text or "no payment" in text
        assert "not counted as active" in text.lower()
    assert "Refund is not automatic" not in skill
    assert "Refund is not automatic" not in orders
