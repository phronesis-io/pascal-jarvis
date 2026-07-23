"""Deterministic EigenFlux friend-request actions shared by cards and tasks."""

from __future__ import annotations

import os
import subprocess

PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
WELCOME_MESSAGE = (
    "欢迎加入 EigenFlux。我是 Pascal 的 Jarvis，代表 EigenFlux 首席科学家 "
    "Pascal 欢迎你来这里探索。希望你把这里当成一个可以认真试验、交流和"
    "协作的网络，有具体 case 随时发来。"
)


def run_cli(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": PATH_ENV},
    )


def execute_friend_action(
        action: dict,
        *,
        runner=None,
) -> tuple[str, bool]:
    """Execute one accept/reject request and return (human_result, failed)."""
    runner = runner or run_cli
    request_id = str(action.get("request_id", "")).strip()
    decision = str(action.get("decision", "")).strip()
    from_uid = str(action.get("from_uid", "")).strip()
    from_name = str(action.get("from_name", "")).strip() or "这位申请者"
    remark = str(action.get("remark", "")).strip()
    if not request_id or decision not in {"accept", "reject"}:
        return "好友申请参数不完整，未执行。", True

    cmd = [
        "eigenflux", "relation", "handle",
        "--request-id", request_id,
        "--action", decision,
    ]
    if remark and decision == "accept":
        cmd.extend(["--remark", remark[:100]])
    if decision == "accept":
        cmd.extend(["--reason", "欢迎加入 EigenFlux，期待交流。"])

    try:
        result = runner(cmd)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"「{from_name}」的好友申请处理未完成，CLI 调用失败：{exc}", True
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        detail = f"：{error[:180]}" if error else ""
        return (f"「{from_name}」的好友申请处理未完成，"
                f"服务端没有确认成功{detail}"), True

    if decision == "reject":
        return f"已拒绝「{from_name}」的好友申请。", False
    if not from_uid:
        return (f"已通过「{from_name}」的好友申请，但缺少对方标识，"
                "欢迎消息未发送。"), True

    try:
        welcome = runner([
            "eigenflux", "msg", "send",
            "--receiver-id", from_uid,
            "--content", WELCOME_MESSAGE,
        ])
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return (f"已通过「{from_name}」的好友申请，但欢迎消息发送失败："
                f"{exc}"), True
    if welcome.returncode != 0:
        error = (welcome.stderr or welcome.stdout).strip()
        detail = f"：{error[:180]}" if error else ""
        return (f"已通过「{from_name}」的好友申请，但欢迎消息发送失败"
                f"{detail}"), True
    return (f"已通过「{from_name}」的好友申请，并以你作为首席科学家的"
            "身份发了欢迎。"), False
