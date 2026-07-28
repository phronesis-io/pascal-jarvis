"""Reply drafts: helpful text, never a delivery claim.

Jarvis cannot send mail. Every test here defends one of the two ways this
feature could hurt: claiming a reply happened, or turning every inbound email
into another thing to process.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard.db as db_module  # noqa: E402
from core import mail_draft  # noqa: E402


@pytest.fixture()
def draft_db(tmp_path, monkeypatch):
    db_module.DB_PATH = tmp_path / "drafts.db"
    db_module._connection = None
    mail_draft._initialized = False
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    (tmp_path / "memory" / "warm").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    if db_module._connection:
        db_module._connection.close()
        db_module._connection = None
    db_module.DB_PATH = db_module._DEFAULT_DB_PATH
    mail_draft._initialized = False


class TestStore:
    def test_saves_and_reads_back(self, draft_db):
        did = mail_draft.save_draft("evt1", "Alice", "Re: 见一面", "下周二可以。")
        row = mail_draft.get_draft(did)
        assert row["body"] == "下周二可以。" and row["status"] == "open"

    def test_empty_draft_is_refused(self, draft_db):
        with pytest.raises(ValueError):
            mail_draft.save_draft("evt1", "Alice", "s", "   ")

    def test_one_draft_per_email(self, draft_db):
        mail_draft.save_draft("evt1", "Alice", "s", "body")
        assert mail_draft.has_draft_for("evt1") is True
        assert mail_draft.has_draft_for("evt2") is False

    def test_status_transitions(self, draft_db):
        did = mail_draft.save_draft("evt1", "A", "s", "b")
        assert mail_draft.set_status(did, mail_draft.STATUS_USED)
        assert mail_draft.get_draft(did)["status"] == "used"

    def test_unknown_status_refused(self, draft_db):
        did = mail_draft.save_draft("evt1", "A", "s", "b")
        with pytest.raises(ValueError):
            mail_draft.set_status(did, "sent")     # there is no 'sent'

    def test_prune_keeps_open_drafts(self, draft_db):
        keep = mail_draft.save_draft("e1", "A", "s", "still waiting")
        gone = mail_draft.save_draft("e2", "B", "s", "handled")
        mail_draft.set_status(gone, mail_draft.STATUS_DROPPED)
        db_module.get_db().execute(
            "UPDATE mail_drafts SET created_at = '2020-01-01 00:00'")
        db_module.get_db().commit()
        assert mail_draft.prune(days=30) == 1
        assert mail_draft.get_draft(keep) is not None


class TestNoDeliveryClaim:
    def test_no_option_claims_the_mail_was_sent(self, draft_db):
        labels = " ".join(o["label"] for o in mail_draft.DRAFT_OPTIONS)
        for forbidden in ("已发", "已回复", "帮你回", "发送成功"):
            assert forbidden not in labels
        assert "我去发" in labels          # the human is the sender

    def test_no_status_value_means_sent(self, draft_db):
        assert "sent" not in (mail_draft.STATUS_OPEN, mail_draft.STATUS_USED,
                              mail_draft.STATUS_REDO, mail_draft.STATUS_DROPPED)

    def test_card_section_says_jarvis_cannot_send(self, draft_db):
        text = mail_draft.card_section("md_1", "正文")
        assert "不能替你发" in text

    def test_options_carry_the_draft_id(self, draft_db):
        opts = mail_draft.options_for("md_abc")
        assert all(o["action"]["params"]["id"] == "md_abc" for o in opts)

    def test_buttons_are_in_the_shape_the_executor_dispatches(self, draft_db):
        """Regression: these were first written as the CLI's 'type:k=v' string.

        _execute_action calls action.get("type"), so a string raises inside the
        callback thread — a button that renders, taps, and does nothing.
        """
        from core.actions import ActionProcessor
        for opt in mail_draft.options_for("md_abc"):
            action = opt["action"]
            assert isinstance(action, dict), "action must be a dict, not a spec string"
            assert isinstance(action.get("params"), dict)
            assert hasattr(ActionProcessor, f"_do_{action['type']}")

    def test_every_button_status_round_trips_through_the_handler(
            self, draft_db, monkeypatch):
        from core.actions import ActionProcessor
        did = mail_draft.save_draft("e1", "A", "s", "b")
        ap = ActionProcessor(jarvis_dir=str(draft_db),
                             memory_dir=str(draft_db / "memory"),
                             jobs_dir=str(draft_db / "jobs"), log_file="")
        for opt in mail_draft.options_for(did):
            raw = "|".join(f"{k}={v}" for k, v in opt["action"]["params"].items())
            result = ap._do_mail_draft_status(raw)
            assert result.startswith("✅"), result
        assert mail_draft.get_draft(did)["status"] == mail_draft.STATUS_DROPPED


class TestVoice:
    def test_default_admits_it_has_no_voice(self, draft_db):
        assert "还没有设定" in mail_draft.voice_guidance()

    def test_memory_file_supplies_voice(self, draft_db, monkeypatch):
        (draft_db / "memory" / "warm" / "mail_voice.md").write_text(
            "写得像便条，不用敬语。", encoding="utf-8")
        monkeypatch.setattr("core.config.Config",
                            lambda *a, **k: type("C", (), {"get": lambda s, k, d=None: d})())
        assert "像便条" in mail_draft.voice_guidance()

    def test_no_personality_is_hardcoded_in_the_repo(self, draft_db):
        """A fresh install must write in nobody's voice, not a stranger's."""
        src = Path(mail_draft.__file__).read_text(encoding="utf-8")
        assert "Pascal" not in src


class TestPostHookWiring:
    """The triage post-hook must degrade to today's behavior, never worse."""

    def _run_post(self, tmp_path, payload, monkeypatch):
        created = []
        import core.memorial as memorial
        monkeypatch.setattr(memorial, "create",
                            lambda **kw: (created.append(kw), ("mem_x", True))[1])
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks"))
        import importlib
        mod = importlib.import_module("mail_triage_post")
        importlib.reload(mod)
        monkeypatch.setattr(mod, "_record_triaged", lambda d: None)
        mod.main()
        return created

    def test_email_without_a_draft_stays_a_plain_notice(self, draft_db, monkeypatch):
        created = self._run_post(draft_db, {
            "triage": [{"event_id": "e1", "decision": "push"}],
            "user_messages": [{"event_id": "e1", "title": "T", "body": "B"}],
        }, monkeypatch)
        assert created[0]["attention"] == "notice"
        assert created[0]["options"] is None
        assert "草稿" not in created[0]["body"]

    def test_draft_is_attached_to_the_same_card(self, draft_db, monkeypatch):
        created = self._run_post(draft_db, {
            "triage": [{"event_id": "e1", "decision": "push"}],
            "user_messages": [{"event_id": "e1", "title": "T", "body": "B"}],
            "drafts": [{"event_id": "e1", "to": "Alice", "subject": "Re: x",
                        "body": "下周二可以。", "why": "她在等时间"}],
        }, monkeypatch)
        assert len(created) == 1                     # one card, one matter
        assert "下周二可以。" in created[0]["body"]
        assert created[0]["attention"] == "decision"  # a draft is an ask
        assert mail_draft.list_drafts()[0]["to_name"] == "Alice"

    def test_draft_for_an_unsurfaced_email_is_ignored(self, draft_db, monkeypatch):
        """A draft without its own push card would be a reply to nothing."""
        created = self._run_post(draft_db, {
            "triage": [{"event_id": "e1", "decision": "silent"}],
            "user_messages": [],
            "drafts": [{"event_id": "e1", "to": "A", "body": "hi"}],
        }, monkeypatch)
        assert created == []
        assert mail_draft.list_drafts() == []

    def test_second_pass_does_not_re_draft_the_same_email(
            self, draft_db, monkeypatch):
        payload = {
            "triage": [{"event_id": "e1", "decision": "push"}],
            "user_messages": [{"event_id": "e1", "title": "T", "body": "B"}],
            "drafts": [{"event_id": "e1", "to": "A", "body": "第一版"}],
        }
        self._run_post(draft_db, payload, monkeypatch)
        created = self._run_post(draft_db, payload, monkeypatch)
        assert len(mail_draft.list_drafts()) == 1     # not nagged twice
        assert "草稿" not in created[0]["body"]

    def test_draft_failure_does_not_lose_the_email_card(
            self, draft_db, monkeypatch):
        monkeypatch.setattr(mail_draft, "save_draft",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
        created = self._run_post(draft_db, {
            "triage": [{"event_id": "e1", "decision": "push"}],
            "user_messages": [{"event_id": "e1", "title": "T", "body": "B"}],
            "drafts": [{"event_id": "e1", "to": "A", "body": "x"}],
        }, monkeypatch)
        assert len(created) == 1 and created[0]["body"] == "B"


class TestPromptContract:
    def test_prompt_forbids_committing_on_his_behalf(self):
        text = Path("HEARTBEAT.md").read_text(encoding="utf-8")
        block = text[text.index("### mail-triage"):]
        block = block[:block.index("## Check-in")]
        assert "不许替他承诺" in block
        assert "没有发信能力" in block
        assert '"drafts"' in block
