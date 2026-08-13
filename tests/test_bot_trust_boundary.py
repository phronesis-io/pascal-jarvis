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


def test_every_shared_chat_suppresses_actions_even_for_owner_messages():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    shared = source[
        source.index('if [ "$chat_type" != "p2p" ]; then'):
        source.index("# ── Non-owner p2p", source.index('if [ "$chat_type" != "p2p" ]; then'))
    ]

    assert "allow_actions=0" in shared
    assert 'if [ -z "$sender_id" ] || [ "$sender_id" != "$USER_ID" ]' not in shared


def test_memorial_thread_closes_only_after_confirmed_reply_delivery():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    send = source.index('if ! JARVIS_DELIVERY_PROVIDER=')
    failure = source.index('log_err "[$session_id] Reply not yet delivery-confirmed', send)
    success = source.index("  else\n", failure)
    close = source.index(
        'resolve_memorial_thread_after_reply "$conv_key" "$reply"', success)
    end = source.index("  fi\n}", success)

    assert failure < success < close < end


def test_deterministic_memorial_continuation_closes_after_commit():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    helper = source.index("resolve_memorial_thread_after_reply()")
    command = source.index("python3 -m core.memorial resolve-thread", helper)
    continuation = source.index("python3 -m core.memorial continue-commit")
    committed = source.index(
        'resolve_memorial_thread_after_reply "$conv_key" "$_continue_reply"',
        continuation,
    )
    branch_end = source.index("else", committed)

    assert 'case "$conv_key" in' in source[helper:command]
    assert "memorial:*)" in source[helper:command]
    assert continuation < committed < branch_end


def test_deterministic_matter_reply_closes_only_after_reliable_delivery():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    matter = source.index("python3 -m core.matter_bridge")
    handled = source.index(".handled // false", matter)
    send = source.index(
        'if delivery_reply_reliable "$message_id" "$_matter_reply"; then',
        handled,
    )
    close = source.index(
        'resolve_memorial_thread_after_reply "$conv_key" "$_matter_reply"',
        send,
    )
    branch_end = source.index("continue", close)

    assert handled < send < close < branch_end
