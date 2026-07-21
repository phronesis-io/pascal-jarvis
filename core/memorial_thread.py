"""Reverse lookup: Lark thread root message → memorial card (REQ-118).

审奏折交互：每张奏折卡要能开一个只谈这件事的专属对话。bot.sh 把 thread
root（或 parent）命中已送达奏折卡的回复路由到 conv_key
"memorial:<id>" —— SessionManager 对任意 conv_key 都会给独立 session，
所以专属对话不需要新的会话机制，只需要这里的反查。

账本沿用 memorials.jsonl 的事件溯源结构，新增 ev=="sent" 事件记录
卡片送达后的飞书 message_id（memorial.py 发送侧调用 record_sent）。
本模块自己扫描 ledger，不依赖 memorial._fold 认识新事件类型。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from core.jsonl import read_jsonl
from core.timeutil import now_local_str

CONV_KEY_PREFIX = "memorial:"


def _ledger_path() -> Path:
    # Share memorial.py's ledger location — including its monkeypatched
    # JARVIS_DIR under test. Computing our own path at import time wrote
    # test events into the PRODUCTION ledger (caught by red-team 7/21):
    # tests patch memorial.JARVIS_DIR, not this module.
    from core import memorial
    return memorial.JARVIS_DIR / "memorials.jsonl"


def record_sent(memorial_id: str, lark_message_id: str) -> None:
    """Append the delivered card's Lark message_id to the ledger.

    Called by memorial.py right after a successful card send. Unknown
    ev types are ignored by memorial._fold, so this is forward-safe."""
    # Guard the id shape: legacy/mocked senders return bool — recording
    # `True` as a message_id poisons the reverse lookup.
    if not memorial_id or not isinstance(lark_message_id, str) \
            or not lark_message_id.startswith("om_"):
        return
    from core.memorial import _append_line  # same append idiom, same file
    _append_line(_ledger_path(), {
        "id": str(memorial_id),
        "ev": "sent",
        "lark_message_id": str(lark_message_id),
        "ts": now_local_str(),
    })


def find_by_lark_mid(lark_mid: str) -> str:
    """memorial_id whose delivered card is Lark message `lark_mid`, or ""."""
    if not lark_mid:
        return ""
    for e in reversed(read_jsonl(_ledger_path())):
        if e.get("ev") == "sent" and e.get("lark_message_id") == lark_mid:
            return str(e.get("id", ""))
    return ""


def context_block(memorial_id: str) -> str:
    """Focused system-prompt block for a per-card session."""
    from core.memorial import get_memorial
    st = get_memorial(memorial_id)
    if not st:
        return ""
    lines = [
        "## 本会话 = 一张奏折的专属对话",
        f"奏折「{st.get('title', '')}」（{st.get('ts', '')}，来源 {st.get('source', '')}）：",
        st.get("body", ""),
    ]
    if st.get("status") == "decided":
        lines.append(f"（已批：{st.get('decided_label') or st.get('decided_opt')}"
                     f" @ {st.get('decided_ts')}）")
    lines.append(
        "只围绕这件事对话；无关话题请用户回主对话说。需要行动时照常使用 action 标记。")
    return "\n".join(x for x in lines if x)


def route(root_id: str, parent_id: str = "") -> tuple[str, str]:
    """(memorial_id, title) for a thread reply, or ("", "").

    Prefer the thread root (the card itself); fall back to parent for the
    first-level reply case where Lark omits root_id."""
    from core.memorial import get_memorial
    for mid_candidate in (root_id, parent_id):
        mem_id = find_by_lark_mid(mid_candidate or "")
        if mem_id:
            st = get_memorial(mem_id) or {}
            return mem_id, str(st.get("title", ""))
    return "", ""


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "route":
        mem_id, title = route(os.environ.get("JV_ROOT", ""),
                              os.environ.get("JV_PARENT", ""))
        if mem_id:
            print(f"{mem_id}\t{title}")
        return 0
    if cmd == "context":
        print(context_block(os.environ.get("JV_MEMORIAL_ID", "")
                            or (argv[1] if len(argv) > 1 else "")))
        return 0
    print("usage: python3 -m core.memorial_thread route|context", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
