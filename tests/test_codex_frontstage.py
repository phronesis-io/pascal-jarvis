"""Product-contract tests for Codex frontstage and Jarvis backstage."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import core.db as db_module
from core.codex_frontstage import (
    abort_matter_run,
    close_frontstage_matter,
    continue_matter_run,
    create_frontstage_matter,
    frontstage_health,
    matter_status,
    release_matter_run,
    search_matters,
    start_matter_run,
)
from core.frontstage_acceptance import acceptance_report, record_acceptance
from core.matter_runs import MatterRunConflict, get_run
from core.matters import create_matter, get_matter


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", tmp_path / "jarvis.db")
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None
    yield
    if db_module._connection is not None:
        db_module._connection.close()
    db_module._connection = None


def test_create_reuses_an_exact_open_matter_title():
    first = create_frontstage_matter(
        title="跨产品连续性",
        summary="Codex 前台，Jarvis 后台",
        next_action="实现连接层",
    )
    second = create_frontstage_matter(title="  跨产品连续性  ")

    assert first["created"] is True
    assert second["created"] is False
    assert second["matter"]["id"] == first["matter"]["id"]
    assert search_matters(query="连接层")["matters"][0]["id"] == (
        first["matter"]["id"]
    )


def test_continue_resolves_one_natural_query_and_starts_the_run(tmp_path):
    matter = create_matter(
        "EigenFlux 白皮书",
        summary="两周三个 Vlog",
        next_action="整理路线图",
    )

    result = continue_matter_run(
        query="白皮书",
        task="继续整理时间节点",
        workspace=str(tmp_path),
        surface="mobile",
    )

    assert result["status"] == "started"
    assert result["matter"]["id"] == matter["id"]
    assert result["run"]["matter_id"] == matter["id"]
    assert result["context_packet"]["matter"]["next_action"] == "整理路线图"


def test_continue_fails_closed_on_ambiguous_or_missing_query(tmp_path):
    create_matter("白皮书论证")
    create_matter("白皮书发布")

    ambiguous = continue_matter_run(
        query="白皮书", task="继续", workspace=str(tmp_path)
    )
    missing = continue_matter_run(
        query="董事责任险", task="继续", workspace=str(tmp_path)
    )

    assert ambiguous["status"] == "ambiguous"
    assert len(ambiguous["candidates"]) == 2
    assert missing["status"] == "not_found"
    assert missing["candidates"] == []


def test_frontstage_close_needs_owner_confirmation_and_reconciles(tmp_path):
    matter = create_matter("自然关闭")

    result = close_frontstage_matter(
        matter_id=matter["id"],
        outcome="已完成测试",
        owner_confirmation="确认已经完成",
    )

    assert result["status"] == "closed"
    assert get_matter(matter["id"])["status"] == "done"


def test_start_returns_bounded_packet_and_records_the_codex_task(tmp_path):
    matter = create_matter(
        "手机继续工作",
        summary="在 Codex Remote 继续同一个结果",
        next_action="实现并验证",
    )

    started = start_matter_run(
        matter_id=matter["id"],
        task="完成 Phase 1",
        workspace=str(tmp_path),
        task_ref="codex-task-123",
        model="gpt-test",
        surface="mobile",
    )

    packet = started["context_packet"]
    run = started["run"]
    assert packet["schema"] == "jarvis.context-packet.v2"
    assert packet["matter"]["id"] == matter["id"]
    assert packet["authority"]["may_complete_matter"] is False
    assert packet["receipt_contract"]["model_narrative"] == "unverified"
    assert run["status"] == "running"
    assert run["session_id"] == "codex-task-123"
    assert run["surface"] == "mobile"
    assert Path(started["context_path"]).is_file()
    assert matter_status(matter["id"])["active_runs"][0]["id"] == run["id"]


def test_only_one_frontstage_can_own_a_matter(tmp_path):
    matter = create_matter("单一执行租约")
    first = start_matter_run(
        matter_id=matter["id"], task="first", workspace=str(tmp_path)
    )

    with pytest.raises(MatterRunConflict, match="active run"):
        start_matter_run(
            matter_id=matter["id"], task="second", workspace=str(tmp_path)
        )

    assert get_run(first["run"]["id"])["status"] == "running"


def test_release_is_idempotent_and_does_not_complete_the_matter(tmp_path):
    artifact = tmp_path / "result.md"
    artifact.write_text("verified result", encoding="utf-8")
    matter = create_matter("收据不等于事项完成", next_action="人工复核")
    started = start_matter_run(
        matter_id=matter["id"], task="write result", workspace=str(tmp_path)
    )
    packet = started["context_packet"]
    arguments = {
        "run_id": started["run"]["id"],
        "context_generation": packet["context_generation"],
        "context_digest": packet["digest"],
        "narrative": "我认为已完成",
        "artifacts": ["result.md"],
    }

    first = release_matter_run(**arguments)
    second = release_matter_run(**arguments)

    assert second["receipt_id"] == first["receipt_id"]
    assert first["artifacts"][0]["sha256"]
    assert first["narrative_trust"] == "unverified_model_report"
    assert first["matter_completed"] is False
    assert get_matter(matter["id"])["status"] == "active"


def test_abort_releases_a_failed_task_for_the_next_frontstage(tmp_path):
    matter = create_matter("失败后可接续")
    started = start_matter_run(
        matter_id=matter["id"], task="attempt", workspace=str(tmp_path)
    )

    aborted = abort_matter_run(started["run"]["id"], error="task cancelled")
    replacement = start_matter_run(
        matter_id=matter["id"], task="retry", workspace=str(tmp_path)
    )

    assert aborted["status"] == "failed"
    assert aborted["receipt"]["narrative_trust"] == "system_observation"
    assert replacement["run"]["run_sequence"] == 2


def test_health_reports_unreleased_residue_without_mutating_it(tmp_path):
    matter = create_matter("健康审计")
    started = start_matter_run(
        matter_id=matter["id"], task="work", workspace=str(tmp_path)
    )

    report = frontstage_health()

    assert report["audit"]["counts"]["running"] == 1
    assert get_run(started["run"]["id"])["status"] == "running"


def test_official_mcp_adapter_exposes_only_the_bounded_matter_contract(
        monkeypatch):
    from core import codex_mcp

    create_matter("MCP 协议测试", summary="不能暴露原始会话")
    monkeypatch.setattr(codex_mcp, "model_usage_status", lambda refresh=True: {
        "schema": "jarvis.model-status.v1",
        "refreshed": refresh,
        "text": "Codex 7 天额度还剩约 50%",
        "report": {
            "codex": {"windows": [{"remaining_percent": 50}]},
        },
    })

    async def scenario():
        from mcp import Client

        async with Client(codex_mcp.create_server()) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "jarvis_frontstage_health",
                "jarvis_model_status",
                "jarvis_matter_abort",
                "jarvis_matter_close",
                "jarvis_matter_continue",
                "jarvis_matter_create",
                "jarvis_matter_release",
                "jarvis_matter_review",
                "jarvis_matter_renew",
                "jarvis_matter_search",
                "jarvis_matter_start",
                "jarvis_matter_status",
                "jarvis_memory_review",
                "jarvis_memory_search",
            }
            by_name = {tool.name: tool for tool in tools.tools}
            assert by_name["jarvis_matter_search"].annotations.read_only_hint
            assert by_name["jarvis_matter_abort"].annotations.destructive_hint
            assert by_name["jarvis_matter_release"].annotations.idempotent_hint
            assert by_name["jarvis_matter_review"].annotations.read_only_hint
            assert by_name["jarvis_memory_search"].annotations.read_only_hint
            assert by_name["jarvis_memory_review"].annotations.destructive_hint
            result = await client.call_tool(
                "jarvis_matter_search", {"query": "MCP"}
            )
            assert result.is_error is False
            assert result.structured_content["count"] == 1
            assert "events" not in result.structured_content["matters"][0]
            usage = await client.call_tool(
                "jarvis_model_status", {"refresh": True}
            )
            assert usage.is_error is False
            assert usage.structured_content["refreshed"] is True
            assert usage.structured_content["report"]["codex"]["windows"][0][
                "remaining_percent"
            ] == 50
            review = await client.call_tool("jarvis_matter_review", {})
            assert review.is_error is False
            assert review.structured_content["schema"] == "jarvis.matter-review.v1"

    asyncio.run(scenario())


def test_acceptance_is_human_reviewed_and_blocks_lark_retirement(tmp_path):
    matter = create_matter("真实接续样本")
    started = start_matter_run(
        matter_id=matter["id"],
        task="mobile continuation",
        workspace=str(tmp_path),
        surface="mobile",
    )
    packet = started["context_packet"]
    release_matter_run(
        run_id=started["run"]["id"],
        context_generation=packet["context_generation"],
        context_digest=packet["digest"],
    )

    review = record_acceptance(
        run_id=started["run"]["id"],
        surface="mobile",
        matter_discovered_correct=True,
        context_packet_correct=True,
        task_completed=True,
        duplicate_effect=False,
        reexplanation_required=False,
        reviewer="owner",
        now=100.0,
    )
    report = acceptance_report()

    assert review["receipt_valid"] == 1
    assert report["surfaces"]["mobile"]["reviewed"] == 1
    assert report["surfaces"]["mobile"]["critical_failures"] == 0
    assert report["surfaces"]["mobile"]["ready"] is False
    assert report["surfaces"]["desktop"]["reviewed"] == 0
    assert report["ready"] is False
    assert report["retirement_boundary"].startswith("no_lark_path_retires")


def test_acceptance_rejects_surface_conflicts_and_non_boolean_claims(tmp_path):
    matter = create_matter("验收不能伪造")
    started = start_matter_run(
        matter_id=matter["id"],
        task="desktop continuation",
        workspace=str(tmp_path),
        surface="desktop",
    )
    packet = started["context_packet"]
    release_matter_run(
        run_id=started["run"]["id"],
        context_generation=packet["context_generation"],
        context_digest=packet["digest"],
    )

    with pytest.raises(ValueError, match="surface conflicts"):
        record_acceptance(
            run_id=started["run"]["id"],
            surface="mobile",
            matter_discovered_correct=True,
            context_packet_correct=True,
            task_completed=True,
            duplicate_effect=False,
            reexplanation_required=False,
            reviewer="owner",
        )
    with pytest.raises(ValueError, match="must be boolean"):
        record_acceptance(
            run_id=started["run"]["id"],
            surface="desktop",
            matter_discovered_correct="yes",
            context_packet_correct=True,
            task_completed=True,
            duplicate_effect=False,
            reexplanation_required=False,
            reviewer="owner",
        )


def test_failed_run_cannot_be_reviewed_as_a_completed_task(tmp_path):
    matter = create_matter("失败不能伪装完成")
    started = start_matter_run(
        matter_id=matter["id"],
        task="desktop failure",
        workspace=str(tmp_path),
        surface="desktop",
    )
    packet = started["context_packet"]
    release_matter_run(
        run_id=started["run"]["id"],
        context_generation=packet["context_generation"],
        context_digest=packet["digest"],
        exit_code=1,
    )

    with pytest.raises(ValueError, match="completion conflicts"):
        record_acceptance(
            run_id=started["run"]["id"],
            surface="desktop",
            matter_discovered_correct=True,
            context_packet_correct=True,
            task_completed=True,
            duplicate_effect=False,
            reexplanation_required=False,
            reviewer="owner",
        )


def test_frontstage_acceptance_cli_reports_an_empty_fail_closed_gate(tmp_path):
    env = {
        **os.environ,
        "JARVIS_DB_PATH": str(tmp_path / "cli.db"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "core.frontstage_acceptance", "report"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "jarvis.frontstage-acceptance.v1"
    assert payload["ready"] is False


def test_plugin_launcher_starts_from_a_codex_cache_directory(tmp_path):
    async def scenario():
        from mcp import Client, StdioServerParameters

        env = {
            **os.environ,
            "JARVIS_DIR": str(Path(__file__).resolve().parents[1]),
            "JARVIS_PYTHON": sys.executable,
            "JARVIS_DB_PATH": str(tmp_path / "stdio.db"),
        }
        params = StdioServerParameters(
            command=str(
                Path(__file__).resolve().parents[1]
                / "plugins"
                / "jarvis-matters"
                / "scripts"
                / "launch-jarvis-mcp"
            ),
            cwd=tmp_path,
            env=env,
        )
        async with Client(params) as client:
            result = await client.call_tool("jarvis_frontstage_health", {})
            assert result.is_error is False
            assert result.structured_content["schema"] == (
                "jarvis.frontstage-health.v1"
            )

    asyncio.run(scenario())


def test_release_smoke_handshakes_with_the_installed_frontstage(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_codex_frontstage.py")],
        cwd=tmp_path,
        env={
            **os.environ,
            "JARVIS_DIR": str(root),
            "JARVIS_PYTHON": sys.executable,
            "JARVIS_DB_PATH": str(tmp_path / "release-smoke.db"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "jarvis.codex-frontstage-smoke.v1"
