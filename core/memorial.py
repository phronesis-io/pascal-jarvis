"""Memorial (奏折) cards — the unified "ask the owner" surface.

Every proactive output that needs the owner's eyes (mail triage, decisions,
follow-ups, heartbeat asks…) becomes one durable memorial.  Lark is the only
delivery surface (REQ-119, 2026-08-11): alerts, decisions, and notices all go
to the chat, while ambient monitoring exhaust (AMBIENT_SOURCES) stays
ledger-only and is batched into the morning anchor's digest line.
Tapping an option = 批红: it is recorded (and optionally
executes an action through ActionProcessor). Tapping「聊聊这个」injects the
memorial's full context into the p2p conversation via bot.sh's existing
pending-merge channel, so the owner's NEXT message lands with the topic loaded.

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
sentence the owner would have typed; (2) ``SOURCE_DEFAULT_PRESET`` for sources
that inherently ask for a decision or a follow-up; (3)「已阅／标为重点」.

Every new user-visible card also carries a short work receipt. Proactive model
output must author it as ``WORKED: ...``; deterministic callers pass
``work_receipt=...``. The receipt says what Jarvis already completed before
asking for the owner's attention. Missing receipts fail closed at model and
Routine boundaries instead of turning unfinished work into a card.

CLI (any emitter can send a memorial in one line):
    python3 -m core.memorial send --source mail --title "..." --body "..." \
        --worked "read and classified the message" --preset fyi
    python3 -m core.memorial send --source x --title t --body b \
        --worked "verified trigger and current state" \
        --option '准=intent_close:id=xxx,outcome=done' --option '缓'
    python3 -m core.memorial send --source x --title t --body b \
        --worked "compared the available paths" \
        --options '加钱|限流到月底|让它自然停'
    python3 -m core.memorial list [--pending]
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from core.card import extract_card_text
from core.card_split import split_matters
from core.jsonl import read_jsonl
from core import memorial_cards, memorial_ledger, memorial_transport
from core.card_envelope import (
    is_card_payload,
    memorial_action_id,
    strip_memorial_actions,
    trusted_ledger_card_id,
)
from core.log import log
from core.interruption import evaluate as evaluate_interruption
from core.interruption import (MESSAGE_CONTRACT_FIELDS, build_message_contract,
                               infer_owner_need)
from core.memorial_contracts import (
    ATTENTION_ALERT,
    ATTENTION_DECISION,
    ATTENTION_NOTICE,
    STATUS_LAPSED,
)
from core.timeutil import now_local, now_local_str

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR",
                                 Path(__file__).resolve().parent.parent))
_INITIAL_JARVIS_DIR = JARVIS_DIR
# Backward-compatible name introduced by the write-isolation audit.
_IMPORTED_JARVIS_DIR = _INITIAL_JARVIS_DIR


def runtime_root() -> Path:
    """Resolve the active data root without defeating facade monkeypatches.

    Tests historically patch ``core.memorial.JARVIS_DIR``.  Keep that hook
    authoritative while also observing a ``JARVIS_DIR`` environment change
    made after import when the facade global is untouched.
    """
    root = Path(JARVIS_DIR)
    if root != _INITIAL_JARVIS_DIR:
        return root
    configured = os.environ.get("JARVIS_DIR", "").strip()
    return Path(configured) if configured else root


def _ops_log(message: str, *, level: str = "info", **fields) -> None:
    """Emit grep-friendly operational evidence without private card text."""
    try:
        log("memorial", message, level=level, **fields)
    except Exception:
        pass

# The chat button is framework-owned: every memorial gets it, emitters can't
# claim the key for their own options.
CHAT_OPT_KEY = "chat"
FULL_TEXT_OPT_KEY = "full_text"
# 「看不懂」(2026-08-03, owner: 「很多东西我都看不懂他在说什么」). Not a 批红:
# the card stays pending — he hasn't answered it, he couldn't parse it. The
# tap is (1) an honest style-failure signal on the ledger, (2) a request for
# an immediate plain-language retelling (explain queue → heartbeat), and
# (3) a negative example future card-writing prompts are shown.
CONFUSED_OPT_KEY = "confused"
CHAT_BUTTON_TEXT = "聊聊这个"
CHAT_BUTTON_LABEL = f"💬 {CHAT_BUTTON_TEXT}"
FULL_TEXT_BUTTON_LABEL = "📖 查看全文"

# Same retry profile as core.heartbeat_loop (REQ-11).
SEND_RETRY_DELAYS = (2, 5)

# A re-tap of「聊聊这个」within this window is a no-op (the first tap already
# sent the opener + queued the injection): Lark re-pushes un-ACKed callback
# events andthe owner re-taps after a client-side "操作失败", so chat() must not
# stack duplicate openers/injections.
CHAT_RETAP_THROTTLE_S = 120

# The ledger keeps the full event; the phone card is the decision surface, not
# the archive.  This bound prevents an emitter regression from recreating the
# old wall-of-text experience while「聊聊这个」still receives richer context.
CARD_BODY_MAX_CHARS = 480
CARD_BODY_MAX_LINES = 6

# When the card had to clip, 「聊聊这个」sends the untruncated source text as a
# chat message. Bounding it keeps a runaway emitter from pasting a novel into
# the conversation, but it sits far above the card bound: the card is the
# decision surface, the chat is where the full record is allowed to land.
# Owner feedback: the mobile card showed only clipped fragments. Tapping used to load
# context for the MODEL and tell HIM nothing he hadn't already seen.)
FULL_TEXT_MAX_CHARS = 4000
# Follow-up chunks leave room for the title and an honest remaining/done note
# inside one Lark text message.
CONTINUATION_CHUNK_CHARS = 3500
# The 背景 section rides after the full body with its own budget, so a long
# body can never silently amputate it.
CHAT_OPENER_CONTEXT_MAX = 1000

# The model's injected chat context must cover at least everything the opener
# just showed the owner(FULL_TEXT_MAX_CHARS of body) — if he quotes the tail of
# the「全文」he was sent and the model never saw it, the model confabulates in
# the very conversation whose point was giving both sides the same record.
CHAT_CONTEXT_MAX_CHARS = 6000

# Says what the button DOES, not that context exists somewhere.
CLIP_NOTICE = "…还有下半段。点第一排「查看全文」，一次发完"

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
    # Companion checkin (2026-08-02). The positive end of the gradient is the
    # 「聊聊这个」button adopt_card already adds to every card, so this preset
    # supplies only the neutral and the NEGATIVE — the one that was missing.
    #
    # Until now a checkin offered「已阅／标为重点」, and 22 of 23 cards ever sent
    # were acknowledged. That number taught nothing: 「已阅」is emitted both by
    # "that was good" and by "noted, go away". the owner's only way to say the
    # second was to complain out of band, which he did four times, after which
    # a human overcorrected the prompt into ten days of total silence.
    #
    #「这类不必」names the card's KIND rather than the card, so one tap costs no
    # more than dismissing it but teaches something general. core.companion
    # turns it into a smaller daily allowance for that kind — never zero, since
    # a mute kind can never earn its way back.
    "companion": [
        {"key": "ack", "label": "知道了", "action": None},
        {"key": "not_this_kind", "label": "这类不必", "action": None},
    ],
}

# A tap on a REPLY option means "the owner said this sentence". The label is the
# suggested reply itself, so it is carried into the next conversation turn
# first-person (see _queue_decision_context) instead of being filed away as a
# generic 批红 rating. FYI keys are the only taps that stay purely analytic.
# "pause" is the routines mute control (「这条以后别发了」) — like
# "not_this_kind" it silences a source rather than answering an ask, so it
# must not promote the card to decision class or speak first-person.
_FYI_KEYS = {"read", "watch", "ack", "not_this_kind", "pause"}

# ── 缴回制度 (escrow) ────────────────────────────────────────────────────
# A card sent once and never tapped used to scroll out of Lark and vanish:
# 314 of 600 memorials sat pending forever, 110 of them older than a week,
# 47 of those decision-class — real asks silently lost, indistinguishable from
# noise nobody ever intended to answer. Nothing swept pending cards at all.
#
# Deadlines are measured, not guessed (7/29, over memorials decided since 7/01):
#   decision  median 2.1h, 75% inside 24h, 90% inside 48h, only 3% ever later
#   alert     median 0.2h, 78% inside 24h — a stale alert has no salvage value
#   notice    is informational, not work the owner owes the system. The 2026-08-17
#             owner reset made this explicit: after one day it leaves the live
#             queue and remains recoverable as 留中. This bounds attention debt
#             even when a delivery outage creates a large historical backlog.
ESCROW_DEADLINE_H = {
    ATTENTION_ALERT: 24,
    ATTENTION_NOTICE: 24,
    ATTENTION_DECISION: 48,
}
# A decision past its deadline is NOT archived — it is re-surfaced in the daily
# 匣子 docket. But 批红 that never comes is itself an answer: past this hard
# ceiling it is filed as 留中 so the docket cannot nag forever. A bounded
# production-ledger review found no decisions completed after the ordinary
# overdue window; exact private activity metrics remain outside this public
# repository.
#
# The old 14-day ceiling let obsolete asks ride the morning docket long after
# they stopped being actionable. Four days retains a generous decision window
# while bounding repeated asks. 留中 is archival, not deletion: the row stays
# in the ledger and the docket still says so in one line.
ESCROW_HARD_LAPSE_H = 24 * 4
# 御门听政: the docket goes out once a day, in the morning, as ONE card.
# Re-pushing stale cards individually is the card storm this system was
# already burned by (7/22) — the emperor gets a docket, not the pile.
ESCROW_DIGEST_HOURS = range(8, 12)
ESCROW_DIGEST_SOURCE = "memorial-escrow"

REVIEW_LARK = "lark"
REVIEW_PHONE = "phone"
REVIEW_NONE = "none"
REVIEW_SURFACES = {REVIEW_LARK, REVIEW_PHONE, REVIEW_NONE}

# Successful handoff means either an interrupting Lark delivery or a durable
# ledger placement (ambient exhaust, REQ-119). Callers that ingest external
# events use this contract to mark upstream input seen without falling back to
# another channel. "phone_ready"/"web_only" survive only as legacy ledger
# values from the era when a phone/web desk was a delivery surface — no new
# row gets them.
ACCEPTED_DELIVERY_STATUSES = {
    "delivered", "queued", "retry_queued", "ledger_only",
    "phone_ready", "web_only",
}

# These are synchronization/ambient-signal producers, not user-facing owners
# of a decision. Their cards stay notice-class even when an LLM helpfully
# invents reply options; a real decision should be promoted by a dedicated
# source. (Historic name: they were "web-first" when a web desk existed.)
WEB_FIRST_SOURCES = {
    "cross-session-sync",
    "eigenflux-feed-triage",
}

# Monitoring exhaust is ledger-only. Alerts and decisions still outrank this
# compatibility set; new producers declare their first-principles owner_need.
AMBIENT_SOURCES = {
    "cross-session-sync",
}

ALERT_SOURCES = {
    "calendar-sync",
}
# Sources whose cards are notices BY DESIGN, whatever buttons they carry.
# A checkin's buttons are feedback (知道了/这类不必/聊聊), not an ask — but the
# 8/3 09:17 card proved inference can't be trusted here: the model imitated
# historical cards, emitted its own OPTIONS line, and the r1/r2 keys flipped
# the card to decision-class (48h escrow deadline, decision ROI lane, phone
# review surface). A companion's voice must not be able to accidentally
# promote itself into a demand for a decision.
NOTICE_SOURCES = {
    "checkin",
    "exercise-week",
}
# Sources whose natural class is a notice, whatever their buttons say — the
# single set both attention classifiers test (they were two hand-synced
# inline unions before).
NATURAL_NOTICE_SOURCES = WEB_FIRST_SOURCES | NOTICE_SOURCES
# Routine cards are notices by design, same contract as checkin: their only
# buttons are 「知道了」 and the routine_pause mute, and a propose-level
# routine's real approvals travel through conversation, never card options
# (core.routines refuses ungranted actions in prose). The pause key still
# promoted every routine card to decision class — 51 起来动动 rehab cards in
# 7 days each carrying a 48h 待批 deadline, against the standing rule that
# rehab never becomes a demand. Prefix match because routine sources are
# user-named (`routine:<name>`), not a fixed set.
ROUTINE_SOURCE_PREFIX = "routine:"
# Sources that may not author their own buttons: the preset is the contract.
# Enforced in create() — the one boundary every card passes through — because
# stripping at a particular entry path is exactly what the directive-strip
# comment below warns leaked four times. checkin's OPTIONS line (8/3) shipped
# a card WITHOUT「这类不必」, the signal its learning loop depends on.
PRESET_LOCKED_SOURCES = {
    "checkin",
}
# Prose cards whose source is inherently a decision/follow-up ask should not
# fall back to「已阅／标为重点」. Only consulted when the emitter did not author
# its own options (see _extract_inline_options).
SOURCE_DEFAULT_PRESET = {
    "checkin": "companion",
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
    # The governed class IS the natural class run through the engagement
    # governor — stated as composition so the two can never drift (they were
    # duplicated bodies before, and the 8/3 NOTICE_SOURCES fix had to be
    # applied to both by hand). _governed only ever demotes decision → notice,
    # so notice and alert pass through it unchanged.
    return _governed(str(source or ""),
                     natural_attention(source, options, extra_buttons))


def natural_attention(source: str, options: list[dict],
                      extra_buttons: list[dict]) -> str:
    """The class a card would have with no engagement governor applied.

    core.attention_roi measures against this so a demoted source's own
    demotion cannot be read back as evidence about it.
    """
    src = str(source or "")
    if src in NATURAL_NOTICE_SOURCES or src.startswith(ROUTINE_SOURCE_PREFIX):
        return ATTENTION_NOTICE
    inferred = _infer_attention(options, extra_buttons)
    if src in ALERT_SOURCES and inferred == ATTENTION_NOTICE:
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
    except Exception as exc:
        _ops_log(
            "attention_policy_unavailable",
            level="warn",
            source=source,
            error_type=type(exc).__name__,
        )
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


def review_surface(state: dict) -> str:
    """Preferred approval surface, with a truthful fallback for old rows.

    Since REQ-119 (2026-08-11) every decision reviews on Lark — the
    phone/web desk is retired (14d read rates: Lark 95.7% vs web 1.8%).
    An explicit legacy value on an old ledger row is still reported as
    written; ``REVIEW_PHONE`` survives only as that historical value.
    """
    explicit = str(state.get("review_surface", "") or "")
    if explicit in REVIEW_SURFACES:
        return explicit
    if not requires_decision(state):
        return REVIEW_NONE
    return REVIEW_LARK


def delivery_accepted(state: dict) -> bool:
    return str(state.get("delivery_status", "")) in ACCEPTED_DELIVERY_STATUSES


def should_push_to_lark(state: dict) -> bool:
    """Route first-principles owner needs to Lark; keep exhaust local."""
    if state.get("owner_need"):
        return evaluate_interruption(state)["lane"] == "lark"
    if str(state.get("attention", "") or "") == ATTENTION_ALERT:
        return True
    return requires_decision(state) or str(
        state.get("source", "")) not in AMBIENT_SOURCES


_ALERT_RE = re.compile(
    r"(?:\b(?:urgent|critical)\b|紧急|严重|告警|只剩\s*\d|"
    r"服务(?:中断|不可用)|数据丢失|安全风险)",
    re.I,
)


def _looks_like_alert(text: str) -> bool:
    return bool(_ALERT_RE.search(str(text or "")))


def _can_infer_alert_from_prose(source: str) -> bool:
    """Whether model-authored prose may promote itself into the alert lane.

    Natural notice sources are deliberately non-authoritative.  In particular,
    a cross-session digest often reports that another system has ``0 告警`` or
    describes an ``告警阈值``; keyword matching must not turn that exhaust into
    a realtime owner interruption.  Dedicated callers can still pass an
    explicit ``attention='alert'`` to ``create`` when they own the signal.
    """
    source = str(source or "")
    return (
        source not in NATURAL_NOTICE_SOURCES
        and not source.startswith(ROUTINE_SOURCE_PREFIX)
    )

# LLM-authored buttons: a heartbeat task ends its card body with a line like
#     OPTIONS: 加钱 | 限流到月底 | 让它自然停
# and those become the buttons. This is the only way buttons can genuinely
# track content — the model writing the card is the one that knows what it is
# asking. Accepts the Chinese label and full-width separators/colon so a task
# author does not have to think about ASCII.
_OPTIONS_LINE_RE = re.compile(r"^\s*(?:OPTIONS|选项)\s*[:：]\s*(.+?)\s*$", re.I)
_ANY_OPTIONS_LINE_RE = re.compile(r"^\s*(?:OPTIONS|选项)\s*[:：].*$", re.I)
_OPTIONS_SPLIT_RE = re.compile(r"\s*[|｜/／]\s*")

# LLM-authored card title, same contract shape as OPTIONS: the FIRST line of
# the body may read「TITLE: 一句话说清这件事」. Without it, cards from prose
# fell back to the per-source generic label — 48 cards headed literally
# 「Intent」in 11 days, burying e.g. the weekly 首席科学家发声 candidates
# under a header nobody opens.
_TITLE_LINE_RE = re.compile(r"^\s*(?:TITLE|标题)\s*[:：]\s*(.+?)\s*$", re.I)
_ANY_TITLE_LINE_RE = re.compile(r"^\s*(?:TITLE|标题)\s*[:：](.*)$", re.I)
# Work receipt: a card is an output of completed preparation, never a request
# that the owner do the preparation himself. TITLE remains first so concatenated
# authored cards can still be split mechanically; WORKED belongs immediately
# below it (the parser accepts any unquoted top-level line for graceful
# recovery). Quoted/fenced examples are protected by _markdown_protected_lines.
_WORKED_LINE_RE = re.compile(
    r"^\s*(?:WORKED|已完成)\s*[:：]\s*(.+?)\s*$", re.I)
_ANY_WORKED_LINE_RE = re.compile(
    r"^\s*(?:WORKED|已完成)\s*[:：].*$", re.I)
_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_MARKDOWN_LIST_FENCE_OPEN_RE = re.compile(
    r"^( {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+)(`{3,}|~{3,})(.*)$")

# 票拟 — the Grand Secretariat's proposed rescript, attached to the memorial so
# the emperor's job is 依议 or 驳, not drafting the answer himself. A decision
# card that only lists options makes the owner do the Secretariat's work: measured
# 7/29, decision cards answered inside 48h ran 90%, but the ones that stalled
# were disproportionately the ones with no stated preference behind them.
#
#     RECOMMEND: 同意 — 三个信源已复现，回滚成本一条命令
#
# label matches an option; everything after the dash is the WHY. A card may not
# recommend without a reason: an unexplained recommendation is an order.
_RECOMMEND_LINE_RE = re.compile(
    r"^\s*(?:RECOMMEND|建议)\s*[:：]\s*(.+?)\s*$", re.I)
_ANY_RECOMMEND_LINE_RE = re.compile(
    r"^\s*(?:RECOMMEND|建议)\s*[:：].*$", re.I)
_RECOMMEND_SPLIT_RE = re.compile(r"\s*(?:—+|--+|－+|·)\s*")
MAX_RECOMMEND_WHY_CHARS = 60
MAX_TITLE_CHARS = 40
MAX_WORK_RECEIPT_CHARS = 180
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
    "cross-session-sync": "跨会话动态",
    "checkin": "关怀",
    "calendar-sync": "日程变动",
    "intention-check": "定时提醒",
    "content-recommend": "推荐",
    "weekly-review": "周回顾",
    "daily-reflect": "复盘",
}

_ID_COUNTER = itertools.count(1)


# ── paths / low-level io ────────────────────────────────────────────────


def _ledger_path() -> Path:
    return memorial_ledger.ledger_path(runtime_root())


def _pending_merge_path() -> Path:
    # bot.sh's bg-job merge channel: lines matching conv_key are prepended to
    # the owner's next message and consumed (rewrite-keep-others). We only ever
    # append — same as bot.sh's own two writers.
    return memorial_ledger.pending_merge_path(runtime_root())


def _pending_context_key(conv_key: str, state: dict | None = None) -> str:
    """Capture the exact logical context that will consume a card handoff."""
    from core.conversation_context import context_snapshot
    key = str(conv_key or "").strip()
    matter_id = str((state or {}).get("matter_id") or "").strip()
    try:
        if matter_id:
            from core.matter_bridge import get_binding
            binding = get_binding(key)
            if binding and binding.get("matter_id") == matter_id:
                return context_snapshot(key, matter_id=matter_id)["context_key"]
        # A card unrelated to the currently selected Matter must not enter it.
        return context_snapshot(key, matter_id="")["context_key"]
    except Exception as exc:
        # Card taps must remain available during a transient DB outage.  The
        # legacy unbound scope is conservative: it can never enter a Matter.
        _ops_log(
            "pending_context_lookup_failed",
            level="warn",
            has_matter=bool(matter_id),
            error_type=type(exc).__name__,
        )
        from core.conversation_context import logical_context_key
        return logical_context_key(key)


def _outbox_path() -> Path:
    return memorial_ledger.outbox_path(runtime_root())


def ledger_lock(ledger: Path):
    """Exclusive cross-process lock for memorials.jsonl writers.

    O_APPEND alone keeps concurrent appends intact, but rotate_ledger's
    read→rewrite→replace must exclude appenders entirely — the size
    re-check left a stat→replace TOCTOU window that could drop a decide
    event landing on the old inode (red-team #4, 7/22). Every ledger
    writer (here, heartbeat_loop delivery events, sidecar decide events
    via this module) takes this lock; appends hold it for microseconds.
    """
    return memorial_ledger.ledger_lock(ledger)


def _append_line(path: Path, entry: dict) -> None:
    """O_APPEND one compact JSON line — atomic for small writes across the
    sidecar / CLI / heartbeat writers (same idiom as engagement_log /
    heartbeat_outbox appends). Ledger writes additionally take ledger_lock
    so a monthly rotation can never replace the file out from under them."""
    memorial_ledger.append_line(path, entry)


def _new_id() -> str:
    # epoch + pid + counter: unique across processes and within one process,
    # without the collision risk of a bare timestamp.
    return f"mem_{int(time.time())}_{os.getpid()}_{next(_ID_COUNTER)}"


def _resolve_user_id() -> str:
    """the owner's open_id: USER_ID env (bot.sh exports it) → jarvis.yaml."""
    uid = os.environ.get("USER_ID", "").strip()
    if uid:
        return uid
    try:
        from core.config import Config
        return str(Config(runtime_root() / "jarvis.yaml").lark.get("user_id", "") or "")
    except Exception as exc:
        _ops_log(
            "lark_user_resolution_failed",
            level="error",
            error_type=type(exc).__name__,
        )
        return ""


# ── ledger fold ─────────────────────────────────────────────────────────


def _fold(events: list[dict]) -> dict[str, dict]:
    """Fold the event stream into {id: current_state}."""
    return memorial_ledger.fold(
        events,
        default_attention=_default_attention,
        lapsed_status=STATUS_LAPSED,
    )


def get_memorial(
    memorial_id: str, *, root: str | Path | None = None,
) -> dict | None:
    """Current folded state for one memorial, or None."""
    return memorial_ledger.get(
        Path(root) if root is not None else runtime_root(),
        memorial_id,
        default_attention=_default_attention,
        lapsed_status=STATUS_LAPSED,
    )


def reconcile_ambient_queue(
    source: str, *, root: str | Path | None = None,
) -> dict:
    """Move legacy queued ambient cards back to their ledger-only contract.

    This migration is intentionally explicit: it only accepts a governed
    ambient source, suppresses queued (never attempting/delivered) envelopes,
    and appends both reclassification and delivery events to the memorial
    ledger.  It is safe to rerun and returns the exact affected identities.
    """
    source = str(source or "").strip()
    if source not in AMBIENT_SOURCES:
        raise ValueError(f"not an ambient source: {source or '<empty>'}")
    base = Path(root) if root is not None else runtime_root()
    from core.delivery import DeliveryPipeline

    pipeline = DeliveryPipeline(base)
    reason = "ambient_ledger_only"
    suppressed = set(pipeline.suppress_queued_source(source, reason=reason))
    # SQLite and the append-only memorial ledger cannot share one transaction.
    # Include rows from an earlier partial run so rerunning repairs a crash
    # between queue suppression and ledger reclassification.
    candidates = pipeline.list_source(
        source, state="suppressed", last_error=reason)
    memorial_ids: list[str] = []
    ledger = memorial_ledger.ledger_path(base)
    for row in candidates:
        memorial_id = str(row.get("memorial_id") or "")
        state = get_memorial(memorial_id, root=base) if memorial_id else None
        if not state or str(state.get("source") or "") != source:
            continue
        if (state.get("attention") == ATTENTION_NOTICE
                and state.get("review_surface") == REVIEW_NONE
                and state.get("delivery_status") == "ledger_only"):
            continue
        memorial_ledger.append_line(ledger, {
            "ev": "reclassify",
            "id": memorial_id,
            "attention": ATTENTION_NOTICE,
            "review_surface": REVIEW_NONE,
            "reason": "ambient_ledger_only",
            "ts": now_local_str(),
        })
        memorial_ledger.append_line(ledger, {
            "ev": "delivery", "id": memorial_id,
            "status": "ledger_only", "ts": now_local_str(),
        })
        memorial_ids.append(memorial_id)
    return {
        "source": source,
        "deliveries_suppressed": sorted(suppressed),
        "memorials_reclassified": memorial_ids,
    }


def list_memorials(pending_only: bool = False) -> list[dict]:
    """All memorials (creation order), optionally only the un-批 ones."""
    return memorial_ledger.list_all(
        runtime_root(),
        pending_only=pending_only,
        default_attention=_default_attention,
        lapsed_status=STATUS_LAPSED,
    )


# ── 缴回制度: sweep pending cards to a terminal state ─────────────────────


def lapse(memorial_id: str, reason: str = "") -> bool:
    """File a never-answered memorial as 留中. Returns True if it moved.

    Deliberately does NOT re-sync the Lark card. The bulk sweep archives
    hundreds of rows on its first run; that many card edits would be a rate
    limit incident, and the original card has long scrolled out of the chat
    anyway. The ledger is the durable record.
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


def counts_in_ledger(state: dict) -> bool:
    """The ONE predicate deciding whether a row is a ledger entry or the
    ledger's own bookkeeping. The docket card reports the backlog — counting
    it (escrow_scan sweeping it into the next docket, ledger_accounting
    reporting it as "a thing waiting for you", lapse-all archiving it while
    it is being tapped) would grow the number by one card a day forever.
    escrow_scan, ledger_accounting, escrow_docket, and the 全部留中 action
    all share this predicate so their 口径 cannot drift apart.
    """
    return str(state.get("source", "")) != ESCROW_DIGEST_SOURCE


def _linked_intents_are_terminal(state: dict) -> bool:
    """Whether every closure intent behind a card has already finished."""
    intent_ids = _intent_ids_for_state(state)
    if not intent_ids:
        return False
    try:
        from core.db import get_db

        placeholders = ",".join("?" for _ in intent_ids)
        rows = get_db().execute(
            f"SELECT id,status FROM intentions WHERE id IN ({placeholders})",
            tuple(sorted(intent_ids)),
        ).fetchall()
    except Exception as exc:
        _ops_log("docket linked-intent lookup failed", level="warn",
                 error_type=type(exc).__name__)
        return False
    terminal = {"executed", "expired", "cancelled"}
    statuses = {str(row["id"]): str(row["status"] or "") for row in rows}
    return len(statuses) == len(intent_ids) and all(
        statuses.get(intent_id) in terminal for intent_id in intent_ids
    )


def _docket_decision(state: dict) -> bool:
    """True only for an ask the owner actually received and still owes."""
    return (
        str(state.get("status") or "") == "pending"
        and str(state.get("attention") or "") == ATTENTION_DECISION
        and str(state.get("delivery_status") or "") == "delivered"
        and not _linked_intents_are_terminal(state)
    )


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
        if not counts_in_ledger(st):
            continue
        age = _age_hours(st, now)
        if age is None:
            continue
        attention = str(st.get("attention", "")) or ATTENTION_NOTICE
        if attention == ATTENTION_DECISION:
            if _linked_intents_are_terminal(st):
                out["lapse"].append((st, "关联事项已经闭环"))
                continue
            if age > ESCROW_HARD_LAPSE_H:
                out["lapse"].append((st, f"逾期未批 {age / 24:.0f} 天"))
            elif (age > ESCROW_DEADLINE_H[ATTENTION_DECISION]
                  and _docket_decision(st)):
                out["overdue"].append(st)
            continue
        deadline = ESCROW_DEADLINE_H.get(attention, ESCROW_DEADLINE_H[ATTENTION_NOTICE])
        if age > deadline:
            out["lapse"].append((st, f"未读满 {age / 24:.0f} 天"))
    return out


def ledger_accounting(window_days: int | None = None,
                      now: datetime | None = None,
                      states: list[dict] | None = None) -> dict:
    """统一闭环口径 (REQ-122): the ONE way to count where cards stand.

    8/11 数字分裂实录: a ledger query counted 106 张未闭环 while the same
    morning's escrow docket announced 「待批 14 件」 — the docket only counted
    overdue decisions, leaving notices and not-yet-due pending rows invisible.
    Both numbers were "correct", so neither was trusted. From now on every
    reporter folds ``memorials.jsonl`` into exactly three buckets:

      pending  待批 — folded status ``pending``, regardless of attention
               class or whether any deadline has passed
      decided  已办 — 批红 or an upstream resolve (both fold to ``decided``)
      lapsed   留中 — archived by the sweep without ever being answered

    Every counted row lands in exactly one bucket, so
    ``pending + decided + lapsed == created`` holds by construction; a row
    with an unknown folded status raises ``ValueError`` (a real exception,
    not an ``assert`` — ``python -O`` must not turn 口径分裂 back on).

    The docket's own cards are excluded via ``counts_in_ledger`` — the same
    predicate escrow_scan uses — so the accounting and the card that reports
    it can never disagree about what counts.

    ``window_days`` filters by creation time (None = the whole ledger).
    Rows with an unparsable ``ts`` are excluded from every bucket — the same
    contract as escrow_scan: never guess an age.
    """
    now = now or now_local()
    rows = list_memorials() if states is None else states
    out = {
        "window_days": window_days,
        "created": 0,
        "pending": 0,
        "decided": 0,
        "lapsed": 0,
        "pending_decision": 0,
        "pending_notice": 0,
        "pending_alert": 0,
    }
    for st in rows:
        if not counts_in_ledger(st):
            continue
        age = _age_hours(st, now)
        if age is None:
            continue
        if window_days is not None and age > window_days * 24:
            continue
        out["created"] += 1
        status = str(st.get("status", ""))
        if status == "pending":
            out["pending"] += 1
            attention = str(st.get("attention", "")) or ATTENTION_NOTICE
            if attention not in (ATTENTION_DECISION, ATTENTION_ALERT):
                attention = ATTENTION_NOTICE
            out[f"pending_{attention}"] += 1
        elif status == "decided":
            out["decided"] += 1
        elif status == STATUS_LAPSED:
            out["lapsed"] += 1
        else:
            raise ValueError(
                f"unknown folded memorial status {status!r} "
                f"(id={st.get('id', '?')}) — teach ledger_accounting its "
                "bucket before it silently splits the 口径")
    return out


def _wait_cn(age_h: float) -> str:
    """「等了 3 天」— human wait time, never a rounded-to-zero 「0 天」."""
    days = int(age_h // 24)
    return f"等了 {days} 天" if days >= 1 else "今天刚来"


def escrow_docket(states: list[dict],
                  now: datetime | None = None) -> tuple[str, str]:
    """Render the daily docket as ``(title, body)``.

    Two contracts, both bought with production feedback:

    Scope: this is not a ledger summary. It names only decision cards that
    reached Lark and whose linked Intent is still live. Unsent, informational,
    alert, and already-resolved rows remain internal bookkeeping.

    文风 (奏折铁律): the 8/11 docket is one of only two cardsthe owner ever
    tapped 「看不懂」 on. So: first sentence is the conclusion, the most
    urgent asks are named by title, the rest is one line, zero asks is said
    out loud (「知道就行」), and bookkeeping jargon (待批/留中/escrow/
    pending) never reaches the card face — 「等你拍板」「自动归档」 are the
    words a human would use.
    """
    now = now or now_local()
    rows = [st for st in states if counts_in_ledger(st)]
    decisions: list[tuple[float, dict]] = []
    for st in rows:
        if not _docket_decision(st):
            continue
        age = _age_hours(st, now)
        if age is None:
            continue
        decisions.append((age, st))
    decisions.sort(key=lambda r: -r[0])
    lines: list[str] = []
    if decisions:
        n = len(decisions)
        top_age, top = decisions[0]
        headline = (str(top.get("title", "")) or "一件事")[:38]
        title = f"{n} 件事等你拍板"
        lines.append(
            f"有 {n} 件事等你拍板，最急的是「{headline}」（{_wait_cn(top_age)}）。")
        for age, st in decisions[1:3]:
            lines.append(
                f"· {(str(st.get('title', '')) or '一件事')[:38]}（{_wait_cn(age)}）")
        rest = n - min(n, 3)
        if rest:
            lines.append(f"其余 {rest} 件不那么急；一直不动的会自动归档，不用专门清。")
        else:
            lines.append("一直不动的会自动归档，不用专门清。")
    else:
        title = "没有等你拍板的事"
        lines.append("没有等你拍板的事，知道就行。")
    return title, "\n".join(lines)


# ── card rendering ──────────────────────────────────────────────────────


def _header(state: dict) -> str:
    return memorial_cards.header(state, SOURCE_EMOJI)


def _button_groups(state: dict, include_options: bool = True,
                   include_chat: bool = True) -> list[list[dict]]:
    """Phone-first action rows: choices, source actions, then conversation.

    The old single row compressed up to five controls into tiny, truncated
    buttons.  Separate rows also encode the real hierarchy: 批示 is the main
    decision, opening a source is supporting context, and Chat is the escape
    hatch that must remain available after a decision.
    """
    groups = memorial_cards.button_groups(
        state,
        include_options=include_options,
        include_chat=include_chat,
        chat_button_label=CHAT_BUTTON_LABEL,
        chat_opt_key=CHAT_OPT_KEY,
        confused_opt_key=CONFUSED_OPT_KEY,
    )
    if (include_chat
            and body_was_clipped(str(state.get("body", "")))):
        # The full-text action is the mobile escape hatch, not a secondary
        # utility.  Give it a dedicated first row so Lark cannot squeeze or
        # truncate it beside Chat / confused controls on narrow screens.
        groups.insert(0, [{
            "text": FULL_TEXT_BUTTON_LABEL,
            "type": "primary",
            "value": {
                "action": "memorial",
                "id": state["id"],
                "opt": FULL_TEXT_OPT_KEY,
            },
        }])
    return groups


def _cut_at_boundary(text: str, limit: int) -> str:
    """Cut to ≤``limit`` chars on a line/space boundary, never inside a
    markdown link (a broken `[label](https://…` fragment renders as noise)."""
    return memorial_cards.cut_at_boundary(text, limit)


def _display_body(body: str) -> str:
    """Compact card copy while preserving the full ledger/chat context."""
    return memorial_cards.display_body(
        body,
        max_lines=CARD_BODY_MAX_LINES,
        max_chars=CARD_BODY_MAX_CHARS,
        clip_notice=CLIP_NOTICE,
    )


def body_was_clipped(body: str) -> bool:
    """True when _display_body had to drop part of the source text.

    Derived from the rendered output rather than restating the clip triggers,
    so it can never drift from what the card actually showed. (A body that
    itself ends with CLIP_NOTICE false-positives — benign: the full text gets
    sent anyway.)
    """
    return _display_body(body).endswith(CLIP_NOTICE)


def _render_card(state: dict, *, body: str | None = None,
                 status_line: str = "", include_options: bool = True,
                 include_chat: bool = True) -> str:
    return memorial_cards.render_card(
        state,
        body=body,
        status_line=status_line,
        include_options=include_options,
        include_chat=include_chat,
        source_emoji=SOURCE_EMOJI,
        chat_button_label=CHAT_BUTTON_LABEL,
        chat_opt_key=CHAT_OPT_KEY,
        confused_opt_key=CONFUSED_OPT_KEY,
        max_lines=CARD_BODY_MAX_LINES,
        max_chars=CARD_BODY_MAX_CHARS,
        clip_notice=CLIP_NOTICE,
        alert_attention=ATTENTION_ALERT,
        requires_decision=requires_decision,
        header_fn=_header,
        display_body_fn=_display_body,
        button_groups_fn=lambda value, show_options, show_chat: _button_groups(
            value,
            include_options=show_options,
            include_chat=show_chat,
        ),
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


def pipeline_card_json(memorial_id: str) -> str:
    """Ledger card plus its private work receipt for heartbeat adoption.

    ``card_json`` remains valid for direct Lark transport. This envelope is for
    caller-owned stdout pipelines: ``adopt_card`` removes the private field,
    verifies the remaining card byte-for-byte against the ledger rendering,
    and never forwards the field to Lark.
    """
    state = get_memorial(memorial_id)
    if state is None:
        raise KeyError(f"memorial not found: {memorial_id}")
    card = json.loads(_render_card(state))
    receipt = " ".join(str(state.get("work_receipt") or "").split())
    if receipt:
        card["__jarvis_work_receipt"] = receipt[:MAX_WORK_RECEIPT_CHARS]
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))


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
    return memorial_cards.replacement_card(
        rendered,
        state,
        chat_button_label=CHAT_BUTTON_LABEL,
        chat_opt_key=CHAT_OPT_KEY,
    )


def _decided_card(state: dict) -> dict:
    """Replacement after 批红: durable proof plus a conversation escape hatch."""
    if _decided_is_reply(state):
        # A suggested reply reads back as something the owner said, not as an
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
    stay tappable sothe owner can still 批 while (or after) chatting."""
    status = f"💬 聊天中 · {_hhmm(ts)} — 直接回消息就行"
    return _replacement_card(
        _render_card(
            state, status_line=status,
            include_options=state["status"] == "pending",
            # A clipped body keeps the button: its CLIP_NOTICE names it, and
            # if the opener send failed this is the only retry surface — a
            # rendered pointer to a missing button would be a dead end.
            include_chat=body_was_clipped(str(state.get("body", ""))),
        ),
        state,
    )


def _full_text_status_card(state: dict, status: str) -> dict:
    """Keep decisions available while full text is sent beside the card."""
    return _replacement_card(
        _render_card(
            state,
            status_line=status,
            include_options=state["status"] == "pending",
            include_chat=True,
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
    return memorial_transport.send(
        args,
        retries=retries,
        retry_delays=SEND_RETRY_DELAYS,
        runner=subprocess.run,
        sleeper=time.sleep,
    )


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
    sources andthe owner's 批红 without a new schema. Accounting must never
    break a delivery or a card callback, hence the broad except."""
    try:
        row.setdefault("ts", now_local_str("%Y-%m-%d %H:%M"))
        row.setdefault("epoch", int(time.time()))
        _append_line(runtime_root() / "engagement_log.jsonl", row)
    except Exception as e:
        _ops_log(
            "engagement_log_failed", level="warn",
            error_type=type(e).__name__,
        )


def suppressed_delivery_status(reason: str) -> str:
    """Ledger status for a delivery the pipeline suppressed.

    Budget overflow ("no room in today's nine slots") is not the same event as
    "this card is obsolete", and the ledger must not spell them the same way.
    A cap drop still owes the owner a mention, so it takes the ledger-only status
    that core.presence batches into the morning anchor's 攒批 line — the same
    surface ambient exhaust uses. Everything else (recovery_incident_obsolete,
    recovery_item_resolved, expired_ttl, ambient dedup) is stale by design and
    must NOT be resurfaced; those keep plain "suppressed".

    Measured on 2026-08-19: the wake-up backlog spent all nine budgeted slots
    between 13:03 and 13:26, and the thirteen cards created over the next ten
    waking hours were suppressed with `global_daily_cap` — with no delivery
    event written anywhere, so `presence.ledger_only()` could not see them and
    the next morning's anchor said nothing. They simply stopped existing.
    """
    from core.delivery import BUDGET_CAP_REASONS
    return "ledger_only" if str(reason) in BUDGET_CAP_REASONS else "suppressed"


def _record_delivery(memorial_id: str, status: str, source: str = "",
                     message_id: str = "") -> None:
    _append_line(_ledger_path(), {
        "ev": "delivery", "id": memorial_id, "status": status,
        "ts": now_local_str(),
    })
    # Queue-path deliveries get their "sent" rows from heartbeat_loop's flush
    # (via=memorial-card-queue); this covers DIRECT sends only, so sources
    # like a CLI-sent release card stop reading as zero-output to
    # engagement-analyze.
    if status == "delivered" and source:
        row = {"source": source, "type": "sent", "via": "memorial-direct"}
        # engagement-analyze's delivery-ack attribution only counts sends
        # carrying message_ids; without this every direct-sent card (all
        # routines included) is invisible to read-receipt joins and shows
        # up as "sent N, read 0". "sent" is _send's unparsed placeholder,
        # never a real Lark id.
        if message_id and message_id != "sent":
            row["message_ids"] = [message_id]
        _record_engagement(row)


def _quiet_hours_now() -> bool:
    """Delegate to the delivery layer's quiet-hours clock (23:30-10:00).

    Direct sends must respect the same night gate as everything else — the
    whole point of the night queue is that a 2am non-urgent ask waits for
    morning. Fail-open (send) if the import ever breaks: losing a delivery
    is worse than a rare night ping."""
    try:
        from core.heartbeat_loop import _in_quiet_hours
        return _in_quiet_hours()
    except Exception as exc:
        _ops_log(
            "quiet_hours_check_failed",
            level="error",
            error_type=type(exc).__name__,
        )
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
    queue_path = runtime_root() / MEMORIAL_QUEUE_FILE
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
# the single event loop that forwards ALL of the owner's messages. Module-level
# so tests can join() it deterministically.
_opener_thread: threading.Thread | None = None


def _finish_opener_continuation(meta: dict, delivered: bool) -> bool:
    """Publish the first safe offset after the opener attempt finishes."""
    if not meta:
        return False
    conv_key = str(meta.get("conv_key") or "")
    memorial_id = str(meta.get("memorial_id") or "")
    token = str(meta.get("token") or "")
    latest = _latest_chat_continuation([conv_key], memorial_id=memorial_id)
    if (not latest or str(latest.get("activation_token") or "") != token
            or not latest.get("awaiting_opener")):
        return False
    offset = int(meta.get("delivered_offset") or 0) if delivered else 0
    _append_line(_ledger_path(), {
        "ev": "chat_continuation", "id": memorial_id,
        "conv_key": conv_key, "offset": offset, "done": False,
        "awaiting_opener": False, "activation_token": token,
        "ts": now_local_str(), "epoch": int(time.time()),
    })
    return True


def _deliver_opener(text: str, chat_id: str,
                    continuation: dict | None = None) -> None:
    try:
        from core.delivery import (DeliveryEnvelope, TransportResult,
                                   deliver as deliver_envelope)

        def transport(envelope, channel):
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
                # bypass_dedup: every opener is user-tap-triggered, and for a
                # clipped card it IS the payload — a re-tap an hour later must
                # resend, not get eaten by the 6h dedup window while the toast
                # claims success. Double-taps are already caught upstream by
                # CHAT_RETAP_THROTTLE_S.
                metadata={"bypass_throttle": True,
                          "bypass_dedup": True,
                          "dedup_text": f"{chat_id}\0{text}"},
            ),
            root=runtime_root(),
            transport=transport,
        )
        if result.state == "delivered":
            _write_outbox(text)
            _finish_opener_continuation(continuation or {}, delivered=True)
        else:
            # A later 「继续发」must restart at zero, never skip an opener the
            # delivery authority could not confirm.
            _finish_opener_continuation(continuation or {}, delivered=False)
    except Exception as e:
        _finish_opener_continuation(continuation or {}, delivered=False)
        _ops_log(
            "opener_delivery_failed", level="error",
            error_type=type(e).__name__,
        )


def _send_opener_async(text: str, chat_id: str,
                       continuation: dict | None = None) -> None:
    global _opener_thread
    _opener_thread = threading.Thread(target=_deliver_opener,
                                      args=(text, chat_id, continuation),
                                      daemon=True)
    _opener_thread.start()


# ── option normalization ────────────────────────────────────────────────


def _extract_inline_options(text: str) -> tuple[str, list[dict] | None]:
    """Split a trailing ``OPTIONS: a | b | c`` line off LLM-authored prose.

    Returns ``(body_without_the_line, options)`` — or ``(text, None)`` when the
    card did not author its own buttons. Only a TRAILING line counts: an
    'OPTIONS:' in the middle of the copy is prose, not a button declaration.
    """
    return memorial_cards.extract_inline_options(
        text,
        protected_lines=_markdown_protected_lines,
        options_line_re=_OPTIONS_LINE_RE,
        options_split_re=_OPTIONS_SPLIT_RE,
        max_label_chars=MAX_OPTION_LABEL_CHARS,
        max_options=MAX_INLINE_OPTIONS,
    )


def _markdown_protected_lines(lines: list[str]) -> set[int]:
    """Return lines whose directive-looking text is Markdown content.

    Protect fenced code, four-space/tab-indented code, and blockquotes. A
    closing fence may contain only the fence marker and whitespace; a line
    such as `````oops`` remains code. An unclosed fence protects the rest of
    the body.
    """
    return memorial_cards.markdown_protected_lines(
        lines,
        fence_open_re=_MARKDOWN_FENCE_OPEN_RE,
        list_fence_open_re=_MARKDOWN_LIST_FENCE_OPEN_RE,
    )


def _split_authored_card_blocks(text: str) -> list[str]:
    """Split concatenated ``TITLE:`` card drafts without requiring ``---``.

    Heartbeat prompts ask for one directive block per card, but models can
    occasionally omit the separator between two otherwise-valid blocks.  In
    that shape the first card's OPTIONS line is no longer trailing, so treating
    the whole response as one card leaks authoring syntax and merges two
    decisions.  A second TITLE line is an unambiguous new-card boundary.
    """
    return memorial_cards.split_authored_card_blocks(
        text,
        protected_lines=_markdown_protected_lines,
        any_title_line_re=_ANY_TITLE_LINE_RE,
    )


def _scrub_embedded_authoring_directives(text: str) -> str:
    """Remove malformed directives at the proactive model-output boundary.

    Code fences and quoted material are content and remain byte-for-byte. This
    helper is intentionally not used by generic ``create`` callers such as
    mail or research ingestion.
    """
    return memorial_cards.scrub_embedded_authoring_directives(
        text,
        protected_lines=_markdown_protected_lines,
        any_options_line_re=_ANY_OPTIONS_LINE_RE,
        any_recommend_line_re=_ANY_RECOMMEND_LINE_RE,
        any_title_line_re=_ANY_TITLE_LINE_RE,
        any_worked_line_re=_ANY_WORKED_LINE_RE,
    )


def _extract_work_receipt(text: str) -> tuple[str, str]:
    """Remove one top-level ``WORKED:`` line and return its compact receipt.

    The directive is accepted only outside Markdown quotes/code. Multiple
    directives are rejected: one card must have one auditable statement of
    completed preparation, not a list of vague claims.
    """
    lines = str(text or "").splitlines()
    protected = _markdown_protected_lines(lines)
    matches: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if index in protected:
            continue
        match = _WORKED_LINE_RE.match(line)
        if match:
            compact = " ".join(match.group(1).split())
            if compact:
                matches.append((index, compact[:MAX_WORK_RECEIPT_CHARS]))
        elif _ANY_WORKED_LINE_RE.match(line):
            matches.append((index, ""))
    if len(matches) != 1 or not matches[0][1]:
        return text, ""
    index, receipt = matches[0]
    cleaned = "\n".join(lines[:index] + lines[index + 1:]).strip()
    return cleaned, receipt


def parse_authored_cards(text: str) -> list[dict]:
    """Parse model-authored card prose into isolated, cleaned card drafts.

    This is the single parser for direct model post-hooks, proactive prose,
    and legacy rich-card adoption. Callers with a one-card contract use the
    first result; batch-capable callers may render every result.
    """
    parsed: list[dict] = []
    for block in _split_authored_card_blocks(str(text or "")):
        title, remainder = _extract_title_line(block)
        remainder, work_receipt = _extract_work_receipt(remainder)
        remainder, recommend = _extract_recommendation(remainder)
        body, options = _extract_inline_options(remainder)
        body = _scrub_embedded_authoring_directives(body)
        parsed.append({
            "title": title,
            "body": body,
            "work_receipt": work_receipt,
            "options": options,
            "recommend": recommend,
        })
    return parsed


def _extract_recommendation(text: str) -> tuple[str, dict | None]:
    """Split a ``RECOMMEND: <label> — <why>`` line off LLM-authored prose.

    Scans the trailing few lines so it works whether the author wrote it above
    or below OPTIONS. Returns ``(body_without_the_line, {"label", "why"})``.
    A RECOMMEND with no reason is dropped, not rendered: a recommendation the
    user cannot audit is just an instruction wearing a suggestion's clothes.
    """
    return memorial_cards.extract_recommendation(
        text,
        protected_lines=_markdown_protected_lines,
        recommend_line_re=_RECOMMEND_LINE_RE,
        any_recommend_line_re=_ANY_RECOMMEND_LINE_RE,
        recommend_split_re=_RECOMMEND_SPLIT_RE,
        max_label_chars=MAX_OPTION_LABEL_CHARS,
        max_why_chars=MAX_RECOMMEND_WHY_CHARS,
    )


def _normalize_recommendation(recommend: dict | None,
                              options: list[dict]) -> dict | None:
    """Bind a 票拟 to a real option key, or drop it.

    A recommendation naming a button that does not exist would render advice
    the user cannot act on, so it is discarded rather than shown. Matching is
    by label first (that is what the author wrote) and key second.
    """
    return memorial_cards.normalize_recommendation(
        recommend,
        options,
        max_why_chars=MAX_RECOMMEND_WHY_CHARS,
    )


def _normalize_options(options: list[dict] | None, preset: str | None) -> list[dict]:
    return memorial_cards.normalize_options(
        options,
        preset,
        presets=PRESETS,
        reserved_keys={CHAT_OPT_KEY, FULL_TEXT_OPT_KEY, CONFUSED_OPT_KEY},
    )


def _normalize_extra_buttons(buttons: list[dict] | None) -> list[dict]:
    """Validate task-native buttons carried into an adopted memorial card."""
    return memorial_cards.normalize_extra_buttons(buttons)


# ── public API ──────────────────────────────────────────────────────────


def _find_recent_duplicate(source: str, title: str, body: str,
                           options: list[dict], extra_buttons: list[dict],
                           context: str, chat_id: str,
                           matter_id: str = "",
                           dedup_key: str = "",
                           work_receipt: str = "",
                           attention: str = "",
                           owner_need: str = "",
                           review_at: str = "") -> dict | None:
    """A still-pending memorial with identical content created within the
    dedup window, or the same explicit external identity."""
    now = time.time()
    for st in _fold(read_jsonl(_ledger_path())).values():
        if (dedup_key and source == "guardian-daemon"
                and st.get("source") == source
                and st.get("dedup_key", "") == dedup_key
                and st.get("epoch")
                and now - float(st["epoch"]) < 24 * 3600):
            return st
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
                and st.get("work_receipt", "") == work_receipt
                and str(st.get("attention", "")) == attention
                and str(st.get("owner_need") or infer_owner_need(
                    st.get("source", ""), st.get("attention", ""))) == owner_need
                and review_surface(st) == review_at
                and st.get("epoch") and now - st["epoch"] < DEDUP_WINDOW_S):
            return st
    return None


def _deliver_existing(
    state: dict,
    urgent: bool = False,
    recovery_reason: str = "",
) -> bool:
    """Hand an already-ledgered memorial to the unified delivery pipeline.

    Ledger-only cards (REQ-119: ambient exhaust) never get an envelope —
    the append-only ledger IS their durable surface, and the morning
    anchor's digest line is their batched shot at being seen.
    """
    from core.delivery import (DeliveryEnvelope, TransportResult,
                               deliver as deliver_envelope)

    mid = state["id"]
    if not should_push_to_lark(state):
        _record_delivery(mid, "ledger_only")
        return True

    cj = _render_card(state)
    review_at = review_surface(state)
    force_queue = not urgent and _quiet_hours_now()

    def transport(envelope, channel):
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
            requested_channel=REVIEW_LARK,
            urgent=urgent,
            conversation_bound=bool(state.get("chat_id")),
            chat_id=state.get("chat_id", ""),
            memorial_id=mid,
            matter_id=state.get("matter_id", ""),
            dedup_key=str(state.get("dedup_key") or f"memorial:{mid}"),
            throttle_key=str(state.get("throttle_key") or ""),
            metadata={
                "review_surface": review_at,
                **{key: state.get(key, "") for key in
                   MESSAGE_CONTRACT_FIELDS if key != "owner_need_explicit"},
                "dedup_text": json.dumps({
                    "title": state.get("title", ""),
                    "body": state.get("body", ""),
                    "work_receipt": state.get("work_receipt", ""),
                    "options": state.get("options", []),
                }, ensure_ascii=False, sort_keys=True),
                "force_queue": force_queue,
                # Memorial owns the review-surface-specific quiet-hours
                # decision above. Avoid a second wall-clock check in the
                # generic pipeline disagreeing with that decision.
                "bypass_quiet": not force_queue,
                # A private EigenFlux message is inbound conversation, not
                # proactive feed noise. It must not disappear behind a daily
                # interruption cap; eigenflux-feed-triage remains budgeted.
                "bypass_throttle": state.get("source") == "eigenflux",
                "retry_existing": True,
                "recovery_reason": str(recovery_reason or ""),
                # Guardian reports failures in this same pipeline. Its alert
                # must never create another dead letter about itself, and a
                # stable incident stays deduplicated for one day even after
                # the acknowledgement button closes the Item.
                **({
                    "suppress_dead_letter": True,
                    "dedup_window_seconds": 24 * 3600,
                } if state.get("source") == "guardian-daemon" else {}),
            },
        ),
        root=runtime_root(),
        transport=transport,
    )

    if result.state == "delivered":
        _record_delivery(
            mid, "delivered", source=state.get("source", "memorial"),
            message_id=str(result.message_id or ""))
        if result.message_id:
            # REQ-118 奏折专属对话: remember the delivered card's Lark
            # message_id so a reply in its thread routes to a per-card session.
            try:
                from core.memorial_thread import record_sent
                record_sent(mid, result.message_id)
            except Exception as e:
                _ops_log(
                    "thread_receipt_record_failed", level="warn",
                    memorial_id=mid, error_type=type(e).__name__,
                )
        _write_outbox(readable + f"\n\n（卡片 {mid} 已发出，等你回）")
        return True

    if result.state == "suppressed":
        _record_delivery(mid, suppressed_delivery_status(result.reason))
        return True

    if result.state == "attempting":
        _record_delivery(mid, "retry_queued")
        return True

    if force_queue and result.reason == "quiet_hours":
        _record_delivery(mid, "queued")
        _ops_log("quiet_hours_queued", memorial_id=mid)
        return True

    _record_delivery(mid, "failed")
    _record_delivery(mid, "retry_queued")
    return False


def redeliver(memorial_id: str, reason: str) -> bool:
    """Retry one pending memorial after a proven transport outage.

    Ordinary terminal failures remain terminal. A reconciler must provide an
    incident reason, and closed user decisions are never resurrected.
    """
    state = get_memorial(memorial_id)
    if not state or state.get("status") != "pending":
        return False
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("redelivery reason is required")
    _append_line(_ledger_path(), {
        "ev": "delivery_recovery",
        "id": str(memorial_id),
        "ts": now_local_str(),
        "reason": reason[:240],
    })
    return _deliver_existing(
        state,
        urgent=False,
        recovery_reason=reason,
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
            _ops_log("ledger_rotated", archived_cards=n)
    except Exception as e:
        _ops_log(
            "ledger_rotation_failed", level="error",
            error_type=type(e).__name__,
        )
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
           review_at: str = "",
           recommend: dict | None = None,
           work_receipt: str = "",
           owner_need: str = "",
           why_now: str = "",
           owner_action: str = "", silence_cost: str = "",
           require_work_receipt: bool = False,
           authoring_protocol: bool = False,
           authoring_audit_text: str | None = None) -> tuple[str, bool]:
    """Create a memorial, append it to the ledger, and route it.

    Returns ``(memorial_id, accepted)``. Accepted means the memorial is
    either accepted by the Lark delivery path or durably ledger-only
    (ambient exhaust, REQ-119). The ledger write happens before either.

    send=False skips outbound delivery (no direct send, no outbox mirror) for
    emitters that own a transport. Ledger-only rows are still marked, because
    the ledger itself is their surface and no caller transports them.

    Direct sends respect the delivery layer's gates: an identical pending
    memorial within 6h is not re-created, and an explicit ``dedup_key`` stays
    unique for as long as that memorial is pending. Non-urgent sends during
    quiet hours (23:30-10:00) go to the night queue instead of buzzing the
    phone; urgent=True bypasses the night gate.
    """
    _maybe_rotate()
    source, title, body = str(source), str(title), str(body)
    # Parse model authoring syntax only at an explicit authoring boundary.
    # Generic callers carry mail, research and network quotations, where a
    # leading TITLE or trailing OPTIONS/RECOMMEND may be legitimate content.
    parsed_recommend = None
    parsed_work_receipt = ""
    inline_options = None
    if authoring_protocol and authoring_audit_text is None:
        authored = parse_authored_cards(body)[0]
        body = str(authored["body"])
        parsed_work_receipt = str(authored.get("work_receipt") or "")
        parsed_recommend = authored["recommend"]
        inline_options = authored["options"]
        leading_title = str(authored["title"])
        if leading_title and not str(title).strip():
            title = leading_title
    work_receipt = " ".join(
        str(work_receipt or parsed_work_receipt).split()
    )[:MAX_WORK_RECEIPT_CHARS]
    if require_work_receipt and not work_receipt:
        raise ValueError("model-authored memorial requires a WORKED receipt")
    if source in PRESET_LOCKED_SOURCES:
        # The model imitating historical cards must not displace the preset
        # (8/3: an OPTIONS line cost checkin its「这类不必」button on every
        # entry path this lock now covers).
        options, preset = None, SOURCE_DEFAULT_PRESET.get(source, preset)
    elif options is None and inline_options:
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
    contract = build_message_contract(
        source=source, attention=attention, work_receipt=work_receipt,
        owner_need=owner_need, why_now=why_now, owner_action=owner_action,
        silence_cost=silence_cost,
    )
    owner_need = contract["owner_need"]
    owner_action = contract["owner_action"]
    silence_cost = contract["silence_cost"]
    review_at = REVIEW_LARK if attention == ATTENTION_DECISION else REVIEW_NONE

    if not matter_id:
        try:
            from core.matter_router import matter_id_from_context
            matter_id = matter_id_from_context(context)
        except Exception as exc:
            _ops_log(
                "matter_context_resolution_failed",
                level="warn",
                source=source,
                error_type=type(exc).__name__,
            )
            matter_id = ""

    dup = _find_recent_duplicate(
        source, title, body, opts, native_buttons, str(context), str(chat_id),
        str(matter_id), str(dedup_key), work_receipt, attention,
        owner_need, review_at)
    if dup is not None:
        _ops_log(
            "pending_duplicate_reused",
            memorial_id=dup["id"], window_hours=DEDUP_WINDOW_S // 3600,
        )
        if not send:
            if (not should_push_to_lark(dup)
                    and not delivery_accepted(dup)):
                return dup["id"], _deliver_existing(dup, urgent=urgent)
            return dup["id"], False
        if delivery_accepted(dup):
            return dup["id"], True
        return dup["id"], _deliver_existing(dup, urgent=urgent)

    mid = _new_id()
    ts = now_local_str()
    ev = {"ev": "create", "id": mid, "ts": ts, "epoch": int(time.time()),
          "source": source, "title": title, "body": body, "options": opts,
          "extra_buttons": native_buttons, "context": str(context),
          "attention": attention, "review_surface": review_at,
          **contract,
          "authoring_protocol": bool(authoring_protocol),
          "work_receipt": work_receipt}
    if authoring_audit_text is not None:
        ev["authoring_audit_text"] = str(authoring_audit_text)
    # An explicit caller outranks the parsed line, same precedence the options
    # and title directives already follow.
    final_recommend = _normalize_recommendation(recommend or parsed_recommend, opts)
    if final_recommend:
        ev["recommend"] = final_recommend
    if dedup_key:
        ev["dedup_key"] = str(dedup_key)
    if chat_id:
        ev["chat_id"] = str(chat_id)
    if matter_id:
        ev["matter_id"] = str(matter_id)
    _append_line(_ledger_path(), ev)

    if matter_id:
        # The append-only ledger is already durable. Linking is best-effort so
        # a temporary store lock can never suppress a user-facing card.
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
            _ops_log(
                "matter_link_failed", level="warn", memorial_id=mid,
                error_type=type(e).__name__,
            )

    state = _fold([ev])[mid]
    # send=False leaves Lark-routed cards to the caller's transport; a
    # ledger-only card has no other transport, so its status records now.
    if not send and should_push_to_lark(state):
        return mid, False
    return mid, _deliver_existing(state, urgent=urgent)


def _card_memorial_id(card: dict) -> str:
    return memorial_action_id(card)


def _trusted_ledger_card_memorial_id(card: dict) -> str:
    expected = lambda mid: json.loads(card_json(mid)) if get_memorial(mid) else None
    return trusted_ledger_card_id(card, _card_memorial_id, expected)


def _clean_adopted_title(header: str, source: str) -> str:
    """Remove transport chrome from a legacy card title."""
    import re
    cleaned = re.sub(r"^[\s📜📡📬🩺🎯🧠🫀🌿📅💡⏰📺📊🪞🧭📋]+", "",
                     header or "")
    cleaned = cleaned.strip(" ·|-")
    return cleaned or SOURCE_TITLE.get(source, source or "一件事")


def adopt_card(source: str, legacy_card_json: str, context: str = "",
               suppress_accepted: bool = False,
               skip_ledger_only: bool = False,
               require_work_receipt: bool = False) -> str:
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
    preserved_body = card.pop("__jarvis_full_body", "")
    if not isinstance(preserved_body, str):
        preserved_body = ""
    structured_work_receipt = " ".join(
        str(card.pop("__jarvis_work_receipt", "") or "").split()
    )[:MAX_WORK_RECEIPT_CHARS]
    # Same marker convention as __jarvis_source: an emitter that only owns
    # stdout (a task post-hook printing a card) has no other way to attach
    # structured context to the memorial it will become. core.companion uses
    # it to carry the checkin's KIND into the ledger, which is what makes
    # per-kind learning possible at all — before this every checkin was logged
    # as an undifferentiated `source=checkin`.
    context = str(card.pop("__jarvis_context", "") or context)
    if _trusted_ledger_card_memorial_id(card):
        return json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    if _card_memorial_id(card):
        _ops_log("untrusted_memorial_card", level="warn", source=source)
        # A forged callback-shaped id cannot borrow a producer-authored receipt
        # to cross the strict gate. Legitimate pipeline cards matched the exact
        # ledger rendering above after the private receipt field was removed.
        structured_work_receipt = ""
        strip_memorial_actions(card)

    # core.card keeps a bounded visible fallback for direct Lark callers, but
    # carries the uncut body inside its internal envelope.  Restore it before
    # parsing/splitting so the Memorial ledger, reader, and conversation path
    # retain the source of truth.  The private marker was popped above and can
    # never reach the rendered Lark card.
    if preserved_body:
        for element in card.get("elements", []):
            text = element.get("text") if isinstance(element, dict) else None
            if isinstance(text, dict) and text.get("content"):
                text["content"] = preserved_body
                break

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
    text_elements: list[tuple[int, str, list[str]]] = []
    for element_index, element in enumerate(card.get("elements", [])):
        element_text = str(element.get("text", {}).get("content", ""))
        if element_text:
            text_elements.append((
                element_index, element_text,
                _split_authored_card_blocks(element_text)))
    if sum(len(blocks) for _, _, blocks in text_elements) > 1:
        outputs: list[str] = []
        elements = card.get("elements", [])
        for text_offset, (element_index, _, blocks) in enumerate(text_elements):
            next_text_index = (
                text_elements[text_offset + 1][0]
                if text_offset + 1 < len(text_elements) else len(elements))
            for authored_block in blocks:
                text_element = copy.deepcopy(elements[element_index])
                text_element["text"]["content"] = authored_block
                block_elements = [text_element]
                # Actions following a one-block text element belong to that
                # element. If one text element itself contains multiple card
                # drafts, callback ownership is unknowable: fail closed and let
                # each draft's own OPTIONS become safe suggested replies.
                if len(blocks) == 1:
                    block_elements.extend(copy.deepcopy(
                        elements[element_index + 1:next_text_index]))
                else:
                    text_element.pop("actions", None)
                block_card = copy.deepcopy(card)
                block_card["elements"] = block_elements
                if structured_work_receipt:
                    block_card["__jarvis_work_receipt"] = (
                        structured_work_receipt
                    )
                adopted = adopt_card(
                    source, json.dumps(block_card, ensure_ascii=False),
                    context=context, suppress_accepted=suppress_accepted,
                    skip_ledger_only=skip_ledger_only,
                    require_work_receipt=require_work_receipt)
                if adopted:
                    outputs.append(adopted)
        return "\n".join(outputs)
    title = _clean_adopted_title(header, source)
    # A task that builds its own rich card still writes TITLE:/OPTIONS: — they
    # are model authoring directives, not content, and this path never ran the
    # extractors, so both shipped verbatim (audit P0 #268/#282/#285,
    # daily-reflect 7/22–7/27). The header of a directly-built card is a
    # decorative source label ("🌙 回顾"); an explicit TITLE line is the one
    # thing that says what THIS card is about, so it wins.
    body, adopted_work_receipt = _extract_work_receipt(body)
    adopted_work_receipt = (
        structured_work_receipt or adopted_work_receipt
    )
    if require_work_receipt and not adopted_work_receipt:
        _ops_log("work_receipt_missing", level="warn", source=source)
        return ""
    body, adopted_recommend = _extract_recommendation(body)
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
        _ops_log(
            "card_split", source=source or "heartbeat",
            split_kind="adopted_card", card_count=len(matters),
        )
    outputs: list[str] = []
    for matter in matters:
        mid, _ = create(
            source=source or "heartbeat", title=title, body=matter,
            options=options, recommend=adopted_recommend,
            work_receipt=adopted_work_receipt,
            require_work_receipt=require_work_receipt,
            authoring_protocol=True,
            preset=None if (has_native_action or inline_options)
            else fallback_preset,
            context=context, send=False, extra_buttons=native_buttons,
            attention=(ATTENTION_ALERT if _can_infer_alert_from_prose(source)
                       and _looks_like_alert(f"{title}\n{matter}")
                       and not has_native_action and not inline_options
                       and fallback_preset == "fyi"
                       else ""),
        )
        state = get_memorial(mid) or {}
        if skip_ledger_only and not should_push_to_lark(state):
            # REQ-119: ambient exhaust stays in the ledger; nothing renders.
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
    if 0 in _markdown_protected_lines(lines):
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


# Clause boundaries a Chinese/English headline may legitimately end on. Order
# matters: prefer the widest complete thought that still fits the header.
_HEADLINE_BREAKS = "。！？!?；;，,、"


def _headline_from_prose(first_line: str) -> str:
    """Cut a card headline out of an ordinary prose opening, or "" if hopeless.

    A single-paragraph card has no short first line to promote, so it used to
    fall all the way through to the per-source label — the reason cards read
    「Intent」/「heartbeat」while their own opening sentence said exactly what
    had happened. Clipping mid-word would be worse than a generic label, so
    this only fires when the text breaks cleanly inside the header budget.
    """
    text = str(first_line or "").replace("**", "").lstrip("#").strip()
    if len(text) < 8:
        return ""
    if len(text) <= MAX_TITLE_CHARS:
        return text
    window = text[:MAX_TITLE_CHARS]
    cut = max(window.rfind(ch) for ch in _HEADLINE_BREAKS)
    # Require the break to land in the back half: an early comma would title
    # the card with a fragment that says less than the module label did.
    if cut < MAX_TITLE_CHARS // 2:
        return ""
    return window[:cut].strip()


def _title_for_chunk(chunk: str, source: str) -> tuple[str, str]:
    """Derive a content title for one card; returns (title, body).

    A short first line over more content reads as this card's own headline —
    promote it to the header (and drop it from the body when it was written
    as a markdown heading, to avoid saying it twice). Anything else keeps the
    per-source generic label.
    """
    # An explicit TITLE: directive is the author's own 事由 and outranks every
    # heuristic below. Measuring the RAW line instead was a two-sided bug: the
    # literal「TITLE: 」prefix either leaked into the card header, or its 7
    # characters pushed a perfectly good headline past MAX_TITLE_CHARS and the
    # card fell back to a module label. 79 cards shipped headed literally
    # 「Intent」while their own one-line summary sat unused in the body.
    declared, remainder = _extract_title_line(chunk)
    if declared:
        return declared[:MAX_TITLE_CHARS], remainder

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
    headline = _headline_from_prose(stripped[0] if stripped else "")
    if headline:
        return headline, chunk
    return SOURCE_TITLE.get(source, source or "一件事"), chunk


def _dropped_text_shape(text: str) -> dict:
    """Describe dropped output by line shape only (no card/prose content)."""
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]
    stripped = [ln.strip() for ln in lines]
    first = stripped[0] if stripped else ""
    if first.startswith("CARD:"):
        first_kind = "envelope"
    elif first.startswith("{"):
        first_kind = "json"
    elif first.startswith(("```", "~~~")):
        first_kind = "fence"
    elif first.startswith(">"):
        first_kind = "quote"
    else:
        first_kind = "prose" if first else "empty"
    return {
        "line_count": len(lines),
        "json_lines": sum(1 for ln in stripped if ln.startswith("{")),
        "envelope_lines": sum(1 for ln in stripped if ln.startswith("CARD:")),
        "first_line_kind": first_kind,
    }


def memorialize_output(
    output: str,
    source: str = "heartbeat",
    *,
    require_work_receipt: bool = False,
) -> str:
    """Convert proactive heartbeat output to one memorial card per event.

    Existing decision cards pass through. Legacy cards are adopted while
    preserving their native actions. Ambient-source output stays ledger-only
    (REQ-119) instead of being pushed into Lark as another pending card.
    Raw internal JSON remains blocked.
    """
    source_names = [s.strip() for s in str(source).split(",") if s.strip()]
    single_source = source_names[0] if len(source_names) == 1 else "heartbeat"
    active_source = single_source
    rendered: list[str] = []
    prose: list[str] = []

    def _render_existing(existing_id: str, card: dict) -> None:
        state = get_memorial(existing_id) or {}
        if should_push_to_lark(state) and not delivery_accepted(state):
            card.pop("__jarvis_source", None)
            rendered.append(json.dumps(
                card, ensure_ascii=False, separators=(",", ":")))
        # else: ledger-only (REQ-119) or already accepted

    def _rescue_ledger_cards() -> None:
        # A ledger-backed card is provenance-verified: it is the byte-exact
        # render of its own ledger state, so it carries nothing but its own
        # callbacks and can never be a Markdown example. Whatever put it
        # among prose (a stray line ahead, an indent, a fence) must not turn
        # it into text the work-receipt gate then drops — T26 (2026-08-25/27/
        # 28): every multi-card mail-triage run vanished with exactly one
        # work_receipt_missing and zero envelopes, while single cards lived.
        kept: list[str] = []
        rescued: list[tuple[str, dict]] = []
        for raw_prose in prose:
            stripped = raw_prose.strip()
            payload = stripped[5:] if stripped.startswith("CARD:") else stripped
            card = None
            if payload.startswith("{"):
                try:
                    card = json.loads(payload)
                except (json.JSONDecodeError, TypeError, ValueError):
                    card = None
            mid = (_trusted_ledger_card_memorial_id(card)
                   if is_card_payload(card) else "")
            if mid:
                rescued.append((mid, card))
            else:
                kept.append(raw_prose)
        if not rescued:
            return
        prose[:] = kept
        _ops_log(
            "ledger_card_rescued", level="warn", source=active_source,
            card_count=len(rescued),
            prose_lines=sum(1 for k in kept if k.strip()),
        )
        for mid, card in rescued:
            _render_existing(mid, card)

    def flush_prose() -> None:
        _rescue_ledger_cards()
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
        authored_blocks = _split_authored_card_blocks(text)
        if len(authored_blocks) > 1:
            _ops_log(
                "card_split", source=active_source,
                split_kind="concatenated_directives",
                card_count=len(authored_blocks),
            )
            for authored_block in authored_blocks:
                prose.extend(authored_block.splitlines())
                flush_prose()
            return
        explicit_title, text = _extract_title_line(text)
        text, work_receipt = _extract_work_receipt(text)
        if require_work_receipt and not work_receipt:
            # Shape only, never content: enough to tell "model forgot the
            # receipt" from "cards were demoted to prose" after the fact.
            _ops_log(
                "work_receipt_missing", level="warn", source=active_source,
                **_dropped_text_shape(text),
            )
            return
        if not text and explicit_title:
            text = explicit_title
        if not text:
            return
        # Buttons follow the card: an OPTIONS line authored by the task wins;
        # otherwise fall back to what this source is usually asking for, and
        # only then to「已阅」.
        # RECOMMEND may legally follow OPTIONS. Remove it first so the
        # trailing-line OPTIONS parser still sees the authored buttons, then
        # carry the recommendation explicitly into create().
        text, authored_recommend = _extract_recommendation(text)
        body, inline_options = _extract_inline_options(text)
        body = _scrub_embedded_authoring_directives(body)
        preset = (None if inline_options
                  else SOURCE_DEFAULT_PRESET.get(active_source, "fyi"))
        # 一张卡一件事 (REQ-117): the prompt contract is the first line of
        # defense; this is the mechanical backstop for bodies that merged
        # several matters anyway. A card whose author wrote its own OPTIONS
        # line designed ONE interactive ask — never split that.
        chunks = ([body] if inline_options
                  else split_matters(body))
        if len(chunks) > 1:
            _ops_log(
                "card_split", source=active_source,
                split_kind="prose_body", card_count=len(chunks),
            )
        for chunk in chunks:
            if explicit_title and len(chunks) == 1:
                chunk_title, chunk_body = explicit_title, chunk
            else:
                chunk_title, chunk_body = _title_for_chunk(chunk, active_source)
            mid, _ = create(active_source, chunk_title, chunk_body,
                            options=inline_options, preset=preset,
                            recommend=authored_recommend,
                            work_receipt=work_receipt,
                            require_work_receipt=require_work_receipt,
                            authoring_protocol=True, send=False,
                            attention=(ATTENTION_ALERT
                                       if _can_infer_alert_from_prose(active_source)
                                       and _looks_like_alert(chunk_body)
                                       and not inline_options and preset == "fyi"
                                       else ""))
            state = get_memorial(mid) or {}
            if not should_push_to_lark(state):
                continue  # ledger-only (REQ-119)
            if delivery_accepted(state):
                continue
            rendered.append(card_json(mid))

    # One standalone legacy card, or bare ledger-backed cards one per line
    # (a post-hook printing several card_json), become CARD: envelopes. In
    # mixed prose, anything else needs the explicit CARD: envelope so Markdown
    # examples and lazy blockquote continuations cannot acquire live callbacks.
    from core.card_envelope import envelope_bare_cards
    output_lines = envelope_bare_cards(
        str(output).splitlines(), _trusted_ledger_card_memorial_id)
    protected_output_lines = _markdown_protected_lines(output_lines)
    dropping_bad_card = False
    from core.task_protocol import parse_output_source_marker
    for line_index, raw_line in enumerate(output_lines):
        line = raw_line.strip()
        protocol_line = line_index not in protected_output_lines
        segment_source = (parse_output_source_marker(line)
                          if protocol_line else "")
        if segment_source:
            flush_prose()
            active_source = segment_source
            continue
        is_card_envelope = line.startswith("CARD:")
        card_raw = line[5:] if is_card_envelope else ""
        parsed_card = None
        if is_card_envelope and card_raw:
            try:
                parsed_card = json.loads(card_raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed_card = None
        trusted_id = (_trusted_ledger_card_memorial_id(parsed_card)
                      if is_card_payload(parsed_card) else "")
        if trusted_id:
            # Provenance-verified ledger card: executes regardless of a
            # preceding prose line, a Markdown fence, or a bad envelope
            # being dropped ahead of it (see _rescue_ledger_cards).
            flush_prose()
            _render_existing(trusted_id, parsed_card)
            continue
        if dropping_bad_card:
            if protocol_line and line == "---":
                dropping_bad_card = False
            continue
        if protocol_line and line == "---":
            flush_prose()
            continue
        # CARD is an executable envelope, not inline Markdown. It can only
        # occupy its own top-level block; a preceding quote/list/prose line
        # makes it content and therefore non-executable.
        can_execute_card = (
            protocol_line and is_card_envelope
            and not any(part.strip() for part in prose)
        )
        card = parsed_card if can_execute_card else None
        if is_card_payload(card):
            flush_prose()
            existing_id = _trusted_ledger_card_memorial_id(card)
            if existing_id:
                state = get_memorial(existing_id) or {}
                if should_push_to_lark(state) and not delivery_accepted(state):
                    card.pop("__jarvis_source", None)
                    adopted = json.dumps(
                        card, ensure_ascii=False, separators=(",", ":"))
                else:
                    adopted = ""  # ledger-only (REQ-119)
            else:
                adopted = adopt_card(
                    active_source, card_raw, suppress_accepted=True,
                    skip_ledger_only=True,
                    require_work_receipt=require_work_receipt,
                )
            if adopted:
                rendered.append(adopted)
        elif is_card_envelope and protocol_line:
            # Fail closed: malformed or context-bound internal envelopes are
            # never turned into a user-visible prose card.
            dropping_bad_card = True
            continue
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
    root = runtime_root()
    ap = ActionProcessor(
        jarvis_dir=root,
        memory_dir=os.environ.get("MEMORY_DIR", str(root / "memory")),
        jobs_dir=os.environ.get("JV_JOBS_DIR", str(root / "jobs")),
        log_file=os.environ.get("JV_LOG_FILE", ""),
        owner_authenticated=owner_authenticated,
    )
    handler = getattr(ap, f"_do_{atype}", None)
    if handler is None:
        raise ValueError(f"unknown action type: {atype}")
    return handler(raw) or ""


_LARK_CARD_SYNC_RUNNER = subprocess.run


def _sync_lark_card(memorial_id: str, card: dict, *, runner=None) -> None:
    """Best-effort update of every delivered Lark copy after a state change."""
    memorial_transport.sync_card(
        memorial_id, card, root=runtime_root(), runner=runner,
        cli_runner=_LARK_CARD_SYNC_RUNNER, ops_log=_ops_log,
    )


def _complete_surface_handoffs(memorial_id: str) -> None:
    """Best-effort convergence for phone/desktop continuation affordances."""
    try:
        from core.continuity import complete_entity_handoffs
        complete_entity_handoffs("memorial", memorial_id)
    except Exception as e:
        _ops_log(
            "handoff_completion_failed", level="warn",
            memorial_id=memorial_id, error_type=type(e).__name__,
        )


def resolve(memorial_id: str, label: str,
            action_result: str = "", *, sync_lark: bool = True) -> bool:
    """Converge a memorial to an externally confirmed terminal state.

    Unlike ``decide``, this never runs a button action or injects a synthetic
    user reply. It is for state already completed in the source system.
    ``sync_lark=False`` is reserved for bulk/local residue cleanup where
    editing old remote cards would itself create noise.
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
    if sync_lark and resolved is not None:
        _sync_lark_card(memorial_id, _decided_card(resolved))
    _complete_surface_handoffs(memorial_id)
    return True


def _claim_terminal_event(
    memorial_id: str,
    entry: dict,
    allowed_statuses: set[str],
) -> tuple[bool, dict | None, dict | None]:
    """Atomically append one terminal event when the current state allows it."""
    ledger = _ledger_path()
    with ledger_lock(ledger):
        events = read_jsonl(ledger)
        before = _fold(events).get(memorial_id)
        if before is None or before.get("status") not in allowed_statuses:
            return False, before, None
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        events.append(entry)
        after = _fold(events).get(memorial_id)
    return True, before, after


def _intent_ids_for_state(state: dict) -> set[str]:
    """Intent ids owned by closure actions on one folded Memorial."""
    ids: set[str] = set()
    for option in state.get("options", []) or []:
        action = option.get("action") or {}
        if action.get("type") != "intent_close":
            continue
        params = action.get("params") or {}
        value = str(params.get("id") or action.get("id") or "").strip()
        if value:
            ids.add(value)
    for button in state.get("extra_buttons", []) or []:
        value = button.get("value") or {}
        if value.get("action") == "intent_close" and value.get("id"):
            ids.add(str(value["id"]).strip())
    return ids


def resolve_cancelled_intent_memorials(
    intent_id: str,
    *,
    root: str | Path | None = None,
    reason: str = "",
) -> list[str]:
    """Retire pending cards whose closure action targets a cancelled Intent.

    This is ledger convergence, not a user decision: it never executes a card
    action, records engagement, sends a message, or bulk-edits old Lark cards.
    A later tap on an old remote card still reaches the idempotent closed-intent
    path and updates that single card honestly.
    """
    target = str(intent_id or "").strip()
    if not target:
        return []
    base = Path(root) if root is not None else runtime_root()
    ledger = memorial_ledger.ledger_path(base)
    resolved_ids: list[str] = []
    now = now_local_str()
    with ledger_lock(ledger):
        events = read_jsonl(ledger)
        states = _fold(events)
        candidates = [
            state for state in states.values()
            if state.get("status") == "pending"
            and target in _intent_ids_for_state(state)
        ]
        if not candidates:
            return []
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger, "a", encoding="utf-8") as stream:
            for state in candidates:
                memorial_id = str(state["id"])
                stream.write(json.dumps({
                    "ev": "resolve",
                    "id": memorial_id,
                    "ts": now,
                    "label": "已停止追踪",
                    "result": str(reason or "关联意图已取消"),
                }, ensure_ascii=False) + "\n")
                resolved_ids.append(memorial_id)
    # Handoffs share the live runtime root. Avoid touching an unrelated live
    # continuity store when a test explicitly targets a different root.
    if base.resolve() == runtime_root().resolve():
        for memorial_id in resolved_ids:
            _complete_surface_handoffs(memorial_id)
    if resolved_ids:
        _ops_log(
            "cancelled_intent_memorials_resolved",
            intent_id=target,
            count=len(resolved_ids),
        )
    return resolved_ids


def resolve_thread_conversation(conv_key: str, reply_summary: str = "") -> bool:
    """Close a pending memorial after its Lark thread receives a real reply.

    The conversation key is the trust boundary: only ``memorial:<id>`` keys
    can affect the memorial ledger. Delivery callers invoke this only after
    the assistant reply is confirmed, so a provider or Lark failure leaves
    the original card available for retry.
    """
    prefix = "memorial:"
    key = str(conv_key or "").strip()
    if not key.startswith(prefix):
        return False
    memorial_id = key[len(prefix):].strip()
    if not memorial_id:
        return False
    summary = " ".join(str(reply_summary or "").split())[:200]
    entry = {
        "ev": "resolve",
        "id": memorial_id,
        "ts": now_local_str(),
        "label": "已转入对话",
        "result": summary,
    }

    # Thread replies and card decisions share this claim. Whichever reaches
    # the ledger first owns the terminal transition; the loser must not repeat
    # card syncs or execute an option action from a stale pending read.
    claimed, _before, resolved = _claim_terminal_event(
        memorial_id, entry, {"pending"})
    if not claimed:
        return False

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
            _ops_log(
                "matter_decision_link_failed", level="warn",
                memorial_id=memorial_id, error_type=type(e).__name__,
            )
    if opt.get("reply") or opt_key not in _FYI_KEYS:
        _queue_decision_context(st, opt.get("label", ""), action_result,
                                is_reply=bool(opt.get("reply")))
    # A reply tap with no bound action gets a PROACTIVE follow-up turn; a tap
    # whose real action already ran needs no second responder.
    if opt.get("reply") and not opt.get("action"):
        _queue_reply_followup(st, opt_key, opt.get("label", ""))
    # Re-sync on FAILED/no-op too: an action that outlives decide()'s 2s
    # budget resolves on this async path AFTER the ✓ toast already went out —
    # the card is then the only surface that can tell him it didn't happen.
    if action_failed or _action_result_is_noop(action_result):
        _sync_lark_card(memorial_id, _decided_card(st))


# Action-handler returns that mean "nothing actually happened". Handlers
# report no-ops as prose (they predate any structured contract), so the
# honest-toast check has to recognize the prose. Kept deliberately narrow:
# every pattern here is a string a `_do_*` handler in core/actions.py really
# returns today — widen only with the handler in hand.
_ACTION_NOOP_RE = re.compile(
    r"^FAILED|not found|already closed|already done"
    r"|没有找到|已经处理过|未发送|广播失败|找不到")


def _action_result_is_noop(action_result: str) -> bool:
    return bool(_ACTION_NOOP_RE.search(str(action_result or "")))


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

    # Atomically claim the terminal state BEFORE the action. A simultaneous
    # thread reply or second tap can win this race, in which case this worker
    # returns the authoritative terminal card without running a stale action.
    ts = now_local_str()
    claimed, claimed_from, current = _claim_terminal_event(
        memorial_id,
        {"ev": "decide", "id": memorial_id, "ts": ts,
         "opt": opt_key, "label": opt.get("label", "")},
        {"pending", STATUS_LAPSED},
    )
    if not claimed:
        if current is None:
            current = claimed_from
        if current is None:
            return {"toast": {"type": "info",
                              "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
        _complete_surface_handoffs(memorial_id)
        return {
            "toast": {
                "type": "info",
                "content": f"已批过：{current.get('decided_label', '已处理')}",
            },
            "card": {"type": "raw", "data": _decided_card(current)},
        }
    st = claimed_from or st
    try:
        from core.delivery import DeliveryPipeline
        DeliveryPipeline(runtime_root()).confirm_entity(
            memorial_id=memorial_id, state="acted")
    except Exception as e:
        _ops_log(
            "delivery_confirmation_failed", level="warn",
            memorial_id=memorial_id, error_type=type(e).__name__,
        )
    # 批红 = engagement：same "feedback" shape the legacy card buttons write,
    # so engagement-analyze sees which sources the owner actually acts on.
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
                _ops_log(
                    "decision_action_failed",
                    level="error",
                    memorial_id=memorial_id,
                    error_type=type(e).__name__,
                )
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
    elif has_action and _action_result_is_noop(action_result):
        # 2026-08-03 audit: 「Intent not found or already closed」was toasted
        # as 「已批：✓」 five separate times — the user tapped 做了, nothing
        # happened, and the system claimed success. A no-op is not an error
        # (the tap IS recorded) but ✓ on a nothing is a lie the user can only
        # discover by noticing the thing he closed asking again later.
        toast = {"type": "info",
                 "content": f"已记下，但动作没有执行：{action_result[:60]}"}
    elif opt.get("reply"):
        # Honest promise: the reply-followup task answers proactively — the
        # old「下条消息我接着这个说」meant HE had to speak first (dead end).
        toast = {"type": "success", "content": "收到——我马上接手，稍后回你"}
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


def _injection_queued(conv_key: str, job_id: str,
                      context_key: str = "") -> bool:
    """True if this memorial's context injection is already waiting in
    pending_merge (queued but not yet consumed by bot.sh)."""
    from core.conversation_context import logical_context_key
    target = str(context_key or "").strip()
    return any(
        e.get("conv_key") == conv_key
        and e.get("job_id") == job_id
        and (
            not target
            or str(e.get("context_key") or logical_context_key(conv_key)) == target
        )
        for e in read_jsonl(_pending_merge_path())
    )


def _bounded_chat_context(st: dict) -> str:
    """Build a bounded injection without truncating away state/instructions."""
    fixed = [
        "[奏折上下文] 用户点了「聊聊这个」，下一条消息讨论这件事：",
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
    # Cover at least the FULL_TEXT_MAX_CHARS of body the opener may have just
    # shown the owner; the model must never know less than he does.
    body_budget = min(FULL_TEXT_MAX_CHARS, int(budget * 0.7))
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
   the owner would have typed — it is handed over first-person so the next turn
    acts on it rather than merely filing a preference.
    """
    conv_key = st.get("chat_id", "") or _resolve_user_id()
    if not conv_key:
        return
    job_id = f"memorial-decision:{st['id']}"
    pending_context = _pending_context_key(conv_key, st)
    if _injection_queued(conv_key, job_id, pending_context):
        return
    if is_reply:
        lines = [
            f"[奏折回复] 关于「{st['title']}」，用户点了推荐回复：「{label}」。",
            "当作他刚亲口说了这句话——直接照它行动或接话，不要复述卡片、"
            "不要再问一遍他的意思。",
        ]
    else:
        lines = [
            f"[奏折批示] 用户对「{st['title']}」选择了「{label}」。",
            "把它视为已经确认的偏好或决定，不要原样再问一次。",
        ]
    if action_result:
        lines.append(f"动作结果: {action_result[:400]}")
    _append_line(_pending_merge_path(), {
        "conv_key": conv_key, "context_key": pending_context,
        "job_id": job_id,
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
        except Exception as exc:
            _ops_log(
                "matter_deep_link_failed",
                level="warn",
                matter_id=matter_id,
                error_type=type(exc).__name__,
            )
    chat_id = str(state.get("chat_id", "") or "")
    if chat_id:
        return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"
    user_id = _resolve_user_id()
    if user_id:
        return f"https://applink.feishu.cn/client/chat/open?openId={user_id}"
    return ""


EXPLAIN_QUEUE_FILE = "explain_queue.jsonl"
EXPLAIN_RETAKE_S = 600  # a claimed request older than this is retaken


def _explain_queue_path() -> Path:
    return runtime_root() / "data" / EXPLAIN_QUEUE_FILE


def confused(memorial_id: str) -> dict:
    """「看不懂」tap: record the style failure and promise a plain retelling.

    The card stays PENDING — confusion is not an answer. The heartbeat's
    explain-card task picks the request up (trigger touched so the next
    cycle comes fast) and sends a plain-language retelling as an ordinary
    message.
    """
    st = get_memorial(memorial_id)
    if st is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里问我"}}
    ts = now_local_str()
    _append_line(_ledger_path(), {"ev": "confused", "id": memorial_id,
                                  "ts": ts, "epoch": int(time.time())})
    _record_engagement({"source": st.get("source", "memorial"),
                        "type": "feedback", "rating": "confused"})
    from core.jsonl import append_jsonl_locked
    try:
        append_jsonl_locked(_explain_queue_path(), {
            "memorial_id": memorial_id, "ts": ts, "taken_at": 0})
    except OSError as exc:
        _ops_log(
            "explain_queue_write_failed", level="error",
            memorial_id=memorial_id, error_type=type(exc).__name__,
        )
    try:  # hasten the next heartbeat cycle — best-effort
        Path("/tmp/jarvis-heartbeat-trigger").touch()
    except OSError:
        pass
    st = get_memorial(memorial_id) or st
    banner = "🤔 已记下「看不懂」——大白话版本马上单独发给你，这张卡先不用管"
    card = json.loads(_render_card(st, status_line=banner))
    return {"toast": {"type": "success", "content": "收到，马上用大白话重讲一遍"},
            "card": {"type": "raw", "data": card}}


def explain_claim(now_epoch: int | None = None) -> dict | None:
    """Claim the oldest unexplained request (pre-script side).

    Claiming stamps taken_at instead of deleting: a dead model call must not
    eat the request — he tapped, an unanswered tap is a dead end. Requests
    claimed longer than EXPLAIN_RETAKE_S ago are retaken.
    """
    from core.jsonl import read_jsonl, write_jsonl

    now_e = int(time.time()) if now_epoch is None else int(now_epoch)
    rows = read_jsonl(_explain_queue_path())
    for row in rows:
        if int(row.get("taken_at") or 0) > now_e - EXPLAIN_RETAKE_S:
            continue
        row["taken_at"] = now_e
        write_jsonl(_explain_queue_path(), rows)
        return dict(row)
    return None


def explain_complete(memorial_id: str) -> None:
    """Drop a fulfilled request (post-script side)."""
    from core.jsonl import read_jsonl, write_jsonl

    rows = [r for r in read_jsonl(_explain_queue_path())
            if str(r.get("memorial_id")) != str(memorial_id)]
    write_jsonl(_explain_queue_path(), rows)


REPLY_FOLLOWUP_QUEUE_FILE = "reply_followup_queue.jsonl"
REPLY_FOLLOWUP_RETAKE_S = 600  # a claimed request older than this is retaken
REPLY_FOLLOWUP_MAX_ATTEMPTS = 3  # then drop loudly — no infinite retake loop

_TRIGGER_PATH = Path("/tmp/jarvis-heartbeat-trigger")


def _reply_followup_queue_path() -> Path:
    return runtime_root() / "data" / REPLY_FOLLOWUP_QUEUE_FILE


def _queue_reply_followup(st: dict, opt_key: str, label: str) -> None:
    """A suggested-reply tap is a spoken sentence, not a filed preference.

    Before this queue existed the tap only wrote a pending-merge injection
    that waits forthe owner's NEXT message — so a button labeled with an action
    verb (「现在授权」) sat inert until he typed something himself, which is
    the dead end he called out on 2026-08-07. Queueing here lets the
    reply-followup heartbeat task answer proactively, exactly like
    explain-card answers a 「看不懂」tap.
    """
    from core.jsonl import append_jsonl_locked
    try:
        append_jsonl_locked(_reply_followup_queue_path(), {
            "memorial_id": st["id"], "opt_key": opt_key, "label": label,
            "ts": now_local_str(), "taken_at": 0, "attempts": 0})
    except OSError as exc:
        _ops_log(
            "reply_followup_queue_write_failed", level="error",
            memorial_id=st["id"], error_type=type(exc).__name__,
        )
        return
    try:  # hasten the next heartbeat cycle — best-effort
        _TRIGGER_PATH.touch()
    except OSError:
        pass


def reply_followup_claim(now_epoch: int | None = None) -> dict | None:
    """Claim the oldest unanswered reply-tap (pre-script side).

    Claiming stamps taken_at instead of deleting: a dead model call must not
    eat the request — he tapped, an unanswered tap is a dead end. Requests
    claimed longer than REPLY_FOLLOWUP_RETAKE_S ago are retaken, at most
    REPLY_FOLLOWUP_MAX_ATTEMPTS times: an entry that keeps dying is dropped
    with a stderr trace instead of retrying forever (its answer may be
    tripping looks_like_error every round).

    Locked rewrite: the sidecar's decide() appends concurrently under the
    same flock — an unlocked read-modify-write here could clobber a tap
    that landed mid-rewrite, silently and unrecoverably.
    """
    from core.jsonl import rewrite_jsonl_locked

    now_e = int(time.time()) if now_epoch is None else int(now_epoch)
    claimed: list[dict] = []

    def _take(rows: list[dict]) -> list[dict]:
        kept = []
        for row in rows:
            if int(row.get("attempts") or 0) >= REPLY_FOLLOWUP_MAX_ATTEMPTS:
                _ops_log(
                    "reply_followup_dropped", level="error",
                    memorial_id=str(row.get("memorial_id") or ""),
                    attempts=REPLY_FOLLOWUP_MAX_ATTEMPTS,
                )
                continue
            if not claimed and int(row.get("taken_at") or 0) <= (
                    now_e - REPLY_FOLLOWUP_RETAKE_S):
                row = dict(row, taken_at=now_e,
                           attempts=int(row.get("attempts") or 0) + 1)
                claimed.append(row)
            kept.append(row)
        return kept

    rewrite_jsonl_locked(_reply_followup_queue_path(), _take)
    return dict(claimed[0]) if claimed else None


def reply_followup_complete(memorial_id: str) -> None:
    """Drop a fulfilled request (post-script side)."""
    from core.jsonl import rewrite_jsonl_locked

    rewrite_jsonl_locked(
        _reply_followup_queue_path(),
        lambda rows: [r for r in rows
                      if str(r.get("memorial_id")) != str(memorial_id)])


def settle_decision_context(memorial_id: str, handled_note: str) -> None:
    """Rewrite the still-pending decision injection after a proactive answer.

    The reply-followup task already acted on the tap, but the pending-merge
    injection still says「照它行动」— left as-is,the owner's next real message
    would make the conversational session act a SECOND time. The conversation
    must still learn the decision, so the entry is rewritten, not removed.

    Locked rewrite serializes against every appender and bot.sh's locked claim
    path, so settling a reply cannot race a context consumer or lose a row.
    """
    from core.jsonl import rewrite_jsonl_locked

    job_id = f"memorial-decision:{memorial_id}"

    def _rewrite(rows: list[dict]) -> list[dict]:
        for row in rows:
            if row.get("job_id") == job_id:
                row["summary"] = handled_note
        return rows

    rewrite_jsonl_locked(_pending_merge_path(), _rewrite)


def recent_confused(limit: int = 3) -> list[dict]:
    """The last cards he could not parse — negative examples for the style
    contract, newest first."""
    out = [st for st in list_memorials() if str(st.get("confused_ts", ""))]
    out.sort(key=lambda s: str(s.get("confused_ts", "")), reverse=True)
    return out[:limit]


def _latest_chat_continuation(conv_keys: list[str],
                              memorial_id: str = "") -> dict | None:
    keys = {str(key or "").strip() for key in conv_keys if str(key or "").strip()}
    mid = str(memorial_id or "").strip()
    if not keys and not mid:
        return None
    for event in reversed(read_jsonl(_ledger_path())):
        if event.get("ev") != "chat_continuation":
            continue
        if mid and str(event.get("id") or "") == mid:
            return event
        if not mid and str(event.get("conv_key") or "") in keys:
            return event
    return None


def continue_chat_body(
    conv_key: str,
    *,
    lookup_keys: list[str] | None = None,
    memorial_id: str = "",
    automatic: bool = False,
) -> dict:
    """Prepare the next promised chunk without advancing delivery state."""
    key = str(conv_key or "").strip()
    continuation = _latest_chat_continuation(
        [key, *(lookup_keys or [])], memorial_id=memorial_id)
    if continuation and continuation.get("awaiting_opener"):
        return {
            "handled": True,
            "awaiting_opener": True,
            "reply": "全文还在发送，稍等一下再回「继续发」。",
        }
    if not continuation or continuation.get("done"):
        return {"handled": False, "reply": ""}
    memorial_id = str(continuation.get("id") or "")
    st = get_memorial(memorial_id)
    if st is None:
        return {"handled": False, "reply": ""}
    full = str(st.get("body") or "").strip()
    start = max(0, min(int(continuation.get("offset") or 0), len(full)))
    while start < len(full) and full[start].isspace():
        start += 1
    remaining_text = full[start:]
    if not remaining_text:
        return {"handled": False, "reply": ""}

    chunk = _cut_at_boundary(remaining_text, CONTINUATION_CHUNK_CHARS)
    if not chunk:
        chunk = remaining_text[:CONTINUATION_CHUNK_CHARS]
    next_offset = start + len(chunk)
    while next_offset < len(full) and full[next_offset].isspace():
        next_offset += 1
    rest = max(len(full) - next_offset, 0)
    state_key = str(continuation.get("conv_key") or key)
    if not rest:
        tail = "（原文已发完）"
    elif automatic:
        tail = f"（正在自动发送，剩余约 {rest} 字）"
    else:
        tail = f"（原文还有约 {rest} 字，再回一句「继续发」）"
    section = "全文" if start == 0 else "续文"
    return {
        "handled": True,
        "reply": f"📜 「{st['title']}」{section}：\n\n{chunk}\n\n{tail}",
        "remaining_chars": rest,
        "memorial_id": memorial_id,
        "state_conv_key": state_key,
        "transfer_id": str(continuation.get("transfer_id") or "legacy"),
        "expected_offset": int(continuation.get("offset") or 0),
        "next_offset": next_offset,
    }


def commit_chat_continuation(conv_key: str, state_conv_key: str,
                             memorial_id: str, expected_offset: int,
                             next_offset: int, *,
                             record_context: bool = True) -> bool:
    """Advance one prepared chunk only after Lark confirms delivery."""
    continuation = _latest_chat_continuation(
        [state_conv_key], memorial_id=memorial_id)
    if not continuation or continuation.get("done"):
        return False
    if int(continuation.get("offset") or 0) != int(expected_offset):
        return False
    st = get_memorial(memorial_id)
    if st is None:
        return False
    full = str(st.get("body") or "").strip()
    start = max(0, min(int(expected_offset), len(full)))
    while start < len(full) and full[start].isspace():
        start += 1
    remaining_text = full[start:]
    chunk = _cut_at_boundary(remaining_text, CONTINUATION_CHUNK_CHARS)
    if not chunk:
        chunk = remaining_text[:CONTINUATION_CHUNK_CHARS]
    computed_next = start + len(chunk)
    while computed_next < len(full) and full[computed_next].isspace():
        computed_next += 1
    if computed_next != int(next_offset):
        return False
    done = computed_next >= len(full)
    _append_line(_ledger_path(), {
        "ev": "chat_continuation", "id": memorial_id,
        "conv_key": state_conv_key, "offset": computed_next, "done": done,
        "transfer_id": str(continuation.get("transfer_id") or "legacy"),
        "ts": now_local_str(), "epoch": int(time.time()),
    })
    # Only delivered text becomes model context.
    if record_context:
        _append_line(_pending_merge_path(), {
            "conv_key": str(conv_key or "").strip(),
            "context_key": _pending_context_key(
                str(conv_key or "").strip(), st),
            "job_id": f"memorial-continuation:{memorial_id}:{computed_next}",
            "ts": now_local_str(),
            "summary": f"[卡片续文：{st['title']}]\n{chunk}",
        })
    return True


def read_full(memorial_id: str) -> dict:
    """Stable facade for the extracted one-tap reading workflow."""
    from core.memorial_reader import read_full as _read_full
    return _read_full(memorial_id, api=sys.modules[__name__])


def chat(memorial_id: str) -> dict:
    """「聊聊这个」: inject the memorial's full context into bot.sh's
    pending-merge channel (so the owner's next message arrives with the topic
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
        _ops_log("chat_retap_throttled", memorial_id=memorial_id)
        return {"toast": {"type": "info", "content": "已在聊了——直接回消息就行"},
                "deep_link": conversation_deep_link(st),
                "card": {"type": "raw",
                         "data": _chatting_card(st, st["chat_ts"] or ts)}}

    # 1. One-shot context injection FIRST (it's the soul of the flow — the
    #    opener is only garnish): bot.sh prepends matching lines to the next
    #    message from this conv_key and consumes them (multiple queued
    #    memorials merge automatically). conv_key mirrors bot.sh: p2p =
    #    the owner's open_id, group = chat_id. Injecting before the opener send
    #    means the owner's immediate reply can't race past a slow opener.
    conv_key = st.get("chat_id", "") or _resolve_user_id()
    if conv_key:
        pending_context = _pending_context_key(conv_key, st)
        if _injection_queued(
                conv_key, f"memorial:{memorial_id}", pending_context):
            _ops_log("chat_injection_already_queued", memorial_id=memorial_id)
        else:
            _append_line(_pending_merge_path(), {
                "conv_key": conv_key,
                "context_key": pending_context,
                "job_id": f"memorial:{memorial_id}",
                "ts": ts, "summary": _bounded_chat_context(st),
            })
    else:
        _ops_log(
            "chat_injection_missing_conversation", level="warn",
            memorial_id=memorial_id,
        )

    _append_line(_ledger_path(), {"ev": "chat", "id": memorial_id, "ts": ts,
                                  "epoch": int(time.time())})
    _record_engagement({"source": st.get("source", "memorial"),
                        "type": "feedback", "rating": "chat"})

    # A card that started a conversation is the richest signal this system
    # gets, and until now it was spent entirely on a boolean. Record WHAT he
    # engaged with, against its kind, so the next prompt can be shown the
    # register that actually reaches him instead of re-deriving it from taps.
    if str(st.get("source", "")) == "checkin":
        try:
            from core import companion
            companion.record_engaged(st.get("context", ""),
                                     str(st.get("title", "")))
        except Exception as exc:
            _ops_log(
                "companion_capture_failed", level="warn",
                memorial_id=memorial_id, error_type=type(exc).__name__,
            )

    # 2. Opener sothe owner has something to reply to — off the callback thread.
    #    When the card was clipped, the opener IS the payload: he taps the
    #    button precisely because he cannot see the rest, and until now this
    #    only loaded context for the model and told him "已带上背景".
    full = str(st.get("body", "")).strip()
    extra = str(st.get("context", "")).strip()
    continuation_offset = len(full)
    continuation_done = True
    continuation_meta = None
    if body_was_clipped(full):
        body_part = full
        if len(body_part) > FULL_TEXT_MAX_CHARS:
            # Never a silent cut: the whole point of this opener is that a
            # silent cut is what he complained about. Announce the remainder
            # and offer the follow-up (the ledger keeps the full body, so
            # a reply of「继续发」can deliver the rest in-conversation).
            body_part = _cut_at_boundary(body_part, FULL_TEXT_MAX_CHARS)
            continuation_offset = len(body_part)
            continuation_done = False
            rest = len(full) - len(body_part)
            body_part += (f"\n\n（一条消息只放得下这么多——原文还有约 {rest} 字，"
                          "回一句「继续发」我把剩下的发来）")
        parts = [f"📜 「{st['title']}」全文：", "", body_part]
        if extra:
            parts += ["", "—— 背景 ——", extra[:CHAT_OPENER_CONTEXT_MAX]]
        opener = "\n".join(parts)
    else:
        opener = (f"📜 已带上「{st['title']}」的背景。"
                  "直接说你想追问什么，或告诉我你的倾向。")
    if conv_key:
        # Tombstone every older continuation immediately. For a long opener,
        # the async delivery thread publishes either the confirmed first-chunk
        # offset or zero on failure; it never assumes the opener arrived.
        activation_token = f"{time.time_ns()}:{os.getpid()}"
        _append_line(_ledger_path(), {
            "ev": "chat_continuation", "id": memorial_id,
            "conv_key": conv_key,
            "offset": 0 if not continuation_done else continuation_offset,
            "done": continuation_done,
            "awaiting_opener": not continuation_done,
            "activation_token": activation_token,
            "ts": ts, "epoch": int(time.time()),
        })
        if not continuation_done:
            continuation_meta = {
                "conv_key": conv_key, "memorial_id": memorial_id,
                "token": activation_token,
                "delivered_offset": continuation_offset,
            }
    _send_opener_async(opener, st.get("chat_id", ""), continuation_meta)

    return {"toast": {"type": "success", "content": "已加载背景——回对话窗回复我即可"},
            "deep_link": conversation_deep_link(st),
            "card": {"type": "raw", "data": _chatting_card(st, ts)}}


# ── CLI ─────────────────────────────────────────────────────────────────


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
    sp.add_argument(
        "--worked", required=True,
        help="what Jarvis completed before asking for attention",
    )
    sp.add_argument("--preset", choices=sorted(PRESETS))
    sp.add_argument("--option", action="append", default=[],
                    metavar="'标签[=动作类型:k=v,k=v]'")
    sp.add_argument("--options", default="",
                    metavar="'加钱|限流|让它停'",
                    help="推荐回复按钮（点了等于他说了这句话）")
    sp.add_argument("--context", default="")
    sp.add_argument("--chat-id", dest="chat_id", default="")
    for flag in ("owner-need", "why-now", "owner-action", "silence-cost"):
        sp.add_argument(f"--{flag}", default="")
    sp.add_argument("--urgent", action="store_true",
                    help="bypass quiet hours (only for genuinely urgent asks)")
    # No --review-at flag: decisions always review on Lark (REQ-119) — a
    # choice that silently does nothing is a dead affordance.

    lp = sub.add_parser("list", help="print folded ledger states (JSON lines)")
    lp.add_argument("--pending", action="store_true")

    ap = sub.add_parser(
        "accounting",
        help="闭环三分类 (REQ-122): 待批/已办/留中，加总恒等于创建数")
    # Default 0 = the whole ledger — the docket card counts all-time pending,
    # so the CLI's default 复算 must land on the same numbers, not a window.
    ap.add_argument("--days", type=int, default=0,
                    help="creation window in days (default 0 = whole ledger)")

    cp = sub.add_parser("continue", help="send the next promised body chunk")
    cp.add_argument("--conv-key", required=True)
    cp.add_argument("--lookup-key", action="append", default=[])
    cp.add_argument("--memorial-id", default="")

    ccp = sub.add_parser(
        "continue-commit", help="commit a continuation after delivery")
    ccp.add_argument("--conv-key", required=True)
    ccp.add_argument("--state-conv-key", required=True)
    ccp.add_argument("--memorial-id", required=True)
    ccp.add_argument("--expected-offset", required=True, type=int)
    ccp.add_argument("--next-offset", required=True, type=int)

    rtp = sub.add_parser(
        "resolve-thread",
        help="close a memorial after a confirmed thread reply")
    rtp.add_argument("--conv-key", default=os.environ.get("JV_MEM_CONV_KEY", ""))
    rtp.add_argument("--reply", default=os.environ.get("JV_MEM_REPLY", ""))

    rap = sub.add_parser(
        "reconcile-ambient",
        help="suppress queued ambient cards and restore ledger-only state")
    rap.add_argument("--source", required=True, choices=sorted(AMBIENT_SOURCES))

    args = parser.parse_args(argv)

    if args.cmd == "send":
        try:
            options = ([memorial_cards.parse_option_spec(s, i)
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
                               work_receipt=args.worked,
                               owner_need=args.owner_need,
                               why_now=args.why_now,
                               owner_action=args.owner_action,
                               silence_cost=args.silence_cost,
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

    if args.cmd == "accounting":
        if args.days < 0:
            # A typo'd window silently widening to all-time would be one more
            # number nobody can explain.
            print(f"ERROR: --days must be >= 0, got {args.days}",
                  file=sys.stderr)
            return 2
        window = args.days if args.days > 0 else None
        acct = ledger_accounting(window_days=window)
        print(json.dumps(acct, ensure_ascii=False))
        return 0

    if args.cmd == "reconcile-ambient":
        print(json.dumps(
            reconcile_ambient_queue(args.source), ensure_ascii=False))
        return 0

    if args.cmd == "continue":
        print(json.dumps(continue_chat_body(
            args.conv_key, lookup_keys=args.lookup_key,
            memorial_id=args.memorial_id), ensure_ascii=False))
        return 0

    if args.cmd == "continue-commit":
        committed = commit_chat_continuation(
            args.conv_key, args.state_conv_key, args.memorial_id,
            args.expected_offset, args.next_offset)
        print(json.dumps({"committed": committed}, ensure_ascii=False))
        return 0 if committed else 1

    if args.cmd == "resolve-thread":
        resolved = resolve_thread_conversation(args.conv_key, args.reply)
        print(json.dumps({"resolved": resolved}, ensure_ascii=False))
        return 0

    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
