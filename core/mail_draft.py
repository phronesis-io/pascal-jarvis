"""Reply drafts for mail that actually needs an answer.

`mail-triage` reads every inbound body and surfaces the rare one that matters,
then stops — leaving the whole cost of answering on the human. This module adds
the other half: for a pushed email that plainly wants a reply, keep a draft in
the user's own voice next to the card.

**Nothing here sends anything.** Jarvis's mail stack is read-only (IMAP fetch +
`lark-cli mail` read); there is no send transport, and adding an external
mutation needs its own authority, verification, and rollback design (CLAUDE.md).
So a draft is exactly what it says: text to use, with a truthful button set that
never claims delivery. This also happens to be Town's own rule — it never sends
an email without approval.

Voice guidance is per-user configuration, not code. It comes from `jarvis.yaml`
(`mail.voice`) and an optional memory file, so a fresh install writes in nobody's
voice rather than in a stranger's.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from core.timeutil import now_local, now_local_str

ROOT = Path(__file__).resolve().parent.parent

STATUS_OPEN = "open"          # drafted, waiting on the human
STATUS_USED = "used"          # he said he'd send it
STATUS_REDO = "redo"          # he wants another version
STATUS_DROPPED = "dropped"    # not replying after all

MAX_BODY = 2000
KEEP_DAYS = 30

_sys_path_added = False
_initialized = False


def _get_db():
    global _sys_path_added
    if not _sys_path_added:
        sys.path.insert(0, str(ROOT))
        _sys_path_added = True
    from core.db import get_db
    return get_db()


def _init() -> None:
    global _initialized
    if _initialized:
        return
    db = _get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS mail_drafts (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL DEFAULT '',
            to_name TEXT NOT NULL DEFAULT '',
            subject TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            rationale TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            memorial_id TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_mail_drafts_status
            ON mail_drafts(status, created_at);
    """)
    db.commit()
    _initialized = True


# ── voice ────────────────────────────────────────────────────────────────────

DEFAULT_VOICE = (
    "还没有设定语气。先按对方的语言写，简短、直接、不用客套模板；"
    "不确定的事不要替他承诺。"
)


def voice_guidance() -> str:
    """How he writes. Per-user config — never a hardcoded personality.

    Order: jarvis.yaml `mail.voice` → memory file `warm/mail_voice.md` →
    a neutral default that tells the model it has no voice to imitate.
    """
    try:
        from core.config import Config
        configured = str(Config().get("mail.voice", "") or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    memory_dir = os.environ.get("MEMORY_DIR")
    if memory_dir:
        candidate = Path(memory_dir) / "warm" / "mail_voice.md"
        try:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text[:1500]
        except OSError:
            pass
    return DEFAULT_VOICE


# ── store ────────────────────────────────────────────────────────────────────


def save_draft(event_id: str, to_name: str, subject: str, body: str,
               rationale: str = "", memorial_id: str = "") -> str:
    _init()
    body = str(body or "").strip()[:MAX_BODY]
    if not body:
        raise ValueError("空草稿不存")
    did = f"md_{uuid.uuid4().hex[:8]}"
    _get_db().execute(
        "INSERT INTO mail_drafts (id, event_id, to_name, subject, body,"
        " rationale, status, created_at, memorial_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (did, str(event_id or ""), str(to_name or "")[:80],
         str(subject or "")[:200], body, str(rationale or "")[:300],
         STATUS_OPEN, now_local_str(), memorial_id))
    _get_db().commit()
    return did


def get_draft(did: str) -> dict | None:
    _init()
    row = _get_db().execute("SELECT * FROM mail_drafts WHERE id = ?",
                            (did,)).fetchone()
    return dict(row) if row else None


def list_drafts(status: str | None = STATUS_OPEN, limit: int = 30) -> list[dict]:
    _init()
    db = _get_db()
    if status:
        rows = db.execute(
            "SELECT * FROM mail_drafts WHERE status = ?"
            " ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM mail_drafts ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
    return [dict(r) for r in rows]


def set_status(did: str, status: str) -> bool:
    _init()
    if status not in (STATUS_OPEN, STATUS_USED, STATUS_REDO, STATUS_DROPPED):
        raise ValueError(f"未知状态 {status!r}")
    cur = _get_db().execute("UPDATE mail_drafts SET status = ? WHERE id = ?",
                            (status, did))
    _get_db().commit()
    return cur.rowcount == 1


def has_draft_for(event_id: str) -> bool:
    """One draft per email. A second card for the same message is nagging."""
    _init()
    if not event_id:
        return False
    return _get_db().execute(
        "SELECT 1 FROM mail_drafts WHERE event_id = ? LIMIT 1",
        (event_id,)).fetchone() is not None


def prune(days: int = KEEP_DAYS) -> int:
    """Drop resolved drafts past the retention window."""
    _init()
    from datetime import timedelta
    cutoff = (now_local() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    cur = _get_db().execute(
        "DELETE FROM mail_drafts WHERE status != ? AND created_at < ?",
        (STATUS_OPEN, cutoff))
    _get_db().commit()
    return cur.rowcount


# ── card rendering ───────────────────────────────────────────────────────────

# Truthful by construction: no button here claims the mail was sent, because
# nothing in Jarvis can send it.
DRAFT_OPTIONS = [
    {"key": "used", "label": "就用这版，我去发", "status": STATUS_USED},
    {"key": "redo", "label": "重写一版", "status": STATUS_REDO},
    {"key": "drop", "label": "这封不用回", "status": STATUS_DROPPED},
]


def card_section(draft_id: str, body: str) -> str:
    """The draft block appended to that email's own card — one card, one matter."""
    return (f"\n\n---\n**回复草稿**（Jarvis 不能替你发，复制去用）\n\n{body}\n\n"
            f"`草稿 {draft_id}`")


def options_for(draft_id: str) -> list[dict]:
    """Card buttons bound to one draft.

    `action` is the {"type", "params"} dict core.memorial._execute_action
    dispatches on — the 'type:k=v' string is CLI --option syntax and is parsed
    only there, so passing it here would raise inside the callback thread and
    leave a button that looks live but does nothing.
    """
    return [{"key": opt["key"], "label": opt["label"],
             "action": {"type": "mail_draft_status",
                        "params": {"id": draft_id, "status": opt["status"]}}}
            for opt in DRAFT_OPTIONS]


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="core.mail_draft", description="邮件回复草稿")
    sub = p.add_subparsers(dest="cmd", required=True)
    lst = sub.add_parser("list", help="列出草稿")
    lst.add_argument("--all", action="store_true")
    show = sub.add_parser("show", help="看一份草稿全文")
    show.add_argument("id")
    st = sub.add_parser("status", help="改草稿状态")
    st.add_argument("id")
    st.add_argument("value", choices=(STATUS_OPEN, STATUS_USED, STATUS_REDO,
                                      STATUS_DROPPED))
    sub.add_parser("voice", help="看当前用的语气设定从哪来")
    sub.add_parser("prune", help="清理过期的已处理草稿")
    args = p.parse_args(argv)

    if args.cmd == "list":
        rows = list_drafts(status=None if args.all else STATUS_OPEN)
        if not rows:
            print("没有待处理的草稿。")
        for r in rows:
            print(f"{r['id']}  [{r['status']}]  致 {r['to_name'] or '?'}"
                  f"  《{r['subject'] or '无主题'}》  {r['created_at']}")
            print(f"    {r['body'][:120].replace(chr(10), ' ')}")
    elif args.cmd == "show":
        row = get_draft(args.id)
        if not row:
            print(f"没有这份草稿：{args.id}", file=sys.stderr)
            return 1
        print(f"致：{row['to_name']}\n主题：{row['subject']}\n"
              f"状态：{row['status']}\n")
        if row["rationale"]:
            print(f"（为什么这么写：{row['rationale']}）\n")
        print(row["body"])
    elif args.cmd == "status":
        if not set_status(args.id, args.value):
            print(f"没有这份草稿：{args.id}", file=sys.stderr)
            return 1
        print(f"{args.id} → {args.value}")
    elif args.cmd == "voice":
        guidance = voice_guidance()
        origin = ("默认（还没设）" if guidance == DEFAULT_VOICE
                  else "jarvis.yaml mail.voice 或 memory warm/mail_voice.md")
        print(f"来源：{origin}\n\n{guidance}")
    elif args.cmd == "prune":
        print(f"清掉 {prune()} 份过期草稿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
