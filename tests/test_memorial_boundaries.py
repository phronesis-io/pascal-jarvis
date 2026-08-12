"""Compatibility contracts for the memorial architecture boundaries."""

from __future__ import annotations

from pathlib import Path

from core import memorial, memorial_ledger


def test_ledger_helpers_do_not_capture_a_repository_root():
    assert not hasattr(memorial_ledger, "JARVIS_DIR")


def test_facade_jarvis_dir_monkeypatch_redirects_every_path(
    tmp_path, monkeypatch,
):
    isolated = tmp_path / "facade-root"
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path / "env-root"))
    monkeypatch.setattr(memorial, "JARVIS_DIR", isolated)

    assert memorial.runtime_root() == isolated
    assert memorial._ledger_path() == isolated / "memorials.jsonl"
    assert memorial._pending_merge_path() == (
        isolated / "jobs" / "pending_merge.jsonl")
    assert memorial._outbox_path() == isolated / "heartbeat_outbox.jsonl"
    assert memorial._explain_queue_path().is_relative_to(isolated)
    assert memorial._reply_followup_queue_path().is_relative_to(isolated)

    memorial._append_line(memorial._ledger_path(), {
        "ev": "create",
        "id": "mem_boundary",
        "source": "test",
        "title": "Boundary",
        "body": "isolated",
        "options": [],
        "extra_buttons": [],
    })

    assert memorial.get_memorial("mem_boundary")["title"] == "Boundary"
    assert (isolated / "memorials.jsonl").exists()
    assert not (tmp_path / "env-root" / "memorials.jsonl").exists()


def test_runtime_root_observes_environment_changes_after_import(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(memorial, "JARVIS_DIR", memorial._INITIAL_JARVIS_DIR)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    assert memorial.runtime_root() == tmp_path
    assert memorial._ledger_path() == tmp_path / "memorials.jsonl"


def test_facade_private_hooks_drive_extracted_implementations(monkeypatch):
    monkeypatch.setattr(
        memorial,
        "_default_attention",
        lambda source, options, extra_buttons: "facade-attention",
    )
    folded = memorial._fold([{
        "ev": "create",
        "id": "mem_hook",
        "source": "test",
        "title": "Hook",
        "body": "body",
        "options": [],
        "extra_buttons": [],
    }])
    assert folded["mem_hook"]["attention"] == "facade-attention"

    monkeypatch.setattr(memorial, "requires_decision", lambda state: True)
    monkeypatch.setattr(memorial, "_display_body", lambda _body: "patched body")
    rendered = memorial._render_card(folded["mem_hook"])
    assert "🎯 等你拍一个" in rendered
    assert "patched body" in rendered
    assert memorial.body_was_clipped("body") is False

    monkeypatch.setattr(
        memorial, "_display_body",
        lambda _body: "patched body\n\n" + memorial.CLIP_NOTICE,
    )
    assert memorial.body_was_clipped("body") is True


def test_legacy_facade_symbols_remain_importable():
    expected = {
        "_append_line",
        "_button_groups",
        "_cut_at_boundary",
        "_display_body",
        "_fold",
        "_header",
        "_markdown_protected_lines",
        "_normalize_extra_buttons",
        "_normalize_options",
        "_normalize_recommendation",
        "_render_card",
        "_replacement_card",
        "_send_card",
        "_send_text",
        "_quiet_hours_now",
        "body_was_clipped",
        "card_json",
        "ledger_lock",
        "parse_authored_cards",
    }
    assert expected <= set(vars(memorial))
