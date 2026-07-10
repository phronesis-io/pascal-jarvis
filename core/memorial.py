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

Sending clones core.heartbeat_loop._lark_send_card (the production-proven
lark-cli path, including the timeout-means-delivered tradeoff). Successful
sends are mirrored into heartbeat_outbox.jsonl so the main session knows the
card went out (direct sends bypass the heartbeat delivery loop's outbox).

CLI (any emitter can send a memorial in one line):
    python3 -m core.memorial send --source mail --title "..." --body "..." \
        --preset fyi
    python3 -m core.memorial send --source x --title t --body b \
        --option '准=intent_close:id=xxx,outcome=done' --option '缓'
    python3 -m core.memorial list [--pending]
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
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

# Same retry profile as core.heartbeat_loop (REQ-11). A local lark-cli
# timeout is treated as delivered — see _send() for the duplicate-vs-loss
# tradeoff inherited from heartbeat_loop._lark_send_card.
SEND_RETRY_DELAYS = (2, 5)

# Common 批红 combos so emitters don't hand-roll options. All record-only
# (action=None): the tap writes the ledger, which heartbeat tasks and the
# main session read back.
PRESETS: dict[str, list[dict]] = {
    "decision": [
        {"key": "approve", "label": "准（去做）", "action": None},
        {"key": "defer", "label": "缓（改天提我）", "action": None},
        {"key": "reject", "label": "驳（不做了）", "action": None},
    ],
    "fyi": [
        {"key": "read", "label": "已阅", "action": None},
        {"key": "watch", "label": "重要，持续盯", "action": None},
    ],
    "followup": [
        {"key": "done", "label": "做了", "action": None},
        {"key": "later", "label": "没做，改天", "action": None},
        {"key": "stop", "label": "别再问了", "action": None},
    ],
}

# Header reads 「📜 {source emoji} {title}」; unknown sources just get 📜.
SOURCE_EMOJI = {
    "mail": "📬",
    "eigenflux": "📡",
    "selfmon": "🩺",
    "intent": "🎯",
    "intentions": "🎯",
    "memory": "🧠",
    "heartbeat": "🫀",
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
                "source": str(e.get("source", "")),
                "title": str(e.get("title", "")),
                "body": str(e.get("body", "")),
                "options": e.get("options") or [],
                "context": str(e.get("context", "")),
                "chat_id": str(e.get("chat_id", "")),
                "status": "pending",
                "decided_opt": "",
                "decided_label": "",
                "decided_ts": "",
                "action_result": "",
                "chat_ts": "",
            }
        elif ev == "decide":
            st = states.get(mid)
            if st is not None and st["status"] == "pending":
                st["status"] = "decided"
                st["decided_opt"] = str(e.get("opt", ""))
                st["decided_label"] = str(e.get("label", ""))
                st["decided_ts"] = str(e.get("ts", ""))
                st["action_result"] = str(e.get("action_result", ""))
        elif ev == "chat":
            st = states.get(mid)
            if st is not None:
                st["chat_ts"] = str(e.get("ts", ""))
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


def _buttons(state: dict, include_chat: bool = True) -> list[dict]:
    btns = [
        {"text": o.get("label", o.get("key", "")),
         "value": {"action": "memorial", "id": state["id"], "opt": o.get("key", "")}}
        for o in state["options"]
    ]
    if include_chat:
        btns.append({"text": CHAT_BUTTON_LABEL,
                     "value": {"action": "memorial", "id": state["id"],
                               "opt": CHAT_OPT_KEY}})
    return btns


def card_json(memorial_id: str) -> str:
    """Pending-state card JSON for a memorial (single line).

    Public so emitters with their own delivery channel (e.g. a heartbeat
    post-script falling back to the CARD: pipe when the direct send failed)
    can print the exact same card — the sidecar doesn't care who sent it.
    """
    st = get_memorial(memorial_id)
    if st is None:
        raise KeyError(f"memorial not found: {memorial_id}")
    return build_card(_header(st), st["body"], buttons=_buttons(st))


def _hhmm(ts: str) -> str:
    # ledger ts is "YYYY-MM-DD HH:MM" — show just the clock on the card.
    return ts[-5:] if len(ts) >= 5 else ts


def _decided_card(state: dict) -> dict:
    """Replacement card after 批红: original title+body, status line, no buttons."""
    body = state["body"] + f"\n\n✅ 已批：{state['decided_label']} · {_hhmm(state['decided_ts'])}"
    if state.get("action_result"):
        body += f"\n{state['action_result']}"
    return json.loads(build_card(_header(state), body))


def _chatting_card(state: dict, ts: str) -> dict:
    """Replacement card after「聊聊这个」: chatting banner, remaining options
    stay tappable so Pascal can still 批 while (or after) chatting."""
    body = state["body"] + f"\n\n💬 聊天中 · {_hhmm(ts)} — 直接回消息就行"
    buttons = _buttons(state, include_chat=False) if state["status"] == "pending" else None
    return json.loads(build_card(_header(state), body, buttons=buttons))


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
            # Local timeout ≠ undelivered — on a slow link the message is
            # usually already out. Assume delivered rather than re-send a
            # duplicate (same tradeoff as heartbeat_loop._lark_send_card).
            print("memorial send timed out — assuming delivered", file=sys.stderr)
            return True
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


# ── option normalization ────────────────────────────────────────────────


def _normalize_options(options: list[dict] | None, preset: str | None) -> list[dict]:
    if options:
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
            normalized.append({"key": key, "label": label,
                               "action": o.get("action") or None})
        return normalized
    name = preset or "fyi"  # a memorial with no options makes no sense — fyi is the safe floor
    if name not in PRESETS:
        raise ValueError(f"unknown preset: {name} (have: {', '.join(sorted(PRESETS))})")
    return [dict(o) for o in PRESETS[name]]


# ── public API ──────────────────────────────────────────────────────────


def create(source: str, title: str, body: str, options: list[dict] | None = None,
           preset: str | None = None, context: str = "",
           chat_id: str = "") -> tuple[str, bool]:
    """Create a memorial, append it to the ledger, and send the card.

    Returns (memorial_id, sent_ok). The ledger write happens BEFORE the send,
    so a failed send still leaves a queryable record (list --pending) and the
    caller can re-deliver via card_json().
    """
    opts = _normalize_options(options, preset)
    mid = _new_id()
    ts = now_local_str()
    ev = {"ev": "create", "id": mid, "ts": ts, "source": str(source),
          "title": str(title), "body": str(body), "options": opts,
          "context": str(context)}
    if chat_id:
        ev["chat_id"] = str(chat_id)
    _append_line(_ledger_path(), ev)

    state = _fold([ev])[mid]
    cj = build_card(_header(state), state["body"], buttons=_buttons(state))
    sent = _send_card(cj, chat_id)
    if sent:
        readable = extract_card_text(cj) or f"📜 {title}"
        _write_outbox(readable + f"\n\n（奏折 {mid} 已发出，等批示）")
    return mid, sent


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

    action_result, action_failed = "", False
    if opt.get("action"):
        try:
            action_result = _execute_action(opt["action"])
        except Exception as e:
            action_result = f"FAILED: {e}"
            action_failed = True

    ts = now_local_str()
    _append_line(_ledger_path(), {"ev": "decide", "id": memorial_id, "ts": ts,
                                  "opt": opt_key, "label": opt.get("label", ""),
                                  "action_result": action_result})
    st.update(status="decided", decided_opt=opt_key,
              decided_label=opt.get("label", ""), decided_ts=ts,
              action_result=action_result)
    if action_failed:
        toast = {"type": "info", "content": "已批，但动作执行出错了——直接在对话里告诉我"}
    else:
        toast = {"type": "success", "content": f"已批：{opt.get('label', '')} ✓"}
    return {"toast": toast, "card": {"type": "raw", "data": _decided_card(st)}}


def _status_line(st: dict) -> str:
    if st["status"] == "decided":
        return f"已批：{st['decided_label']}（{st['decided_ts']}）"
    labels = "／".join(o.get("label", "") for o in st["options"])
    return f"待批（选项：{labels}）"


def chat(memorial_id: str) -> dict:
    """「聊聊这个」: send a conversation opener + inject the memorial's full
    context into bot.sh's pending-merge channel so Pascal's next message
    arrives with the topic loaded. Returns the card-callback payload."""
    st = get_memorial(memorial_id)
    if st is None:
        return {"toast": {"type": "info",
                          "content": "这张卡对应的事项找不到了，直接在对话里告诉我"}}
    ts = now_local_str()

    # 1. Opener so Pascal has something to reply to.
    snippet = st["body"][:200]
    opener = f"📜 聊聊：{st['title']}\n{snippet}\n——你想怎么处理这件事？"
    opener_sent = _send_text(opener, st.get("chat_id", ""))
    if opener_sent:
        _write_outbox(opener)

    # 2. One-shot context injection: bot.sh prepends matching lines to the
    #    next message from this conv_key and consumes them (multiple queued
    #    memorials merge automatically). conv_key mirrors bot.sh: p2p =
    #    Pascal's open_id, group = chat_id.
    conv_key = st.get("chat_id", "") or _resolve_user_id()
    if conv_key:
        parts = [
            "[奏折上下文] Pascal 在奏折卡上点了「💬 聊聊这个」，他接下来要聊的就是这件事：",
            f"来源: {st['source']}",
            f"标题: {st['title']}",
            f"正文: {st['body']}",
        ]
        if st.get("context"):
            parts.append(f"背景: {st['context']}")
        parts.append(f"当前状态: {_status_line(st)}")
        parts.append("直接接住这个话题，别重复念卡片内容。")
        _append_line(_pending_merge_path(), {
            "conv_key": conv_key, "job_id": f"memorial:{memorial_id}",
            "ts": ts, "summary": "\n".join(parts)[:1500],
        })
    else:
        print(f"memorial chat: no conv_key for {memorial_id} — context not injected",
              file=sys.stderr)

    _append_line(_ledger_path(), {"ev": "chat", "id": memorial_id, "ts": ts})

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
    sp.add_argument("--context", default="")
    sp.add_argument("--chat-id", dest="chat_id", default="")

    lp = sub.add_parser("list", help="print folded ledger states (JSON lines)")
    lp.add_argument("--pending", action="store_true")

    args = parser.parse_args(argv)

    if args.cmd == "send":
        try:
            options = ([_parse_option_spec(s, i)
                        for i, s in enumerate(args.option, 1)] or None)
            mid, sent = create(args.source, args.title, args.body,
                               options=options, preset=args.preset,
                               context=args.context, chat_id=args.chat_id)
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
