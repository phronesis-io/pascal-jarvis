"""Trust-boundary contracts for the Bash message dispatcher."""

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


def test_auto_promotion_rehomes_logical_transition_marker():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    promotion = source.index("Promoted to background job")
    marker_rehome = source.rfind(
        'dispatch_marker_handoff_owned "$dispatch_marker"', 0, promotion)
    lock_release = source.rfind('rm -f "$LOCK_FILE"', 0, promotion)

    assert lock_release < marker_rehome < promotion
    assert 'dispatch_marker="$_promoted_marker"' in source[marker_rehome:promotion]


def test_stop_covers_queued_handlers_before_provider_lock():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    stop = source[source.index('# "stop" / "cancel"'):
                  source.index("# ── Normal message", source.index('# "stop" / "cancel"'))]

    assert '".dispatch_conv_${_conv_dispatch_key}_"*' in stop
    assert 'terminate_registered_group "$_queued_marker" "$$"' in stop
    assert 'Refusing queued marker that points at the bot PID' in stop
    assert 'session_lock_identity_for_handler' in stop
    assert 'process_group_is_owned "$_owner_pid" "$_owner_start" "$$"' in stop
    assert 'kill -TERM "$_stop_pid"' in stop
    assert "现在可以切换或重置会话" in stop


def test_handler_unregisters_before_bounded_reaction_cleanup():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    finish = source[source.index("_finish_message_handler() {"):
                    source.index("_abort_message_handler() {")]

    assert finish.index("dispatch_markers_remove_owned") < finish.index(
        "lark_remove_reaction")
    handler = source[source.index("handle_message() {"):
                     source.index("resolve_memorial_thread_after_reply()")]
    assert handler.count("lark_remove_reaction") == 1


def test_dispatch_marker_uses_atomic_parent_pid_handoff_and_owned_cleanup():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    handler = source[source.index("handle_message() {"):
                     source.index("resolve_memorial_thread_after_reply()")]
    dispatch = source[source.index("# Dispatch to background"):
                      source.index("done\n}", source.index("# Dispatch to background"))]

    assert '${BASHPID:-$$}' not in handler
    assert "_finish_message_handler" in handler
    assert "trap '_abort_message_handler $?' EXIT" in handler
    assert "trap '_abort_message_handler; exit 143' TERM INT" in handler
    assert 'dispatch_marker_wait_owned "$dispatch_marker"' in handler
    assert '"$_handler_pid" "$_handler_token" 100' in handler
    assert "Dispatch marker handoff timed out" in handler
    assert 'process_group_is_owned "$_handler_pid" "$_handler_token" "$$"' in handler
    assert 'kill -TERM -- "-$_handler_pid"' in handler
    assert "_handler_pid=$!" in dispatch
    assert 'set -m' in dispatch
    assert 'set +m' in dispatch
    assert 'dispatch_marker_publish "$_dispatch_marker"' in dispatch
    assert '"$_handler_pid" "$_handler_token"' in dispatch


def test_promotion_rehomes_but_does_not_unregister_live_handler():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    promotion = source[source.index("if [ \"$is_group\" -ne 1 ]"):
                       source.index("# Watchdog timeout", source.index("if [ \"$is_group\" -ne 1 ]"))]

    assert ".dispatch_job_${_bg_job_id}_${_handler_pid}" in promotion
    assert "dispatch_marker_handoff_owned" in promotion
    assert 'dispatch_marker="$_promoted_marker"' in promotion


def test_global_cleanup_terminates_registered_handler_trees():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    cleanup = source[source.index("cleanup() {"):
                     source.index("trap cleanup EXIT")]

    assert 'for _dispatch_marker in "$JARVIS_DIR"/.dispatch_*' in cleanup
    assert 'terminate_registered_group "$_dispatch_marker" "$$"' in cleanup


def test_deterministic_matter_reply_closes_only_after_reliable_delivery():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    matter = source.index("_matter_cmd=$(run_matter_command")
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


def test_deterministic_command_hard_exit_is_not_replayed_through_model():
    source = (ROOT / "bot.sh").read_text(encoding="utf-8")
    helper = source[source.index("run_matter_command()"):
                    source.index("delivery_send_reliable()")]

    assert "command_would_handle" in helper
    assert 'deterministic="unknown"' in helper
    assert 'if [ "$status" -ne 0 ] && [ "$deterministic" != "false" ]' in helper
    assert '"handled":true' in helper
    assert "不会交给模型" in helper
