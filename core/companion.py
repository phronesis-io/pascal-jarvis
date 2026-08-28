"""Checkin as a companion that learns from the interaction.

Checkin is an optional, explicitly retained rhythm. On
2026-08-02 it had been **silent for 10 days** (last card 7/23) while reporting
perfect health: `last_status: ok`, 708 runs, "last success" that same evening.

That silence was the designed behaviour of the 7/21 rewrite, which said so:

    If neither exists in the pre-script context: HEARTBEAT_OK. That is the
    EXPECTED outcome most of the time. Silence is the default, not failure.

The 7/21 rewrite was a real correction to a real complaint (the owner named
「乱联系」four times), but it conflated *contacting him for no reason* with
*contacting him without a task*, and banned the second. The result reads like
an assistant waiting for a work item, not a friend.

This module fixes the loop rather than the wording, because the wording was
never the durable problem:

**Before:**the owner complains → a human hand-edits HEARTBEAT.md → the pendulum
overshoots → 10 days of nothing → he complains again. Nothing measured whether
the correction was right.

**The instrument was too blunt to learn from.** Over all 23 checkin cards ever
created, 22 were acknowledged and 3 started a conversation. But 「已阅」 is
emitted both by "that was good" and by "noted, go away", so no amount of data
could separate them. A user whose only channel for "stop doing that" is getting
annoyed enough to say it four times does not have a feedback loop — he has an
escalation path.

Four mechanisms, all built on the ledger that already exists:

1. **A gradient.** 「聊聊这个」(already auto-added to every card) is the positive
   signal, 「知道了」the neutral one, and a new 「这类不必」the negative one. The
   negative tap names the *kind*, not the card, so one tap teaches something
   general and costs no more than dismissing it.
2. **A kind per card** (`followup` / `standing` / `notice` / `guide`). The kind
   is the unit of learning. Until now every card was logged as `source=checkin`
   and the four registers collapsed into one blob nothing could learn from.
3. **A small ceiling**, replacing the binary gate. Each kind's daily allowance
   moves with its own score. Negative evidence may reduce a kind to zero; the
   owner does not owe the system more samples after saying a register is noise.
4. **Silence is a recorded decision**, not an absence. `HEARTBEAT_OK` writes a
   row saying it declined to speak and why, so ten days of muteness is visible
   as an anomaly instead of 708 green runs.

Deliberately NOT a second attention governor. `core.attention_roi` owns which
*lane* a source occupies (decision vs notice); this owns how often one source
*speaks* and in which register. They key on different things and neither reads
the other's table.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

from core.timeutil import now_local, now_local_str
from core.retained_rhythms import is_enabled as retained_rhythm_enabled

ROOT = Path(__file__).resolve().parent.parent

# ── kinds ────────────────────────────────────────────────────────────────────

KIND_FOLLOWUP = "followup"
KIND_STANDING = "standing"
KIND_NOTICE = "notice"
KIND_GUIDE = "guide"

KINDS = (KIND_FOLLOWUP, KIND_STANDING, KIND_NOTICE, KIND_GUIDE)

KIND_HELP = {
    KIND_FOLLOWUP: "他自己留下的线头，有具体下一步（他说要做的事 / 他搁置的问题 / 他在等的结果）",
    KIND_STANDING: "他明确要求过的固定提醒（康复打卡这类）",
    KIND_NOTICE: "对他的节奏、状态、模式的一句观察——朋友的那种注意到，不带议程",
    KIND_GUIDE: "往前推一步的建议或提醒",
}

DEFAULT_KIND = KIND_NOTICE

# ── policy constants ─────────────────────────────────────────────────────────

WINDOW_DAYS = 14

# Below this a rate is noise, not evidence — same reasoning as
# core.attention_roi.MIN_SAMPLE (which is 8), deliberately smaller here:
# checkin speaks a few times a day at most, and waiting 8 samples per KIND
# would leave the governor inert for weeks.
MIN_SAMPLE = 6

# Per-kind daily allowance. A rejected register may go to zero. Re-entry comes
# from an explicit owner request or a different time-sensitive source, not from
# manufacturing another message so the system can collect engagement data.
ALLOWANCE_FLOOR = 0
ALLOWANCE_BASE = 1
ALLOWANCE_CEILING = 2

# Total cards per day across all kinds. A healthy score must not be able to
# turn checkin into the card storm this system was already burned by (7/22).
DAILY_CEILING = 2

# Score weights over the trailing window. A chat is worth far more than a tap
# because it is the only signal that the card started something.
W_CHAT = 1.0
W_ACK = 0.25
W_NEGATIVE = -1.0
W_LAPSED = -0.15

SOURCE = "checkin"

NEGATIVE_OPT = "not_this_kind"
ACK_OPTS = frozenset({"ack", "read", "watch"})


# ── state ────────────────────────────────────────────────────────────────────


def _data_dir() -> Path:
    base = Path(os.environ.get("JARVIS_DIR") or ROOT)
    return base / "data"


def voice_log_path() -> Path:
    """Append-only record of every decision to speak or stay silent."""
    return _data_dir() / "companion_voice.jsonl"


def last_spoke_path() -> Path:
    """Touched whenever a card actually ships.

    A separate file rather than a scan of the log because components.yaml
    watches it with an ordinary `file_age` check — the silence alarm reuses
    existing supervision instead of inventing a private one.
    """
    return _data_dir() / "companion_last_spoke"


def _append(row: dict) -> None:
    # flock matters here: the log is written from both the checkin post-hook
    # and the Lark card-callback thread (record_engaged).
    from core.jsonl import append_jsonl_locked
    try:
        append_jsonl_locked(voice_log_path(), row)
    except OSError as exc:  # never let bookkeeping kill a checkin
        print(f"[companion] voice log write failed: {exc}", file=sys.stderr)


def record_spoke(kind: str, topics: str = "") -> None:
    """A card shipped. Stamps both the log and the freshness file."""
    kind = normalize_kind(kind)
    _append({"ts": now_local_str("%Y-%m-%d %H:%M"), "ev": "spoke",
             "kind": kind, "topics": topics})
    try:
        path = last_spoke_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now_local_str("%Y-%m-%d %H:%M"), encoding="utf-8")
    except OSError as exc:
        print(f"[companion] last-spoke stamp failed: {exc}", file=sys.stderr)


def record_silence(reason: str) -> None:
    """Declining to speak is a decision and is recorded as one.

    The 10-day gap was invisible because HEARTBEAT_OK returned 0 and the task
    was scored a success. A run that chose silence now says so, with why.
    """
    _append({"ts": now_local_str("%Y-%m-%d %H:%M"), "ev": "silent",
             "reason": str(reason or "")[:200]})


def record_engaged(context: object, title: str) -> None:
    """A checkin card started a real conversation — capture what it was.

    Taps say *whether* something landed; a conversation is the only evidence of
    *what* did. Scoring already reads the ledger, so this exists purely so the
    prompt can be shown concrete examples of the register that reaches him,
    rather than a number he cannot act on.
    """
    kind = _kind_from_context(context)
    _append({"ts": now_local_str("%Y-%m-%d %H:%M"), "ev": "engaged",
             "kind": kind, "title": str(title or "")[:120]})


def recent_wins(limit: int = 3, rows: list[dict] | None = None) -> list[dict]:
    """The last few checkins that started a conversation, newest first."""
    rows = read_voice_log() if rows is None else rows
    wins = [r for r in rows if r.get("ev") == "engaged"]
    return list(reversed(wins[-limit:]))


def read_voice_log(limit: int = 500) -> list[dict]:
    from core.jsonl import read_jsonl
    return read_jsonl(voice_log_path())[-limit:]


def hours_since_spoke() -> float | None:
    """Hours since the last card shipped. None when it has never spoken.

    Read from the stamp file's mtime for product analytics only. A missing or
    old stamp is healthy when no qualifying message exists; age never creates
    an obligation to interrupt the owner.
    """
    import time as _time
    try:
        return (_time.time() - last_spoke_path().stat().st_mtime) / 3600.0
    except OSError:
        return None


def normalize_kind(kind: object) -> str:
    value = str(kind or "").strip().lower()
    return value if value in KINDS else DEFAULT_KIND


# ── measurement ──────────────────────────────────────────────────────────────


def _kind_from_context(context: object) -> str:
    """Read a declared kind out of a memorial's stored context blob."""
    if isinstance(context, dict):
        return normalize_kind(context.get("kind"))
    try:
        parsed = json.loads(str(context))
    except (TypeError, ValueError):
        return DEFAULT_KIND
    if isinstance(parsed, dict):
        return normalize_kind(parsed.get("kind"))
    return DEFAULT_KIND


def kind_stats(window_days: int = WINDOW_DAYS,
               states: list[dict] | None = None) -> dict[str, dict]:
    """Per-kind engagement over the trailing window.

    Unlike core.attention_roi._engaged this does not collapse to a boolean:
    the whole point of the gradient is that 「知道了」 and 「这类不必」 are
    different answers, and the old instrument could not tell them apart.
    """
    from core.memorial_contracts import STATUS_LAPSED

    if states is None:
        from core import memorial
        states = memorial.list_memorials()

    cutoff = (now_local() - timedelta(days=window_days)).strftime("%Y-%m-%d %H:%M")
    stats: dict[str, dict] = {
        k: {"n": 0, "chat": 0, "ack": 0, "negative": 0, "lapsed": 0} for k in KINDS
    }
    for state in states:
        if str(state.get("source", "")) != SOURCE:
            continue
        if str(state.get("ts", "")) < cutoff:
            continue
        row = stats[_kind_from_context(state.get("context", ""))]
        row["n"] += 1
        opt = str(state.get("decided_opt", ""))
        if str(state.get("chat_ts", "")):
            row["chat"] += 1
        elif opt == NEGATIVE_OPT:
            row["negative"] += 1
        elif opt in ACK_OPTS:
            row["ack"] += 1
        elif str(state.get("status", "")) == STATUS_LAPSED:
            row["lapsed"] += 1

    for row in stats.values():
        n = row["n"]
        row["score"] = (
            (row["chat"] * W_CHAT + row["ack"] * W_ACK
             + row["negative"] * W_NEGATIVE + row["lapsed"] * W_LAPSED) / n
        ) if n else 0.0
    return stats


def allowances(stats: dict[str, dict] | None = None) -> dict[str, int]:
    """Daily card allowance per kind, derived from its own trailing score.

    A kind under MIN_SAMPLE keeps the base allowance: it has not earned a
    verdict, and starving it would prevent it from ever earning one.
    """
    stats = kind_stats() if stats is None else stats
    out: dict[str, int] = {}
    for kind in KINDS:
        row = stats.get(kind) or {"n": 0, "score": 0.0}
        if row["n"] < MIN_SAMPLE:
            out[kind] = ALLOWANCE_BASE
            continue
        score = row["score"]
        if score >= 0.60:
            out[kind] = ALLOWANCE_CEILING
        elif score >= 0.30:
            out[kind] = ALLOWANCE_BASE + 1
        elif score >= 0.05:
            out[kind] = ALLOWANCE_BASE
        else:
            out[kind] = ALLOWANCE_FLOOR
    return out


def spoken_today(rows: list[dict] | None = None) -> dict[str, int]:
    """Cards already shipped today, per kind."""
    rows = read_voice_log() if rows is None else rows
    today = now_local_str("%Y-%m-%d")
    out = {k: 0 for k in KINDS}
    for row in rows:
        if row.get("ev") != "spoke":
            continue
        if not str(row.get("ts", "")).startswith(today):
            continue
        out[normalize_kind(row.get("kind"))] += 1
    return out


def plan(stats: dict[str, dict] | None = None,
         rows: list[dict] | None = None,
         silent_hours: float | None = None) -> dict:
    """What an explicitly retained checkin rhythm is allowed right now.

    Silence never creates debt. Only an entrusted result, an explicit rhythm,
    or a verified time-sensitive change can justify Jarvis speaking first.
    """
    stats = kind_stats() if stats is None else stats
    allow = allowances(stats)
    used = spoken_today(rows)
    remaining = {k: max(0, allow[k] - used.get(k, 0)) for k in KINDS}
    total_used = sum(used.values())
    day_left = max(0, DAILY_CEILING - total_used)
    if day_left == 0:
        remaining = {k: 0 for k in KINDS}

    hours = hours_since_spoke() if silent_hours is None else silent_hours
    return {
        "stats": stats,
        "allowance": allow,
        "used_today": used,
        "remaining": remaining,
        "day_remaining": day_left,
        "hours_since_spoke": hours,
        "owed": "",
    }


# ── prompt surface ───────────────────────────────────────────────────────────


def brief(state: dict | None = None) -> str:
    """The block injected into the checkin prompt by the pre-script.

    Deterministic code decides the budget; the model only chooses what to say
    within it. Leaving the cadence to the prompt is what produced both the
    card storm and the ten-day silence.
    """
    state = plan() if state is None else state
    lines = ["=== COMPANION BUDGET (代码算的，不要自己改) ==="]
    hours = state["hours_since_spoke"]
    if hours is None:
        lines.append("上次开口：从没有过。")
    else:
        lines.append(f"上次开口：{hours:.1f} 小时前。")
    lines.append(f"今天还能发：{state['day_remaining']} 张（全部 kind 合计）")
    lines.append("")
    lines.append("每个 kind 今天的余额和它自己挣来的分数：")
    for kind in KINDS:
        row = state["stats"].get(kind) or {}
        n = row.get("n", 0)
        rem = state["remaining"][kind]
        if n < MIN_SAMPLE:
            verdict = f"样本 {n} 张，还不够评判"
        else:
            verdict = (f"{n} 张：聊过 {row.get('chat', 0)}，"
                       f"知道了 {row.get('ack', 0)}，"
                       f"这类不必 {row.get('negative', 0)}，"
                       f"分数 {row.get('score', 0):.2f}")
        lines.append(f"  {kind:9} 余 {rem} 张 — {verdict}")
    wins = recent_wins()
    if wins:
        lines.append("")
        lines.append("最近真的聊起来的（这些 register 是够到他的，照着这个方向找）：")
        for win in wins:
            lines.append(f"  [{win.get('kind', '?')}] {win.get('title', '')}")

    lines.append("")
    lines.append("发卡时必须带一行 KIND: followup|standing|notice|guide —")
    for kind in KINDS:
        lines.append(f"  {kind:9} {KIND_HELP[kind]}")
    lines.append(
        "按卡片的真实性质选，预算按它扣，「这类不必」教的也是它——"
        "别为绕开用完的预算把 notice 标成 followup，那会污染用户唯一的信号。"
        "这一行送出前会被剥掉。")

    lines.append("")
    if state["day_remaining"] == 0:
        lines.append("今天的额度用完了。这一轮回 HEARTBEAT_OK。")
    else:
        lines.append(
            "没有消息债。有被托付的结果、明确订阅的节奏，或会过期的真实变化才说；"
            "否则回 HEARTBEAT_OK。不要为了维持存在感而开口。")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="core.companion", description="陪伴式 checkin 的节奏与学习")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("brief", help="给 checkin prompt 用的预算块")
    sub.add_parser("preflight", help="仍有卡片额度时返回成功，不输出内容")
    sub.add_parser("status", help="人看的状态")
    args = p.parse_args(argv)

    if args.cmd == "brief":
        print(brief())
        return 0

    if args.cmd == "preflight":
        return 0 if (retained_rhythm_enabled("checkin")
                     and plan()["day_remaining"] > 0) else 1

    state = plan()
    hours = state["hours_since_spoke"]
    print("=== 陪伴 checkin 状态 ===")
    print(f"上次开口：{'从没有过' if hours is None else f'{hours:.1f} 小时前'}")
    print(f"今天已发 {sum(state['used_today'].values())} 张，"
          f"还能发 {state['day_remaining']} 张")
    print()
    for kind in KINDS:
        row = state["stats"].get(kind) or {}
        print(f"  {kind:9} 额度 {state['allowance'][kind]}  "
              f"今天已发 {state['used_today'].get(kind, 0)}  "
              f"余 {state['remaining'][kind]}  "
              f"样本 {row.get('n', 0)}  分数 {row.get('score', 0):.2f}")
        print(f"            {KIND_HELP[kind]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
