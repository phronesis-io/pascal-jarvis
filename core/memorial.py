"""Memorial (奏折) cards — the unified "ask Pascal" surface.

Every proactive output that needs Pascal's eyes (mail triage, decisions,
follow-ups, heartbeat asks…) becomes ONE Lark card that states one thing in
plain words, with 2-3 approval buttons plus a「💬 聊聊这个」button — like
Claude Code's AskUserQuestion. Tapping an option = 批红: it is recorded (and
optionally executes an action through ActionProcessor), and the card is
replaced in place with the approved state. Tapping「聊聊这个」injects the
memorial's full context into the p2p conversation via bot.sh's existing
pending-merge channel, so Pascal's NEXT message lands with the topic loaded.

Ledger: `memorials.jsonl` (repo root, same level as engagement_log.jsonl).
Append-only event stream — {"ev":"create"|"decide"|"chat", ...} — folded by id
on read. Writes are O_APPEND single small lines (atomic, safe across the
sidecar process and any CLI caller; core.jsonl.append_jsonl's
read-modify-write is NOT safe here).

Direct sending uses the same bounded retry profile as the heartbeat delivery
layer. A timeout is a failed attempt, never proof of delivery. Delivery state
is appended to the ledger so dedup does not turn a failed first attempt into
six hours of silence. Successful direct sends are mirrored into
heartbeat_outbox.jsonl so the main session knows the card went out.

Buttons follow the card, not the emitter. In priority order a memorial's
options come from: (1) an ``OPTIONS: a | b | c`` line the card author wrote at
the end of the body — these become suggested-reply buttons whose label IS the
sentence Pascal would have typed; (2) ``SOURCE_DEFAULT_PRESET`` for sources
that inherently ask for a decision or a follow-up; (3)「已阅／标为重点」.

CLI (any emitter can send a memorial in one line):
    python3 -m core.memorial send --source mail --title "..." --body "..." \
        --preset fyi
    python3 -m core.memorial send --source x --title t --body b \
        --option '准=intent_close:id=xxx,outcome=done' --option '缓'
    python3 -m core.memorial send --source x --title t --body b \
        --options '加钱|限流到月底|让它自然停'
    python3 -m core.memorial list [--pending]
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.card import build_card, extract_card_text
from core.jsonl import read_jsonl
from core.timeutil import now_local_str

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))

# The chat button is framework-owned: every memorial gets it, emitters can't
# claim the key for their own options.
CHAT_OPT_KEY = "chat"
CHAT_BUTTON_LABEL = "💬 聊聊这个"

# Same retry profile as core.heartbeat_loop (REQ-11).
SEND_RETRY_DELAYS = (2, 5)

# A re-tap of「聊聊这个」within this window is a no-op (the first tap already
# sent the opener + queued the injection): Lark re-pushes un-ACKed callback
# events and Pascal re-taps after a client-side "操作失败", so chat() must not
# stack duplicate openers/injections.
CHAT_RETAP_THROTTLE_S = 120

# The ledger keeps the full event; the phone card is the decision surface, not
# the archive.  This bound prevents an emitter regression from recreating the
# old wall-of-text experience while「聊聊这个」still receives richer context.
CARD_BODY_MAX_CHARS = 900
CARD_BODY_MAX_LINES = 8
CHAT_CONTEXT_MAX_CHARS = 1500

# Identical pending memorial within this window → don't create/send another.
# Mirrors heartbeat_loop's 6h _is_duplicate_send (born of the 6/10 incident:
# same error card 7 times in 12h); the outbox-based dedup there can't see
# memorial cards because each embeds a unique id, so we dedup on content at
# the ledger level instead.
DEDUP_WINDOW_S = 6 * 3600

# Memorials are never flattened into the ordinary night digest.  A card is
# the product contract: one event, one intact card, with its options and
# 「聊聊这个」button still present when it is released after quiet hours.
# heartbeat_loop owns draining this queue at the normal batch windows.
MEMORIAL_QUEUE_FILE = "memorial_queue.jsonl"

# Common 批红 combos so emitters don't hand-roll options. All record-only
# (action=None): the tap writes the ledger, which heartbeat tasks and the
# main session read back.
PRESETS: dict[str, list[dict]] = {
    "decision": [
        {"key": "approve", "label": "同意", "action": None},
        {"key": "defer", "label": "暂不处理", "action": None},
        {"key": "reject", "label": "不采纳", "action": None},
    ],
    "fyi": [
        {"key": "read", "label": "已阅", "action": None},
        {"key": "watch", "label": "标为重点", "action": None},
    ],
    "followup": [
        {"key": "done", "label": "做了", "action": None},
        {"key": "later", "label": "还没做", "action": None},
        {"key": "stop", "label": "这次跳过", "action": None},
    ],
}

# A tap on a REPLY option means "Pascal said this sentence". The label is the
# suggested reply itself, so it is carried into the next conversation turn
# first-person (see _queue_decision_context) instead of being filed away as a
# generic 批红 rating. FYI keys are the only taps that stay purely analytic.
_FYI_KEYS = {"read", "watch"}

# Prose cards whose source is inherently a decision/follow-up ask should not
# fall back to「已阅／标为重点」. Only consulted when the emitter did not author
# its own options (see _extract_inline_options).
SOURCE_DEFAULT_PRESET = {
    "intention-check": "followup",
    "intentions": "followup",
    "intent": "followup",
    "watchlater-remind": "followup",
    "task-triage": "followup",
    "selfmon": "decision",
    "eigenflux-publish": "decision",
}

# LLM-authored buttons: a heartbeat task ends its card body with a line like
#     OPTIONS: 加钱 | 限流到月底 | 让它自然停
# and those become the buttons. This is the only way buttons can genuinely
# track content — the model writing the card is the one that knows what it is
# asking. Accepts the Chinese label and full-width separators/colon so a task
# author does not have to think about ASCII.
_OPTIONS_LINE_RE = re.compile(r"^\s*(?:OPTIONS|选项)\s*[:：]\s*(.+?)\s*$", re.I)
_OPTIONS_SPLIT_RE = re.compile(r"\s*[|｜/／]\s*")
MAX_INLINE_OPTIONS = 4
# Lark truncates long button captions on a phone; clip rather than reject, so
# a verbose OPTIONS line degrades to a short button instead of losing the card.
MAX_OPTION_LABEL_CHARS = 14

# Header reads 「📜 {source emoji} {title}」; unknown sources just get 📜.
SOURCE_EMOJI = {
    "mail": "📬",
    "mail-triage": "📬",
    "eigenflux": "📡",
    "eigenflux-feed-triage": "📡",
    "eigenflux-friends": "📡",
    "eigenflux-messages": "📡",
    "eigenflux-publish": "📡",
    "eigenflux-research": "📡",
    "selfmon": "🩺",
    "intent": "🎯",
    "intentions": "🎯",
    "intention-check": "🎯",
    "memory": "🧠",
    "cross-session-sync": "🧠",
    "checkin": "🌿",
    "calendar-sync": "📅",
    "content-recommend": "📺",
    "weekly-review": "📊",
    "daily-reflect": "🪞",
    "phronesis-monitor": "🧭",
    "watchlater-remind": "⏰",
    "task-triage": "📋",
    "heartbeat": "🫀",
}

SOURCE_TITLE = {
    "mail-triage": "邮件",
    "eigenflux-feed-triage": "EigenFlux 动态",
    "eigenflux-friends": "EigenFlux 好友",
    "eigenflux-messages": "EigenFlux 消息",
    "eigenflux-publish": "EigenFlux 广播待确认",
    "eigenflux-research": "EigenFlux 深度",
    "cross-session-sync": "跨 Session 动态",
    "checkin": "关怀",
    "calendar-sync": "日程变动",
    "intention-check": "Intent",
    "content-recommend": "推荐",
    "weekly-review": "周回顾",
    "daily-reflect": "复盘",
}

_ID_COUNTER = itertools.count(1)


# ── paths / low-level io ────────────────────────────────────────────────


def _ledger_path() -> Path:
    return JARVIS_DIR / "memorials.jsonl"


def _pending_merge_path() -> Path:
    # bot.sh's bg-job merge channel: lines matching conv_key are prepended to
    # Pascal's next message and consumed (rewrite-keep-others). We only ever
    # append — same as bot.sh's own two writers.
    return JARVIS_DIR / "jobs" / "pending_merge.jsonl"


def _outbox_path() -> Path:
    return JARVIS_DIR / "heartbeat_outbox.jsonl"


def _append_line(path: Path, entry: dict) -> None:
    """O_APPEND one compact JSON line — atomic for small writes, no lock
    needed across the sidecar / CLI / heartbeat writers (same idiom as
    engagement_log / heartbeat_outbox appends)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _new_id() -> str:
    # epoch + pid + counter: unique across processes and within one process,
    # without the collision risk of a bare timestamp.
    return f"mem_{int(time.time())}_{os.getpid()}_{next(_ID_COUNTER)}"


def _resolve_user_id() -> str:
    """Pascal's open_id: USER_ID env (bot.sh exports it) → jarvis.yaml."""
    uid = os.environ.get("USER_ID", "").strip()
    if uid:
        return uid
    try:
        from core.config import Config
        return str(Config(JARVIS_DIR / "jarvis.yaml").lark.get("user_id", "") or "")
    except Exception:
        return ""


# ── ledger fold ─────────────────────────────────────────────────────────


def _fold(events: list[dict]) -> dict[str, dict]:
    """Fold the event stream into {id: current_state}."""
    states: dict[str, dict] = {}
    for e in events:
        mid = str(e.get("id", ""))
        if not mid:
            continue
        ev = e.get("ev", "")
        if ev == "create":
            states[mid] = {
                "id": mid,
                "ts": e.get("ts", ""),
                "epoch": e.get("epoch", 0),
                "source": str(e.get("source", "")),
                "title": str(e.get("title", "")),
                "body": str(e.get("body", "")),
                "options": e.get("options") or [],
                "extra_buttons": e.get("extra_buttons") or [],
                "context": str(e.get("context", "")),
                "chat_id": str(e.get("chat_id", "")),
                "status": "pending",
                "decided_opt": "",
                "decided_label": "",
                "decided_ts": "",
                "action_result": "",
                "chat_ts": "",
                "chat_epoch": 0,
                "delivery_status": "not_sent",
                "delivery_ts": "",
            }
        elif ev == "decide":
            st = states.get(mid)
            if st is not None and st["status"] == "pending":
                st["status"] = "decided"
                st["decided_opt"] = str(e.get("opt", ""))
                st["decided_label"] = str(e.get("label", ""))
                st["decided_ts"] = str(e.get("ts", ""))
                st["action_result"] = str(e.get("action_result", ""))
        elif ev == "action_result":
            # Written after the decide event once the option action ran —
            # decide() appends the decide event BEFORE executing the action so
            # a crash mid-action can't lead to a double execution on re-tap.
            st = states.get(mid)
            if st is not None:
                st["action_result"] = str(e.get("result", ""))
        elif ev == "chat":
            st = states.get(mid)
            if st is not None:
                st["chat_ts"] = str(e.get("ts", ""))
                st["chat_epoch"] = e.get("epoch", 0)
        elif ev == "delivery":
            st = states.get(mid)
            if st is not None:
                st["delivery_status"] = str(e.get("status", "unknown"))
                st["delivery_ts"] = str(e.get("ts", ""))
    return states


def get_memorial(memorial_id: str) -> dict | None:
    """Current folded state for one memorial, or None."""
    return _fold(read_jsonl(_ledger_path())).get(str(memorial_id))


def list_memorials(pending_only: bool = False) -> list[dict]:
    """All memorials (creation order), optionally only the un-批 ones."""
    states = list(_fold(read_jsonl(_ledger_path())).values())
    if pending_only:
        states = [s for s in states if s["status"] == "pending"]
    return states


# ── card rendering ──────────────────────────────────────────────────────


def _header(state: dict) -> str:
    emoji = SOURCE_EMOJI.get(state["source"], "")
    return " ".join(p for p in ("📜", emoji, state["title"]) if p)


def _button_groups(state: dict, include_options: bool = True,
                   include_chat: bool = True) -> list[list[dict]]:
    """Phone-first action rows: choices, source actions, then conversation.

    The old single row compressed up to five controls into tiny, truncated
    buttons.  Separate rows also encode the real hierarchy: 批示 is the main
    decision, opening a source is supporting context, and Chat is the escape
    hatch that must remain available after a decision.
    """
    groups: list[list[dict]] = []
    if include_options and state.get("options"):
        groups.append([
            {"text": o.get("label", o.get("key", "")),
             "type": "primary" if i == 0 else "default",
             "value": {"action": "memorial", "id": state["id"],
                       "opt": o.get("key", "")}}
            for i, o in enumerate(state["options"])
        ])
    if state.get("extra_buttons"):
        groups.append([{**dict(button), "type": "default"}
                       for button in state["extra_buttons"]])
    if include_chat:
        groups.append([{"text": CHAT_BUTTON_LABEL, "type": "default",
                        "value": {"action": "memorial", "id": state["id"],
                                  "opt": CHAT_OPT_KEY}}])
    return groups


def _display_body(body: str) -> str:
    """Compact card copy while preserving the full ledger/chat context."""
    raw = str(body or "").strip()
    lines = raw.splitlines()
    clipped = len(lines) > CARD_BODY_MAX_LINES
    text = "\n".join(lines[:CARD_BODY_MAX_LINES]).strip()
    if len(text) > CARD_BODY_MAX_CHARS:
        cut = text[:CARD_BODY_MAX_CHARS]
        # Clip on a line/space boundary so the cut can't land inside a
        # markdown link and leave a broken `[label](https://…` fragment.
        for sep in ("\n", " "):
            if sep in cut[CARD_BODY_MAX_CHARS // 2:]:
                cut = cut.rsplit(sep, 1)[0]
                break
        text = cut.rstrip()
        clipped = True
    if clipped:
        text += "\n\n…完整背景可点「聊聊这个」"
    return text


def _render_card(state: dict, *, body: str | None = None,
                 status_line: str = "", include_options: bool = True,
                 include_chat: bool = True) -> str:
    content = _display_body(state["body"] if body is None else body)
    if status_line:
        content += "\n\n" + status_line
    return build_card(
        _header(state), content,
        button_groups=_button_groups(state, include_options, include_chat),
    )


def card_json(memorial_id: str) -> str:
    """Pending-state card JSON for a memorial (single line).

    Public so emitters with their own delivery channel (e.g. a heartbeat
    post-script falling back to the CARD: pipe when the direct send failed)
    can print the exact same card — the sidecar doesn't care who sent it.
    """
    st = get_memorial(memorial_id)
    if st is None:
        raise KeyError(f"memorial not found: {memorial_id}")
    return _render_card(st)


def _hhmm(ts: str) -> str:
    # ledger ts is "YYYY-MM-DD HH:MM" — show just the clock on the card.
    return ts[-5:] if len(ts) >= 5 else ts


def _decided_is_reply(state: dict) -> bool:
    opt = next((o for o in state.get("options", [])
                if o.get("key") == state.get("decided_opt")), None)
    return bool(opt and opt.get("reply"))


def _decided_card(state: dict) -> dict:
    """Replacement after 批红: durable proof plus a conversation escape hatch."""
    if _decided_is_reply(state):
        # A suggested reply reads back as something Pascal said, not as an
        # approval stamp on someone else's proposal.
        status = (f"🗣 你回了：{state['decided_label']} · "
                  f"{_hhmm(state['decided_ts'])}")
    else:
        status = f"✅ 已批：{state['decided_label']} · {_hhmm(state['decided_ts'])}"
    if state.get("action_result"):
        status += f"\n{state['action_result']}"
    return json.loads(_render_card(state, status_line=status, include_options=False,
                                   include_chat=True))


def _chatting_card(state: dict, ts: str) -> dict:
    """Replacement card after「聊聊这个」: chatting banner, remaining options
    stay tappable so Pascal can still 批 while (or after) chatting."""
    status = f"💬 聊天中 · {_hhmm(ts)} — 直接回消息就行"
    return json.loads(_render_card(
        state, status_line=status, include_options=state["status"] == "pending",
        include_chat=False))


# ── sending (clone of heartbeat_loop's production lark-cli path) ────────


def _send(args: list[str]) -> bool:
    for attempt, delay in enumerate((0,) + SEND_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            r = subprocess.run(["lark-cli", "im", "+messages-send", *args,
                                "--as", "bot"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            print(f"memorial send attempt {attempt} timed out", file=sys.stderr)
        except Exception as e:
            print(f"memorial send attempt {attempt} failed: {e}", file=sys.stderr)
    return False


def _send_card(card_json_str: str, chat_id: str = "") -> bool:
    if chat_id:
        target = ["--chat-id", chat_id]
    else:
        uid = _resolve_user_id()
        if not uid:
            return False
        target = ["--user-id", uid]
    return _send([*target, "--msg-type", "interactive", "--content", card_json_str])


def _send_text(text: str, chat_id: str = "") -> bool:
    if not text:
        return False
    if chat_id:
        target = ["--chat-id", chat_id]
    else:
        uid = _resolve_user_id()
        if not uid:
            return False
        target = ["--user-id", uid]
    return _send([*target, "--markdown", text])


def _write_outbox(text: str) -> None:
    """Mirror a direct send into heartbeat_outbox.jsonl (main-session bridge).

    Direct sends bypass core.heartbeat_loop, so without this the main
    conversation would never know the card went out (recon B risk #1)."""
    _append_line(_outbox_path(), {"role": "assistant", "text": text,
                                  "ts": now_local_str(), "source": "memorial"})


def _record_engagement(row: dict) -> None:
    """One row into engagement_log.jsonl — same shapes bot.sh / heartbeat_loop
    already write ("sent" / "feedback"), so engagement-analyze sees memorial
    sources and Pascal's 批红 without a new schema. Accounting must never
    break a delivery or a card callback, hence the broad except."""
    try:
        row.setdefault("ts", now_local_str("%Y-%m-%d %H:%M"))
        row.setdefault("epoch", int(time.time()))
        _append_line(JARVIS_DIR / "engagement_log.jsonl", row)
    except Exception as e:
        print(f"memorial engagement log failed: {e}", file=sys.stderr)


def _record_delivery(memorial_id: str, status: str, source: str = "") -> None:
    _append_line(_ledger_path(), {
        "ev": "delivery", "id": memorial_id, "status": status,
        "ts": now_local_str(),
    })
    # Queue-path deliveries get their "sent" rows from heartbeat_loop's flush
    # (via=memorial-card-queue); this covers DIRECT sends only, so sources
    # like a CLI-sent release card stop reading as zero-output to
    # engagement-analyze.
    if status == "delivered" and source:
        _record_engagement({"source": source, "type": "sent",
                            "via": "memorial-direct"})


def _quiet_hours_now() -> bool:
    """Delegate to the delivery layer's quiet-hours clock (23:30-10:00).

    Direct sends must respect the same night gate as everything else — the
    whole point of the night queue is that a 2am non-urgent ask waits for
    morning. Fail-open (send) if the import ever breaks: losing a delivery
    is worse than a rare night ping."""
    try:
        from core.heartbeat_loop import _in_quiet_hours
        return _in_quiet_hours()
    except Exception:
        return False


def _queue_for_morning(mid: str, card_json_str: str, title: str,
                       source: str = "memorial") -> None:
    """Queue one intact memorial card for the next delivery window.

    Do not put memorials in ``night_queue.jsonl``: that queue deliberately
    composes a length-capped text digest, which destroys the card buttons and
    recreates the exact long/truncated interaction this surface replaces.
    ``heartbeat_loop._flush_memorial_queue`` sends these entries one by one.
    """
    readable = extract_card_text(card_json_str) or f"📜 {title}"
    _append_line(JARVIS_DIR / MEMORIAL_QUEUE_FILE,
                 {"ts": now_local_str(), "epoch": int(time.time()),
                  "text": readable, "source": source or "memorial",
                  "memorial_id": mid, "card_json": card_json_str})


# Handle to the last opener-send thread — chat() runs inside the sidecar's
# websocket callback, which must return within Lark's 3s ACK budget; a
# synchronous lark-cli send (retries + sleeps, worst case ~52s) would freeze
# the single event loop that forwards ALL of Pascal's messages. Module-level
# so tests can join() it deterministically.
_opener_thread: threading.Thread | None = None


def _deliver_opener(text: str, chat_id: str) -> None:
    try:
        if _send_text(text, chat_id):
            _write_outbox(text)
    except Exception as e:
        print(f"memorial opener send failed: {e}", file=sys.stderr)


def _send_opener_async(text: str, chat_id: str) -> None:
    global _opener_thread
    _opener_thread = threading.Thread(target=_deliver_opener,
                                      args=(text, chat_id), daemon=True)
    _opener_thread.start()


# ── option normalization ────────────────────────────────────────────────


def _extract_inline_options(text: str) -> tuple[str, list[dict] | None]:
    """Split a trailing ``OPTIONS: a | b | c`` line off LLM-authored prose.

    Returns ``(body_without_the_line, options)`` — or ``(text, None)`` when the
    card did not author its own buttons. Only a TRAILING line counts: an
    'OPTIONS:' in the middle of the copy is prose, not a button declaration.
    """
    lines = str(text or "").splitlines()
    for idx in range(len(lines) - 1, -1, -1):
        if not lines[idx].strip():
            continue
        match = _OPTIONS_LINE_RE.match(lines[idx])
        if not match:
            return text, None
        labels: list[str] = []
        for part in _OPTIONS_SPLIT_RE.split(match.group(1)):
            part = part.strip().strip("「」\"'")
            if part and part not in labels:
                labels.append(part[:MAX_OPTION_LABEL_CHARS])
        if not labels:
            return text, None
        body = "\n".join(lines[:idx]).rstrip()
        return body, [{"key": f"r{i}", "label": label, "action": None,
                       "reply": True}
                      for i, label in enumerate(labels[:MAX_INLINE_OPTIONS], 1)]
    return text, None


def _normalize_options(options: list[dict] | None, preset: str | None) -> list[dict]:
    if options is not None:
        normalized = []
        seen: set[str] = set()
        for i, o in enumerate(options, 1):
            key = str(o.get("key", "") or f"opt{i}").strip()
            label = str(o.get("label", "")).strip()
            if not label:
                raise ValueError(f"option #{i} has no label")
            if key == CHAT_OPT_KEY:
                raise ValueError(f"option key '{CHAT_OPT_KEY}' is reserved")
            if key in seen:
                raise ValueError(f"duplicate option key: {key}")
            seen.add(key)
            item = {"key": key, "label": label, "action": o.get("action") or None}
            if o.get("reply"):
                item["reply"] = True
            normalized.append(item)
        return normalized
    name = preset or "fyi"  # a memorial with no options makes no sense — fyi is the safe floor
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name} (have: {', '.join(sorted(PRESETS))})")
    return [dict(o) for o in PRESETS[name]]


def _normalize_extra_buttons(buttons: list[dict] | None) -> list[dict]:
    """Validate task-native buttons carried into an adopted memorial card."""
    normalized: list[dict] = []
    for i, button in enumerate(buttons or [], 1):
        text = str(button.get("text", "")).strip()
        if not text:
            raise ValueError(f"extra button #{i} has no text")
        item = {"text": text}
        if button.get("url"):
            item["url"] = str(button["url"])
        elif isinstance(button.get("value"), dict):
            item["value"] = dict(button["value"])
        else:
            raise ValueError(f"extra button #{i} needs url or value")
        normalized.append(item)
    return normalized


# ── public API ──────────────────────────────────────────────────────────


def _find_recent_duplicate(source: str, title: str, body: str,
                           options: list[dict], extra_buttons: list[dict],
                           context: str, chat_id: str) -> dict | None:
    """A still-pending memorial with identical content created within the
    dedup window — the signature of an emitter stuck in a retry loop."""
    now = time.time()
    for st in _fold(read_jsonl(_ledger_path())).values():
        if (st["status"] == "pending"
                and st["source"] == source and st["title"] == title
                and st["body"] == body
                and st.get("options", []) == options
                and st.get("extra_buttons", []) == extra_buttons
                and st.get("context", "") == context
                and st.get("chat_id", "") == chat_id
                and st.get("epoch") and now - st["epoch"] < DEDUP_WINDOW_S):
            return st
    return None


def _deliver_existing(state: dict, urgent: bool = False) -> bool:
    """Deliver an already-ledgered memorial and record the outcome."""
    mid = state["id"]
    cj = _render_card(state)
    if not urgent and _quiet_hours_now():
        _queue_for_morning(mid, cj, state["title"], state.get("source", ""))
        _record_delivery(mid, "queued")
        print(f"memorial {mid}: quiet hours — queued as an intact morning card",
              file=sys.stderr)
        return True

    sent = _send_card(cj, state.get("chat_id", ""))
    _record_delivery(mid, "delivered" if sent else "failed",
                     source=state.get("source", "memorial"))
    if sent:
        readable = extract_card_text(cj) or f"📜 {state['title']}"
        _write_outbox(readable + f"\n\n（奏折 {mid} 已发出，等批示）")
    else:
        # A direct CLI emitter has no outer retry loop.  Keep the exact card
        # for heartbeat_loop to retry at the next delivery window instead of
        # stranding a pending ledger row that nobody automatically revisits.
        _queue_for_morning(mid, cj, state["title"], state.get("source", ""))
        _record_delivery(mid, "retry_queued")
    return sent


def create(source: str, title: str, body: str, options: list[dict] | None = None,
           preset: str | None = None, context: str = "",
           chat_id: str = "", send: bool = True,
           urgent: bool = False,
           extra_buttons: list[dict] | None = None) -> tuple[str, bool]:
    """Create a memorial, append it to the ledger, and send the card.

    Returns (memorial_id, sent_ok). The ledger write happens BEFORE the send,
    so a failed send still leaves a queryable record (list --pending) and the
    caller can re-deliver via card_json().

    send=False skips delivery entirely (no direct send, no outbox mirror) —
    for emitters that own a delivery channel with its own retries/dedup/night
    gate, e.g. a heartbeat post-script printing card_json() to the CARD pipe.

    Direct sends respect the delivery layer's gates: an identical pending
    memorial within 6h is not re-created (returns the existing id), and
    non-urgent sends during quiet hours (23:30-10:00) go to the night queue
    instead of buzzing the phone — urgent=True bypasses the night gate.
    """
    source, title, body = str(source), str(title), str(body)
    # An OPTIONS line in the body wins over any preset: the emitter that wrote
    # that line is stating what this specific card asks. Callers that pass
    # explicit options have already decided, so their body is left alone.
    if options is None:
        body, inline_options = _extract_inline_options(body)
        if inline_options:
            options, preset = inline_options, None
    opts = _normalize_options(options, preset)
    native_buttons = _normalize_extra_buttons(extra_buttons)

    dup = _find_recent_duplicate(
        source, title, body, opts, native_buttons, str(context), str(chat_id))
    if dup is not None:
        print(f"memorial dedup: identical pending {dup['id']} within "
              f"{DEDUP_WINDOW_S // 3600}h — not re-created", file=sys.stderr)
        if not send:
            return dup["id"], False
        if dup.get("delivery_status") in {"delivered", "queued", "retry_queued"}:
            return dup["id"], True
        return dup["id"], _deliver_existing(dup, urgent=urgent)

    mid = _new_id()
    ts = now_local_str()
    ev = {"ev": "create", "id": mid, "ts": ts, "epoch": int(time.time()),
          "source": source, "title": title, "body": body, "options": opts,
          "extra_buttons": native_buttons, "context": str(context)}
    if chat_id:
        ev["chat_id"] = str(chat_id)
    _append_line(_ledger_path(), ev)

    state = _fold([ev])[mid]
    cj = _render_card(state)
    if not send:
        return mid, False
    return mid, _deliver_existing(state, urgent=urgent)


def _card_memorial_id(card: dict) -> str:
    for element in card.get("elements", []):
        for action in element.get("actions", []):
            value = action.get("value") or {}
            if value.get("action") == "memorial" and value.get("id"):
                return str(value["id"])
    return ""


def _clean_adopted_title(header: str, source: str) -> str:
    """Remove transport chrome from a legacy card title."""
    import re
    cleaned = re.sub(r"^[\s📜📡📬🩺🎯🧠🫀🌿📅💡⏰📺📊🪞🧭📋]+", "",
                     header or "")
    cleaned = cleaned.strip(" ·|-")
    return cleaned or SOURCE_TITLE.get(source, source or "一件事")


def adopt_card(source: str, legacy_card_json: str, context: str = "",
               suppress_accepted: bool = False) -> str:
    """Adopt an existing Lark card into the memorial interaction surface.

    Task-native actions/links are preserved. Cards that already offer a real
    action keep those choices and gain「聊聊这个」; read-only/link-only cards
    also gain the common「已阅／标为重点」批红 pair.
    """
    card = json.loads(legacy_card_json)
    if _card_memorial_id(card):
        return legacy_card_json

    header = str(card.get("header", {}).get("title", {}).get("content", ""))
    body_parts: list[str] = []
    native_buttons: list[dict] = []
    for element in card.get("elements", []):
        text = element.get("text", {}).get("content", "")
        if text:
            body_parts.append(str(text))
        for action in element.get("actions", []):
            label = str(action.get("text", {}).get("content", "")).strip()
            if not label:
                continue
            if action.get("url"):
                native_buttons.append({"text": label, "url": action["url"]})
            elif isinstance(action.get("value"), dict):
                native_buttons.append({"text": label,
                                       "value": dict(action["value"])})

    body = "\n\n".join(body_parts).strip()
    title = _clean_adopted_title(header, source)
    if not body:
        body = title
    has_native_action = any("value" in button for button in native_buttons)
    # An existing callback already represents the card's decision options;
    # don't bury it under generic FYI buttons. URL-only cards are read-only,
    # so the common FYI choices remain useful.
    options = [] if has_native_action else None
    fallback_preset = SOURCE_DEFAULT_PRESET.get(source or "heartbeat", "fyi")
    mid, _ = create(
        source=source or "heartbeat", title=title, body=body,
        options=options, preset=None if has_native_action else fallback_preset,
        context=context, send=False, extra_buttons=native_buttons,
    )
    if suppress_accepted:
        state = get_memorial(mid) or {}
        if state.get("delivery_status") in {
                "delivered", "queued", "retry_queued"}:
            return ""
    return card_json(mid)


def memorialize_output(output: str, source: str = "heartbeat") -> str:
    """Convert proactive heartbeat output to one memorial card per event.

    Existing memorial cards pass through. Legacy cards are adopted while
    preserving their native actions; prose chunks separated by ``---`` become
    compact FYI memorials. Raw internal JSON remains blocked.
    """
    source_names = [s.strip() for s in str(source).split(",") if s.strip()]
    single_source = source_names[0] if len(source_names) == 1 else "heartbeat"
    rendered: list[str] = []
    prose: list[str] = []

    def flush_prose() -> None:
        text = "\n".join(prose).strip()
        prose.clear()
        if not text:
            return
        try:
            json.loads(text)
            return
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        title = SOURCE_TITLE.get(single_source, single_source or "一件事")
        # Buttons follow the card: an OPTIONS line authored by the task wins;
        # otherwise fall back to what this source is usually asking for, and
        # only then to「已阅」.
        body, inline_options = _extract_inline_options(text)
        preset = (None if inline_options
                  else SOURCE_DEFAULT_PRESET.get(single_source, "fyi"))
        mid, _ = create(single_source, title, body, options=inline_options,
                        preset=preset, send=False)
        state = get_memorial(mid) or {}
        if state.get("delivery_status") in {
                "delivered", "queued", "retry_queued"}:
            return
        rendered.append(card_json(mid))

    for raw_line in str(output).splitlines():
        line = raw_line.strip()
        if line == "---":
            flush_prose()
            continue
        card_raw = line[5:] if line.startswith("CARD:") else line
        try:
            card = json.loads(card_raw) if card_raw else None
        except (json.JSONDecodeError, TypeError, ValueError):
            card = None
        if isinstance(card, dict) and "config" in card and "elements" in card:
            flush_prose()
            adopted = (card_raw if _card_memorial_id(card)
                       else adopt_card(single_source, card_raw,
                                       suppress_accepted=True))
            if adopted:
                rendered.append(adopted)
        elif line:
            prose.append(raw_line)
    flush_prose()
    return "\n".join(rendered)


def _execute_action(action: dict) -> str:
    """Run one option action through ActionProcessor's _do_* handler —
    the single source of truth for action execution (no new executor)."""
    from core.actions import ActionProcessor
    atype = str(action.get("type", "")).strip()
    if not atype:
        return ""
    params = action.get("params") or {}
    raw = "|".join(f"{k}={v}" for k, v in params.items())
    ap = ActionProcessor(
        jarvis_dir=JARVIS_DIR,
        memory_dir=os.environ.get("MEMORY_DIR", str(JARVIS_DIR / "memory")),
        jobs_dir=os.environ.get("JV_JOBS_DIR", str(JARVIS_DIR / "jobs")),
        log_file=os.environ.get("JV_LOG_FILE", ""),
    )
    handler = getattr(ap, f"_do_{atype}", None)
    if handler is None:
        raise ValueError(f"unknown action type: {atype}")
    return handler(raw) or ""


def decide(memorial_id: str, opt_key: str) -> dict:
    """批红 one option. Returns the card-callback response payload.

    Idempotent: a second tap (any option) returns「已批过」without re-running
    the action or appending another decide event.
    """
    st = get_memorial(memorial_id)
    if st is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
    if st["status"] == "decided":
        return {"toast": {"type": "info", "content": f"已批过：{st['decided_label']}"},
                "card": {"type": "raw", "data": _decided_card(st)}}
    opt = next((o for o in st["options"] if o.get("key") == opt_key), None)
    if opt is None:
        return {"toast": {"type": "info", "content": "出错了，直接在对话里告诉我"}}

    # Ledger BEFORE action: if the process dies mid-action, a re-tap hits the
    # 「已批过」idempotence branch instead of re-running the side effect (a
    # lost action beats a double calendar event). The result is back-filled
    # as a separate action_result event.
    ts = now_local_str()
    _append_line(_ledger_path(), {"ev": "decide", "id": memorial_id, "ts": ts,
                                  "opt": opt_key, "label": opt.get("label", "")})
    # 批红 = engagement：same "feedback" shape the legacy card buttons write,
    # so engagement-analyze sees which sources Pascal actually acts on.
    _record_engagement({"source": st.get("source", "memorial"),
                        "type": "feedback", "rating": opt_key})

    action_result, action_failed = "", False
    if opt.get("action"):
        try:
            action_result = _execute_action(opt["action"])
        except Exception as e:
            action_result = f"FAILED: {e}"
            action_failed = True
        _append_line(_ledger_path(), {"ev": "action_result", "id": memorial_id,
                                      "ts": now_local_str(),
                                      "result": action_result})

    st.update(status="decided", decided_opt=opt_key,
              decided_label=opt.get("label", ""), decided_ts=ts,
              action_result=action_result)
    if opt.get("reply") or opt_key not in _FYI_KEYS:
        _queue_decision_context(st, opt.get("label", ""), action_result,
                                is_reply=bool(opt.get("reply")))
    if action_failed:
        toast = {"type": "info", "content": "已批，但动作执行出错了——直接在对话里告诉我"}
    elif opt.get("reply"):
        toast = {"type": "success", "content": "收到——下条消息我接着这个说"}
    else:
        toast = {"type": "success", "content": f"已批：{opt.get('label', '')} ✓"}
    return {"toast": toast, "card": {"type": "raw", "data": _decided_card(st)}}


def _status_line(st: dict) -> str:
    if st["status"] == "decided":
        return f"已批：{st['decided_label']}（{st['decided_ts']}）"
    labels = "／".join(o.get("label", "") for o in st["options"])
    return f"待批（选项：{labels}）" if labels else "待处理"


def _injection_queued(conv_key: str, job_id: str) -> bool:
    """True if this memorial's context injection is already waiting in
    pending_merge (queued but not yet consumed by bot.sh)."""
    return any(e.get("conv_key") == conv_key and e.get("job_id") == job_id
               for e in read_jsonl(_pending_merge_path()))


def _bounded_chat_context(st: dict) -> str:
    """Build a bounded injection without truncating away state/instructions."""
    fixed = [
        "[奏折上下文] Pascal 点了「聊聊这个」，下一条消息讨论这件事：",
        f"来源: {st['source']}",
        f"标题: {st['title']}",
        f"当前状态: {_status_line(st)}",
        "直接接住话题，不要复述卡片。",
    ]
    # Reserve the fixed tail first. Variable fields are clipped independently,
    # so a huge body can never erase the current state or instruction.
    budget = max(CHAT_CONTEXT_MAX_CHARS - len("\n".join(fixed)) - 32, 200)
    body = str(st.get("body", "")).strip()
    context = str(st.get("context", "")).strip()
    body_budget = min(900, int(budget * 0.7))
    body = body[:body_budget].rstrip()
    context = context[:max(budget - len(body), 0)].rstrip()
    variable = [f"正文: {body}"]
    if context:
        variable.append(f"背景: {context}")
    return "\n".join(fixed[:3] + variable + fixed[3:])[:CHAT_CONTEXT_MAX_CHARS]


def _queue_decision_context(st: dict, label: str, action_result: str = "",
                            is_reply: bool = False) -> None:
    """Carry a card tap into the next conversation turn.

    A record-only 批红 used to disappear into analytics: the assistant could
    ask again because the conversational session never learned the choice.
    The existing pending-merge bridge is the durable per-conversation handoff.

    ``is_reply`` marks a suggested-reply button, whose label IS the sentence
    Pascal would have typed — it is handed over first-person so the next turn
    acts on it rather than merely filing a preference.
    """
    conv_key = st.get("chat_id", "") or _resolve_user_id()
    if not conv_key:
        return
    job_id = f"memorial-decision:{st['id']}"
    if _injection_queued(conv_key, job_id):
        return
    if is_reply:
        lines = [
            f"[奏折回复] 关于「{st['title']}」，Pascal 点了推荐回复：「{label}」。",
            "当作他刚亲口说了这句话——直接照它行动或接话，不要复述卡片、"
            "不要再问一遍他的意思。",
        ]
    else:
        lines = [
            f"[奏折批示] Pascal 对「{st['title']}」选择了「{label}」。",
            "把它视为已经确认的偏好或决定，不要原样再问一次。",
        ]
    if action_result:
        lines.append(f"动作结果: {action_result[:400]}")
    _append_line(_pending_merge_path(), {
        "conv_key": conv_key, "job_id": job_id,
        "ts": now_local_str(), "summary": "\n".join(lines),
    })


def chat(memorial_id: str) -> dict:
    """「聊聊这个」: inject the memorial's full context into bot.sh's
    pending-merge channel (so Pascal's next message arrives with the topic
    loaded) + send a conversation opener. Returns the card-callback payload.

    Runs inside the sidecar's websocket callback (3s ACK budget, single event
    loop): only fast local file ops happen here — the opener's lark-cli send
    (retries, worst case tens of seconds) goes to a background thread.

    Idempotent-ish: a re-tap within CHAT_RETAP_THROTTLE_S (client-side
    "操作失败" re-taps, Lark re-pushing un-ACKed events) is a no-op, and the
    injection is never queued twice while unconsumed.
    """
    st = get_memorial(memorial_id)
    if st is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
    ts = now_local_str()

    if st.get("chat_epoch") and time.time() - st["chat_epoch"] < CHAT_RETAP_THROTTLE_S:
        print(f"memorial chat re-tap throttled: id={memorial_id}", file=sys.stderr)
        return {"toast": {"type": "info", "content": "已在聊了——直接回消息就行"},
                "card": {"type": "raw",
                         "data": _chatting_card(st, st["chat_ts"] or ts)}}

    # 1. One-shot context injection FIRST (it's the soul of the flow — the
    #    opener is only garnish): bot.sh prepends matching lines to the next
    #    message from this conv_key and consumes them (multiple queued
    #    memorials merge automatically). conv_key mirrors bot.sh: p2p =
    #    Pascal's open_id, group = chat_id. Injecting before the opener send
    #    means Pascal's immediate reply can't race past a slow opener.
    conv_key = st.get("chat_id", "") or _resolve_user_id()
    if conv_key:
        if _injection_queued(conv_key, f"memorial:{memorial_id}"):
            print(f"memorial chat: injection for {memorial_id} already queued",
                  file=sys.stderr)
        else:
            _append_line(_pending_merge_path(), {
                "conv_key": conv_key, "job_id": f"memorial:{memorial_id}",
                "ts": ts, "summary": _bounded_chat_context(st),
            })
    else:
        print(f"memorial chat: no conv_key for {memorial_id} — context not injected",
              file=sys.stderr)

    _append_line(_ledger_path(), {"ev": "chat", "id": memorial_id, "ts": ts,
                                  "epoch": int(time.time())})
    _record_engagement({"source": st.get("source", "memorial"),
                        "type": "feedback", "rating": "chat"})

    # 2. Opener so Pascal has something to reply to — off the callback thread.
    opener = (f"📜 已带上「{st['title']}」的背景。"
              "直接说你想追问什么，或告诉我你的倾向。")
    _send_opener_async(opener, st.get("chat_id", ""))

    return {"toast": {"type": "success", "content": "开聊——直接回消息就行"},
            "card": {"type": "raw", "data": _chatting_card(st, ts)}}


# ── CLI ─────────────────────────────────────────────────────────────────


def _parse_option_spec(spec: str, idx: int) -> dict:
    """Parse one --option value.

    '缓'                                → record-only option
    '准=intent_close:id=xxx,outcome=done' → label=准, action intent_close
    """
    spec = spec.strip()
    if "=" not in spec:
        if not spec:
            raise ValueError("empty --option")
        return {"key": f"opt{idx}", "label": spec, "action": None}
    label, action_raw = spec.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"--option has no label: {spec!r}")
    if ":" in action_raw:
        atype, params_raw = action_raw.split(":", 1)
    else:
        atype, params_raw = action_raw, ""
    params = {}
    for seg in params_raw.split(","):
        if "=" in seg:
            k, v = seg.split("=", 1)
            params[k.strip()] = v.strip()
    return {"key": f"opt{idx}", "label": label,
            "action": {"type": atype.strip(), "params": params}}


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="core.memorial",
        description="发奏折卡 / 查台账（memorial cards）")
    sub = parser.add_subparsers(dest="cmd")

    sp = sub.add_parser("send", help="create + send one memorial card")
    sp.add_argument("--source", required=True)
    sp.add_argument("--title", required=True)
    sp.add_argument("--body", required=True)
    sp.add_argument("--preset", choices=sorted(PRESETS))
    sp.add_argument("--option", action="append", default=[],
                    metavar="'标签[=动作类型:k=v,k=v]'")
    sp.add_argument("--options", default="",
                    metavar="'加钱|限流|让它停'",
                    help="推荐回复按钮（点了等于他说了这句话）")
    sp.add_argument("--context", default="")
    sp.add_argument("--chat-id", dest="chat_id", default="")
    sp.add_argument("--urgent", action="store_true",
                    help="bypass quiet hours (only for genuinely urgent asks)")

    lp = sub.add_parser("list", help="print folded ledger states (JSON lines)")
    lp.add_argument("--pending", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "send":
        try:
            options = ([_parse_option_spec(s, i)
                        for i, s in enumerate(args.option, 1)] or None)
            if args.options:
                # Reuse the same parser the OPTIONS body line uses, so the CLI
                # and an LLM-authored card produce identical buttons.
                _, reply_options = _extract_inline_options(
                    f"OPTIONS: {args.options}")
                if not reply_options:
                    raise ValueError(f"--options 没解析出按钮: {args.options!r}")
                options = (options or []) + reply_options
            mid, sent = create(args.source, args.title, args.body,
                               options=options, preset=args.preset,
                               context=args.context, chat_id=args.chat_id,
                               urgent=args.urgent)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(mid)
        if not sent:
            print("发送失败（已入台账，可用 card_json 重发）", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "list":
        for st in list_memorials(pending_only=args.pending):
            print(json.dumps(st, ensure_ascii=False))
        return 0

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
