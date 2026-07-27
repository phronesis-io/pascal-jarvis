#!/usr/bin/env python3
"""Post-hook for eigenflux-friends: execute actions or create actionable reviews.

Stdin: model JSON with ``actions`` and ``reviews``.
Stdout: verified auto-action outcomes only. Review cards are created directly
so their buttons carry the server request_id and execute the real operation.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import memorial
from core.card import build_card
from core.eigenflux_friends import (
    PATH_ENV,
    WELCOME_MESSAGE,
    execute_friend_action,
    temporary_friend_policy_active,
)
from core.safety import looks_like_error, parse_json_response


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=30,
        env={**os.environ, "PATH": PATH_ENV},
    )


def _execute_action(action: dict) -> tuple[str, bool]:
    result, failed = execute_friend_action(action, runner=_run)
    if failed:
        print(f"[eigenflux-friends] {result}", file=sys.stderr)
    return result, failed


def _pending_requests() -> dict[str, dict] | None:
    """Re-read server truth so model-copied identifiers are never authoritative."""
    try:
        result = _run([
            "eigenflux", "relation", "list",
            "--direction", "incoming",
            "--limit", "100",
            "-f", "json",
        ])
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"[eigenflux-friends] pending query failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        error = (result.stderr or result.stdout).strip()
        print(f"[eigenflux-friends] pending query failed: {error[:300]}",
              file=sys.stderr)
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        print("[eigenflux-friends] pending query returned invalid JSON",
              file=sys.stderr)
        return None
    requests = payload.get("requests", []) if isinstance(payload, dict) else []
    return {
        str(item.get("request_id", "")).strip(): item
        for item in requests
        if isinstance(item, dict) and str(item.get("request_id", "")).strip()
    }


def _canonical_request(model_item: dict, pending: dict[str, dict]) -> dict | None:
    """Join a model decision to the current server row by request_id."""
    request_id = str(model_item.get("request_id", "")).strip()
    server_item = pending.get(request_id)
    if server_item is None:
        return None
    canonical = dict(model_item)
    canonical.update({
        "request_id": request_id,
        "from_uid": server_item.get("from_uid", ""),
        "from_name": server_item.get("from_name", ""),
        "greeting": server_item.get("greeting", ""),
    })
    return canonical


def _safe_param(value: object) -> str:
    """Keep memorial's pipe-delimited action bridge unambiguous."""
    return str(value or "").replace("|", " ").replace("\n", " ").strip()


def _create_review(review: dict) -> bool:
    request_id = _safe_param(review.get("request_id"))
    if not request_id:
        return False
    from_uid = _safe_param(review.get("from_uid"))
    from_name = _safe_param(review.get("from_name")) or "某个 Agent"
    greeting = str(review.get("greeting") or "").strip()
    risk = str(review.get("risk_reason") or "").strip()
    remark = _safe_param(review.get("remark")) or from_name

    body = f"{from_name} 请求加 Jarvis 为 EigenFlux 好友。"
    if greeting:
        body += f"\n\n招呼语：{greeting}"
    if risk:
        body += f"\n\n需要你确认：{risk}"

    common = {
        "request_id": request_id,
        "from_uid": from_uid,
        "from_name": from_name,
        "remark": remark,
    }
    options = [
        {
            "key": "accept",
            "label": "通过",
            "action": {
                "type": "eigenflux_friend",
                "params": {**common, "decision": "accept"},
            },
        },
        {
            "key": "reject",
            "label": "拒绝",
            "action": {
                "type": "eigenflux_friend",
                "params": {**common, "decision": "reject"},
            },
        },
    ]
    context = json.dumps(
        {"kind": "eigenflux_friend_request", "request_id": request_id,
         "from_uid": from_uid},
        ensure_ascii=False,
        sort_keys=True,
    )
    memorial.create(
        source="eigenflux-friends",
        title="EigenFlux · 好友申请",
        body=body,
        options=options,
        context=context,
        dedup_key=f"eigenflux-friend:{request_id}",
    )
    return True


def _create_fallback_reviews(pending: dict[str, dict], reason: str) -> None:
    """Turn schema drift into stable actionable cards, never prose loops."""
    for request in pending.values():
        review = dict(request)
        review["risk_reason"] = reason[:500]
        _create_review(review)


def main() -> int:
    message = sys.stdin.read().strip()
    if not message or message == "HEARTBEAT_OK":
        return 0
    if looks_like_error(message):
        print("[eigenflux-friends] skipping — looks like error output", file=sys.stderr)
        return 0

    data = parse_json_response(message)
    if data is None:
        pending = _pending_requests()
        if pending is not None:
            _create_fallback_reviews(
                pending, "自动判断没有返回可执行结构，请你直接决定。")
        return 0

    actions = [a for a in data.get("actions", []) if isinstance(a, dict)]
    reviews = [r for r in data.get("reviews", []) if isinstance(r, dict)]
    had_structured_items = bool(actions or reviews)
    user_message = data.get("user_message", "")
    if actions or reviews or user_message:
        pending = _pending_requests()
        if pending is None:
            # Fail closed. The request remains pending and the next scheduled
            # run can retry after the server/CLI recovers.
            return 0
        policy_active = temporary_friend_policy_active()
        canonical_actions = []
        for action in actions:
            canonical = _canonical_request(action, pending)
            if canonical is None:
                continue
            if canonical.get("decision") == "accept" and policy_active:
                canonical_actions.append(canonical)
            elif canonical.get("decision") == "accept":
                canonical["risk_reason"] = (
                    "临时自动通过策略当前未启用，请你确认是否通过。")
                reviews.append(canonical)
            else:
                canonical["risk_reason"] = (
                    "自动拒绝不被允许，请你确认是否拒绝。")
                reviews.append(canonical)
        actions = canonical_actions

        canonical_reviews = []
        for review in reviews:
            canonical = _canonical_request(review, pending)
            if canonical is not None:
                canonical_reviews.append(canonical)
        reviews = canonical_reviews

        if not had_structured_items and user_message:
            _create_fallback_reviews(pending, str(user_message))
            return 0

    action_results = []
    for action in actions:
        result, _ = _execute_action(action)
        if result:
            action_results.append(result)

    for review in reviews:
        _create_review(review)

    # The model never gets to narrate an action as successful: status lines
    # above come only from CLI return codes.
    body = "\n".join(action_results)
    if body:
        print(build_card("📡 EigenFlux · 好友申请", body,
                         source="eigenflux-friends"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
