"""Evidence and drift tests for scripts/capability_inventory.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.capability_inventory import (
    build_inventory,
    classify_capability,
    main,
    render_markdown,
    validate_inventory,
)

ROOT = Path(__file__).resolve().parent.parent


def _fixture_repo(tmp_path: Path) -> Path:
    (tmp_path / "core").mkdir()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "components.yaml").write_text(
        "components:\n"
        "  - name: bot\n"
        "    check: pid\n"
        "    path: .bot.pid\n"
        "    critical: true\n",
        encoding="utf-8",
    )
    (tmp_path / "HEARTBEAT.md").write_text(
        "### sample-task\n"
        "- interval: 10m\n"
        "- pre: tasks/sample_task_pre.sh\n"
        "- post: tasks/sample_task_post.py\n",
        encoding="utf-8",
    )
    (tmp_path / "tasks" / "sample_task_pre.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "tasks" / "sample_task_post.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "core" / "components.py").write_text(
        "_CHECKS = {\"pid\": lambda item: item}\n",
        encoding="utf-8",
    )
    (tmp_path / "core" / "sample.py").write_text(
        '"""Sample CLI."""\n'
        "import argparse\n"
        "def main():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    sub = parser.add_subparsers()\n"
        "    sub.add_parser('show')\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    (tmp_path / "core" / "matter_bridge.py").write_text(
        "def handle_lark_command(command):\n"
        "    aliases = {'new': 'new', '当前': 'current'}\n"
        "    if command in {'/model codex'}:\n"
        "        return 'codex'\n"
        "    if command in {'/model auto'}:\n"
        "        return 'auto'\n"
        "    if command in {'/model'}:\n"
        "        return 'status'\n",
        encoding="utf-8",
    )
    (tmp_path / "bot.sh").write_text(
        'if [ "$content" = "继续发" ] || [ "$content" = "继续发送" ]; then :; fi\n'
        'if [ "$content" = "发" ] || [ "$content" = "不发" ]; then :; fi\n'
        'if [ "$content_lower" = "stop" ] || [ "$content_lower" = "cancel" ]; then :; fi\n',
        encoding="utf-8",
    )
    (tmp_path / "admin.py").write_text(
        "class Handler:\n"
        "    def do_GET(self):\n"
        "        path = self.path\n"
        "        if path == '/health':\n"
        "            return None\n"
        "        if path.startswith('/api/session/'):\n"
        "            return None\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_facts.py").write_text(
        "# sample-task sample_task core.sample handle_lark_command /health\n"
        "# name: bot /model codex /model auto /model continue-commit pending_publish\n"
        "# content_lower stop cancel\n",
        encoding="utf-8",
    )
    return tmp_path


def test_fixture_discovers_every_fact_source(tmp_path):
    inventory = build_inventory(_fixture_repo(tmp_path))
    by_id = {item["id"]: item for item in inventory["capabilities"]}

    assert "component:bot" in by_id
    assert "heartbeat:sample-task" in by_id
    assert "cli:core.sample" in by_id
    assert "admin-route:get:/health" in by_id
    assert "admin-route:get:/api/session/{param}" in by_id
    assert "lark:matter:new" in by_id
    assert "lark:model:codex" in by_id
    assert "lark:session:stop" in by_id
    assert by_id["cli:core.sample"]["metadata"]["subcommands"] == ["show"]
    assert validate_inventory(inventory) == []


def test_every_audited_gap_is_resolved_or_explicitly_explained():
    inventory = build_inventory(ROOT)
    by_id = {item["id"]: item for item in inventory["capabilities"]}
    audited = inventory["resolved_evidence_audit"]

    assert len(audited) == 12
    assert set(audited) <= set(by_id)
    unresolved = {
        capability_id: by_id[capability_id]["status_reason"]
        for capability_id in audited
        if by_id[capability_id]["status"] != "keep"
    }
    assert unresolved == {}


def test_status_rules_are_conservative():
    base = {
        "id": "example",
        "source_evidence": [{"path": "x", "line": 1}],
        "implementation_evidence": [{"path": "x", "line": 2}],
        "runtime_evidence": [{"type": "cli", "entrypoint": "x"}],
        "test_evidence": [{"path": "test_x", "line": 1}],
        "retirement_evidence": [],
    }
    assert classify_capability(dict(base))["status"] == "keep"

    no_test = dict(base, test_evidence=[])
    assert classify_capability(no_test)["status"] == "fix"
    assert "missing test reference" in no_test["evidence_gaps"]

    retired = dict(
        base,
        runtime_evidence=[],
        retirement_evidence=[{"path": "x", "line": 3}],
    )
    assert classify_capability(retired)["status"] == "retire-candidate"
    assert "human replacement/migration review required" in retired["evidence_gaps"]

    contradictory = dict(
        base,
        retirement_evidence=[{"path": "x", "line": 3}],
    )
    assert classify_capability(contradictory)["status"] == "fix"
    assert "conflicts" in contradictory["status_reason"]


def test_module_level_string_is_not_executable_test_evidence(tmp_path):
    root = _fixture_repo(tmp_path)
    (root / "tests" / "test_facts.py").write_text(
        'UNUSED = "core.sample sample-task name: bot"\n',
        encoding="utf-8",
    )

    inventory = build_inventory(root)
    by_id = {item["id"]: item for item in inventory["capabilities"]}

    assert by_id["cli:core.sample"]["status"] == "fix"
    assert by_id["heartbeat:sample-task"]["status"] == "fix"
    assert "missing test reference" in by_id["cli:core.sample"]["evidence_gaps"]


def test_function_name_and_docstring_are_not_executable_test_evidence(tmp_path):
    root = _fixture_repo(tmp_path)
    (root / "tests" / "test_facts.py").write_text(
        "def test_core_sample_name_only():\n"
        '    \"\"\"sample-task handle_lark_command name: bot.\"\"\"\n'
        "    pass\n",
        encoding="utf-8",
    )

    inventory = build_inventory(root)
    by_id = {item["id"]: item for item in inventory["capabilities"]}

    assert by_id["cli:core.sample"]["status"] == "fix"
    assert by_id["heartbeat:sample-task"]["status"] == "fix"


def test_matching_test_filename_without_behavior_is_not_evidence(tmp_path):
    root = _fixture_repo(tmp_path)
    (root / "tests" / "test_sample.py").write_text(
        "def test_unrelated_arithmetic():\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    inventory = build_inventory(root)
    by_id = {item["id"]: item for item in inventory["capabilities"]}

    assert by_id["cli:core.sample"]["status"] == "fix"
    assert "missing test reference" in by_id["cli:core.sample"]["evidence_gaps"]


def test_real_inventory_has_expected_anchors_and_unique_ids():
    inventory = build_inventory(ROOT)
    by_id = {item["id"]: item for item in inventory["capabilities"]}

    assert len(by_id) == inventory["summary"]["total"]
    assert len(by_id) >= 100
    for capability_id in (
        "component:bot",
        "heartbeat:provider-canary",
        "heartbeat:cross-session-sync",
        "cli:core.codex_fallback",
        "cli:core.memorial",
        "admin-route:get:/health",
        "admin-route:post:/api/bot/restart",
        "lark:model:codex",
        "lark:matter:handoff",
        "lark:session:new",
        "lark:session:switch",
        "lark:session:reset",
        "lark:session:close",
        "lark:eigenflux:publish-confirm",
    ):
        assert capability_id in by_id
        assert by_id[capability_id]["source_evidence"]
    # A retired surface leaves an explicit trace, never a silent disappearance.
    assert any("dashboard :3457" in key for key in inventory["retired_surfaces"])
    assert any("3458" in key for key in inventory["retired_surfaces"])
    assert not any(item["kind"].startswith("dashboard") for item in by_id.values())
    assert validate_inventory(inventory) == []


def test_json_and_markdown_are_deterministic(tmp_path, capsys):
    root = _fixture_repo(tmp_path)
    first = build_inventory(root)
    second = build_inventory(root)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert render_markdown(first) == render_markdown(second)

    assert main(["--root", str(root), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1


def test_checked_document_matches_generator():
    inventory = build_inventory(ROOT)
    document = ROOT / "docs" / "capability_inventory.md"
    assert document.read_text(encoding="utf-8") == render_markdown(inventory)
    assert main(["--root", str(ROOT), "--check-doc", str(document)]) == 0
