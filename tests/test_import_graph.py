import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "import_graph.py"
SPEC = importlib.util.spec_from_file_location("jarvis_import_graph", SCRIPT)
assert SPEC and SPEC.loader
import_graph = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = import_graph
SPEC.loader.exec_module(import_graph)


def _write(root: Path, relative: str, source: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _fixture_repo(tmp_path: Path) -> Path:
    _write(tmp_path, "pkg/__init__.py", "from . import alpha\n")
    _write(
        tmp_path,
        "pkg/alpha.py",
        "import os\nfrom pkg import beta\nfrom .sub import gamma\n",
    )
    _write(tmp_path, "pkg/beta.py", "from pkg.sub.gamma import VALUE\n")
    _write(tmp_path, "pkg/sub/__init__.py")
    _write(tmp_path, "pkg/sub/gamma.py", "from .. import beta\nVALUE = 1\n")
    return tmp_path


def test_build_graph_resolves_absolute_relative_and_package_imports(tmp_path):
    root = _fixture_repo(tmp_path)

    graph = import_graph.build_graph(root, ["pkg"])

    assert graph.outgoing["pkg"] == frozenset({"pkg.alpha"})
    assert graph.outgoing["pkg.alpha"] == frozenset({
        "pkg.beta",
        "pkg.sub.gamma",
    })
    assert graph.outgoing["pkg.beta"] == frozenset({"pkg.sub.gamma"})
    assert graph.outgoing["pkg.sub.gamma"] == frozenset({"pkg.beta"})
    assert "os" not in graph.outgoing["pkg.alpha"]


def test_counts_use_unique_neighbors_for_bidirectional_adjacency(tmp_path):
    root = _fixture_repo(tmp_path)
    graph = import_graph.build_graph(root, ["pkg"])
    counts = {row.module: row for row in graph.counts()}

    assert counts["pkg.beta"].fan_in == 2
    assert counts["pkg.beta"].fan_out == 1
    assert counts["pkg.beta"].adjacency == 2
    assert counts["pkg.sub"].adjacency == 0


def test_json_and_focused_mermaid_reports_are_machine_readable(tmp_path):
    root = _fixture_repo(tmp_path)
    graph = import_graph.build_graph(root, ["pkg"])

    payload = json.loads(import_graph.render_json(graph))
    assert payload["module_count"] == 5
    assert payload["edge_count"] == 5
    assert payload["direct_cycles"] == [["pkg.beta", "pkg.sub.gamma"]]
    alpha = next(row for row in payload["modules"]
                 if row["module"] == "pkg.alpha")
    assert alpha["fan_out"] == 2

    mermaid = import_graph.render_mermaid(graph, ["pkg.alpha"])
    assert mermaid.startswith("flowchart LR\n")
    assert "pkg.alpha<br/>" in mermaid
    assert "pkg.beta<br/>" in mermaid
    assert "pkg.sub.gamma<br/>" in mermaid
    assert "pkg.sub<br/>" not in mermaid


def test_cli_warns_and_can_fail_when_threshold_is_exceeded(tmp_path):
    root = _fixture_repo(tmp_path)
    command = [
        sys.executable,
        str(SCRIPT),
        "pkg",
        "--root",
        str(root),
        "--metric",
        "fan-out",
        "--threshold",
        "1",
    ]

    warning = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert warning.returncode == 0
    assert "pkg.alpha=2" in warning.stderr
    assert "ranked by fan-out" in warning.stdout

    failure = subprocess.run(
        [*command, "--fail-on-threshold"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert failure.returncode == 2
    assert "WARNING:" in failure.stderr


def test_graph_fails_closed_on_syntax_error(tmp_path):
    _write(tmp_path, "pkg/__init__.py")
    _write(tmp_path, "pkg/broken.py", "def nope(:\n")

    try:
        import_graph.build_graph(tmp_path, ["pkg"])
    except import_graph.GraphError as exc:
        assert "cannot parse" in str(exc)
        assert "broken.py" in str(exc)
    else:
        raise AssertionError("syntax errors must not produce a partial graph")


def test_cli_direct_cycle_budget_fails_on_regression(tmp_path):
    root = _fixture_repo(tmp_path)
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT), "pkg", "--root", str(root),
            "--max-direct-cycles", "0",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 2
    assert "pkg.beta<->pkg.sub.gamma" in result.stderr


def test_core_direct_cycle_budget_does_not_grow():
    graph = import_graph.build_graph(ROOT, ["core"])
    cycles = set(graph.direct_cycles())

    # Existing deferred-import debt is explicit. New cycles fail CI until
    # they are removed or consciously reviewed into this bounded baseline.
    allowed = {
        ("core.actions", "core.memorial"),
        ("core.attention_roi", "core.memorial"),
        ("core.companion", "core.memorial"),
        ("core.continuity", "core.matters"),
        ("core.continuity", "core.memorial"),
        ("core.delegation_projection", "core.delegations"),
        ("core.heartbeat_loop", "core.memorial"),
        ("core.jobs", "core.matters"),
        ("core.matter_router", "core.memorial"),
        ("core.matters", "core.memorial"),
        ("core.memorial", "core.memorial_thread"),
    }
    assert cycles <= allowed
