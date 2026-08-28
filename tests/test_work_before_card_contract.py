"""Repository-wide contract for work-before-card production emitters."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_every_production_memorial_create_declares_a_work_receipt():
    missing: list[str] = []
    paths = [*ROOT.glob("core/**/*.py"), *ROOT.glob("tasks/**/*.py")]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_create_names = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "core.memorial"
            for alias in node.names
            if alias.name == "create"
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            memorial_call = (
                isinstance(func, ast.Attribute)
                and func.attr == "create"
                and isinstance(func.value, ast.Name)
                and func.value.id == "memorial"
            )
            local_facade_call = (
                isinstance(func, ast.Name)
                and (
                    (path.name == "memorial.py" and func.id == "create")
                    or func.id in imported_create_names
                )
            )
            if not (memorial_call or local_facade_call):
                continue
            if not any(keyword.arg == "work_receipt"
                       for keyword in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert missing == []


def test_every_direct_memorial_producer_declares_the_full_message_gate():
    missing: list[str] = []
    paths = [*ROOT.glob("core/**/*.py"), *ROOT.glob("tasks/**/*.py")]
    for path in paths:
        if path.name == "memorial.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "create"
                and isinstance(func.value, ast.Name)
                and func.value.id == "memorial"
            ):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            for required in (
                "owner_need", "why_now", "owner_action", "silence_cost",
            ):
                if required not in keywords:
                    missing.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{required}"
                    )
    assert missing == []


def test_every_locally_imported_memorial_create_declares_interruption_contract():
    missing: list[str] = []
    paths = [*ROOT.glob("core/**/*.py"), *ROOT.glob("tasks/**/*.py")]
    for path in paths:
        if path.name == "memorial.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        create_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "core.memorial"
            for alias in node.names
            if alias.name == "create"
        }
        if not create_aliases:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in create_aliases
            ):
                continue
            keywords = {keyword.arg for keyword in node.keywords}
            for required in (
                "work_receipt", "owner_need", "why_now", "owner_action",
                "silence_cost",
            ):
                if required not in keywords:
                    missing.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{required}"
                    )
    assert missing == []


def test_heartbeat_enables_the_proactive_receipt_gate():
    tree = ast.parse(
        (ROOT / "core" / "heartbeat_loop.py").read_text(encoding="utf-8")
    )
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "memorialize_output"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    required = keywords.get("require_work_receipt")
    assert isinstance(required, ast.Constant) and required.value is True


def test_every_production_native_card_declares_a_work_receipt():
    missing: list[str] = []
    for path in ROOT.glob("tasks/**/*.py"):
        if "_quarantine" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_names = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "core.card"
            for alias in node.names
            if alias.name in {"build_card", "build_rich_card"}
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in imported_names:
                continue
            if not any(keyword.arg == "work_receipt"
                       for keyword in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert missing == []
