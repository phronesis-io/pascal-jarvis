#!/usr/bin/env python3
"""Inspect repository-local Python imports without third-party dependencies.

The graph is intentionally static and conservative: it follows regular
``import`` and ``from ... import ...`` statements parsed by :mod:`ast`, and it
reports only modules discovered under the requested paths. Dynamic imports are
outside its scope.

Examples:
    python3 scripts/import_graph.py core --threshold 20
    python3 scripts/import_graph.py core dashboard tasks --format json
    python3 scripts/import_graph.py core --format mermaid --focus core.delivery
    python3 scripts/import_graph.py core --threshold 20 --fail-on-threshold
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
METRICS = ("adjacency", "fan-in", "fan-out")


class GraphError(RuntimeError):
    """Raised when the requested source graph cannot be built safely."""


@dataclass(frozen=True, slots=True)
class ModuleSource:
    name: str
    path: Path
    is_package: bool


@dataclass(frozen=True, slots=True)
class ModuleCounts:
    module: str
    fan_in: int
    fan_out: int
    adjacency: int

    def value(self, metric: str) -> int:
        if metric == "fan-in":
            return self.fan_in
        if metric == "fan-out":
            return self.fan_out
        if metric == "adjacency":
            return self.adjacency
        raise ValueError(f"unsupported metric: {metric}")


@dataclass(frozen=True, slots=True)
class ImportGraph:
    root: Path
    sources: dict[str, ModuleSource]
    outgoing: dict[str, frozenset[str]]

    @property
    def incoming(self) -> dict[str, frozenset[str]]:
        values: dict[str, set[str]] = {name: set() for name in self.sources}
        for source, dependencies in self.outgoing.items():
            for dependency in dependencies:
                values[dependency].add(source)
        return {name: frozenset(callers) for name, callers in values.items()}

    def counts(self) -> list[ModuleCounts]:
        incoming = self.incoming
        rows = []
        for module in self.sources:
            callers = incoming[module]
            dependencies = self.outgoing[module]
            rows.append(ModuleCounts(
                module=module,
                fan_in=len(callers),
                fan_out=len(dependencies),
                adjacency=len(callers | dependencies),
            ))
        return rows


def _module_name(root: Path, path: Path) -> tuple[str, bool]:
    try:
        relative = path.resolve().relative_to(root)
    except ValueError as exc:
        raise GraphError(f"source is outside repository root: {path}") from exc
    parts = list(relative.with_suffix("").parts)
    is_package = bool(parts and parts[-1] == "__init__")
    if is_package:
        parts.pop()
    name = ".".join(parts)
    if not name:
        raise GraphError(f"cannot derive a module name for {path}")
    return name, is_package


def _python_files(target: Path) -> Iterable[Path]:
    if target.is_file():
        if target.suffix == ".py":
            yield target
        return
    for path in sorted(target.rglob("*.py")):
        if not any(part in IGNORED_DIRECTORY_NAMES for part in path.parts):
            yield path


def discover_modules(
    root: str | Path,
    targets: Sequence[str | Path],
) -> dict[str, ModuleSource]:
    resolved_root = Path(root).resolve()
    sources: dict[str, ModuleSource] = {}
    for raw_target in targets:
        target = Path(raw_target)
        if not target.is_absolute():
            target = resolved_root / target
        if not target.exists():
            raise GraphError(f"source path does not exist: {target}")
        for path in _python_files(target):
            name, is_package = _module_name(resolved_root, path)
            existing = sources.get(name)
            if existing and existing.path != path.resolve():
                raise GraphError(
                    f"duplicate module {name}: {existing.path} and {path}"
                )
            sources[name] = ModuleSource(name, path.resolve(), is_package)
    if not sources:
        raise GraphError("no Python modules found under the requested paths")
    return sources


def _longest_module(candidate: str, module_names: set[str]) -> str | None:
    parts = [part for part in candidate.split(".") if part]
    while parts:
        current = ".".join(parts)
        if current in module_names:
            return current
        parts.pop()
    return None


def _from_base(source: ModuleSource, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    source_parts = source.name.split(".")
    package_parts = source_parts if source.is_package else source_parts[:-1]
    parents_to_drop = node.level - 1
    if parents_to_drop > len(package_parts):
        return ""
    if parents_to_drop:
        package_parts = package_parts[:-parents_to_drop]
    suffix = (node.module or "").split(".") if node.module else []
    return ".".join([*package_parts, *suffix])


def _dependencies_for(
    source: ModuleSource,
    module_names: set[str],
) -> frozenset[str]:
    try:
        tree = ast.parse(source.path.read_text(encoding="utf-8"), source.path.name)
    except (OSError, UnicodeError, SyntaxError) as exc:
        location = ""
        if isinstance(exc, SyntaxError) and exc.lineno:
            location = f":{exc.lineno}"
        raise GraphError(f"cannot parse {source.path}{location}: {exc}") from exc

    dependencies: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(source, node)
            if not base:
                continue
            for alias in node.names:
                candidates.append(
                    base if alias.name == "*" else f"{base}.{alias.name}"
                )
        else:
            continue

        for candidate in candidates:
            dependency = _longest_module(candidate, module_names)
            if dependency and dependency != source.name:
                dependencies.add(dependency)
    return frozenset(dependencies)


def build_graph(
    root: str | Path,
    targets: Sequence[str | Path],
) -> ImportGraph:
    resolved_root = Path(root).resolve()
    sources = discover_modules(resolved_root, targets)
    module_names = set(sources)
    outgoing = {
        name: _dependencies_for(source, module_names)
        for name, source in sources.items()
    }
    return ImportGraph(resolved_root, sources, outgoing)


def threshold_violations(
    graph: ImportGraph,
    metric: str,
    threshold: int | None,
) -> list[ModuleCounts]:
    if threshold is None:
        return []
    return sorted(
        (row for row in graph.counts() if row.value(metric) > threshold),
        key=lambda row: (-row.value(metric), row.module),
    )


def render_text(graph: ImportGraph, metric: str, limit: int) -> str:
    rows = sorted(
        graph.counts(),
        key=lambda row: (-row.value(metric), row.module),
    )
    if limit > 0:
        rows = rows[:limit]
    header = f"{'module':48} {'fan-in':>7} {'fan-out':>8} {'adjacency':>10}"
    lines = [
        f"Python import graph: {len(graph.sources)} modules; ranked by {metric}",
        header,
        "-" * len(header),
    ]
    lines.extend(
        f"{row.module:48} {row.fan_in:7d} {row.fan_out:8d} {row.adjacency:10d}"
        for row in rows
    )
    return "\n".join(lines)


def render_json(graph: ImportGraph) -> str:
    incoming = graph.incoming
    counts = {row.module: row for row in graph.counts()}
    payload = {
        "root": str(graph.root),
        "module_count": len(graph.sources),
        "edge_count": sum(len(values) for values in graph.outgoing.values()),
        "modules": [
            {
                "module": module,
                "path": str(graph.sources[module].path.relative_to(graph.root)),
                "fan_in": counts[module].fan_in,
                "fan_out": counts[module].fan_out,
                "adjacency": counts[module].adjacency,
                "incoming": sorted(incoming[module]),
                "outgoing": sorted(graph.outgoing[module]),
            }
            for module in sorted(graph.sources)
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _focused_modules(graph: ImportGraph, focuses: Sequence[str]) -> set[str]:
    if not focuses:
        return set(graph.sources)
    unknown = sorted(set(focuses) - set(graph.sources))
    if unknown:
        raise GraphError("unknown focus module(s): " + ", ".join(unknown))
    incoming = graph.incoming
    selected = set(focuses)
    for module in focuses:
        selected.update(graph.outgoing[module])
        selected.update(incoming[module])
    return selected


def render_mermaid(graph: ImportGraph, focuses: Sequence[str]) -> str:
    selected = _focused_modules(graph, focuses)
    counts = {row.module: row for row in graph.counts()}
    identifiers = {
        module: f"m{index}" for index, module in enumerate(sorted(selected))
    }
    lines = ["flowchart LR"]
    for module in sorted(selected):
        row = counts[module]
        label = (
            f"{module}<br/>in {row.fan_in} | out {row.fan_out} | "
            f"adj {row.adjacency}"
        )
        lines.append(f'  {identifiers[module]}["{label}"]')
    for source in sorted(selected):
        for dependency in sorted(graph.outgoing[source] & selected):
            lines.append(
                f"  {identifiers[source]} --> {identifiers[dependency]}"
            )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report repository-local Python import adjacency.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["core"],
        help="Python files or directories, relative to --root",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root used for module names",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "mermaid"),
        default="text",
        help="report format",
    )
    parser.add_argument(
        "--metric",
        choices=METRICS,
        default="adjacency",
        help="metric used for text ranking and threshold checks",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        help="warn when the selected metric is greater than this value",
    )
    parser.add_argument(
        "--fail-on-threshold",
        action="store_true",
        help="exit 2 when at least one module exceeds --threshold",
    )
    parser.add_argument(
        "--focus",
        action="append",
        default=[],
        help="for Mermaid, include this module and its one-hop neighbors",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="maximum text rows; 0 means all modules",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the report to this file instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.threshold is not None and args.threshold < 0:
        print("error: --threshold must be >= 0", file=sys.stderr)
        return 1
    if args.limit < 0:
        print("error: --limit must be >= 0", file=sys.stderr)
        return 1
    if args.fail_on_threshold and args.threshold is None:
        print("error: --fail-on-threshold requires --threshold", file=sys.stderr)
        return 1
    try:
        graph = build_graph(args.root, args.paths)
        if args.format == "json":
            report = render_json(graph)
        elif args.format == "mermaid":
            report = render_mermaid(graph, args.focus)
        else:
            report = render_text(graph, args.metric, args.limit)
    except GraphError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)

    violations = threshold_violations(graph, args.metric, args.threshold)
    if violations:
        detail = ", ".join(
            f"{row.module}={row.value(args.metric)}" for row in violations
        )
        print(
            f"WARNING: {len(violations)} module(s) exceed {args.metric} "
            f"threshold {args.threshold}: {detail}",
            file=sys.stderr,
        )
        if args.fail_on_threshold:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
