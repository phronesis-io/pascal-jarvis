#!/usr/bin/env python3
"""Fail when accepted large-module debt grows instead of shrinking."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path


def load_budgets(path: str | Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("maintainability budget must be a JSON object")
    for relative, limits in value.items():
        if not isinstance(relative, str) or not isinstance(limits, dict):
            raise ValueError("each budget must map a path to an object")
        for key in ("max_lines", "max_function_lines"):
            if not isinstance(limits.get(key), int) or limits[key] < 0:
                raise ValueError(f"{relative}: {key} must be a non-negative integer")
    return value


def _function_size(node: ast.AST) -> int:
    return int(getattr(node, "end_lineno", node.lineno)) - int(node.lineno) + 1


def audit_budgets(root: str | Path, budgets: dict) -> dict:
    project = Path(root)
    files = []
    violations = []
    for relative, limits in sorted(budgets.items()):
        path = project / relative
        if not path.is_file():
            violations.append({
                "file": relative,
                "metric": "missing",
                "actual": None,
                "budget": None,
            })
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            violations.append({
                "file": relative,
                "metric": "parse_error",
                "actual": str(exc),
                "budget": None,
            })
            continue
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        metrics = {
            "file": relative,
            "lines": len(source.splitlines()),
            "max_function_lines": max(
                (_function_size(node) for node in functions), default=0,
            ),
        }
        files.append(metrics)
        for metric in ("lines", "max_function_lines"):
            budget_key = f"max_{metric}" if metric == "lines" else metric
            budget = int(limits[budget_key])
            if metrics[metric] > budget:
                violations.append({
                    "file": relative,
                    "metric": metric,
                    "actual": metrics[metric],
                    "budget": budget,
                })
    return {"ok": not violations, "files": files, "violations": violations}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enforce non-growing debt budgets for large Python modules",
    )
    parser.add_argument(
        "--root", default=str(Path(__file__).resolve().parent.parent),
    )
    parser.add_argument(
        "--budget", default="docs/maintainability_budget.json",
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    budget_path = Path(args.budget)
    if not budget_path.is_absolute():
        budget_path = root / budget_path
    try:
        result = audit_budgets(root, load_budgets(budget_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"ok": False, "files": [], "violations": [{
            "file": str(budget_path),
            "metric": "budget_error",
            "actual": str(exc),
            "budget": None,
        }]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
