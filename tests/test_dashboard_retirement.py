"""The retired NiceGUI dashboard (:3457) cannot quietly become active again.

Mirror of tests/test_mobile_retirement.py (REQ-120). Frozen 2026-08-07 by
owner verdict, retired 2026-08-21 after the audit found 8.3k LOC with ~zero
consumption in 30 days. Archive duty lives in the morning-anchor batch line
and the Admin console (:3456); the shared SQLite layer moved to core/db.py;
the code archive is git history. A revival needs a new design and a new
supervision entry, not a re-import.
"""

import re
from pathlib import Path


ROOT = Path(__file__).parent.parent

LIVE_CODE = (
    "admin.py",
    "daemon.py",
    "bot.sh",
    "restart.sh",
    "setup.sh",
)
LIVE_TREES = ("core", "tasks", "handlers", "plugins", "scripts", "sources")


def _live_python_files():
    for tree in LIVE_TREES:
        yield from (ROOT / tree).glob("**/*.py")
    for name in ("admin.py", "daemon.py"):
        yield ROOT / name


def test_dashboard_package_and_launchd_template_are_deleted():
    assert not (ROOT / "dashboard").exists()
    assert not (
        ROOT / "scripts" / "launchd" / "com.pascal.jarvis.dashboard.plist"
    ).exists()


def test_component_manifest_has_no_dashboard_entry():
    manifest = (ROOT / "components.yaml").read_text(encoding="utf-8")
    live = "\n".join(
        line for line in manifest.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "name: dashboard" not in live
    assert "3457" not in live
    assert "com.pascal.jarvis.dashboard" not in live


def test_no_live_code_imports_the_dashboard_package():
    import_re = re.compile(
        r"^\s*(from\s+dashboard[.\s]|import\s+dashboard\b)", re.M)
    for path in _live_python_files():
        assert not import_re.search(path.read_text(encoding="utf-8")), (
            f"{path} still imports the retired dashboard package"
        )


def test_no_live_surface_references_port_3457_or_the_launchd_job():
    for name in LIVE_CODE:
        text = (ROOT / name).read_text(encoding="utf-8")
        live = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "3457" not in live, f"{name} still references :3457"
        assert "com.pascal.jarvis.dashboard" not in live, (
            f"{name} still references the retired launchd job"
        )


def test_daemon_no_longer_probes_or_recovers_the_dashboard():
    # The recovery-branch contract lives in tests/test_daemon_regressions.py::
    # test_retired_dashboard_recovery_branch_stays_deleted.
    import daemon as daemon_mod

    assert "dashboard :3457" not in daemon_mod._COMPONENT_LABELS
    assert "dashboard" not in daemon_mod._MANIFEST_COVERED


def test_capability_inventory_carries_the_retirement_trace():
    from scripts.capability_inventory import (
        KIND_ORDER, RETIRED_SURFACES, build_inventory)

    assert not any(kind.startswith("dashboard") for kind in KIND_ORDER)
    assert any("dashboard :3457" in key for key in RETIRED_SURFACES)

    inventory = build_inventory(ROOT)
    assert not any(
        item["kind"].startswith("dashboard")
        for item in inventory["capabilities"]
    )
    assert any(
        "dashboard :3457" in key for key in inventory["retired_surfaces"])

    doc = (ROOT / "docs" / "capability_inventory.md").read_text(
        encoding="utf-8")
    assert "dashboard :3457" in doc  # explicit trace, not silent disappearance


def test_deploy_watchlist_no_longer_tracks_the_dashboard_tree():
    from core.deploy import CODE_PATHS, RUNTIME_PATHS

    assert "dashboard" not in RUNTIME_PATHS
    assert "dashboard" not in CODE_PATHS


def test_legacy_tables_remain_for_non_destructive_retention():
    from core.db import get_db

    names = {
        row[0] for row in get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "scheduled_tasks",
        "bookmarks",
        "engagement_events",
        "agent_log",
    } <= names
