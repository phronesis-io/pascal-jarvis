"""Conservative Phase-0 delegation capture.

This is intentionally a precision-first shadow classifier.  It records only a
stable source reference and a coarse prediction; the private message body is
never copied into the control-plane database.  Shadow rows do not create
Items, execute tools, or appear in normal Delegation lists.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass

from core.delegations import DelegationStore


@dataclass(frozen=True, slots=True)
class Prediction:
    is_delegation: bool
    operation: str
    risk_tier: int
    verifier: str
    reason: str


_LIFE_ONLY = re.compile(
    r"(感觉|挺舒服|挺爽|今天|刚刚|最近|我觉得|我可以尝试|可能会|想一想)"
    r".*(运动|恢复|放松|睡觉|吃饭|肌肉|肩|大腿|情绪)"
)
_DISCUSSION = re.compile(
    r"(怎么理解|怎么看|你觉得|为什么|是什么|有没有可能|可不可以|吗[？?]?$)"
)
_EXPLICIT = (
    ("deploy", re.compile(r"(请你|帮我|现在|继续|开始).{0,20}(部署|上线)")),
    ("git_push", re.compile(r"(请你|帮我|现在|继续|开始).{0,20}(提交|推送|commit|push)")),
    (
        "document_update",
        re.compile(r"(请你|帮我|给我|开始|继续).{0,30}(写|更新|修改|整理).{0,20}(PRD|文档|报告|总结)"),
    ),
    (
        "message_send",
        re.compile(r"(请你|帮我|把|替我).{0,50}(发给|转发给|告诉|回复).+"),
    ),
    (
        "friend_relationship",
        re.compile(r"(请你|帮我|现在|把).{0,30}(通过|接受|拒绝).{0,20}(好友|申请)"),
    ),
    (
        "calendar_upsert",
        re.compile(r"(请你|帮我|给我|把).{0,30}(安排|创建|修改|取消|加到).{0,20}(日程|日历|会议|提醒)"),
    ),
    (
        "code_change",
        re.compile(r"(请你|帮我|开始|继续|全部).{0,30}(修复|实现|开发|写代码|改代码|完成).+"),
    ),
    (
        "task_track",
        re.compile(r"(请你|帮我|给我).{0,30}(跟踪|持续关注|到时提醒|记得提醒).+"),
    ),
)


def classify(text: str) -> Prediction:
    """Classify only high-confidence action requests.

    Questions and life narration are protected before action patterns run.
    This intentionally accepts lower recall during shadow collection.
    """
    normalized = re.sub(r"^\[[^\]]+\]\s*", "", str(text or "").strip())
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return Prediction(False, "discussion", 0, "", "empty")
    if _LIFE_ONLY.search(normalized):
        return Prediction(False, "life_expression", 0, "", "life_protection")
    if _DISCUSSION.search(normalized) and not re.search(r"(请你|帮我|替我)", normalized):
        return Prediction(False, "discussion", 0, "", "question")

    for operation, pattern in _EXPLICIT:
        if not pattern.search(normalized):
            continue
        if operation in {"message_send", "calendar_upsert", "document_update"}:
            risk = 2
        elif operation == "friend_relationship":
            risk = 2
        elif operation == "deploy":
            risk = 2
        else:
            risk = 1
        verifier = {
            "message_send": "lark_message",
            "friend_relationship": "eigenflux_friend",
            "calendar_upsert": "lark_calendar",
            "document_update": "lark_doc",
            "git_push": "git_remote",
            "deploy": "runtime_deploy",
            "code_change": "git_diff",
            "task_track": "task_store",
        }[operation]
        return Prediction(True, operation, risk, verifier, "explicit_action")
    return Prediction(False, "discussion", 0, "", "no_explicit_action")


def capture(
    *,
    text: str,
    source: str,
    source_ref: str,
    principal_id: str,
    matter_id: str = "",
    store: DelegationStore | None = None,
) -> tuple[Prediction, str, bool]:
    prediction = classify(text)
    store = store or DelegationStore()
    row, created = store.record_shadow_prediction(
        principal_id=principal_id,
        source=source,
        source_ref=source_ref,
        title=f"影子委托：{prediction.operation}",
        operation=prediction.operation,
        predicted_is_delegation=prediction.is_delegation,
        predicted_target_risk=prediction.risk_tier,
        predicted_verifier=prediction.verifier,
        matter_id=matter_id,
    )
    return prediction, str(row["id"]), created


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Delegation shadow capture")
    sub = parser.add_subparsers(dest="command", required=True)
    capture_cmd = sub.add_parser("capture")
    capture_cmd.add_argument("--source", required=True)
    capture_cmd.add_argument("--source-ref", required=True)
    capture_cmd.add_argument("--principal", required=True)
    capture_cmd.add_argument("--matter-id", default="")
    classify_cmd = sub.add_parser("classify")
    classify_cmd.add_argument("--text", default="")
    label = sub.add_parser("label")
    label.add_argument("--id", required=True)
    label.add_argument("--is-delegation", choices=("true", "false"), required=True)
    label.add_argument("--risk", type=int, required=True)
    label.add_argument("--verifier", default="")
    sub.add_parser("metrics")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "classify":
        text = args.text or sys.stdin.read()
        print(json.dumps(asdict(classify(text)), ensure_ascii=False))
        return 0
    store = DelegationStore()
    if args.command == "capture":
        prediction, delegation_id, created = capture(
            text=sys.stdin.read(),
            source=args.source,
            source_ref=args.source_ref,
            principal_id=args.principal,
            matter_id=args.matter_id,
            store=store,
        )
        print(
            json.dumps(
                {
                    "id": delegation_id,
                    "created": created,
                    **asdict(prediction),
                },
                ensure_ascii=False,
            )
        )
        return 0
    if args.command == "label":
        store.label_shadow(
            args.id,
            actual_is_delegation=args.is_delegation == "true",
            actual_target_risk=args.risk,
            actual_verifier=args.verifier,
        )
        print(json.dumps({"status": "ok", "id": args.id}))
        return 0
    print(json.dumps(store.shadow_metrics(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
