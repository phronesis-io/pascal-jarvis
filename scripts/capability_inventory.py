#!/usr/bin/env python3
"""Build a deterministic, evidence-backed inventory of Jarvis capabilities.

The scanner is intentionally static. It reads only repository facts and never
imports Jarvis runtime modules, opens state databases, or probes live services.

Usage:
    python3 scripts/capability_inventory.py --format json
    python3 scripts/capability_inventory.py --format markdown
    python3 scripts/capability_inventory.py --write-doc docs/capability_inventory.md
    python3 scripts/capability_inventory.py --check-doc docs/capability_inventory.md
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
STATUSES = ("keep", "fix", "retire-candidate")
KIND_ORDER = {
    "runtime-component": 0,
    "heartbeat-task": 1,
    "core-cli": 2,
    "dashboard-page": 3,
    "dashboard-api": 4,
    "admin-api": 5,
    "lark-command": 6,
}
STATUS_REQUIREMENTS = {
    "keep": [
        "definition evidence",
        "implementation evidence",
        "runtime/entrypoint evidence",
        "test reference",
        "no explicit retirement marker",
    ],
    "fix": [
        "definition evidence",
        "an active implementation or entrypoint",
        "at least one named evidence gap to close",
    ],
    "retire-candidate": [
        "definition evidence",
        "an explicit retirement/deprecation marker",
        "no active runtime/entrypoint evidence",
        "human review of replacement, migration, and data-retention impact",
    ],
}
ROUTE_TEST_ALIASES = {
    ("GET", "/api/tasks/due"): ("get_due_tasks(",),
    ("GET", "/api/engagement/stats"): ("engagement_stats(",),
    ("GET", "/api/delegations/metrics"): ("store.metrics()",),
}
ADMIN_ROUTE_TEST_ALIASES = {
    ("GET", "/api/lark_chats"): ("lark_chats()",),
    ("GET", "/api/live"): ("live_chat()",),
    ("GET", "/api/session/{suffix}"): ("load_full_session(",),
    ("GET", "/api/sessions_meta"): ("sessions_meta()",),
    ("GET", "/healthz"): ('/health"', "/health'"),
    ("GET", "/view/{suffix}"): ("/view/",),
    ("POST", "/api/heartbeat/force/{suffix}"): ("/api/heartbeat/force/",),
}
RESOLVED_EVIDENCE_AUDIT = {
    "component:caffeinate-launchd": "existing launchd install contract uses com.pascal.jarvis.caffeinate",
    "component:daemon-launchd": "existing launchd batch rollback/install contracts use com.pascal.jarvis.daemon",
    "component:dashboard-launchd": "existing dashboard launchd install contracts use com.pascal.jarvis.dashboard",
    "heartbeat:memory-weekly": "focused contract runs the pre-hook and verifies archive/backup/atomic digest output",
    "cli:core.runtime_provider": "focused CLI contract persists set/get preference and validates bad input",
    "cli:core.usage_stats": "focused CLI contract parses numeric usage while excluding transcript text",
    "route:page:/routines": "focused HTTP page smoke exercises the registered NiceGUI route",
    "route:page:/usage": "focused HTTP page smoke exercises the registered NiceGUI route with isolated usage data",
    "route:get:/api/delegations/metrics": "existing DelegationStore.metrics behavioral tests cover the route dependency",
    "route:get:/api/engagement/stats": "existing engagement_stats behavioral tests cover honesty and cache refresh",
    "route:get:/api/tasks/due": "existing get_due_tasks scheduler tests cover due/not-due behavior",
    "admin-route:get:/api/lark_chats": "existing lark_chats tests cover counters, chat kinds, and missing files",
    "admin-route:get:/api/live": "focused contract selects the newest tracked live conversation",
    "admin-route:get:/api/session/{param}": "focused contract loads a valid session and rejects traversal",
    "admin-route:get:/api/sessions_meta": "existing sessions_meta test covers transcript discovery",
    "admin-route:get:/healthz": "same health branch as the existing real HTTP /health contract",
    "admin-route:get:/view/{param}": "existing real HTTP RichView test covers a concrete /view/<id> route",
    "admin-route:post:/api/heartbeat/force/{param}": "existing real HTTP tests cover known and unknown concrete task names",
}
RETIRE_RE = re.compile(
    r"^\s*(?:[>#\"'/]+\s*)?"
    r"(?:deprecated|obsolete|retired|已废弃|废弃)(?:\s*[:：-]|\b)",
    re.I,
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _rel(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _ref(root: Path, path: Path, line: int, detail: str) -> dict[str, Any]:
    return {
        "path": _rel(root, path),
        "line": max(1, int(line)),
        "detail": detail,
    }


def _line_for(lines: list[str], pattern: re.Pattern[str], start: int = 0) -> int:
    for index, line in enumerate(lines[start:], start=start + 1):
        if pattern.search(line):
            return index
    return 0


def _dedupe(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


@lru_cache(maxsize=16)
def _parse_test_corpus(
    root_text: str,
    signature: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[Path, tuple[str, ...], tuple[tuple[int, int], ...]], ...]:
    """Parse each test module once for one immutable repository snapshot."""
    root = Path(root_text)
    corpus = []
    for relative, _mtime_ns, _size in signature:
        path = root / relative
        source = _read(path)
        lines = tuple(source.splitlines())
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        test_regions: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    or not node.name.startswith("test_"):
                continue
            for decorator in node.decorator_list:
                test_regions.append((
                    decorator.lineno,
                    int(decorator.end_lineno or decorator.lineno),
                ))
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body = body[1:]
            if body:
                test_regions.append((
                    body[0].lineno,
                    int(node.end_lineno or body[-1].end_lineno or body[0].lineno),
                ))
        if test_regions:
            corpus.append((path, lines, tuple(test_regions)))
    return tuple(corpus)


def _test_corpus(
    root: Path,
) -> tuple[tuple[Path, tuple[str, ...], tuple[tuple[int, int], ...]], ...]:
    paths = [
        path for path in sorted((root / "tests").glob("test_*.py"))
        if path.name != "test_capability_inventory.py"
    ]
    signature = tuple(
        (
            path.relative_to(root).as_posix(),
            path.stat().st_mtime_ns,
            path.stat().st_size,
        )
        for path in paths
    )
    return _parse_test_corpus(str(root.resolve()), signature)


def _test_references(
    root: Path,
    needles: Iterable[str],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    terms = [term for term in dict.fromkeys(needles) if term and len(term) >= 3]
    if not terms:
        return []
    references = []
    for path, lines, test_regions in _test_corpus(root):
        matched = False
        for start, end in test_regions:
            for line_number in range(start, min(end, len(lines)) + 1):
                line = lines[line_number - 1]
                if line.lstrip().startswith("#"):
                    continue
                match = next((term for term in terms if term in line), None)
                if match is None:
                    continue
                references.append(_ref(
                    root, path, line_number,
                    f"executable test body references {match!r}",
                ))
                matched = True
                break
            if matched:
                break
        if len(references) >= limit:
            break
    return references


def _retirement_references(
    root: Path,
    path: Path,
    *,
    start_line: int = 1,
    end_line: int | None = None,
) -> list[dict[str, Any]]:
    lines = _read(path).splitlines()
    stop = min(len(lines), end_line or len(lines))
    refs = []
    for line_number in range(max(1, start_line), stop + 1):
        line = lines[line_number - 1]
        if RETIRE_RE.search(line):
            refs.append(_ref(root, path, line_number, line.strip()[:160]))
    return refs[:3]


def _capability(
    *,
    capability_id: str,
    kind: str,
    name: str,
    description: str,
    source_evidence: list[dict[str, Any]],
    implementation_evidence: list[dict[str, Any]],
    runtime_evidence: list[dict[str, Any]],
    test_evidence: list[dict[str, Any]],
    retirement_evidence: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = {
        "id": capability_id,
        "kind": kind,
        "name": name,
        "description": description,
        "owner": source_evidence[0].get("path", "") if source_evidence else "",
        "status": "fix",
        "status_reason": "",
        "source_evidence": _dedupe(source_evidence),
        "implementation_evidence": _dedupe(implementation_evidence),
        "runtime_evidence": _dedupe(runtime_evidence),
        "test_evidence": _dedupe(test_evidence),
        "retirement_evidence": _dedupe(retirement_evidence or []),
        "evidence_gaps": [],
        "metadata": metadata or {},
    }
    return classify_capability(item)


def classify_capability(item: dict[str, Any]) -> dict[str, Any]:
    """Apply conservative, evidence-only status rules to one capability."""
    gaps = []
    for field, label in (
        ("source_evidence", "missing definition evidence"),
        ("implementation_evidence", "missing implementation evidence"),
        ("runtime_evidence", "missing runtime/entrypoint evidence"),
        ("test_evidence", "missing test reference"),
    ):
        if not item.get(field):
            gaps.append(label)

    retired = bool(item.get("retirement_evidence"))
    active = bool(item.get("runtime_evidence"))
    if retired and not active:
        item["status"] = "retire-candidate"
        item["status_reason"] = (
            "explicit retirement marker and no active entrypoint; human migration review required"
        )
        if "human replacement/migration review required" not in gaps:
            gaps.append("human replacement/migration review required")
    elif gaps or retired:
        item["status"] = "fix"
        if retired:
            gaps.append("active entrypoint conflicts with retirement marker")
        item["status_reason"] = "; ".join(dict.fromkeys(gaps))
    else:
        item["status"] = "keep"
        item["status_reason"] = (
            "definition, implementation, entrypoint, and executable-test reference present"
        )
    item["evidence_gaps"] = list(dict.fromkeys(gaps))
    return item


def _component_capabilities(root: Path) -> list[dict[str, Any]]:
    path = root / "components.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
        data = yaml.safe_load(_read(path)) or {}
    except Exception:
        return []
    lines = _read(path).splitlines()
    checker_path = root / "core" / "components.py"
    checker_lines = _read(checker_path).splitlines()
    capabilities = []
    for component in data.get("components", []):
        name = str(component.get("name") or "").strip()
        if not name:
            continue
        line = _line_for(lines, re.compile(rf"^\s*-\s+name:\s*{re.escape(name)}\s*$"))
        check = str(component.get("check") or "")
        check_line = _line_for(
            checker_lines,
            re.compile(rf"^\s*[\"']{re.escape(check)}[\"']\s*:"),
        )
        locator = next(
            (
                f"{key}={component[key]}"
                for key in ("url", "path", "pattern", "label")
                if component.get(key)
            ),
            "manifest-defined",
        )
        test_needles = [
            f'"{name}"',
            f"'{name}'",
            f"name: {name}",
            f"[{name}]",
        ]
        if len(name) >= 5:
            test_needles.append(name)
        if name.endswith("-launchd"):
            service = name.removesuffix("-launchd")
            test_needles.append(f"com.pascal.jarvis.{service}")
        capabilities.append(_capability(
            capability_id=f"component:{name}",
            kind="runtime-component",
            name=name,
            description=str(component.get("note") or f"Runtime component checked by {check}"),
            source_evidence=[_ref(root, path, line, "component manifest entry")],
            implementation_evidence=[
                _ref(root, checker_path, check_line, f"registered {check} checker")
            ] if check_line else [],
            runtime_evidence=[{
                "type": "component-check",
                "entrypoint": "python3 -m core.components --json",
                "probe": check,
                "locator": locator,
                "critical": bool(component.get("critical", False)),
            }],
            test_evidence=_test_references(root, test_needles),
            retirement_evidence=_retirement_references(root, path, start_line=line, end_line=line + 8),
            metadata={
                "check": check,
                "critical": bool(component.get("critical", False)),
                "optional_gates": {
                    key: component[key]
                    for key in ("requires_cmd", "requires_file", "requires_config")
                    if component.get(key)
                },
            },
        ))
    return capabilities


def _heartbeat_capabilities(root: Path) -> list[dict[str, Any]]:
    path = root / "HEARTBEAT.md"
    lines = _read(path).splitlines()
    headings = []
    for index, line in enumerate(lines, start=1):
        match = re.fullmatch(r"### ([a-z0-9][a-z0-9-]*)", line.strip())
        if match:
            headings.append((match.group(1), index))
    capabilities = []
    for position, (name, start_line) in enumerate(headings):
        end_line = headings[position + 1][1] - 1 if position + 1 < len(headings) else len(lines)
        section = lines[start_line:end_line]
        fields: dict[str, str] = {}
        for line in section:
            match = re.match(r"^- (interval|pre|post):\s*(.+?)\s*$", line)
            if match:
                fields[match.group(1)] = match.group(2)
        implementations = []
        missing_hooks = []
        for hook in ("pre", "post"):
            value = fields.get(hook)
            if not value:
                continue
            hook_path = root / value
            if hook_path.is_file():
                implementations.append(_ref(
                    root, hook_path, 1, f"declared {hook} hook"
                ))
            else:
                missing_hooks.append(value)
        needles = [name, name.replace("-", "_")]
        needles.extend(Path(value).name for value in fields.values() if "/" in value)
        capability = _capability(
            capability_id=f"heartbeat:{name}",
            kind="heartbeat-task",
            name=name,
            description=f"Scheduled heartbeat task ({fields.get('interval', 'interval not declared')})",
            source_evidence=[_ref(root, path, start_line, "heartbeat task definition")],
            implementation_evidence=implementations,
            runtime_evidence=[{
                "type": "heartbeat-schedule",
                "entrypoint": "python3 -m core.heartbeat_loop",
                "interval": fields.get("interval", ""),
                "pre": fields.get("pre", ""),
                "post": fields.get("post", ""),
            }] if fields.get("interval") else [],
            test_evidence=_test_references(root, needles),
            retirement_evidence=_retirement_references(
                root, path, start_line=start_line, end_line=end_line
            ),
            metadata={"missing_declared_hooks": missing_hooks},
        )
        if missing_hooks:
            capability["evidence_gaps"].append(
                "missing declared hook: " + ", ".join(missing_hooks)
            )
            capability["status"] = "fix"
            capability["status_reason"] = "; ".join(capability["evidence_gaps"])
        capabilities.append(capability)
    return capabilities


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _core_cli_capabilities(root: Path) -> list[dict[str, Any]]:
    capabilities = []
    for path in sorted((root / "core").glob("*.py")):
        source = _read(path)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        guards = [node for node in ast.walk(tree) if _is_main_guard(node)]
        if not guards:
            continue
        module = f"core.{path.stem}"
        subcommands = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "add_parser" and node.args:
                value = node.args[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    subcommands.append(value.value)
        doc = (ast.get_docstring(tree) or "").strip().splitlines()
        line = min(node.lineno for node in guards)
        tests = _test_references(root, [module, path.stem, f"test_{path.stem}"])
        capabilities.append(_capability(
            capability_id=f"cli:{module}",
            kind="core-cli",
            name=module,
            description=doc[0] if doc else f"Command-line entrypoint for {module}",
            source_evidence=[_ref(root, path, line, "module __main__ guard")],
            implementation_evidence=[_ref(root, path, line, "executable module")],
            runtime_evidence=[{
                "type": "cli",
                "entrypoint": f"python3 -m {module}",
                "subcommands": sorted(set(subcommands)),
            }],
            test_evidence=tests,
            retirement_evidence=_retirement_references(root, path, start_line=1, end_line=40),
            metadata={"subcommands": sorted(set(subcommands))},
        ))
    return capabilities


def _decorated_routes(root: Path) -> list[dict[str, Any]]:
    capabilities = []
    paths = sorted((root / "dashboard").glob("*.py"))
    paths.extend(sorted((root / "dashboard" / "pages").glob("*.py")))
    for path in paths:
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not decorator.args:
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute):
                    continue
                owner = func.value.id if isinstance(func.value, ast.Name) else ""
                route_node = decorator.args[0]
                if not isinstance(route_node, ast.Constant) or not isinstance(route_node.value, str):
                    continue
                route = route_node.value
                if owner == "ui" and func.attr == "page":
                    kind = "dashboard-page"
                    method = "PAGE"
                elif owner == "app" and func.attr in {"get", "post", "put", "delete", "patch"}:
                    kind = "dashboard-api"
                    method = func.attr.upper()
                else:
                    continue
                normalized = re.sub(r"\{[^}]+\}", "{param}", route)
                capability_id = f"route:{method.lower()}:{normalized}"
                route_needles = [f'"{route}"', f"'{route}'", node.name]
                base = route.split("/{", 1)[0]
                if len(base) > 2:
                    route_needles.extend((f'"{base}', f"'{base}"))
                route_needles.extend(ROUTE_TEST_ALIASES.get((method, route), ()))
                capabilities.append(_capability(
                    capability_id=capability_id,
                    kind=kind,
                    name=f"{method} {route}",
                    description=f"Dashboard {method.lower()} route handled by {node.name}",
                    source_evidence=[_ref(root, path, decorator.lineno, "route decorator")],
                    implementation_evidence=[_ref(root, path, node.lineno, f"handler {node.name}")],
                    runtime_evidence=[{
                        "type": "http-route",
                        "entrypoint": route,
                        "method": method,
                    }],
                    test_evidence=_test_references(root, route_needles),
                    retirement_evidence=[],
                    metadata={"handler": node.name, "method": method, "route": route},
                ))
    return capabilities


def _admin_routes(root: Path) -> list[dict[str, Any]]:
    """Discover the stdlib HTTP server routes implemented in admin.py."""
    path = root / "admin.py"
    try:
        tree = ast.parse(_read(path))
    except SyntaxError:
        return []
    capabilities = []
    seen: set[tuple[str, str]] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef) or function.name not in {"do_GET", "do_POST"}:
            continue
        method = function.name.removeprefix("do_")
        for node in ast.walk(function):
            route = ""
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
                if node.left.id != "path" or len(node.comparators) != 1:
                    continue
                value = node.comparators[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    route = value.value
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "path"
                and node.func.attr == "startswith"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                prefix = node.args[0].value
                if prefix != "/api/":
                    route = prefix + "{suffix}"
            if not route or not route.startswith("/"):
                continue
            key = (method, route)
            if key in seen:
                continue
            seen.add(key)
            normalized = route.replace("{suffix}", "{param}")
            needles = [route, f'"{route}"', f"'{route}'"]
            if route.endswith("{suffix}"):
                prefix = route.removesuffix("{suffix}")
                needles.extend((prefix, f'"{prefix}', f"'{prefix}"))
            needles.extend(ADMIN_ROUTE_TEST_ALIASES.get((method, route), ()))
            capabilities.append(_capability(
                capability_id=f"admin-route:{method.lower()}:{normalized}",
                kind="admin-api",
                name=f"{method} {route}",
                description=f"Admin :3456 {method} route handled by {function.name}",
                source_evidence=[_ref(root, path, node.lineno, "admin route branch")],
                implementation_evidence=[
                    _ref(root, path, function.lineno, f"handler {function.name}")
                ],
                runtime_evidence=[{
                    "type": "http-route",
                    "entrypoint": f"http://127.0.0.1:3456{route}",
                    "method": method,
                }],
                test_evidence=_test_references(root, needles),
                retirement_evidence=[],
                metadata={"handler": function.name, "method": method, "route": route},
            ))
    return capabilities


def _matter_commands(root: Path) -> list[dict[str, Any]]:
    path = root / "core" / "matter_bridge.py"
    source = _read(path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    commands: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "aliases" for target in node.targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for value in node.value.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                commands.setdefault(value.value, value.lineno)
    capabilities = []
    for command, line in sorted(commands.items()):
        trigger = f"/matter {command}"
        capabilities.append(_capability(
            capability_id=f"lark:matter:{command}",
            kind="lark-command",
            name=trigger,
            description=f"Deterministic Lark Matter command: {command}",
            source_evidence=[_ref(root, path, line, "canonical Matter command")],
            implementation_evidence=[_ref(root, path, line, f"handler branch for {command}")],
            runtime_evidence=[{
                "type": "lark-command",
                "entrypoint": trigger,
                "owner_only": True,
            }],
            test_evidence=_test_references(root, [trigger, f'command == "{command}"', "handle_lark_command"]),
            retirement_evidence=[],
        ))
    return capabilities


def _model_commands(root: Path) -> list[dict[str, Any]]:
    path = root / "core" / "matter_bridge.py"
    lines = _read(path).splitlines()
    definitions = [
        ("lark:model:status", "/model", "Show the last successful provider/model and route health"),
        ("lark:model:codex", "/model codex", "Prefer Codex for the current private conversation"),
        ("lark:model:auto", "/model auto", "Restore Claude-first routing with automatic fallback"),
    ]
    capabilities = []
    for capability_id, trigger, description in definitions:
        line = _line_for(lines, re.compile(re.escape(trigger)))
        if not line:
            continue
        capabilities.append(_capability(
            capability_id=capability_id,
            kind="lark-command",
            name=trigger,
            description=description,
            source_evidence=[_ref(root, path, line, "Lark model command literal")],
            implementation_evidence=[_ref(root, path, line, "model route handler")],
            runtime_evidence=[{
                "type": "lark-command",
                "entrypoint": trigger,
                "owner_only": True,
            }],
            test_evidence=_test_references(root, [trigger, "handle_lark_command"]),
            retirement_evidence=[],
        ))
    return capabilities


def _bot_inline_commands(root: Path) -> list[dict[str, Any]]:
    path = root / "bot.sh"
    lines = _read(path).splitlines()
    definitions = [
        (
            "lark:memorial:continue",
            "继续发 / 继续发送 / 发剩下的",
            r'\[ "\$content" = "继续发" \]',
            "Continue a clipped memorial without advancing state before delivery",
            ["继续发", "continue-commit"],
        ),
        (
            "lark:eigenflux:publish-confirm",
            "发 / 不发",
            r'\[ "\$content" = "发" \].*\[ "\$content" = "不发" \]',
            "Confirm or reject the pending EigenFlux broadcast",
            ["pending_publish", "不发"],
        ),
        (
            "lark:session:stop",
            "stop / cancel / 结束 / 停止",
            r'\[ "\$content_lower" = "stop" \].*\[ "\$content_lower" = "cancel" \]',
            "Stop the active provider process for the current conversation",
            ["content_lower", "stop", "cancel"],
        ),
    ]
    capabilities = []
    for capability_id, trigger, pattern, description, test_needles in definitions:
        line = _line_for(lines, re.compile(pattern))
        if not line:
            continue
        capabilities.append(_capability(
            capability_id=capability_id,
            kind="lark-command",
            name=trigger,
            description=description,
            source_evidence=[_ref(root, path, line, "owner-only inline command gate")],
            implementation_evidence=[_ref(root, path, line, "inline bot command handler")],
            runtime_evidence=[{
                "type": "lark-command",
                "entrypoint": trigger,
                "owner_only": True,
            }],
            test_evidence=_test_references(root, test_needles),
            retirement_evidence=[],
        ))
    return capabilities


def validate_inventory(inventory: dict[str, Any]) -> list[str]:
    errors = []
    if inventory.get("schema_version") != SCHEMA_VERSION:
        errors.append("unexpected schema_version")
    capabilities = inventory.get("capabilities")
    if not isinstance(capabilities, list):
        return errors + ["capabilities must be a list"]
    ids = [item.get("id") for item in capabilities]
    duplicates = sorted(key for key, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append("duplicate capability ids: " + ", ".join(duplicates))
    for item in capabilities:
        prefix = str(item.get("id") or "<missing-id>")
        if not item.get("owner"):
            errors.append(f"{prefix}: missing owner")
        if item.get("status") not in STATUSES:
            errors.append(f"{prefix}: invalid status")
        if not item.get("source_evidence"):
            errors.append(f"{prefix}: missing source evidence")
        if item.get("status") == "keep" and item.get("evidence_gaps"):
            errors.append(f"{prefix}: keep capability has evidence gaps")
        if item.get("status") == "retire-candidate":
            if not item.get("retirement_evidence"):
                errors.append(f"{prefix}: retirement candidate has no retirement evidence")
            if item.get("runtime_evidence"):
                errors.append(f"{prefix}: retirement candidate still has an active entrypoint")
    return errors


def build_inventory(root: Path) -> dict[str, Any]:
    root = root.resolve()
    capabilities = []
    capabilities.extend(_component_capabilities(root))
    capabilities.extend(_heartbeat_capabilities(root))
    capabilities.extend(_core_cli_capabilities(root))
    capabilities.extend(_decorated_routes(root))
    capabilities.extend(_admin_routes(root))
    capabilities.extend(_matter_commands(root))
    capabilities.extend(_model_commands(root))
    capabilities.extend(_bot_inline_commands(root))
    capabilities.sort(key=lambda item: (
        KIND_ORDER.get(item["kind"], 99), item["id"]
    ))
    by_kind = Counter(item["kind"] for item in capabilities)
    by_status = Counter(item["status"] for item in capabilities)
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "generator": "scripts/capability_inventory.py",
        "fact_sources": [
            "components.yaml",
            "HEARTBEAT.md",
            "core/*.py __main__ guards",
            "dashboard route decorators",
            "admin.py HTTP route branches",
            "core/matter_bridge.py command tables",
            "bot.sh owner-only inline command gates",
        ],
        "status_requirements": STATUS_REQUIREMENTS,
        "summary": {
            "total": len(capabilities),
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": {status: by_status.get(status, 0) for status in STATUSES},
        },
        "resolved_evidence_audit": RESOLVED_EVIDENCE_AUDIT,
        "capabilities": capabilities,
    }
    errors = validate_inventory(inventory)
    if errors:
        raise ValueError("invalid capability inventory:\n- " + "\n- ".join(errors))
    return inventory


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def _evidence_label(items: list[dict[str, Any]]) -> str:
    if not items:
        return "-"
    return ", ".join(
        f"`{item.get('path')}:{item.get('line')}`"
        if item.get("path")
        else _escape(item.get("entrypoint") or item.get("type"))
        for item in items[:3]
    )


def render_markdown(inventory: dict[str, Any]) -> str:
    summary = inventory["summary"]
    lines = [
        "# Jarvis Capability Inventory",
        "",
        "This file is generated by `scripts/capability_inventory.py`. It is an evidence map,",
        "not a product verdict: `fix` means static evidence is missing or contradictory;",
        "`retire-candidate` never authorizes deletion and always requires human migration review.",
        "An executable-test reference proves that a `test_*` body or parametrization",
        "mentions the capability, not that every behavior is covered.",
        "",
        "## Check",
        "",
        "```bash",
        "python3 scripts/capability_inventory.py --check-doc docs/capability_inventory.md",
        "python3 scripts/capability_inventory.py --format json > /tmp/jarvis-capabilities.json",
        "```",
        "",
        "## Decision Rules",
        "",
    ]
    for status in STATUSES:
        requirements = "; ".join(inventory["status_requirements"][status])
        lines.append(f"- **{status}**: {requirements}.")
    lines.extend([
        "",
        "## Summary",
        "",
        f"- Total: **{summary['total']}**",
        "- Status: " + ", ".join(
            f"{status} **{summary['by_status'][status]}**" for status in STATUSES
        ),
        "- Kinds: " + ", ".join(
            f"{kind} **{count}**" for kind, count in summary["by_kind"].items()
        ),
        "",
        "## Inventory",
        "",
        "| ID | Kind | Status | Definition | Implementation | Runtime / entrypoint | Tests | Evidence gap |",
        "|---|---|---|---|---|---|---|---|",
    ])
    for item in inventory["capabilities"]:
        runtime = ", ".join(
            _escape(evidence.get("entrypoint") or evidence.get("probe") or evidence.get("type"))
            for evidence in item["runtime_evidence"][:2]
        ) or "-"
        gaps = "; ".join(item["evidence_gaps"]) or "-"
        lines.append(
            "| " + " | ".join((
                f"`{_escape(item['id'])}`",
                _escape(item["kind"]),
                f"**{_escape(item['status'])}**",
                _evidence_label(item["source_evidence"]),
                _evidence_label(item["implementation_evidence"]),
                runtime,
                _evidence_label(item["test_evidence"]),
                _escape(gaps),
            )) + " |"
        )
    lines.extend(["", "## Resolved Evidence Audit", ""])
    by_id = {item["id"]: item for item in inventory["capabilities"]}
    for capability_id, resolution in inventory["resolved_evidence_audit"].items():
        item = by_id.get(capability_id, {})
        status = item.get("status", "missing")
        lines.append(f"- `{capability_id}` — **{status}**: {resolution}.")
    lines.extend([
        "",
        "## Review Contract",
        "",
        "- Keep capabilities remain in regression scope; missing runtime health is handled by their existing probes.",
        "- Fix capabilities need the named evidence gap closed before their status changes.",
        "- Retire candidates are review prompts only. Deletion requires replacement/migration evidence and a separate PR.",
        "- Regenerate this file whenever a component, heartbeat task, CLI, dashboard route, or Lark command changes.",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: script parent)",
    )
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--write-doc", type=Path)
    output.add_argument("--check-doc", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    inventory = build_inventory(args.root)
    markdown = render_markdown(inventory)
    if args.write_doc:
        path = args.write_doc if args.write_doc.is_absolute() else args.root / args.write_doc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        print(path)
        return 0
    if args.check_doc:
        path = args.check_doc if args.check_doc.is_absolute() else args.root / args.check_doc
        actual = _read(path)
        if actual != markdown:
            print(
                f"capability inventory drift: regenerate {path} with --write-doc",
                file=sys.stderr,
            )
            return 1
        print(f"capability inventory is current: {path}")
        return 0
    if args.format == "markdown":
        print(markdown, end="")
    else:
        json.dump(inventory, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
