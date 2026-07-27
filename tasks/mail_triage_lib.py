#!/usr/bin/env python3
"""Mail-triage engine — the "RSS for email" reader.

Pascal asked (2026-06-13): treat every incoming email like an RSS item — when
it arrives, READ THE WHOLE BODY, then think about it and surface what matters.

The perception layer (sources/lark_mail.py + sources/imap_mail.py) already syncs
email *metadata* (from/subject/date) into memory/system/inbox_private_mail.md but
deliberately does NOT inject bodies (anti-injection + token thrift). This module
is the second stage: for each NOT-YET-TRIAGED email in that buffer, it fetches
the FULL body on demand (IMAP RFC822 for 163, `lark-cli mail +message` for Feishu),
and prints an enriched block that the mail-triage heartbeat prompt reads.

Pure read of the buffer + network fetch. The only state it writes is a pending
batch marker (so post.py knows which event_ids were shown this cycle). Dedup
state (`triaged.jsonl`) is read here, written by post.py.

Never raises on a single email's fetch failure — degrades to metadata + a note,
so one bad message never starves the rest.
"""
from __future__ import annotations

import email
import html as _html
import imaplib
import json
import os
import re
import ssl
import time
from email.header import decode_header
from pathlib import Path

# --- knobs -------------------------------------------------------------------
MAX_PER_CYCLE = int(os.environ.get("JARVIS_MAIL_MAX_PER_CYCLE", "15"))
BODY_CHARS = int(os.environ.get("JARVIS_MAIL_BODY_CHARS", "1800"))
CONNECT_TIMEOUT = 30
DEFAULT_163_SECRET = "~/Desktop/jarvis/secrets/163_imap.json"


def _jarvis_dir() -> Path:
    return Path(os.environ.get(
        "JARVIS_DIR", Path(__file__).resolve().parent.parent))


def _memory_dir() -> Path:
    return Path(os.environ.get("MEMORY_DIR", _jarvis_dir() / "memory"))


def _mail_dir() -> Path:
    p = _jarvis_dir() / "mail"
    p.mkdir(parents=True, exist_ok=True)
    return p


def buffer_path() -> Path:
    return _memory_dir() / "system" / "inbox_private_mail.md"


def triaged_path() -> Path:
    return _mail_dir() / "triaged.jsonl"


def pending_path() -> Path:
    return _mail_dir() / ".pending_batch.json"


# --- buffer parsing ----------------------------------------------------------
def parse_buffer(text: str) -> list[dict]:
    """Parse inbox_private_mail.md into a list of email entries (oldest first).

    Each block:
        ### <event_id> | <source_id> | <who> | <ts> | <sensitivity> | buffer
        📧 <title>
        From: <sender>
        Mailbox: <label> [INBOX] ...
        Date: ...
        Subject: <subject>
        (正文未注入...)
    """
    entries: list[dict] = []
    block: list[str] = []

    def flush(blk: list[str]):
        if not blk:
            return
        head = blk[0]
        if not head.startswith("### "):
            return
        parts = [p.strip() for p in head[4:].split(" | ")]
        if len(parts) < 5:
            return
        event_id, source_id = parts[0], parts[1]
        who, ts, sensitivity = parts[2], parts[3], parts[4]
        sender = subject = mailbox = ""
        for ln in blk[1:]:
            if ln.startswith("From:"):
                sender = ln[5:].strip()
            elif ln.startswith("Subject:"):
                subject = ln[8:].strip()
            elif ln.startswith("Mailbox:"):
                mailbox = ln[8:].strip()
        entries.append({
            "event_id": event_id, "source_id": source_id,
            "who": who, "ts": ts, "sensitivity": sensitivity,
            "sender": sender or who, "subject": subject, "mailbox_raw": mailbox,
        })

    for line in text.splitlines():
        if line.startswith("### "):
            flush(block)
            block = [line]
        elif block:
            block.append(line)
    flush(block)
    return entries


def _imap_coords(entry: dict) -> tuple[str, int] | None:
    """For an imap_mail entry, return (label, uid) from the event_id
    `imap:<label>:<uidvalidity>:<uid>`. label may not contain ':'."""
    eid = entry["event_id"]
    if not eid.startswith("imap:"):
        return None
    parts = eid.split(":")
    if len(parts) < 4:
        return None
    try:
        uid = int(parts[-1])
    except ValueError:
        return None
    label = ":".join(parts[1:-2])
    return label, uid


def _lark_mailbox(entry: dict) -> str:
    """Mailbox id for `lark-cli mail +message` — first token of the Mailbox
    line ('me [INBOX]' -> 'me')."""
    mb = (entry.get("mailbox_raw") or "").split("[")[0].strip()
    return mb.split()[0] if mb else "me"


# --- body fetch --------------------------------------------------------------
def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</p>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def _decode_hdr(raw: str) -> str:
    try:
        out = []
        for txt, enc in decode_header(raw):
            if isinstance(txt, bytes):
                out.append(txt.decode(enc or "utf-8", "replace"))
            else:
                out.append(txt)
        return "".join(out)
    except Exception:
        return raw


def _msg_to_text(msg) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    body = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
                if body.strip():
                    break
        if not body.strip():
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    try:
                        raw = part.get_payload(decode=True).decode(
                            part.get_content_charset() or "utf-8", "replace")
                        body = _html_to_text(raw)
                    except Exception:
                        continue
                    if body.strip():
                        break
    else:
        try:
            raw = msg.get_payload(decode=True)
            dec = raw.decode(msg.get_content_charset() or "utf-8", "replace") \
                if raw else ""
            body = _html_to_text(dec) if msg.get_content_type() == "text/html" \
                else dec
        except Exception:
            body = ""
    return body.strip()


def _load_163_secret() -> dict | None:
    # Prefer the path declared in sources.yaml; fall back to the default.
    path = os.environ.get("JARVIS_163_SECRET", "")
    if not path:
        try:
            import yaml
            cfg = yaml.safe_load((_jarvis_dir() / "sources.yaml").read_text())
            for src in (cfg or {}).get("sources", []):
                if src.get("type") == "imap_mail":
                    path = src.get("collect", {}).get("secret_file", "")
                    break
        except Exception:
            path = ""
    path = os.path.expanduser(path or DEFAULT_163_SECRET)
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def fetch_imap_bodies(uids: list[int], folder: str = "INBOX") -> dict[int, str]:
    """Fetch full text bodies for a batch of 163 UIDs over one connection.
    Returns {uid: body_text}; missing/failed uids simply absent."""
    out: dict[int, str] = {}
    if not uids:
        return out
    sec = _load_163_secret()
    if not sec:
        return out
    host = sec.get("imap_host", "imap.163.com")
    port = int(sec.get("imap_port", 993))
    try:
        M = imaplib.IMAP4_SSL(host, port,
                              ssl_context=ssl.create_default_context())
        M.socket().settimeout(CONNECT_TIMEOUT)
        typ, _ = M.login(sec["email"], sec["auth_code"])
        if typ != "OK":
            return out
        # 163/Coremail requires an IMAP ID handshake before SELECT.
        try:
            tag = M._new_tag().decode()
            M.send((f'{tag} ID ("name" "jarvis-mail" "version" "1.0")'
                    "\r\n").encode())
            deadline = time.time() + 10
            while time.time() < deadline:
                line = M.readline()
                if not line or line.decode(errors="replace").startswith(tag):
                    break
        except Exception:
            pass
        typ, _ = M.select(folder, readonly=True)
        if typ != "OK":
            try:
                M.logout()
            except Exception:
                pass
            return out
        for u in uids:
            try:
                typ, data = M.uid("fetch", str(u), "(RFC822)")
                if typ == "OK" and data and data[0]:
                    msg = email.message_from_bytes(data[0][1])
                    out[u] = _msg_to_text(msg)
            except Exception:
                continue
        try:
            M.logout()
        except Exception:
            pass
    except Exception:
        return out
    return out


def fetch_lark_body(mailbox: str, message_id: str) -> str:
    """Fetch a Feishu mail body via lark-cli; returns plain text ('' on fail)."""
    import subprocess
    try:
        r = subprocess.run(
            ["lark-cli", "mail", "+message", "--mailbox", mailbox,
             "--message-id", message_id, "--format", "json"],
            capture_output=True, text=True, timeout=40)
        if r.returncode != 0:
            return ""
        # Output may carry a leading 'tip:' line before the JSON.
        s = r.stdout
        i = s.find("{")
        if i < 0:
            return ""
        obj = json.loads(s[i:])
        data = obj.get("data", obj) or {}
        text = data.get("body_plain_text") or data.get("body_text") or ""
        if not text:
            html_body = data.get("body_html") or ""
            text = _html_to_text(html_body)
        return text.strip()
    except Exception:
        return ""


# --- the cycle ---------------------------------------------------------------
def collect_new(max_n: int = MAX_PER_CYCLE) -> list[dict]:
    """Find not-yet-triaged emails in the buffer, fetch their bodies.
    Returns enriched records (oldest first), capped to max_n."""
    bp = buffer_path()
    if not bp.exists():
        return []
    entries = parse_buffer(bp.read_text(encoding="utf-8", errors="ignore"))
    seen = {e.get("event_id") for e in _read_jsonl(triaged_path())}
    fresh = [e for e in entries if e["event_id"] not in seen]
    if not fresh:
        return []
    fresh = fresh[:max_n]

    # Batch the IMAP fetches over a single connection.
    imap_uid_map: dict[int, dict] = {}
    for e in fresh:
        coords = _imap_coords(e)
        if coords:
            e["_uid"] = coords[1]
            imap_uid_map[coords[1]] = e
    bodies = fetch_imap_bodies(list(imap_uid_map.keys())) if imap_uid_map else {}

    out = []
    for e in fresh:
        body = ""
        if "_uid" in e:
            body = bodies.get(e["_uid"], "")
            if not body:
                body = "(正文拉取失败：IMAP fetch 未返回，仅有元数据)"
        elif e["source_id"] == "lark_mail":
            body = fetch_lark_body(_lark_mailbox(e), e["event_id"])
            if not body:
                body = "(正文拉取失败：lark-cli 未返回，仅有元数据)"
        else:
            body = "(未知来源，未拉取正文)"
        if len(body) > BODY_CHARS:
            body = body[:BODY_CHARS] + "…[截断]"
        e["body"] = body
        out.append(e)
    return out


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return rows


def render(records: list[dict]) -> str:
    """Render enriched emails as the DATA block the heartbeat prompt reads."""
    chunks = []
    for e in records:
        chunks.append(
            f"--- EMAIL ---\n"
            f"EVENT_ID: {e['event_id']}\n"
            f"FROM: {e['sender']}\n"
            f"MAILBOX: {e.get('mailbox_raw', '')}\n"
            f"DATE: {e['ts']}\n"
            f"SUBJECT: {e.get('subject', '')}\n"
            f"BODY:\n{e['body']}\n")
    return "\n".join(chunks)


def main() -> int:
    records = collect_new()
    if not records:
        # Nothing new — print nothing so the cycle stays cheap.
        return 0
    # Mark the batch as pending so post.py can record them all as triaged.
    pending = pending_path()
    pending.parent.mkdir(parents=True, exist_ok=True)
    tmp = pending.with_suffix(".tmp")
    tmp.write_text(
        json.dumps([{"event_id": e["event_id"], "sender": e["sender"],
                     "subject": e.get("subject", "")} for e in records],
                   ensure_ascii=False))
    os.replace(tmp, pending)
    print(render(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
