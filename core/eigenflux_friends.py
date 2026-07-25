"""Deterministic EigenFlux friend-request actions shared by cards and tasks."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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


def _payload_list(result: subprocess.CompletedProcess, key: str) -> list[dict]:
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    value = payload.get(key) if isinstance(payload, dict) else None
    if value is None and isinstance(payload.get("data"), dict):
        value = payload["data"].get(key)
    return [row for row in (value or []) if isinstance(row, dict)]


def _next_cursor(result: subprocess.CompletedProcess) -> str:
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    cursor = str(
        data.get("next_cursor")
        or data.get("nextCursor")
        or payload.get("next_cursor")
        or payload.get("nextCursor")
        or ""
    ).strip()
    # EigenFlux uses the string "0" as its end-of-pagination sentinel.
    # Sending it back restarts pagination and creates a false cursor loop.
    return "" if cursor == "0" else cursor


def _friend_by_id(
    agent_id: str,
    runner,
) -> dict | None:
    cursor = ""
    seen: set[str] = set()
    for _ in range(20):
        command = [
            "eigenflux",
            "relation",
            "friends",
            "--limit",
            "100",
        ]
        if cursor:
            command.extend(["--cursor", cursor])
        command.extend(["-f", "json", "--no-interactive"])
        result = runner(command)
        if result.returncode != 0:
            detail = (
                result.stderr or result.stdout or "friend readback failed"
            ).strip()
            raise RuntimeError(detail[:300])
        match = next(
            (
                row
                for row in _payload_list(result, "friends")
                if str(row.get("agent_id") or "") == agent_id
            ),
            None,
        )
        if match is not None:
            return match
        next_cursor = _next_cursor(result)
        if not next_cursor:
            return None
        if next_cursor in seen:
            raise RuntimeError("friend readback pagination cursor repeated")
        seen.add(next_cursor)
        cursor = next_cursor
    raise RuntimeError("friend readback pagination exceeded 20 pages")


def execute_friend_action(
        action: dict,
        *,
        runner=None,
        root: str | Path | None = None,
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
    if decision == "reject":
        try:
            result = runner(cmd)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return f"「{from_name}」的好友申请处理未完成，CLI 调用失败：{exc}", True
        if result.returncode != 0:
            error = (result.stderr or result.stdout).strip()
            detail = f"：{error[:180]}" if error else ""
            return (
                f"「{from_name}」的好友申请处理未完成，"
                f"服务端没有确认成功{detail}"
            ), True
        return f"已拒绝「{from_name}」的好友申请。", False

    if not from_uid:
        return (f"「{from_name}」的好友申请缺少稳定对象标识，未执行。"), True

    from core.delegation_connectors import (
        project_eigenflux_message_receipt,
        record_connector_receipt,
        reserve_connector_action,
    )

    friend_expected = {
        "agent_id": from_uid,
        "relationship": "friend",
    }

    def project_friend(friend: dict | None, *, matched: bool) -> None:
        record_connector_receipt(
            source="eigenflux-friend",
            source_ref=f"request:{request_id}:accept",
            title=f"通过 {from_name} 的好友申请",
            operation="friend_accept",
            target_type="agent",
            target_id=from_uid,
            target_label=from_name,
            authority="eigenflux_relationship_service",
            verifier="eigenflux_friend",
            expected=friend_expected,
            observed={
                "agent_id": from_uid,
                "relationship": "friend" if matched else "absent",
                **(
                    {
                        "agent_name": str(
                            (friend or {}).get("agent_name") or from_name
                        )
                    }
                    if matched
                    else {}
                ),
            },
            matched=matched,
            resource_locator=f"eigenflux-friend:{from_uid}",
            verification_policy={"agent_id": from_uid},
            root=root,
        )

    try:
        store, delegation, step = reserve_connector_action(
            source="eigenflux-friend",
            source_ref=f"request:{request_id}:accept",
            title=f"通过 {from_name} 的好友申请",
            operation="friend_accept",
            target_type="agent",
            target_id=from_uid,
            target_label=from_name,
            authority="eigenflux_relationship_service",
            verifier="eigenflux_friend",
            expected=friend_expected,
            verification_policy={"agent_id": from_uid},
            root=root,
        )
    except Exception as exc:
        return (
            f"「{from_name}」的好友申请尚未执行："
            f"无法先建立可恢复记录（{exc}）"
        ), True
    if delegation["status"] == "completed":
        already_friend = {"agent_id": from_uid, "agent_name": from_name}
    else:
        try:
            already_friend = _friend_by_id(from_uid, runner)
        except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return (
                f"「{from_name}」的好友关系读取失败；可恢复记录已保留：{exc}"
            ), True
    was_existing_friend = already_friend is not None
    if (
        already_friend is not None
        and step is not None
        and step["status"] == "executing"
    ):
        try:
            store.record_attempt(
                delegation["id"],
                step["id"],
                expected_version=delegation["contract_version"],
                owner="eigenflux-friend",
                succeeded=True,
                artifact_locator=f"eigenflux-friend:{from_uid}",
            )
        except Exception:
            # The authoritative friend read-back is still recorded below.
            # A competing worker or expired lease will be recovered normally.
            pass

    if remark:
        cmd.extend(["--remark", remark[:100]])
    cmd.extend(["--reason", "欢迎加入 EigenFlux，期待交流。"])

    if already_friend is None:
        if step is None:
            return f"「{from_name}」的好友申请缺少可执行步骤。", True
        try:
            store.claim_step(
                delegation["id"],
                step["id"],
                expected_version=delegation["contract_version"],
                owner="eigenflux-friend",
                lease_seconds=120,
            )
        except Exception as exc:
            return (
                f"「{from_name}」的好友申请正在由其他执行器处理：{exc}"
            ), True
        try:
            result = runner(cmd)
        except subprocess.TimeoutExpired as exc:
            return (
                f"「{from_name}」的好友申请结果未知，已保留执行租约等待回读："
                f"{exc}"
            ), True
        except FileNotFoundError as exc:
            store.record_attempt(
                delegation["id"],
                step["id"],
                expected_version=delegation["contract_version"],
                owner="eigenflux-friend",
                succeeded=False,
                error_code="eigenflux_cli_not_found",
            )
            return f"「{from_name}」的好友申请处理未完成，CLI 调用失败：{exc}", True
        if result.returncode != 0:
            # An interrupted first callback may have committed remotely before
            # the client observed it.  Read back once before reporting failure.
            try:
                already_friend = _friend_by_id(from_uid, runner)
            except Exception:
                already_friend = None
            if already_friend is None:
                store.record_attempt(
                    delegation["id"],
                    step["id"],
                    expected_version=delegation["contract_version"],
                    owner="eigenflux-friend",
                    succeeded=False,
                    error_code="eigenflux_friend_accept_failed",
                )
                error = (result.stderr or result.stdout).strip()
                detail = f"：{error[:180]}" if error else ""
                return (
                    f"「{from_name}」的好友申请处理未完成，"
                    f"服务端没有确认成功{detail}"
                ), True
        store.record_attempt(
            delegation["id"],
            step["id"],
            expected_version=delegation["contract_version"],
            owner="eigenflux-friend",
            succeeded=True,
            artifact_locator=f"eigenflux-friend:{from_uid}",
        )

    try:
        friend = _friend_by_id(from_uid, runner)
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return (
            f"已尝试通过「{from_name}」，但好友关系仍在核验：{exc}"
        ), True
    if friend is None:
        return (
            f"已尝试通过「{from_name}」，但权威好友列表尚未确认，"
            "系统不会重复点击。"
        ), True

    try:
        project_friend(friend, matched=True)
    except Exception:
        # The pre-mutation projection remains in verifying and the scheduled
        # reconciler can recover it from the relationship authority.
        pass

    try:
        from core.eigenflux_messages import EigenFluxMessenger

        messenger = EigenFluxMessenger(
            root=root,
            runner=lambda command, **_: runner(command)
        )
        welcome = messenger.send_to_friend_id(
            from_uid,
            WELCOME_MESSAGE,
        )
        project_eigenflux_message_receipt(welcome, root=root)
    except Exception as exc:
        return (
            f"已核验通过「{from_name}」的好友申请，但欢迎消息尚未核验：{exc}"
        ), True
    if not welcome.completed:
        return (
            f"已核验通过「{from_name}」的好友申请；欢迎消息已执行，"
            "仍在回读核验。"
        ), True
    duplicate = "；没有重复执行好友操作" if was_existing_friend else ""
    return (
        f"已通过并核验「{from_name}」的好友申请，并以你作为首席科学家的"
        f"身份发了欢迎{duplicate}。"
    ), False
