"""lark_mail adapter — incremental new-mail metadata from Lark mailboxes.

Polls `lark-cli mail +triage` (metadata only: date/from/subject) for the
user's primary mailbox plus any public mailboxes. Bodies are deliberately
NOT ingested: mail content is untrusted external input (prompt-injection
surface) and bulky — the main session pulls a specific message on demand
via `mail +message`. Register with a perceive buffer named
inbox_private_*.md so the core.memory outbound gate keeps mail metadata
out of outward-facing contexts (eigenflux-publish, auto-replies).
"""

from __future__ import annotations

import json
import subprocess
import time

MAX_SIGNALS_PER_RUN = 30
FIRST_RUN_LOOKBACK_H = 24
_TRIAGE_PAGE = 20


def _date_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _iso(date_str: str) -> str:
    """Triage date 'YYYY-MM-DD HH:MM' → ISO-8601 with local offset."""
    try:
        t = time.strptime(date_str, "%Y-%m-%d %H:%M")
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.mktime(t)))
    except (ValueError, OverflowError):
        return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def _triage(mailbox: str, folder: str) -> list[dict]:
    """One metadata-level +triage call. Raises RuntimeError(error_type)."""
    try:
        r = subprocess.run(
            ["lark-cli", "mail", "+triage", "--as", "user",
             "--mailbox", mailbox, "--filter", json.dumps({"folder": folder}),
             "--max", str(_TRIAGE_PAGE), "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout")
    except Exception:
        raise RuntimeError("crash")
    if r.returncode != 0:
        raise RuntimeError("network")
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError("crash")
    return (data.get("messages") or []) if isinstance(data, dict) else []


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    mailboxes = cfg.get("mailboxes") or ["me"]
    folder = cfg.get("folder", "INBOX")
    exclude_from = [s.lower() for s in (cfg.get("exclude_from") or [])]
    cursors = dict(state.get("cursors") or {})
    signals: list[dict] = []
    error = None

    for mb in mailboxes:
        since = cursors.get(mb) or _date_str(time.time() - FIRST_RUN_LOOKBACK_H * 3600)
        try:
            messages = _triage(mb, folder)
        except RuntimeError as e:
            error = str(e) or "crash"
            continue
        newest = since
        for m in messages:
            date = m.get("date") or ""
            if not date or date < since:  # lexical compare fits 'YYYY-MM-DD HH:MM'
                continue
            if date > newest:
                newest = date  # cursor advances past excluded noise too
            sender = m.get("from") or ""
            if any(p in sender.lower() for p in exclude_from):
                continue
            mid = m.get("message_id") or ""
            if not mid:
                continue
            subject = m.get("subject") or "(无主题)"
            signals.append({
                "event_id": mid,
                "ts": _iso(date),
                "title": ("📧 " + subject)[:120],
                "summary": f"{sender} → {mb}: {subject}"[:200],
                "body": (f"From: {sender}\nMailbox: {mb} [{folder}]\nDate: {date}\n"
                         f"Subject: {subject}\n"
                         f"(正文未注入——需要时: lark-cli mail +message "
                         f"--mailbox '{mb}' --message-id '{mid}')"),
                "url": "",
                "actor": {"raw": sender, "resolved": ""},
                "payload": {"mailbox": mb},
            })
        cursors[mb] = newest

    return signals[:MAX_SIGNALS_PER_RUN], {"cursors": cursors, "error_type": error}
