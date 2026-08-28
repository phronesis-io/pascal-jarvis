"""Versioned product contract for when Codex or Jarvis should be used."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


OPERATING_MODEL_VERSION = "1.2.0"

_OPERATING_MODEL: dict[str, Any] = {
    "schema": "jarvis.operating-model.v1",
    "version": OPERATING_MODEL_VERSION,
    "summary": (
        "主动想做事时用 Codex；需要跨时间连续性、正确时机、异步返回、"
        "权威收口或少量主动保留的陪伴时，才需要 Jarvis。"
    ),
    "first_principle": (
        "Jarvis 主动消息的预期价值必须大于它造成的上下文切换成本。"
    ),
    "proactive_message_goals": [
        "保护会过期的时间、机会或安全",
        "返回已明确托付的异步结果",
        "把已经核验的重要外部变化带回当前目标",
        "把可代劳工作做完后交付一个最小判断或授权",
        "履行本人明确保留且可随时停用的少数陪伴节奏",
    ],
    "engagement_is_not_a_goal": True,
    "retained_rhythm_policy": {
        "configuration": "private jarvis.yaml retained_rhythms",
        "default": "disabled",
        "maximum_enabled": 2,
        "silence_creates_debt": False,
    },
    "default_entry": {
        "surface": "codex",
        "reason": (
            "提问、研究、写作、代码、文件和长分析是用户发起的前台工作；"
            "新目标开新任务。"
        ),
    },
    "jarvis_is_needed_when": [
        {
            "id": "durable_continuity",
            "meaning": "同一目标要跨任务、设备、产品、仓库、执行者或日期。",
            "jarvis_role": "保留 Matter、有效决定、证据和下一步，给新任务最小上下文。",
        },
        {
            "id": "time_trigger",
            "meaning": "期限、日程、安全或机会只在正确时机出现才有价值。",
            "jarvis_role": "在错过会有代价时主动唤醒，而不是等你先想起来。",
        },
        {
            "id": "material_external_change",
            "meaning": "外部的人或权威状态发生了与当前目标相关的重要变化。",
            "jarvis_role": "先核验、聚合和去重，再带变化与影响出现。",
        },
        {
            "id": "entrusted_async_result",
            "meaning": "你明确托付的后台工作已完成、失败或只剩本人判断。",
            "jarvis_role": "带可验证结果返回，让你不用守着 Agent 等。",
        },
        {
            "id": "authority_and_closure",
            "meaning": "外部发布、费用、权限、承诺、不可逆动作或最终收口需要本人。",
            "jarvis_role": "先完成可逆工作，只把一个真实判断或授权交给你。",
        },
        {
            "id": "retained_companion_rhythm",
            "meaning": "你明确选择保留的一两个生活或工作节奏到点。",
            "jarvis_role": "带本轮证据出现；可忽略、可停用，不把生活做成打卡分数。",
        },
    ],
    "direct_lark_is_exception_for": [
        "Codex 没开时快速记下一件以后要继续的事",
        "回复 Jarvis 刚发来的期限、判断或授权",
        "当前动作原生属于飞书消息、日历、文档、联系人、群聊或审批",
        "Codex 暂时不可用时的降级沟通",
    ],
    "jarvis_must_not_interrupt_for": [
        "维持存在感或证明系统在运行",
        "成功的自愈、重试、模型切换、队列清理或健康快照",
        "Jarvis 仍能自己完成的调查、去重、修复或验证",
        "以后打开 Codex 再问也不会损失的普通信息",
        "重复状态、传输活动、Agent 活动或模型自评",
    ],
    "message_gate": [
        "为什么必须现在看？",
        "Jarvis 已经先完成了什么？",
        "为什么剩下的必须由本人做？",
        "是否只要求一个最小动作？",
        "若不发，是否真的会损失时间、机会、连续性或受托结果？",
    ],
    "message_contract_fields": [
        "owner_need",
        "work_receipt",
        "why_now",
        "owner_action",
        "silence_cost",
    ],
    "success_is": [
        "继续旧事时不用重讲",
        "Jarvis 先完成能代劳的工作",
        "一次回答收掉重复卡片、提醒和待办状态",
        "外部动作有权威读回和可审计收据",
        "有用闭环增加，同时主动消息和本人操作成本下降",
    ],
    "quiet_is_healthy": True,
}


def operating_model() -> dict[str, Any]:
    """Return an immutable-by-caller copy of the owner-facing contract."""
    return deepcopy(_OPERATING_MODEL)
