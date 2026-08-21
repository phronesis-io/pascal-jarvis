"""Tests for the mail-triage task (RSS-for-email)."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tasks"))

import mail_triage_lib as m  # noqa: E402

POST = ROOT / "tasks" / "mail_triage_post.py"

SAMPLE_BUFFER = """\
### WmgyOE== | lark_mail | Gmail 小组 <forwarding-noreply@google.com> | 2026-06-12T16:57:00+0800 | private | buffer
📧 (Gmail) 转发确认
From: Gmail 小组 <forwarding-noreply@google.com>
Mailbox: me [INBOX]
Date: 2026-06-12 16:57
Subject: (Gmail) 转发确认
(正文未注入——需要时: lark-cli mail +message --mailbox 'me' --message-id 'WmgyOE==')

### imap:user_1998@163.com:1:1000000042 | mail_163 | alice <alice@example.org> | 2026-06-11T08:04:38+0800 | private | buffer
📧 Re: your dataset proposal
From: alice <alice@example.org>
Mailbox: user_1998@163.com [INBOX] UID 1000000042
Date: 2026-06-11T08:04:38+0800
Subject: Re: your dataset proposal
(正文未注入——163 邮件用 IMAP 按需拉取该 UID)
"""


# --- parsing -----------------------------------------------------------------
def test_parse_buffer_extracts_fields():
    entries = m.parse_buffer(SAMPLE_BUFFER)
    assert len(entries) == 2
    lark, imap = entries
    assert lark["event_id"] == "WmgyOE=="
    assert lark["source_id"] == "lark_mail"
    assert lark["subject"] == "(Gmail) 转发确认"
    assert imap["source_id"] == "mail_163"
    assert imap["sender"].startswith("alice")
    assert imap["subject"] == "Re: your dataset proposal"


def test_imap_coords_extracts_label_and_uid():
    e = {"event_id": "imap:user_1998@163.com:1:1000000042"}
    label, uid = m._imap_coords(e)
    assert label == "user_1998@163.com"
    assert uid == 1000000042


def test_imap_coords_none_for_lark():
    assert m._imap_coords({"event_id": "WmgyOE=="}) is None


def test_lark_mailbox_parsing():
    assert m._lark_mailbox({"mailbox_raw": "me [INBOX]"}) == "me"
    assert m._lark_mailbox(
        {"mailbox_raw": "contact@eigenflux.one [INBOX]"}) == "contact@eigenflux.one"
    assert m._lark_mailbox({"mailbox_raw": ""}) == "me"


def test_html_to_text_strips_tags():
    out = m._html_to_text("<p>Hello</p><br/><b>world</b> &amp; more")
    assert "Hello" in out and "world" in out and "&" in out
    assert "<" not in out


# --- collect_new dedup + body attach -----------------------------------------
def _setup_dirs(tmp_path, monkeypatch, buffer_text, triaged=None):
    mem = tmp_path / "memory"
    (mem / "system").mkdir(parents=True)
    (mem / "system" / "inbox_private_mail.md").write_text(buffer_text)
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    if triaged:
        md = tmp_path / "mail"
        md.mkdir(parents=True, exist_ok=True)
        (md / "triaged.jsonl").write_text(
            "\n".join(json.dumps(t) for t in triaged) + "\n")


def test_collect_new_skips_triaged_and_attaches_body(tmp_path, monkeypatch):
    _setup_dirs(tmp_path, monkeypatch, SAMPLE_BUFFER,
                triaged=[{"event_id": "WmgyOE=="}])
    monkeypatch.setattr(m, "fetch_imap_bodies",
                        lambda uids, folder="INBOX": {1000000042: "Hi there, lets chat"})
    monkeypatch.setattr(m, "fetch_lark_body", lambda mb, mid: "should not be called")

    recs = m.collect_new()
    # Lark one already triaged → only the imap one remains.
    assert len(recs) == 1
    assert recs[0]["event_id"].endswith("1000000042")
    assert recs[0]["body"] == "Hi there, lets chat"


def test_main_writes_pending_and_prints(tmp_path, monkeypatch, capsys):
    _setup_dirs(tmp_path, monkeypatch, SAMPLE_BUFFER,
                triaged=[{"event_id": "WmgyOE=="}])
    monkeypatch.setattr(m, "fetch_imap_bodies",
                        lambda uids, folder="INBOX": {1000000042: "Hi there"})
    rc = m.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "EVENT_ID: imap:user_1998@163.com:1:1000000042" in out
    assert "Hi there" in out
    pend = json.loads(m.pending_path().read_text())
    assert [p["event_id"] for p in pend] == ["imap:user_1998@163.com:1:1000000042"]


def test_collect_new_empty_when_all_triaged(tmp_path, monkeypatch):
    _setup_dirs(tmp_path, monkeypatch, SAMPLE_BUFFER,
                triaged=[{"event_id": "WmgyOE=="},
                         {"event_id": "imap:user_1998@163.com:1:1000000042"}])
    monkeypatch.setattr(m, "fetch_imap_bodies", lambda *a, **k: {})
    assert m.collect_new() == []


def test_collect_new_body_fetch_failure_degrades(tmp_path, monkeypatch):
    _setup_dirs(tmp_path, monkeypatch, SAMPLE_BUFFER)
    monkeypatch.setattr(m, "fetch_imap_bodies", lambda *a, **k: {})  # fetch fails
    monkeypatch.setattr(m, "fetch_lark_body", lambda mb, mid: "")
    recs = m.collect_new()
    assert len(recs) == 2
    for r in recs:
        assert "拉取失败" in r["body"]


# --- post.py: dedup recording + quiet-hours hold -----------------------------
def _run_post(reply: str, tmp_path, quiet="awake", pending=None) -> str:
    md = tmp_path / "mail"
    md.mkdir(parents=True, exist_ok=True)
    if pending is not None:
        (md / ".pending_batch.json").write_text(json.dumps(pending))
    env = {"JARVIS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin",
           "MEMORY_DIR": str(tmp_path / "memory"),
           "JARVIS_EF_QUIET_OVERRIDE": quiet}
    r = subprocess.run([sys.executable, str(POST)], input=reply,
                       capture_output=True, text=True, env=env)
    return r.stdout


PENDING = [
    {"event_id": "a1", "sender": "x", "subject": "s1"},
    {"event_id": "b2", "sender": "y", "subject": "s2"},
]


def test_post_records_nonurgent_notice_and_all_triaged(tmp_path):
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"},
                   {"event_id": "b2", "decision": "silent"}],
        "user_message": "📬 来自 X 的邮件", "urgent": False})
    out = _run_post(reply, tmp_path, pending=PENDING)
    # Non-urgent pushed mail is transported too (the card rides the CARD
    # route); only .urgent_send stays urgent-only.
    assert "来自 X" in out
    assert not (tmp_path / ".urgent_send").exists()
    assert "来自 X" in (tmp_path / "memorials.jsonl").read_text()
    rows = [json.loads(x) for x in
            (tmp_path / "mail" / "triaged.jsonl").read_text().splitlines() if x]
    assert {r["event_id"] for r in rows} == {"a1", "b2"}
    # pending consumed
    assert not (tmp_path / "mail" / ".pending_batch.json").exists()


def test_post_silent_when_no_message(tmp_path):
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "silent"},
                   {"event_id": "b2", "decision": "silent"}],
        "user_message": "", "urgent": False})
    out = _run_post(reply, tmp_path, pending=PENDING)
    assert out.strip() == ""
    rows = [json.loads(x) for x in
            (tmp_path / "mail" / "triaged.jsonl").read_text().splitlines() if x]
    assert {r["event_id"] for r in rows} == {"a1", "b2"}


def test_post_quiet_hours_still_hands_nonurgent_card_to_loop(tmp_path):
    # Quiet-hour deferral belongs to heartbeat_loop's intact-card queue, so
    # the post-hook prints the card even at night — it must NOT touch
    # .urgent_send (that would break through the queue).
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"}],
        "user_message": "📬 夜间来信", "urgent": False})
    out = _run_post(reply, tmp_path, quiet="quiet",
                    pending=[{"event_id": "a1", "subject": "s"}])
    assert "夜间来信" in out
    assert not (tmp_path / ".urgent_send").exists()
    ledger = (tmp_path / "memorials.jsonl").read_text(encoding="utf-8")
    assert "夜间来信" in ledger and '"attention": "notice"' in ledger
    # still recorded as triaged (won't be re-read)
    rows = (tmp_path / "mail" / "triaged.jsonl").read_text()
    assert "a1" in rows
    assert not (tmp_path / "mail" / "mail_backlog.jsonl").exists()


def test_post_prints_one_card_per_nonurgent_pushed_email(tmp_path):
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"},
                   {"event_id": "b2", "decision": "push"}],
        "user_messages": [
            {"event_id": "a1", "title": "安全提醒", "body": "新设备登录"},
            {"event_id": "b2", "title": "合作来信", "body": "有人约你聊项目"},
        ],
        "urgent": False,
    })
    out = _run_post(reply, tmp_path, pending=PENDING)
    cards = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(cards) == 2
    rows = [json.loads(line) for line in
            (tmp_path / "memorials.jsonl").read_text().splitlines()]
    creates = [row for row in rows if row.get("ev") == "create"]
    assert [row["title"] for row in creates] == ["安全提醒", "合作来信"]
    assert all(row["attention"] == "notice" for row in creates)
    assert not (tmp_path / "mail" / "mail_backlog.jsonl").exists()


def test_post_nonurgent_card_carries_its_memorial_id(tmp_path):
    """Regression (2026-08-21): 12 of 13 mail memorials since 7/31 went
    create→lapse — non-urgent pushed mail was created with send=False and
    nobody printed the card, so it fed the retired web notice stream (i.e.
    nowhere). The printed card must carry the memorial id so heartbeat_loop
    can route it and record delivery back onto the ledger."""
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"}],
        "user_messages": [
            {"event_id": "a1", "title": "银行扣款异常", "body": "同笔重复扣款"},
        ],
        "urgent": False,
    })
    out = _run_post(reply, tmp_path, pending=PENDING)
    rows = [json.loads(line) for line in
            (tmp_path / "memorials.jsonl").read_text().splitlines()]
    create = next(row for row in rows if row.get("ev") == "create")
    card = json.loads(out.strip().splitlines()[0])
    ids = [action.get("value", {}).get("id")
           for element in card.get("elements", [])
           for action in element.get("actions", [])
           if action.get("value", {}).get("action") == "memorial"]
    assert create["id"] in ids


def test_post_urgent_breaks_through_quiet_hours(tmp_path):
    reply = json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"}],
        "user_message": "📬 紧急", "urgent": True})
    out = _run_post(reply, tmp_path, quiet="quiet",
                    pending=[{"event_id": "a1", "subject": "s"}])
    assert "紧急" in out
    assert (tmp_path / ".urgent_send").exists()


def test_post_parse_failure_does_not_record(tmp_path):
    out = _run_post("not json at all", tmp_path, pending=PENDING)
    assert out.strip() == ""
    # pending preserved (retry next cycle), nothing triaged
    assert (tmp_path / "mail" / ".pending_batch.json").exists()
    assert not (tmp_path / "mail" / "triaged.jsonl").exists()
