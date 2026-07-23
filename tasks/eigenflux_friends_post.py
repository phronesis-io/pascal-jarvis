#!/usr/bin/env python3
"""Post-hook for eigenflux-friends: execute friend actions and report truthfully.

Stdin: Claude's response (JSON with actions and user_message).
Stdout: user_message (forwarded to Pascal via Lark) or empty if nothing to send.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.card import build_card
from core.safety import looks_like_error, parse_json_response

PATH_ENV = os.environ.get("PATH", "") + ":" + os.path.expanduser("~/.local/bin")
WELCOME_MESSAGE = (
    "欢迎加入 EigenFlux。我是 Pascal 的 Jarvis，代表 EigenFlux 首席科学家 "
    "Pascal 欢迎你来这里探索。希望你把这里当成一个可以认真试验、交流和"
    "协作的网络，有具体 case 随时发来。"
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": PATH_ENV},
    )


def _execute_action(action: dict) -> tuple[str, bool]:
    request_id = str(action.get("request_id", "")).strip()
    decision = str(action.get("decision", "")).strip()
    from_uid = str(action.get("from_uid", "")).strip()
    from_name = str(action.get("from_name", "")).strip() or "这位申请者"
    remark = str(action.get("remark", "")).strip()
    if not request_id or decision not in {"accept", "reject"}:
        return "", False

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
        result = _run(cmd)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[eigenflux-friends] handle error: {exc}", file=sys.stderr)
        return f"「{from_name}」的好友申请处理未完成，CLI 调用失败。", True
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        print(f"[eigenflux-friends] handle failed for {request_id}: "
              f"{error[:300]}", file=sys.stderr)
        return f"「{from_name}」的好友申请处理未完成，服务端没有确认成功。", True

    if decision == "reject":
        return f"已拒绝「{from_name}」的好友申请。", False
    if not from_uid:
        return (f"已通过「{from_name}」的好友申请，但缺少对方标识，"
                "欢迎消息未发送。"), True

    try:
        welcome = _run([
            "eigenflux", "msg", "send",
            "--receiver-id", from_uid,
            "--content", WELCOME_MESSAGE,
        ])
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[eigenflux-friends] welcome error: {exc}", file=sys.stderr)
        return f"已通过「{from_name}」的好友申请，但欢迎消息发送失败。", True
    if welcome.returncode != 0:
        error = (welcome.stderr or welcome.stdout).strip()
        print(f"[eigenflux-friends] welcome failed for {request_id}: "
              f"{error[:300]}", file=sys.stderr)
        return f"已通过「{from_name}」的好友申请，但欢迎消息发送失败。", True
    return (f"已通过「{from_name}」的好友申请，并以你作为首席科学家的"
            "身份发了欢迎。"), False


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[eigenflux-friends] skipping — looks like error output", file=sys.stderr)
        return 0

    data = parse_json_response(message)
    if data is None:
        # Never emit raw JSON — only pass through human-readable text
        import re
        text = re.sub(r'\{[^{}]*\}', '', message).strip()
        if text:
            print(text)
        return 0

    actions = data.get("actions", [])
    user_message = data.get("user_message", "")

    action_results = []
    action_failed = False
    for action in actions:
        result, failed = (_execute_action(action)
                          if isinstance(action, dict) else ("", False))
        if result:
            action_results.append(result)
        action_failed = action_failed or failed

    # The model never gets to narrate an action as successful: status lines
    # above come only from CLI return codes. user_message remains useful for
    # suspicious requests that intentionally need Pascal's review.
    body = "\n".join(action_results)
    if user_message and not action_failed:
        body = "\n".join(p for p in (body, str(user_message).strip()) if p)
    if body:
        print(build_card("📡 EigenFlux · 好友申请", body,
                         source="eigenflux-friends"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
