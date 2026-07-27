"""Static trust-boundary contracts for the Bash message dispatcher."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_private_owner_path_requires_chat_type_and_exact_sender():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")

    assert 'chat_type="${6:-unknown}"' in source
    assert '[ "$sender_id" = "$USER_ID" ]' in source
    assert 'prompt_chat_type="external_p2p"' in source
    assert 'JV_CHAT_TYPE="$prompt_chat_type"' in source
    assert "P2P message missing sender_id" in source


def test_inline_actions_and_engagement_are_owner_gated():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")

    assert '_inline_cmd_ok=0' in source
    assert 'if [ "$_owner_p2p" -eq 1 ]; then' in source
    assert 'allow_actions=0' in source
