"""Shared text utilities for Jarvis core modules."""

import re as _re

# ── internal id → plain-Chinese display name ────────────────────────────
#
# Every user-facing surface that mentions a heartbeat task or delivery source
# must speak Chinese a non-engineer understands (feedback-card-style-contract,
# 2026-08-24: guardian/selfmon cards read「reply-followup ... STARVED」and
# 「- selfmon / c…」to Pascal). One shared map so watermarks, brain_health and
# the guardian daemon can never drift apart. Deliberately import-free: this
# module sits at the bottom of the core import graph and must stay there.
#
# Multi-user rule: names describe the mechanism generically — no personal
# data, no owner-specific wording. Unmapped ids fall back to the raw id so a
# new task is never hidden behind a wrong label.
TASK_DISPLAY_NAMES: dict[str, str] = {
    # EigenFlux
    "eigenflux-inbox-reconcile": "EigenFlux 收件核对",
    "eigenflux-feed-triage": "EigenFlux 动态筛选",
    "eigenflux-publish": "EigenFlux 广播",
    "eigenflux-profile": "EigenFlux 名片更新",
    "eigenflux-friends": "EigenFlux 好友往来",
    "eigenflux-preinstall": "EigenFlux 预装检查",
    # 日常节律
    "checkin": "日常关怀",
    "morning-anchor": "晨间锚点",
    "daily-plan": "每日计划",
    "daily-reflect": "每日复盘",
    "activity-log": "活动记录",
    "weekly-review": "每周回顾",
    "exercise-week": "每周运动回顾",
    # 信息与内容
    "mail-triage": "邮件整理",
    "content-recommend": "内容推荐",
    "calendar-sync": "日历同步",
    "perception-collect": "信息采集",
    "metrics-digest": "指标摘要",
    "phronesis-monitor": "团队群关注",
    "cross-session-sync": "跨会话同步",
    "engagement-analyze": "互动分析",
    # 提醒与跟进
    "intention-check": "定时提醒",
    "intentions": "定时提醒",
    "closure": "事后跟进",
    "reply-followup": "按钮跟进",
    "explain-card": "卡片解释",
    "memorial-escrow": "卡片到期处理",
    "routine-run": "例程执行",
    # 记忆
    "memory-hourly": "记忆整理（每小时）",
    "memory-daily": "记忆整理（每天）",
    "memory-weekly": "记忆整理（每周）",
    "memory-consolidate": "记忆深度整理",
    "memory-tidy": "记忆收纳",
    "thinking-review": "思考回顾",
    # 运维
    "self-diagnostic": "系统自检",
    "self-improve-cycle": "自我改进",
    "delegation-reconcile": "委托任务核对",
    "iteration-observe": "迭代观察",
    "log-maintenance": "日志维护",
    "provider-canary": "模型通道体检",
    "repos-sync": "代码仓同步",
    "personal-site": "个人网站更新",
    # 非任务的投递来源 id（守护进程的失败清单等会列到卡片上；
    # 名单对照过生产投递表里的真实 source 分布，2026-08-24）
    "guardian-daemon": "系统守护",
    "selfmon": "自诊断",
    "attention-roi": "打扰级别调节",
    "bot-reply": "对话回复",
    "deploy-smoke": "部署后自检",
    "host-absence": "缺席回执",
    "heartbeat": "后台任务",
    "heartbeat-loop": "心跳调度",
    "mail": "邮件",
    "calendar": "日历",
    "eigenflux": "EigenFlux",
    "eigenflux-stream": "EigenFlux 实时消息",
    "eigenflux-message": "EigenFlux 消息",
    "eigenflux-messages": "EigenFlux 消息",
    "eigenflux-feed": "EigenFlux 动态",
    "eigenflux-friend": "EigenFlux 好友",
    "eigenflux-research": "EigenFlux 深度研究",
    "memorial-chat": "卡片对话",
    "memorial-full-text": "卡片全文",
    "watchlater-remind": "稍后看提醒",
    "task-triage": "任务分拣",
    "free-time-nudge": "空闲提醒",
    "taskline": "工程任务",
    "manual": "手动发送",
    "mobile-onboarding": "手机首次使用",
    "pgc-improvement": "PGC 改进",
    "pgc_pulse": "PGC 指标日报",
    "release-canary": "发布前体检",
    "test": "测试消息",
}

_ROUTINE_PREFIX = "routine:"


def task_display_name(task_id: str) -> str:
    """Plain-Chinese display name for an internal task/source id.

    Unmapped ids come back unchanged (never truncated): a raw id is ugly but
    honest, while a cut-off one (「c…」) is undecodable.
    """
    key = str(task_id or "").strip()
    if "," in key:
        parts = [part.strip() for part in key.split(",") if part.strip()]
        return "、".join(task_display_name(part) for part in parts)
    if key.startswith(_ROUTINE_PREFIX):
        name = key[len(_ROUTINE_PREFIX):].strip()
        return f"例程「{name}」" if name else "例程"
    return TASK_DISPLAY_NAMES.get(key, key)


def ellipsize(value: str, max_chars: int) -> str:
    """Display-width bounded text with an explicit omission marker."""
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    if max_chars == 1:
        return "…"
    return _prefix_chars(text, max_chars - 1, word_boundary=True) + "…"


def middle_ellipsize(value: str, max_chars: int) -> str:
    """Keep both the subject and distinguishing suffix of a compact title."""
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 2:
        return ellipsize(text, max_chars)
    available = max_chars - 1
    head_width = max(1, (available * 2) // 3)
    tail_width = max(1, available - head_width)
    head = _prefix_chars(text, head_width, word_boundary=True)
    tail = _suffix_chars(text, tail_width, word_boundary=True)
    return head.rstrip() + "…" + tail.lstrip()


def _prefix_chars(text: str, limit: int, *, word_boundary: bool) -> str:
    cut = text[:limit]
    if (word_boundary and cut and limit < len(text)
            and cut[-1].isascii() and cut[-1].isalnum()
            and text[limit].isascii() and text[limit].isalnum()):
        bounded = cut.rsplit(" ", 1)[0].rstrip(" -_/:")
        if bounded:
            cut = bounded
    return cut.rstrip()


def _suffix_chars(text: str, limit: int, *, word_boundary: bool) -> str:
    start = max(0, len(text) - limit)
    cut = text[start:]
    if (word_boundary and start > 0 and cut
            and text[start - 1].isascii() and text[start - 1].isalnum()
            and cut[0].isascii() and cut[0].isalnum()):
        parts = cut.split(" ", 1)
        if len(parts) == 2 and parts[1].strip():
            cut = parts[1].lstrip(" -_/:")
    return cut.lstrip()


# Mechanism words the closure machinery historically stacked onto intent
# names —「闭环: X 后闭环」/「闭环再问: …」reached the owner verbatim as card
# titles (2026-08-24 audit; the style contract says a title carries the
# matter itself, not the mechanism). Lives here, not in core.intentions,
# because tasks/intentions_post.py needs it for legacy rows too.
_CLOSURE_NAME_PREFIX_RE = _re.compile(
    r"^(?:闭环再问|闭环跟进|闭环|再问|跟进)\s*[:：]\s*")
# 「后闭环」only strips as the calendar template's own space-separated tail
# (it always emitted「{title} 后闭环」): a compound name that merely ENDS in
# those characters (「示例餐厅饭后闭环」) is the matter itself and must survive.
_CLOSURE_NAME_SUFFIX_RE = _re.compile(r"(?:\s+后闭环|\s*（事后跟进）)\s*$")


def closure_matter(name: str) -> str:
    """The matter itself: an intent name with closure mechanism words removed.

    Strips repeatedly so stacked decorations (「闭环: X 后闭环」) all go.
    Falls back to the original name when stripping would leave nothing.
    """
    original = str(name or "").strip()
    text = original
    while True:
        stripped = _CLOSURE_NAME_SUFFIX_RE.sub(
            "", _CLOSURE_NAME_PREFIX_RE.sub("", text))
        if stripped == text:
            break
        text = stripped
    return text or original


def extract_text(content) -> str:
    """Flatten a Claude Code message.content into a single plain-text string.

    Handles the three shapes produced by Claude Code sessions:
      - str (plain assistant text)
      - list of blocks (text / tool_use / tool_result) — only text blocks extracted
      - anything else → empty string
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts).strip()
    return ""
