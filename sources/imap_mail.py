"""imap_mail adapter — incremental new-mail metadata from any IMAP mailbox.

Generic IMAP poller (built for 163/NetEase, works for any IMAP-SSL host).
Like lark_mail, ONLY metadata is ingested (date/from/subject): mail bodies
are untrusted external input (prompt-injection surface) and bulky, so the
main session pulls a body on demand. Register with an inbox_private_*.md
buffer so the core.memory outbound gate keeps it out of outward-facing
contexts (eigenflux-publish, auto-replies).

Credentials never live in sources.yaml (which is in git). The source block
points at a 0600 secrets JSON via `secret_file`:
    {"email":..., "imap_host":..., "imap_port":993, "auth_code":...}

163 quirk: after LOGIN, the client MUST issue an IMAP ID command before
SELECT, or Coremail rejects with "Unsafe Login". Handled in _fetch_new.
"""

from __future__ import annotations

import email
import imaplib
import json
import os
import ssl
import time
from email.header import decode_header

MAX_SIGNALS_PER_RUN = 30
FIRST_RUN_LOOKBACK_H = 24
_CONNECT_TIMEOUT = 20


def _decode_hdr(raw: str) -> str:
    """Decode a possibly MIME-encoded header (=?utf-8?B?..?=) to text."""
    if not raw:
        return ""
    out = []
    try:
        for part, enc in decode_header(raw):
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(part)
    except Exception:
        return raw
    return "".join(out).replace("\r", " ").replace("\n", " ").strip()


def _iso_from_maildate(date_hdr: str, fallback_epoch: float) -> str:
    """RFC-2822 Date header → ISO-8601 with local offset (best effort)."""
    try:
        dt = email.utils.parsedate_to_datetime(date_hdr)
        if dt is not None:
            return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError, OverflowError):
        pass
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(fallback_epoch))


def _load_secret(cfg: dict) -> dict | None:
    path = cfg.get("secret_file")
    if not path:
        return None
    path = os.path.expanduser(path)
    try:
        with open(path) as fh:
            s = json.load(fh)
        if s.get("email") and s.get("auth_code"):
            return s
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _fetch_new(secret: dict, folder: str, since_uid: int,
               first_run: bool) -> tuple[list[dict], int, int]:
    """Network step (monkeypatched in tests). Returns (messages, max_uid,
    uidvalidity). Each message: {uid,int from,subject,date_iso,message_id}.
    Raises RuntimeError(error_type) on failure — never leaks raw exceptions."""
    host = secret.get("imap_host", "imap.163.com")
    port = int(secret.get("imap_port", 993))
    try:
        M = imaplib.IMAP4_SSL(host, port,
                              ssl_context=ssl.create_default_context())
        M.socket().settimeout(_CONNECT_TIMEOUT)
    except Exception:
        raise RuntimeError("connect")
    try:
        typ, _ = M.login(secret["email"], secret["auth_code"])
        if typ != "OK":
            raise RuntimeError("auth")
        # 163/Coremail requires an IMAP ID handshake before SELECT.
        try:
            tag = M._new_tag().decode()
            M.send((f'{tag} ID ("name" "jarvis-perception" "version" "1.0")'
                    "\r\n").encode())
            deadline = time.time() + 10
            while time.time() < deadline:
                line = M.readline()
                if not line or line.decode(errors="replace").startswith(tag):
                    break
        except Exception:
            pass  # ID is best-effort; non-163 servers ignore it
        typ, data = M.select(folder, readonly=True)
        if typ != "OK":
            raise RuntimeError("select")
        typ, uvd = M.status(folder, "(UIDVALIDITY)")
        uidvalidity = 0
        if typ == "OK" and uvd and uvd[0]:
            try:
                uidvalidity = int(uvd[0].decode().split("UIDVALIDITY")[1]
                                  .strip(" ()").split()[0])
            except (IndexError, ValueError):
                uidvalidity = 0
        # Pick the UID search window.
        if first_run:
            since = time.strftime("%d-%b-%Y",
                                  time.localtime(time.time()
                                                 - FIRST_RUN_LOOKBACK_H * 3600))
            typ, ids = M.uid("search", None, f'(SINCE {since})')
        else:
            typ, ids = M.uid("search", None, f"(UID {since_uid + 1}:*)")
        if typ != "OK":
            raise RuntimeError("search")
        uids = [int(x) for x in (ids[0].split() if ids and ids[0] else [])]
        # UID n:* always returns >=1 row; drop ones we've already seen.
        uids = [u for u in uids if u > since_uid]
        uids.sort()
        max_uid = max(uids) if uids else since_uid
        messages = []
        for u in uids[-MAX_SIGNALS_PER_RUN:]:
            typ, d = M.uid("fetch", str(u),
                           "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not d or not d[0]:
                continue
            raw = d[0][1].decode("utf-8", errors="replace")
            msg = email.message_from_string(raw)
            messages.append({
                "uid": u,
                "from": _decode_hdr(msg.get("From", "")),
                "subject": _decode_hdr(msg.get("Subject", "")) or "(无主题)",
                "date_iso": _iso_from_maildate(msg.get("Date", ""), time.time()),
            })
        return messages, max_uid, uidvalidity
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("fetch")
    finally:
        try:
            M.logout()
        except Exception:
            pass


def collect(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    secret = _load_secret(cfg)
    if not secret:
        return [], {**state, "error_type": "no_secret"}

    folder = cfg.get("folder", "INBOX")
    exclude_from = [s.lower() for s in (cfg.get("exclude_from") or [])]
    label = secret.get("email", "imap")

    prev_uv = state.get("uidvalidity") or 0
    last_uid = int(state.get("last_uid") or 0)
    first_run = last_uid == 0

    try:
        messages, max_uid, uidvalidity = _fetch_new(
            secret, folder, last_uid, first_run)
    except RuntimeError as e:
        return [], {**state, "error_type": str(e) or "fetch"}

    # UIDVALIDITY rotation invalidates old UIDs — resync from this batch's top.
    if prev_uv and uidvalidity and uidvalidity != prev_uv:
        return [], {"uidvalidity": uidvalidity, "last_uid": max_uid,
                    "error_type": "uidvalidity_reset"}

    signals = []
    for m in messages:
        sender = m["from"]
        if any(p in sender.lower() for p in exclude_from):
            continue
        subject = m["subject"]
        uid = m["uid"]
        signals.append({
            "event_id": f"imap:{label}:{uidvalidity}:{uid}",
            "ts": m["date_iso"],
            "title": ("📧 " + subject)[:120],
            "summary": f"{sender} → {label}: {subject}"[:200],
            "body": (f"From: {sender}\nMailbox: {label} [{folder}] UID {uid}\n"
                     f"Date: {m['date_iso']}\nSubject: {subject}\n"
                     f"(正文未注入——163 邮件用 IMAP 按需拉取该 UID)"),
            "url": "",
            "actor": {"raw": sender, "resolved": ""},
            "payload": {"mailbox": label, "uid": uid},
        })

    return signals[:MAX_SIGNALS_PER_RUN], {
        "uidvalidity": uidvalidity or prev_uv,
        "last_uid": max_uid,
        "error_type": None,
    }
