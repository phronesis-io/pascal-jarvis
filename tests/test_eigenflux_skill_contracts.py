"""Integrity checks for Jarvis's preinstalled EigenFlux skills.

The eigenflux-preinstall heartbeat task mirrors these files into Jarvis, and
core.prompt.load_ef_skills() injects them into the assistant context. These
docs are therefore runtime behavior, not passive README text.
"""

from pathlib import Path
import os
import re
import subprocess

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
    # v0.10.2 reworded it again ("never a bare URL or one-time auto-login
    # link" / "never … mint a one-time auto-login link") — anchor on the
    # ban's object plus a negation in the same sentence, not any exact string.
    def _bans_auto_login(text: str) -> bool:
        return any(
            "one-time auto-login link" in sentence
            and any(neg in sentence.lower() for neg in ("never", "do not"))
            for sentence in text.replace("\n", " ").split(". "))

    for text in (contract, feed):
        assert "feed_delivery_preference" in text
        assert "https://www.eigenflux.ai/dashboard" in text
        assert _bans_auto_login(text)
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


def test_retired_trading_skill_is_not_preinstalled_or_routed():
    feed = _read("ef-broadcast/references/feed.md")

    assert not (EF / "ef-trading" / "SKILL.md").exists()
    assert not list((EF / "ef-trading" / "references").glob("*.md"))
    assert "trading flow" not in feed
    assert "**`trade`**" not in feed


def test_communication_skill_keeps_jarvis_verified_message_contract():
    skill = _read("ef-communication/SKILL.md")

    assert BEGIN in skill and END in skill
    assert "python3 -m core.eigenflux_messages send" in skill
    assert 'eigenflux msg send --content "YOUR MESSAGE" --receiver-id' not in skill
    assert "must not be retried manually" in skill
    assert "--repeat-token" in skill


def test_automated_reports_never_embed_one_time_dashboard_codes():
    message = _read("ef-communication/references/message.md")
    profile = _read("ef-profile/SKILL.md")

    assert "Carry the stable dashboard link" in message
    assert "Never run `eigenflux dashboard`" in message
    assert "Automated reports, heartbeat pushes" in profile
    assert "Never put a one-time login code" in profile
    assert "valid for about 15 minutes" in profile


def test_public_skill_bundle_does_not_activate_staged_v2_commands():
    broadcast = _read("ef-broadcast/SKILL.md")
    profile = _read("ef-profile/SKILL.md")

    assert "eigenflux context pull" not in broadcast
    assert "onboarding-v2.md" not in profile
    assert not (EF / "ef-profile" / "references" / "onboarding-v2.md").exists()


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


def test_preinstall_verifier_references_only_live_test_files():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")
    references = re.findall(
        r'\$JARVIS_DIR/tests/([A-Za-z0-9_./-]+\.py)', script)

    assert references
    missing = [name for name in references if not (ROOT / "tests" / name).is_file()]
    assert missing == []
    assert "test_eigenflux_messages.py" in references


def test_preinstall_backlog_dedup_treats_checkbox_as_data():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")

    assert 'grep -Fqx -- "- [ ] $r"' in script
    assert "open_review_count=$(grep -Ec" in script
    assert "eigenflux_preinstall_retire.py" in script
    assert '"skills_removed": int(nd)' in script
    assert "removed top-level CLI command(s)" in script


def test_preinstall_pytest_budget_covers_the_measured_suite_runtime():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")
    assert "bounded 120 python3 -m pytest" in script
    assert "timed out before finishing (no summary line)" in script


def test_preinstall_source_repos_can_be_overridden_for_worktrees():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")

    assert 'REPOS_DIR="${JARVIS_REPOS_DIR:-$(dirname "$JARVIS_DIR")}"' in script
    assert 'PLUGIN_DIR="${EIGENFLUX_PLUGIN_DIR:-$REPOS_DIR/eigenflux-claude-plugin}"' in script
    assert 'MAIN_DIR="${EIGENFLUX_MAIN_DIR:-$REPOS_DIR/eigenflux}"' in script
    assert 'git -C "$MAIN_DIR" rev-parse --git-dir' in script
    assert 'git -C "$PLUGIN_DIR" rev-parse --git-dir' in script
    assert '[ ! -d "$MAIN_DIR/.git" ]' not in script
    assert '[ -d "$repo/.git" ]' not in script
    assert 'mkdir -p "$(dirname "$STATE_FILE")"' in script


def test_preinstall_is_detect_only_unless_a_maintainer_explicitly_applies():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")

    assert "EIGENFLUX_PREINSTALL_APPLY" in script
    assert "APPLY_SKILL_SYNC=false" in script
    assert 'retire_args+=(--dry-run)' in script
    assert "deployed source left untouched" in script
    assert 'if [ "$APPLY_SKILL_SYNC" = true ]' in script
    assert "named non-main maintenance worktree" in script
    assert "refused while this checkout owns a live Jarvis bot" in script
    assert 'kill -0 "$live_bot_pid"' in script
    assert 'if [ "$APPLY_SKILL_SYNC" = true ] || [ "$skill_change_count" -eq 0 ]' in script


def test_preinstall_distinguishes_network_eof_from_contract_rejection():
    script = (ROOT / "tasks" / "eigenflux_preinstall_pre.sh").read_text(
        encoding="utf-8")

    assert "inconclusive (transient network failure)" in script
    assert "feedback write regression" in script
    assert "EOF|timed? ?out|connection (reset|refused)" in script


def test_preinstall_apply_refuses_main_and_live_runtime(tmp_path):
    script = ROOT / "tasks" / "eigenflux_preinstall_pre.sh"

    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(main)],
        check=True,
        capture_output=True,
        text=True,
    )
    main_result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "JARVIS_DIR": str(main),
            "EIGENFLUX_PREINSTALL_APPLY": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert main_result.returncode == 0
    assert "named non-main maintenance worktree" in main_result.stdout
    assert main_result.stdout.rstrip().endswith("PREINSTALL_FAIL")

    live = tmp_path / "maintenance"
    live.mkdir()
    subprocess.run(
        ["git", "init", "-b", "maintenance", str(live)],
        check=True,
        capture_output=True,
        text=True,
    )
    (live / ".bot.pid").write_text(str(os.getpid()), encoding="utf-8")
    live_result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "JARVIS_DIR": str(live),
            "EIGENFLUX_PREINSTALL_APPLY": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert live_result.returncode == 0
    assert "refused while this checkout owns a live Jarvis bot" in live_result.stdout
    assert live_result.stdout.rstrip().endswith("PREINSTALL_FAIL")
