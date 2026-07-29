"""Memorial (奏折) cards — the unified "ask Pascal" surface.

Every proactive output that needs Pascal's eyes (mail triage, decisions,
follow-ups, heartbeat asks…) becomes one durable memorial.  The ledger records
where Pascal should act: ordinary, batchable decisions go to the phone desk;
only urgent, conversation-bound, or Lark-native decisions interrupt Lark.
Alerts may reach Lark but are explicitly not approvals, and routine notices
stay in the web feed. Tapping an option = 批红: it is recorded (and optionally
executes an action through ActionProcessor). Tapping「聊聊这个」injects the
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

import fcntl
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.card import build_card, extract_card_text
from core.card_split import split_matters
from core.jsonl import read_jsonl
from core.timeutil import now_local, now_local_str

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
ATTENTION_DECISION = "decision"
ATTENTION_NOTICE = "notice"
ATTENTION_ALERT = "alert"

# ── 缴回制度 (escrow) ────────────────────────────────────────────────────
# A card sent once and never tapped used to scroll out of Lark and vanish:
# 314 of 600 memorials sat pending forever, 110 of them older than a week,
# 47 of those decision-class — real asks silently lost, indistinguishable from
# noise nobody ever intended to answer. Nothing swept pending cards at all.
#
# Deadlines are measured, not guessed (7/29, over memorials decided since 7/01):
#   decision  median 2.1h, 75% inside 24h, 90% inside 48h, only 3% ever later
#   alert     median 0.2h, 78% inside 24h — a stale alert has no salvage value
#   notice    24h/48h/72h response is FLAT at 48% while the median lands at
#             75h: Pascal taps some the same day and sweeps the rest days
#             later. 7d clears the dead tail without stealing from that sweep.
ESCROW_DEADLINE_H = {
    ATTENTION_ALERT: 24,
    ATTENTION_NOTICE: 24 * 7,
    ATTENTION_DECISION: 48,
}
# A decision past its deadline is NOT archived — it is re-surfaced in the daily
# 匣子 docket. But 批红 that never comes is itself an answer: past this hard
# ceiling it is filed as 留中 so the docket cannot nag forever. Nothing in the
# measured window was ever decided this late (p95 = 128h, max = 179h).
ESCROW_HARD_LAPSE_H = 24 * 14
# 御门听政: the docket goes out once a day, in the morning, as ONE card, and
# groups by source. Re-pushing stale cards individually is the card storm this
# system was already burned by (7/22) — the emperor gets a docket, not the pile.
# Grouping is what makes the backlog legible: the first real docket was 37 rows
# but only 5 sources, 14 of them one repeating broken flow.
ESCROW_DIGEST_HOURS = range(8, 12)
ESCROW_DIGEST_SOURCE = "memorial-escrow"
ESCROW_DIGEST_MAX_GROUPS = 6
STATUS_LAPSED = "lapsed"

REVIEW_LARK = "lark"
REVIEW_PHONE = "phone"
REVIEW_NONE = "none"
REVIEW_SURFACES = {REVIEW_LARK, REVIEW_PHONE, REVIEW_NONE}

# Successful handoff means either an interrupting Lark delivery or a durable
# placement on the phone/web desk. Callers that ingest external events use this
# contract to mark upstream input seen without falling back to another channel.
ACCEPTED_DELIVERY_STATUSES = {
    "delivered", "queued", "retry_queued", "phone_ready", "web_only",
}

# These are synchronization/ambient-signal producers, not user-facing owners
# of a decision. Their output stays web-first even when an LLM helpfully invents
# reply options; a real decision should be promoted by a dedicated source.
WEB_FIRST_SOURCES = {
    "cross-session-sync",
    "eigenflux-feed-triage",
}
ALERT_SOURCES = {
    "calendar-sync",
}
# Calendar choices expire with the clock; unlike ordinary planning decisions,
# delaying them can create a real conflict. They retain the immediate lane.
LARK_REVIEW_SOURCES = {
    "calendar-sync",
}

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


def _infer_attention(options: list[dict], extra_buttons: list[dict]) -> str:
    """Infer whether a legacy card asks for a decision or merely informs."""
    if any(str(option.get("key", "")) not in _FYI_KEYS for option in options):
        return ATTENTION_DECISION
    if any(isinstance(button.get("value"), dict) for button in extra_buttons):
        return ATTENTION_DECISION
    return ATTENTION_NOTICE


def _default_attention(source: str, options: list[dict],
                       extra_buttons: list[dict]) -> str:
    if str(source or "") in WEB_FIRST_SOURCES:
        return ATTENTION_NOTICE
    inferred = _infer_attention(options, extra_buttons)
    if str(source or "") in ALERT_SOURCES and inferred == ATTENTION_NOTICE:
        return ATTENTION_ALERT
    return _governed(str(source or ""), inferred)


def natural_attention(source: str, options: list[dict],
                      extra_buttons: list[dict]) -> str:
    """The class a card would have with no engagement governor applied.

    core.attention_roi measures against this so a demoted source's own
    demotion cannot be read back as evidence about it.
    """
    if str(source or "") in WEB_FIRST_SOURCES:
        return ATTENTION_NOTICE
    inferred = _infer_attention(options, extra_buttons)
    if str(source or "") in ALERT_SOURCES and inferred == ATTENTION_NOTICE:
        return ATTENTION_ALERT
    return inferred


def _governed(source: str, inferred: str) -> str:
    """Let measured engagement quiet a decision lane nobody answers.

    Only ever decision → notice, and never for a protected source (see
    core.attention_roi). A failure here must not change routing, so the
    ungoverned class is returned on any error.
    """
    try:
        from core.attention_roi import class_for
        return class_for(source, inferred)
    except Exception:
        return inferred


def requires_decision(state: dict) -> bool:
    """Semantic attention class, backward compatible with old ledger rows."""
    attention = str(state.get("attention", "") or "")
    if attention:
        return attention == ATTENTION_DECISION
    return _default_attention(
        str(state.get("source", "")),
        list(state.get("options") or []),
        list(state.get("extra_buttons") or []),
    ) == ATTENTION_DECISION


def _infer_review_surface(source: str, attention: str,
                          extra_buttons: list[dict],
                          *, urgent: bool = False,
                          chat_id: str = "") -> str:
    """Choose where a human decision should happen.

    Phone is the humane default for real decisions: it supports deliberate,
    batched review without turning every ask into an interruption. Lark is
    reserved for decisions whose delay matters, asks already scoped to a live
    conversation, and legacy callbacks the web surface cannot execute.
    """
    if attention != ATTENTION_DECISION:
        return REVIEW_NONE
    has_lark_native_action = any(
        isinstance(button.get("value"), dict) for button in extra_buttons
    )
    if (urgent or str(chat_id or "").strip() or has_lark_native_action
            or str(source or "") in LARK_REVIEW_SOURCES):
        return REVIEW_LARK
    return REVIEW_PHONE


def review_surface(state: dict) -> str:
    """Preferred approval surface, with a truthful fallback for old rows."""
    explicit = str(state.get("review_surface", "") or "")
    if explicit in REVIEW_SURFACES:
        return explicit
    if not requires_decision(state):
        return REVIEW_NONE
    # Before this field existed, a delivered/queued decision was a Lark card.
    if str(state.get("delivery_status", "")) in {
            "delivered", "queued", "retry_queued"}:
        return REVIEW_LARK
    return _infer_review_surface(
        str(state.get("source", "")),
        ATTENTION_DECISION,
        list(state.get("extra_buttons") or []),
        chat_id=str(state.get("chat_id", "")),
    )


def delivery_accepted(state: dict) -> bool:
    return str(state.get("delivery_status", "")) in ACCEPTED_DELIVERY_STATUSES


def should_push_to_lark(state: dict) -> bool:
    """Lark receives sparse alerts and only Lark-routed decisions."""
    attention = str(state.get("attention", "") or "")
    if attention == ATTENTION_ALERT:
        return True
    return requires_decision(state) and review_surface(state) == REVIEW_LARK


_ALERT_RE = re.compile(
    r"(?:\b(?:urgent|critical)\b|紧急|严重|告警|只剩\s*\d|"
    r"服务(?:中断|不可用)|数据丢失|安全风险)",
    re.I,
)


def _looks_like_alert(text: str) -> bool:
    return bool(_ALERT_RE.search(str(text or "")))

# LLM-authored buttons: a heartbeat task ends its card body with a line like
#     OPTIONS: 加钱 | 限流到月底 | 让它自然停
# and those become the buttons. This is the only way buttons can genuinely
# track content — the model writing the card is the one that knows what it is
# asking. Accepts the Chinese label and full-width separators/colon so a task
# author does not have to think about ASCII.
_OPTIONS_LINE_RE = re.compile(r"^\s*(?:OPTIONS|选项)\s*[:：]\s*(.+?)\s*$", re.I)
_OPTIONS_SPLIT_RE = re.compile(r"\s*[|｜/／]\s*")

# LLM-authored card title, same contract shape as OPTIONS: the FIRST line of
# the body may read「TITLE: 一句话说清这件事」. Without it, cards from prose
# fell back to the per-source generic label — 48 cards headed literally
# 「Intent」in 11 days, burying e.g. the weekly 首席科学家发声 candidates
# under a header nobody opens.
_TITLE_LINE_RE = re.compile(r"^\s*(?:TITLE|标题)\s*[:：]\s*(.+?)\s*$", re.I)
MAX_TITLE_CHARS = 40
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


@contextmanager
def ledger_lock(ledger: Path):
    """Exclusive cross-process lock for memorials.jsonl writers.

    O_APPEND alone keeps concurrent appends intact, but rotate_ledger's
    read→rewrite→replace must exclude appenders entirely — the size
    re-check left a stat→replace TOCTOU window that could drop a decide
    event landing on the old inode (red-team #4, 7/22). Every ledger
    writer (here, heartbeat_loop delivery events, sidecar decide events
    via this module) takes this lock; appends hold it for microseconds.
    """
    lock_path = ledger.parent / (ledger.name + ".lock")
    with open(lock_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _append_line(path: Path, entry: dict) -> None:
    """O_APPEND one compact JSON line — atomic for small writes across the
    sidecar / CLI / heartbeat writers (same idiom as engagement_log /
    heartbeat_outbox appends). Ledger writes additionally take ledger_lock
    so a monthly rotation can never replace the file out from under them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    if path.name == "memorials.jsonl":
        with ledger_lock(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        return
    with open(path, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(line)


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
            options = e.get("options") or []
            extra_buttons = e.get("extra_buttons") or []
            states[mid] = {
                "id": mid,
                "ts": e.get("ts", ""),
                "epoch": e.get("epoch", 0),
                "source": str(e.get("source", "")),
                "title": str(e.get("title", "")),
                "body": str(e.get("body", "")),
                "options": options,
                "extra_buttons": extra_buttons,
                "attention": str(
                    e.get("attention") or
                    _default_attention(str(e.get("source", "")),
                                       options, extra_buttons)
                ),
                "review_surface": str(e.get("review_surface", "")),
                "context": str(e.get("context", "")),
                "dedup_key": str(e.get("dedup_key", "")),
                "chat_id": str(e.get("chat_id", "")),
                "matter_id": str(e.get("matter_id", "")),
                "status": "pending",
                "lapsed_ts": "",
                "lapse_reason": "",
                "decided_opt": "",
                "decided_label": "",
                "decided_ts": "",
                "action_result": "",
                "resolved_label": "",
                "resolved_ts": "",
                "chat_ts": "",
                "chat_epoch": 0,
                "delivery_status": "not_sent",
                "delivery_ts": "",
            }
        elif ev == "decide":
            st = states.get(mid)
            # A 留中 card stays tappable: scrolling back in Lark and answering
            # an archived ask must revive it, not silently no-op. Events are
            # chronological, so a later lapse cannot overwrite a real 批红
            # (the lapse branch only fires on pending).
            if st is not None and st["status"] in ("pending", STATUS_LAPSED):
                st["status"] = "decided"
                st["lapsed_ts"] = ""
                st["lapse_reason"] = ""
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
        elif ev == "resolve":
            # External source truth can become terminal after (or without) a
            # card tap. Resolution overrides an earlier reply-only decision
            # so every delivered copy converges to the real state.
            st = states.get(mid)
            if st is not None:
                st["status"] = "decided"
                st["decided_opt"] = "__external__"
                st["decided_label"] = str(e.get("label", "已处理"))
                st["decided_ts"] = str(e.get("ts", ""))
                st["action_result"] = str(e.get("result", ""))
                st["resolved_label"] = str(e.get("label", "已处理"))
                st["resolved_ts"] = str(e.get("ts", ""))
        elif ev == "lapse":
            # 留中: the deadline passed with no 批红. A terminal state that is
            # explicitly NOT a decision — never fold it into decided_*, or the
            # ledger would claim Pascal answered something he never saw.
            st = states.get(mid)
            if st is not None and st["status"] == "pending":
                st["status"] = STATUS_LAPSED
                st["lapsed_ts"] = str(e.get("ts", ""))
                st["lapse_reason"] = str(e.get("reason", ""))
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


# ── 缴回制度: sweep pending cards to a terminal state ─────────────────────


def lapse(memorial_id: str, reason: str = "") -> bool:
    """File a never-answered memorial as 留中. Returns True if it moved.

    Deliberately does NOT re-sync the Lark card. The bulk sweep archives
    hundreds of rows on its first run; that many card edits would be a rate
    limit incident, and the original card has long scrolled out of the chat
    anyway. The ledger and the web desk are the durable surface.
    """
    st = get_memorial(memorial_id)
    if st is None or st["status"] != "pending":
        return False
    _append_line(_ledger_path(), {
        "ev": "lapse",
        "id": memorial_id,
        "ts": now_local_str(),
        "reason": str(reason or ""),
    })
    _complete_surface_handoffs(memorial_id)
    return True


def _age_hours(state: dict, now: datetime) -> float | None:
    """Hours since creation, or None if the row has no parsable timestamp.

    Ledger timestamps are naive LOCAL time while now_local() is tz-aware;
    stamping the parsed value with now's tzinfo is what makes the subtraction
    both legal and correct. An unparsable ts must never be treated as age 0
    (silently immortal) or age ∞ (silently archived) — it is skipped.
    """
    try:
        created = datetime.strptime(str(state["ts"]), "%Y-%m-%d %H:%M")
    except (ValueError, KeyError, TypeError):
        return None
    if now.tzinfo is not None:
        created = created.replace(tzinfo=now.tzinfo)
    return (now - created).total_seconds() / 3600.0


def escrow_scan(now: datetime | None = None,
                states: list[dict] | None = None) -> dict:
    """Classify every pending memorial against its deadline. Pure — no writes.

    Returns ``{"lapse": [(state, reason)], "overdue": [state]}``:
      lapse    — terminal, archive as 留中 (alert/notice past deadline, or a
                 decision past the hard ceiling nobody will ever answer)
      overdue  — decision past its deadline but still answerable: it belongs
                 in the docket, NOT the archive.
    Cards still inside their deadline appear in neither list.
    """
    now = now or now_local()
    rows = list_memorials() if states is None else states
    out: dict = {"lapse": [], "overdue": []}
    for st in rows:
        if st.get("status") != "pending":
            continue
        # The docket itself is a memorial. Sweeping it into its own next
        # docket would make the backlog grow by one card a day forever.
        if str(st.get("source", "")) == ESCROW_DIGEST_SOURCE:
            continue
        age = _age_hours(st, now)
        if age is None:
            continue
        attention = str(st.get("attention", "")) or ATTENTION_NOTICE
        if attention == ATTENTION_DECISION:
            if age > ESCROW_HARD_LAPSE_H:
                out["lapse"].append((st, f"逾期未批 {age / 24:.0f} 天"))
            elif age > ESCROW_DEADLINE_H[ATTENTION_DECISION]:
                out["overdue"].append(st)
            continue
        deadline = ESCROW_DEADLINE_H.get(attention, ESCROW_DEADLINE_H[ATTENTION_NOTICE])
        if age > deadline:
            out["lapse"].append((st, f"未读满 {age / 24:.0f} 天"))
    return out


def escrow_docket(overdue: list[dict],
                  now: datetime | None = None) -> tuple[str, str]:
    """Render the daily docket as ``(title, body)``, grouped by source.

    37 raw rows read as 37 unanswered asks. Grouped, the same backlog read as
    "eigenflux-publish has 14 stuck" — a broken flow, not 14 decisions. The
    docket exists to make that distinction visible at a glance.
    """
    now = now or now_local()
    groups: dict[str, list[tuple[float, dict]]] = {}
    for st in overdue:
        age = _age_hours(st, now)
        groups.setdefault(str(st.get("source", "?")), []).append((age or 0.0, st))
    ranked = sorted(groups.items(), key=lambda kv: -max(a for a, _ in kv[1]))
    oldest = max((a for rows in groups.values() for a, _ in rows), default=0.0)
    title = f"待批 {len(overdue)} 件，最久 {oldest / 24:.0f} 天"
    lines = []
    for source, rows in ranked[:ESCROW_DIGEST_MAX_GROUPS]:
        top = max(a for a, _ in rows)
        label = SOURCE_TITLE.get(source, source)
        sample = sorted(rows, key=lambda r: -r[0])[0][1].get("title", "")
        detail = f"· **{label}** {len(rows)} 件 · 最久 {top / 24:.0f} 天"
        if len(rows) == 1 and sample:
            detail += f"\n  {sample[:38]}"
        lines.append(detail)
    rest = ranked[ESCROW_DIGEST_MAX_GROUPS:]
    if rest:
        lines.append(f"· 另有 {sum(len(r) for _, r in rest)} 件，来自 {len(rest)} 个来源")
    lines.append("\n未处理的会在 14 天后自动留中归档。")
    return title, "\n".join(lines)


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
        # If we still landed inside a markdown link, back up further.
        last_open = cut.rfind("[")
        if last_open != -1:
            after = cut[last_open:]
            close_b = after.find("]")
            if close_b == -1 or after.find(")", close_b) == -1:
                cut = cut[:last_open].rstrip()
        text = cut.rstrip()
        clipped = True
    if clipped:
        text += "\n\n…完整背景可点「聊聊这个」"
    return text


def _render_card(state: dict, *, body: str | None = None,
                 status_line: str = "", include_options: bool = True,
                 include_chat: bool = True) -> str:
    content = _display_body(state["body"] if body is None else body)
    if not status_line and state.get("status") == "pending":
        if (requires_decision(state)
                and review_surface(state) == REVIEW_LARK):
            status_line = "⚡ 请在飞书即时批"
        elif str(state.get("attention", "")) == ATTENTION_ALERT:
            status_line = "⚡ 即时提醒 · 无需批"
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


def _replacement_card(rendered: str, state: dict) -> dict:
    """Decode a replacement card, with a safe terminal-state fallback.

    The sentinel gate intentionally suppresses cards whose original body
    contains internal heartbeat residue. A later decision must still ACK the
    callback instead of trying to decode that empty render.
    """
    if rendered:
        try:
            card = json.loads(rendered)
            if isinstance(card, dict):
                return card
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    fallback = build_card(
        "Jarvis · 事项",
        "状态已更新。请在 Jarvis「事项」中查看完整记录。",
        button_groups=[[
            {
                "text": CHAT_BUTTON_LABEL,
                "type": "default",
                "value": {
                    "action": "memorial",
                    "id": state["id"],
                    "opt": CHAT_OPT_KEY,
                },
            }
        ]],
    )
    return json.loads(fallback)


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
    return _replacement_card(
        _render_card(
            state, status_line=status, include_options=False,
            include_chat=True,
        ),
        state,
    )


def _chatting_card(state: dict, ts: str) -> dict:
    """Replacement card after「聊聊这个」: chatting banner, remaining options
    stay tappable so Pascal can still 批 while (or after) chatting."""
    status = f"💬 聊天中 · {_hhmm(ts)} — 直接回消息就行"
    return _replacement_card(
        _render_card(
            state, status_line=status,
            include_options=state["status"] == "pending",
            include_chat=False,
        ),
        state,
    )


# ── sending (clone of heartbeat_loop's production lark-cli path) ────────


def _send(args: list[str], *, retries: bool = True) -> str:
    """Send via lark-cli. Returns the Lark message_id on success ("sent" if
    the id can't be parsed — still truthy), "" on failure. Callers that only
    care about success/failure keep working: the return is bool-compatible.

    ``retries=False`` is used by core.delivery, which owns the one canonical
    retry schedule.  The default remains for compatibility callers that invoke
    this low-level helper directly.
    """
    delays = (0,) + SEND_RETRY_DELAYS if retries else (0,)
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            r = subprocess.run(["lark-cli", "im", "+messages-send", *args,
                                "--as", "bot", "--json"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                try:
                    data = json.loads(r.stdout).get("data") or {}
                    mid = (data.get("message_id")
                           or (data.get("message") or {}).get("message_id") or "")
                    if not mid:
                        msgs = data.get("messages") or []
                        mid = (msgs[0].get("message_id") if msgs else "") or ""
                except Exception:
                    mid = ""
                return str(mid) or "sent"
        except subprocess.TimeoutExpired:
            print(f"memorial send attempt {attempt} timed out", file=sys.stderr)
        except Exception as e:
            print(f"memorial send attempt {attempt} failed: {e}", file=sys.stderr)
    return ""


def _send_card(card_json_str: str, chat_id: str = "") -> str:
    """Returns the sent card's Lark message_id ("sent" if unparsed, "" on
    failure) — REQ-118 needs the id for thread → memorial reverse lookup."""
    if chat_id:
        target = ["--chat-id", chat_id]
    else:
        uid = _resolve_user_id()
        if not uid:
            return ""
        target = ["--user-id", uid]
    return _send(
        [*target, "--msg-type", "interactive", "--content", card_json_str],
        retries=False,
    )


def _send_text(text: str, chat_id: str = "") -> str:
    if not text:
        return False
    if chat_id:
        target = ["--chat-id", chat_id]
    else:
        uid = _resolve_user_id()
        if not uid:
            return False
        target = ["--user-id", uid]
    return _send([*target, "--markdown", text], retries=False)


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
    # Compatibility audit only: SQLite delivery_envelopes is authoritative.
    # Avoid minting duplicate shadow rows when a caller re-submits the same
    # already-queued memorial.
    queue_path = JARVIS_DIR / MEMORIAL_QUEUE_FILE
    try:
        if queue_path.exists() and any(
                json.loads(line).get("memorial_id") == mid
                for line in queue_path.read_text(encoding="utf-8").splitlines()
                if line.strip()):
            return
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    readable = extract_card_text(card_json_str) or f"📜 {title}"
    _append_line(queue_path,
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
        from core.delivery import (DeliveryEnvelope, TransportResult,
                                   deliver as deliver_envelope)

        def transport(envelope, channel):
            if channel == "web":
                return TransportResult(True)
            sent = _send_text(str(envelope.payload.get("text") or ""), chat_id)
            message_id = "" if sent is True else str(sent or "")
            return TransportResult(bool(sent), message_id)

        result = deliver_envelope(
            DeliveryEnvelope(
                source="memorial-chat",
                kind="text",
                payload={"text": text},
                attention="reply",
                requested_channel="lark",
                conversation_bound=True,
                chat_id=chat_id,
                metadata={"bypass_throttle": True,
                          "dedup_text": f"{chat_id}\0{text}"},
            ),
            root=JARVIS_DIR,
            transport=transport,
        )
        if result.state == "delivered":
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
                           context: str, chat_id: str,
                           matter_id: str = "",
                           dedup_key: str = "",
                           attention: str = "",
                           review_at: str = "") -> dict | None:
    """A still-pending memorial with identical content created within the
    dedup window, or the same explicit external identity."""
    now = time.time()
    for st in _fold(read_jsonl(_ledger_path())).values():
        if (dedup_key and st["status"] == "pending"
                and st["source"] == source
                and st.get("dedup_key", "") == dedup_key):
            return st
        if (st["status"] == "pending"
                and st["source"] == source and st["title"] == title
                and st["body"] == body
                and st.get("options", []) == options
                and st.get("extra_buttons", []) == extra_buttons
                and st.get("context", "") == context
                and st.get("chat_id", "") == chat_id
                and st.get("matter_id", "") == matter_id
                and str(st.get("attention", "")) == attention
                and review_surface(st) == review_at
                and st.get("epoch") and now - st["epoch"] < DEDUP_WINDOW_S):
            return st
    return None


def _deliver_existing(
    state: dict,
    urgent: bool = False,
    *,
    proactive_reach: bool = True,
) -> bool:
    """Hand an already-ledgered memorial to the unified delivery pipeline."""
    from core.delivery import (DeliveryEnvelope, TransportResult,
                               deliver as deliver_envelope)

    mid = state["id"]
    cj = _render_card(state)
    review_at = review_surface(state)
    push_lark = should_push_to_lark(state)
    force_queue = (
        push_lark and not urgent and _quiet_hours_now()
    )

    def transport(envelope, channel):
        if channel == "web":
            return TransportResult(True)
        sent = _send_card(
            str(envelope.payload.get("card_json") or ""),
            state.get("chat_id", ""),
        )
        # Old tests and third-party adapters may still return bool.  Never
        # persist the literal "True" as a Lark message id.
        message_id = "" if sent is True else str(sent or "")
        return TransportResult(bool(sent), message_id)

    readable = extract_card_text(cj) or f"📜 {state['title']}"
    result = deliver_envelope(
        DeliveryEnvelope(
            source=state.get("source", "memorial"),
            kind="card",
            payload={"card_json": cj, "text": readable},
            attention=str(state.get("attention") or ATTENTION_NOTICE),
            requested_channel=REVIEW_LARK if push_lark else review_at,
            urgent=urgent,
            conversation_bound=bool(state.get("chat_id")),
            chat_id=state.get("chat_id", ""),
            memorial_id=mid,
            matter_id=state.get("matter_id", ""),
            dedup_key=str(state.get("dedup_key") or f"memorial:{mid}"),
            throttle_key=str(state.get("throttle_key") or ""),
            metadata={
                "review_surface": review_at,
                "dedup_text": json.dumps({
                    "title": state.get("title", ""),
                    "body": state.get("body", ""),
                    "options": state.get("options", []),
                }, ensure_ascii=False, sort_keys=True),
                "force_queue": force_queue,
                # Memorial owns the review-surface-specific quiet-hours
                # decision above. Avoid a second wall-clock check in the
                # generic pipeline disagreeing with that decision.
                "bypass_quiet": not force_queue,
                "retry_existing": True,
            },
        ),
        root=JARVIS_DIR,
        transport=transport,
    )

    if not push_lark:
        status = "phone_ready" if requires_decision(state) else "web_only"
        _record_delivery(mid, status)
        if status == "web_only" and proactive_reach:
            _request_proactive_reach(state)
        return result.accepted

    if result.state == "delivered":
        _record_delivery(
            mid, "delivered", source=state.get("source", "memorial"))
        if result.message_id:
            # REQ-118 奏折专属对话: remember the delivered card's Lark
            # message_id so a reply in its thread routes to a per-card session.
            try:
                from core.memorial_thread import record_sent
                record_sent(mid, result.message_id)
            except Exception as e:
                print(f"memorial {mid}: record_sent failed: {e}", file=sys.stderr)
        _write_outbox(readable + f"\n\n（奏折 {mid} 已发出，等批示）")
        return True

    if result.state == "suppressed":
        _record_delivery(mid, "suppressed")
        return True

    if result.state == "attempting":
        _record_delivery(mid, "retry_queued")
        return True

    if force_queue and result.reason == "quiet_hours":
        _record_delivery(mid, "queued")
        print(f"memorial {mid}: quiet hours — queued in delivery state",
              file=sys.stderr)
        return True

    _record_delivery(mid, "failed")
    _record_delivery(mid, "retry_queued")
    return False


def _request_proactive_reach(state: dict) -> None:
    """Best-effort phone reach after a notice is durably in Memorial."""
    try:
        from core.proactive import maybe_push_signal
        maybe_push_signal(state, root=JARVIS_DIR)
    except Exception as exc:
        # The durable Memorial is the primary handoff. Optional reach must
        # never turn a stored signal into a failed delivery.
        print(
            f"memorial {state.get('id', '')}: proactive reach skipped: {exc}",
            file=sys.stderr,
        )


ROTATE_AFTER_DAYS = 45


def rotate_ledger(now: float | None = None) -> int:
    """Archive event groups of cards older than ROTATE_AFTER_DAYS.

    The ledger is append-only with no retirement, ~80 lines/day — a year in,
    every get_memorial() folds ~30k lines. Move each card's COMPLETE event
    group (state folds from the full group, so it must travel together) into
    memorials.YYYY-MM.jsonl next to the ledger, bucketed by creation month.
    A 45-day-old un-批 card is dead weight: a late button tap degrades to
    「这张卡对应的事项找不到了」, which decide() already handles.

    Returns the number of archived cards. The whole read→rewrite→replace
    runs under ledger_lock, so concurrent writers (sidecar decide events,
    heartbeat deliveries — all of whom take the same lock) can never land
    an append on the old inode mid-swap. Archive appends happen only AFTER
    a successful swap and are deduped against lines already in the month
    file, so an aborted earlier attempt cannot double-archive a group.
    """
    now = now or time.time()
    path = _ledger_path()
    with ledger_lock(path):
        return _rotate_ledger_locked(path, now)


def _rotate_ledger_locked(path: Path, now: float) -> int:
    try:
        size_before = path.stat().st_size
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    cutoff = now - ROTATE_AFTER_DAYS * 86400
    groups: dict[str, list[int]] = {}
    anchors: dict[str, dict] = {}
    for i, line in enumerate(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        mid = str(ev.get("id", ""))
        groups.setdefault(mid, []).append(i)
        if ev.get("ev") == "create" and mid not in anchors:
            anchors[mid] = ev
    archive_idx: set[int] = set()
    buckets: dict[str, list[str]] = {}
    archived_cards = 0
    for mid, idxs in groups.items():
        anchor = anchors.get(mid)
        if not anchor:
            continue  # orphan events (no create here): leave in place
        epoch = anchor.get("epoch")
        if not isinstance(epoch, (int, float)) or epoch >= cutoff:
            continue
        month = datetime.fromtimestamp(epoch).strftime("%Y-%m")
        buckets.setdefault(month, []).extend(lines[i] for i in idxs)
        archive_idx.update(idxs)
        archived_cards += 1
    if not archive_idx:
        return 0
    retained = [ln for i, ln in enumerate(lines) if i not in archive_idx]
    try:
        # Verify → swap → then archive. Archiving before an aborted swap
        # leaves duplicate groups behind; a crash between swap and append
        # is recoverable from the nightly backup, whereas silent
        # duplication would poison the archive forever.
        if path.stat().st_size != size_before:
            return 0  # someone appended mid-rotation; retry next month
        tmp = path.with_suffix(".jsonl.rotating")
        tmp.write_text("\n".join(retained) + ("\n" if retained else ""),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return 0
    for month, month_lines in sorted(buckets.items()):
        dest = path.parent / f"memorials.{month}.jsonl"
        try:
            existing = set(dest.read_text(encoding="utf-8").splitlines())
        except OSError:
            existing = set()
        fresh = [ln for ln in month_lines if ln not in existing]
        if not fresh:
            continue
        with open(dest, "a", encoding="utf-8") as f:
            f.write("\n".join(fresh) + "\n")
    return archived_cards


def _maybe_rotate() -> None:
    """Once per calendar month, on the first create() of the month."""
    marker = _ledger_path().parent / ".memorials_rotated.json"
    month = datetime.now().strftime("%Y-%m")
    try:
        if (json.loads(marker.read_text(encoding="utf-8")) or {}).get("month") == month:
            return
    except (OSError, ValueError):
        pass
    try:
        n = rotate_ledger()
        if n:
            print(f"memorial ledger rotated: {n} cards archived", file=sys.stderr)
    except Exception as e:
        print(f"memorial ledger rotation failed: {e}", file=sys.stderr)
        return
    try:
        marker.write_text(json.dumps({"month": month}), encoding="utf-8")
    except OSError:
        pass


def create(source: str, title: str, body: str, options: list[dict] | None = None,
           preset: str | None = None, context: str = "",
           chat_id: str = "", send: bool = True,
           urgent: bool = False,
           extra_buttons: list[dict] | None = None,
           matter_id: str = "",
           dedup_key: str = "",
           attention: str = "",
           review_at: str = "") -> tuple[str, bool]:
    """Create a memorial, append it to the ledger, and route it.

    Returns ``(memorial_id, accepted)``. Accepted means the memorial is either
    visible on its durable phone/web surface or accepted by the Lark delivery
    path. The ledger write happens before either route.

    send=False skips outbound delivery (no direct send, no outbox mirror) for
    emitters that own a transport. Phone/web-routed rows are still marked ready
    because the ledger itself is their durable surface.

    Direct sends respect the delivery layer's gates: an identical pending
    memorial within 6h is not re-created, and an explicit ``dedup_key`` stays
    unique for as long as that memorial is pending. Non-urgent sends during
    quiet hours (23:30-10:00) go to the night queue instead of buzzing the
    phone; urgent=True bypasses the night gate.
    """
    _maybe_rotate()
    source, title, body = str(source), str(title), str(body)
    # TITLE:/OPTIONS: are authoring directives for the model, never content.
    # Stripping them is unconditional and lives HERE, at the one boundary every
    # card passes through, because attaching it to a particular entry path is
    # what leaked it four times (audit P0 #268/#276/#282/#285, 7/22–7/27):
    #   - a caller passing explicit options skipped the OPTIONS strip entirely
    #     (intentions closure cards shipped the raw line);
    #   - adopt_card builds its body from card elements and never ran either
    #     extractor, so directly-built rich cards (daily-reflect) shipped both.
    # Whose buttons win is a SEPARATE decision from whether the residue is
    # removed: an explicit caller still overrides the parsed line below.
    body, inline_options = _extract_inline_options(body)
    leading_title, stripped_body = _extract_title_line(body)
    if leading_title:
        body = stripped_body
        if not str(title).strip():
            title = leading_title
    if options is None and inline_options:
        options, preset = inline_options, None
    opts = _normalize_options(options, preset)
    native_buttons = _normalize_extra_buttons(extra_buttons)
    attention = str(
        attention or _default_attention(source, opts, native_buttons)
    )
    if urgent and attention == ATTENTION_NOTICE:
        attention = ATTENTION_ALERT
    if attention not in {ATTENTION_DECISION, ATTENTION_NOTICE, ATTENTION_ALERT}:
        raise ValueError("attention must be decision, notice, or alert")
    inferred_review_at = _infer_review_surface(
        source, attention, native_buttons, urgent=urgent, chat_id=chat_id)
    # Hard time/conversation/native-action constraints cannot be downgraded by
    # a caller accidentally passing phone. Ordinary decisions may explicitly
    # opt into Lark when a trusted emitter has stronger context.
    review_at = str(
        REVIEW_LARK if inferred_review_at == REVIEW_LARK
        else (review_at or inferred_review_at)
    )
    if review_at not in REVIEW_SURFACES:
        raise ValueError("review_at must be lark, phone, or none")
    if attention == ATTENTION_DECISION and review_at == REVIEW_NONE:
        raise ValueError("decisions must be reviewed on lark or phone")
    if attention != ATTENTION_DECISION and review_at != REVIEW_NONE:
        raise ValueError("only decisions can have a review surface")

    if not matter_id:
        try:
            from core.matter_router import matter_id_from_context
            matter_id = matter_id_from_context(context)
        except Exception:
            matter_id = ""

    dup = _find_recent_duplicate(
        source, title, body, opts, native_buttons, str(context), str(chat_id),
        str(matter_id), str(dedup_key), attention, review_at)
    if dup is not None:
        print(f"memorial dedup: identical pending {dup['id']} within "
              f"{DEDUP_WINDOW_S // 3600}h — not re-created", file=sys.stderr)
        if not send:
            if (not should_push_to_lark(dup)
                    and not delivery_accepted(dup)):
                return dup["id"], _deliver_existing(
                    dup, urgent=urgent, proactive_reach=False)
            return dup["id"], False
        if delivery_accepted(dup):
            return dup["id"], True
        if should_push_to_lark(dup):
            return dup["id"], _deliver_existing(dup, urgent=urgent)
        return dup["id"], _deliver_existing(dup, urgent=urgent)

    mid = _new_id()
    ts = now_local_str()
    ev = {"ev": "create", "id": mid, "ts": ts, "epoch": int(time.time()),
          "source": source, "title": title, "body": body, "options": opts,
          "extra_buttons": native_buttons, "context": str(context),
          "attention": attention, "review_surface": review_at}
    if dedup_key:
        ev["dedup_key"] = str(dedup_key)
    if chat_id:
        ev["chat_id"] = str(chat_id)
    if matter_id:
        ev["matter_id"] = str(matter_id)
    _append_line(_ledger_path(), ev)

    if matter_id:
        # The append-only ledger is already durable. Linking is best-effort so
        # a temporary dashboard lock can never suppress a user-facing card.
        try:
            from core.matters import add_event, link_entity
            link_entity(matter_id, "memorial", mid, provider="jarvis", title=title,
                        metadata={"source": source, "status": "pending",
                                  "review_surface": review_at},
                        actor="memorial")
            add_event(
                matter_id, "memorial_created", title, actor=source,
                payload={"memorial_id": mid, "review_surface": review_at})
        except Exception as e:
            print(f"memorial {mid}: matter link failed: {e}", file=sys.stderr)

    state = _fold([ev])[mid]
    cj = _render_card(state)
    if not send:
        if not should_push_to_lark(state):
            return mid, _deliver_existing(
                state, urgent=urgent, proactive_reach=False)
        return mid, False
    if should_push_to_lark(state):
        return mid, _deliver_existing(state, urgent=urgent)
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
               suppress_accepted: bool = False,
               route_notices_to_web: bool = False,
               proactive_reach: bool = False) -> str:
    """Adopt an existing Lark card into the memorial interaction surface.

    Task-native actions/links are preserved. Cards that already offer a real
    action keep those choices and gain「聊聊这个」; read-only/link-only cards
    also gain the common「已阅／标为重点」批红 pair.

    Returns one card JSON per line: a button-free body that mechanically
    merged several matters is split (一张卡一件事, REQ-117), so the result
    may be several newline-joined single-line card JSONs.
    """
    card = json.loads(legacy_card_json)
    source = str(card.pop("__jarvis_source", "") or source).strip()
    if _card_memorial_id(card):
        return json.dumps(card, ensure_ascii=False, separators=(",", ":"))

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
    # A task that builds its own rich card still writes TITLE:/OPTIONS: — they
    # are model authoring directives, not content, and this path never ran the
    # extractors, so both shipped verbatim (audit P0 #268/#282/#285,
    # daily-reflect 7/22–7/27). The header of a directly-built card is a
    # decorative source label ("🌙 回顾"); an explicit TITLE line is the one
    # thing that says what THIS card is about, so it wins.
    body, inline_options = _extract_inline_options(body)
    explicit_title, rest = _extract_title_line(body)
    if explicit_title:
        title, body = explicit_title, rest
    if not body:
        body = title
    has_native_action = any("value" in button for button in native_buttons)
    # An existing callback already represents the card's decision options;
    # don't bury it under generic FYI buttons. URL-only cards are read-only,
    # so the common FYI choices remain useful.
    options = [] if has_native_action else (inline_options or None)
    fallback_preset = SOURCE_DEFAULT_PRESET.get(source or "heartbeat", "fyi")
    # 一张卡一件事 (REQ-117): a button-free legacy card that mechanically
    # merged several matters (the 7/21 日程变动 card carried three 改期 lines)
    # becomes one memorial per matter. Cards with native buttons are never
    # split — their buttons bind to the card as a whole and replicating a
    # callback across cards would multiply its action.
    # A card whose author wrote its own OPTIONS line designed ONE interactive
    # ask — splitting it would replicate that ask across cards. Same rule the
    # plain-text route already applies to inline options.
    matters = ([body] if (native_buttons or inline_options)
               else split_matters(body))
    if len(matters) > 1:
        print(f"memorial split: adopted {source or 'heartbeat'} card → "
              f"{len(matters)} cards (一张卡一件事)", file=sys.stderr)
    outputs: list[str] = []
    for matter in matters:
        mid, _ = create(
            source=source or "heartbeat", title=title, body=matter,
            options=options,
            preset=None if (has_native_action or inline_options)
            else fallback_preset,
            context=context, send=False, extra_buttons=native_buttons,
            attention=(ATTENTION_ALERT if _looks_like_alert(f"{title}\n{matter}")
                       and not has_native_action and not inline_options
                       and fallback_preset == "fyi"
                       else ""),
        )
        state = get_memorial(mid) or {}
        if route_notices_to_web and not should_push_to_lark(state):
            if proactive_reach:
                _request_proactive_reach(state)
            continue
        if suppress_accepted:
            if delivery_accepted(state):
                continue
        outputs.append(card_json(mid))
    return "\n".join(outputs)


def _extract_title_line(text: str) -> tuple[str, str]:
    """Pop an explicit TITLE:/标题： first line; returns (title, rest).

    A TITLE-only output stays a card (title doubles as body); an over-long
    title is clipped for the header but the full line is kept as the body's
    first line — degrade, never drop words.
    """
    lines = text.splitlines()
    if not lines:
        return "", text
    m = _TITLE_LINE_RE.match(lines[0])
    if not m:
        return "", text
    full = m.group(1)
    rest = "\n".join(lines[1:]).strip()
    if len(full) > MAX_TITLE_CHARS:
        rest = (full + "\n" + rest).strip()
    if not rest:
        rest = full
    return full[:MAX_TITLE_CHARS], rest


def _title_for_chunk(chunk: str, source: str) -> tuple[str, str]:
    """Derive a content title for one card; returns (title, body).

    A short first line over more content reads as this card's own headline —
    promote it to the header (and drop it from the body when it was written
    as a markdown heading, to avoid saying it twice). Anything else keeps the
    per-source generic label.
    """
    lines = chunk.splitlines()
    stripped = [ln.strip() for ln in lines if ln.strip()]
    if len(stripped) >= 2:
        first = stripped[0]
        # Bold markers are pure markup — remove them everywhere (asymmetric
        # cases like「**紧急**：磁盘满」must not leave stray asterisks in the
        # header). 【】/《》 are content, not markup: keep them intact.
        clean = first.replace("**", "").lstrip("#").strip()
        if 4 <= len(clean) <= MAX_TITLE_CHARS:
            # Drop the line from the body only when it was a PURE heading
            # (fully wrapped markup) — a prose first line stays in the body.
            pure_heading = (first.startswith("#")
                            or (first.startswith("**") and first.endswith("**")))
            if pure_heading:
                idx = next(i for i, ln in enumerate(lines) if ln.strip())
                body = "\n".join(lines[idx + 1:]).strip()
                if body:
                    return clean, body
            return clean, chunk
    return SOURCE_TITLE.get(source, source or "一件事"), chunk


def memorialize_output(output: str, source: str = "heartbeat") -> str:
    """Convert proactive heartbeat output to one memorial card per event.

    Existing decision cards pass through. Legacy cards are adopted while
    preserving their native actions. Prose without explicit choices is stored
    as a web notice instead of being pushed into Lark as another pending card.
    Raw internal JSON remains blocked.
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
        # Echoed prompt framing ("=== TASK: x ===", "[CHECKIN]", "[ts] task")
        # is never card content — same class fix as checkin_post (REQ-104).
        from core.safety import strip_task_framing
        text = strip_task_framing(text)
        if not text:
            return
        explicit_title, text = _extract_title_line(text)
        if not text:
            return
        # Buttons follow the card: an OPTIONS line authored by the task wins;
        # otherwise fall back to what this source is usually asking for, and
        # only then to「已阅」.
        body, inline_options = _extract_inline_options(text)
        preset = (None if inline_options
                  else SOURCE_DEFAULT_PRESET.get(single_source, "fyi"))
        # 一张卡一件事 (REQ-117): the prompt contract is the first line of
        # defense; this is the mechanical backstop for bodies that merged
        # several matters anyway. A card whose author wrote its own OPTIONS
        # line designed ONE interactive ask — never split that.
        chunks = ([body] if inline_options
                  else split_matters(body))
        if len(chunks) > 1:
            print(f"memorial split: {single_source} prose body → "
                  f"{len(chunks)} cards (一张卡一件事)", file=sys.stderr)
        for chunk in chunks:
            if explicit_title and len(chunks) == 1:
                chunk_title, chunk_body = explicit_title, chunk
            else:
                chunk_title, chunk_body = _title_for_chunk(chunk, single_source)
            mid, _ = create(single_source, chunk_title, chunk_body,
                            options=inline_options, preset=preset, send=False,
                            attention=(ATTENTION_ALERT
                                       if _looks_like_alert(chunk_body)
                                       and not inline_options and preset == "fyi"
                                       else ""))
            state = get_memorial(mid) or {}
            if not should_push_to_lark(state):
                _request_proactive_reach(state)
                continue
            if delivery_accepted(state):
                continue
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
            existing_id = _card_memorial_id(card)
            if existing_id:
                state = get_memorial(existing_id) or {}
                if should_push_to_lark(state):
                    card.pop("__jarvis_source", None)
                    adopted = json.dumps(
                        card, ensure_ascii=False, separators=(",", ":"))
                else:
                    _request_proactive_reach(state)
                    adopted = ""
            else:
                adopted = adopt_card(
                    single_source, card_raw, suppress_accepted=True,
                    route_notices_to_web=True,
                    proactive_reach=True,
                )
            if adopted:
                rendered.append(adopted)
        elif line:
            prose.append(raw_line)
    flush_prose()
    return "\n".join(rendered)


def _execute_action(
        action: dict, *, owner_authenticated: bool = False) -> str:
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
        owner_authenticated=owner_authenticated,
    )
    handler = getattr(ap, f"_do_{atype}", None)
    if handler is None:
        raise ValueError(f"unknown action type: {atype}")
    return handler(raw) or ""


def _sync_lark_card(memorial_id: str, card: dict) -> None:
    """Best-effort update of every delivered Lark copy after a web decision."""
    try:
        from core.memorial_thread import sent_message_ids
        message_ids = sent_message_ids(memorial_id)
    except Exception:
        return
    if not message_ids:
        return
    data = json.dumps(
        {"content": json.dumps(card, ensure_ascii=False)}, ensure_ascii=False)
    for message_id in message_ids:
        try:
            result = subprocess.run(
                ["lark-cli", "api", "PATCH",
                 f"/open-apis/im/v1/messages/{message_id}",
                 "--data", data, "--as", "bot"],
                capture_output=True, text=True, timeout=12,
            )
            if result.returncode != 0:
                print(f"memorial {memorial_id}: Lark card sync failed for "
                      f"{message_id}: {(result.stderr or result.stdout)[:180]}",
                      file=sys.stderr)
        except Exception as e:
            print(f"memorial {memorial_id}: Lark card sync failed: {e}",
                  file=sys.stderr)


def _complete_surface_handoffs(memorial_id: str) -> None:
    """Best-effort convergence for phone/desktop continuation affordances."""
    try:
        from core.continuity import complete_entity_handoffs
        complete_entity_handoffs("memorial", memorial_id)
    except Exception as e:
        print(f"memorial {memorial_id}: handoff completion failed: {e}",
              file=sys.stderr)


def resolve(memorial_id: str, label: str,
            action_result: str = "") -> bool:
    """Converge a memorial to an externally confirmed terminal state.

    Unlike ``decide``, this never runs a button action or injects a synthetic
    user reply. It is for state already completed in the source system.
    """
    st = get_memorial(memorial_id)
    if st is None:
        return False
    label = str(label or "已处理").strip()
    action_result = str(action_result or "").strip()
    if (st.get("resolved_label") == label
            and st.get("action_result", "") == action_result):
        return False
    _append_line(_ledger_path(), {
        "ev": "resolve",
        "id": memorial_id,
        "ts": now_local_str(),
        "label": label,
        "result": action_result,
    })
    resolved = get_memorial(memorial_id)
    if resolved is not None:
        _sync_lark_card(memorial_id, _decided_card(resolved))
    _complete_surface_handoffs(memorial_id)
    return True


def _finish_decide_side_effects(
        st: dict, memorial_id: str, opt_key: str, opt: dict,
        action_result: str, action_failed: bool,
) -> None:
    """Post-decision bookkeeping (matter link, context queue, card update).

    Safe to call from a background thread — no return value needed.
    When action_failed, re-syncs the Lark card to show the error state.
    """
    if st.get("matter_id"):
        try:
            from core.matters import add_event, link_entity
            link_entity(
                st["matter_id"], "memorial", memorial_id, provider="jarvis",
                title=st.get("title", ""),
                metadata={"source": st.get("source", ""), "status": "decided",
                          "decision": opt.get("label", ""),
                          "review_surface": review_surface(st)},
                actor="memorial",
            )
            add_event(st["matter_id"], "memorial_decided",
                      opt.get("label", ""), actor="user",
                      payload={"memorial_id": memorial_id, "option": opt_key,
                               "action_result": action_result})
        except Exception as e:
            print(f"memorial {memorial_id}: matter decision link failed: {e}",
                  file=sys.stderr)
    if opt.get("reply") or opt_key not in _FYI_KEYS:
        _queue_decision_context(st, opt.get("label", ""), action_result,
                                is_reply=bool(opt.get("reply")))
    if action_failed:
        _sync_lark_card(memorial_id, _decided_card(st))


def decide(
        memorial_id: str,
        opt_key: str,
        *,
        owner_authenticated: bool = False,
) -> dict:
    """批红 one option. Returns the card-callback response payload.

    Idempotent: a second tap (any option) returns「已批过」without re-running
    the action or appending another decide event.
    """
    st = get_memorial(memorial_id)
    if st is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
    if st["status"] == "decided":
        _complete_surface_handoffs(memorial_id)
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
    try:
        from core.delivery import DeliveryPipeline
        DeliveryPipeline(JARVIS_DIR).confirm_entity(
            memorial_id=memorial_id, state="acted")
    except Exception as e:
        print(f"memorial delivery confirm failed: {e}", file=sys.stderr)
    # 批红 = engagement：same "feedback" shape the legacy card buttons write,
    # so engagement-analyze sees which sources Pascal actually acts on.
    _record_engagement({"source": st.get("source", "memorial"),
                        "type": "feedback", "rating": opt_key})

    # Actions (eigenflux_publish ~30s, calendar_create ~15s) can exceed the
    # 3-second Lark card-callback ACK deadline.  Run in a thread; join with a
    # short budget — if the action finishes in time, include the result on the
    # returned card.  Otherwise return immediately and let the thread finish +
    # re-sync the Lark card asynchronously.
    _ACTION_BUDGET_S = 2.0
    has_action = bool(opt.get("action"))
    action_result, action_failed = "", False

    if has_action:
        result_box: list = []

        def _run_action():
            try:
                r = _execute_action(
                    opt["action"],
                    owner_authenticated=owner_authenticated,
                )
                result_box.append((r, False))
            except Exception as e:
                result_box.append((f"FAILED: {e}", True))

        t = threading.Thread(
            target=_run_action, daemon=True,
            name=f"memorial-action-{memorial_id[:8]}")
        t.start()
        t.join(timeout=_ACTION_BUDGET_S)

        if result_box:
            action_result, action_failed = result_box[0]
            _append_line(_ledger_path(), {"ev": "action_result",
                                          "id": memorial_id,
                                          "ts": now_local_str(),
                                          "result": action_result})
        else:
            # Action still running — fire-and-forget; thread will log + sync.
            def _await_and_sync():
                t.join()
                if not result_box:
                    return
                res, failed = result_box[0]
                _append_line(_ledger_path(), {"ev": "action_result",
                                              "id": memorial_id,
                                              "ts": now_local_str(),
                                              "result": res})
                st.update(action_result=res)
                _finish_decide_side_effects(
                    st, memorial_id, opt_key, opt, res, failed)
            threading.Thread(
                target=_await_and_sync, daemon=True,
                name=f"memorial-await-{memorial_id[:8]}").start()

    st.update(status="decided", decided_opt=opt_key,
              decided_label=opt.get("label", ""), decided_ts=ts,
              action_result=action_result)
    if not (has_action and not result_box):
        _finish_decide_side_effects(
            st, memorial_id, opt_key, opt, action_result, action_failed)

    if action_failed:
        toast = {"type": "info", "content": "已批，但动作执行出错了——直接在对话里告诉我"}
    elif opt.get("reply"):
        toast = {"type": "success", "content": "收到——下条消息我接着这个说"}
    else:
        toast = {"type": "success", "content": f"已批：{opt.get('label', '')} ✓"}
    decided_card = _decided_card(st)
    _sync_lark_card(memorial_id, decided_card)
    _complete_surface_handoffs(memorial_id)
    return {"toast": toast, "card": {"type": "raw", "data": decided_card}}


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


def conversation_deep_link(state: dict) -> str:
    """Best Lark destination for continuing one memorial conversation."""
    matter_id = str(state.get("matter_id", "") or "")
    if matter_id:
        try:
            from core.matter_bridge import bindings_for_matter, lark_deep_link
            bindings = bindings_for_matter(matter_id)
            if bindings:
                return lark_deep_link(bindings[0])
        except Exception:
            pass
    chat_id = str(state.get("chat_id", "") or "")
    if chat_id:
        return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"
    user_id = _resolve_user_id()
    if user_id:
        return f"https://applink.feishu.cn/client/chat/open?openId={user_id}"
    return ""


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
                "deep_link": conversation_deep_link(st),
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

    return {"toast": {"type": "success", "content": "已加载背景——回对话窗回复我即可"},
            "deep_link": conversation_deep_link(st),
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
    sp.add_argument("--review-at", choices=(REVIEW_PHONE, REVIEW_LARK),
                    default="", help="preferred approval surface")

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
                               urgent=args.urgent, review_at=args.review_at)
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
