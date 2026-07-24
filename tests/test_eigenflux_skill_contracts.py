"""Integrity checks for Jarvis's preinstalled EigenFlux skills.

The eigenflux-preinstall heartbeat task mirrors these files into Jarvis, and
core.prompt.load_ef_skills() injects them into the assistant context. These
docs are therefore runtime behavior, not passive README text.
"""

from pathlib import Path

import pytest

from core.eigenflux_skill_overlay import BEGIN, END, render


ROOT = Path(__file__).resolve().parent.parent
EF = ROOT / "plugins" / "eigenflux" / "skills"


def _read(path: str) -> str:
    return (EF / path).read_text(encoding="utf-8")


def test_broadcast_contract_mirrors_feed_runtime_triggers():
    contract = _read("ef-broadcast/references/contract.md")
    feed = _read("ef-broadcast/references/feed.md")

    # These are binding triggers: contract.md is injected with every feed poll,
    # while feed.md carries the full examples/procedure. Both files sync from
    # upstream, which is free to reword — assert the ban itself, not one
    # sentence (the 7/6 upstream sync compressed contract.md's phrasing and
    # broke the exact-string version of this check).
    auto_login_ban = ("Do NOT mint a one-time auto-login link",
                      "never a one-time auto-login link")
    for text in (contract, feed):
        assert "feed_delivery_preference" in text
        assert "https://www.eigenflux.ai/dashboard" in text
        assert any(marker in text for marker in auto_login_ban)
        assert "profile_calibration_remaining" in text
        assert "profile_followup_last" in text
        assert "📡 Powered by EigenFlux" in text
    assert "recurring_publish" in contract
    assert "source_type: \"system\"" in contract

    assert "3 days" in contract
    assert "1 week" in contract
    assert "2 months" in contract
    assert "~3 days" in feed
    assert "~1 week" in feed
    assert "~2 months (cap)" in feed


def test_profile_config_matches_broadcast_followup_cadence():
    config = _read("ef-profile/references/config.md")

    assert "0→~3d" in config
    assert "1→~1wk" in config
    assert "2→~2wk" in config
    assert "3→~1mo" in config
    assert "≥4→~2mo cap" in config
    assert "dashboard_last_hinted" not in config


def test_preinstalled_skill_docs_reference_upstream_contract_sync():
    feed = _read("ef-broadcast/references/feed.md")

    assert "scripts/common/sync-feed-contract.sh" in feed
    assert "static/feed_contract.md" in feed


def test_trading_expiry_is_consistent_across_skill_and_reference_docs():
    skill = _read("ef-trading/SKILL.md")
    orders = _read("ef-trading/references/orders.md")

    for text in (skill, orders):
        assert "Expiry closes the order" in text or "expired" in text
        assert "No payment" in text or "no payment" in text
        assert "not counted as active" in text.lower()
    assert "Refund is not automatic" not in skill
    assert "Refund is not automatic" not in orders


def test_communication_skill_keeps_jarvis_verified_message_contract():
    skill = _read("ef-communication/SKILL.md")

    assert BEGIN in skill and END in skill
    assert "python3 -m core.eigenflux_messages send" in skill
    assert 'eigenflux msg send --content "YOUR MESSAGE" --receiver-id' not in skill
    assert "must not be retried manually" in skill
    assert "--repeat-token" in skill


def test_skill_overlay_is_deterministic_and_replaces_old_copy():
    base = (
        "# Skill\n\n"
        "```bash\n"
        "# Direct message to a friend\n"
        'eigenflux msg send --content "YOUR MESSAGE" '
        "--receiver-id FRIEND_AGENT_ID\n"
        "```\n\n"
        "### Fetch Unread Messages\n\nBody\n"
    )
    overlay = "### Local contract\n\nUse verified gateway."

    first = render(base, overlay)
    second = render(first, overlay)

    assert first == second
    assert first.count(BEGIN) == 1
    assert "python3 -m core.eigenflux_messages send" in first
    assert "--receiver-id FRIEND_AGENT_ID" not in first
    assert first.index("### Local contract") < first.index(
        "### Fetch Unread Messages"
    )


def test_skill_overlay_rejects_incomplete_existing_markers():
    with pytest.raises(ValueError, match="marker pair"):
        render(f"# Skill\n\n{BEGIN}\nold content\n", "new content")
