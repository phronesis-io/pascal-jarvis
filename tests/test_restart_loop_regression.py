"""Regression tests for the 2026-06-04 restart-loop incident.

Incident: Pascal said「重启吧」. The Claude handling it ran restart.sh from
WORK_DIR (the repo's parent), which has no `core/` package. restart.sh launched
bot.sh WITHOUT cd'ing to JARVIS_DIR first, so the new bot inherited CWD=WORK_DIR.
Every `python3 -m core.X` helper then died with `ModuleNotFoundError: No module
named 'core'`, taking heartbeat + ef-stream down and spiralling into a restart
loop. Each restart SIGTERM-killed the in-flight Claude (exit 143); the 143 branch
blindly told the user「说继续即可接着干」, and saying 继续 just re-ran the restart.
Chinese「结束」could not stop it (only English stop/cancel was recognized).

These are static-source guards — the fixes live in shell, so we assert on the
script text the way test_daemon_regressions.py asserts on daemon constants.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BOT_SH = (ROOT / "bot.sh").read_text()
RESTART_SH = (ROOT / "restart.sh").read_text()
HEARTBEAT_LOOP = (ROOT / "core" / "heartbeat_loop.py").read_text()
LARK_SIDECAR = (ROOT / "scripts" / "lark_event_sidecar.py").read_text()


def _existing_work_dir(tmp_path: Path) -> str:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return str(work)


def test_bot_anchors_cwd_to_jarvis_dir():
    """RC: bot.sh must cd into JARVIS_DIR so `python3 -m core.X` always resolves.

    Without this, a launch/exec from any other directory brings the bot up broken
    (ModuleNotFoundError: No module named 'core') and starts a restart loop.
    """
    assert 'cd "$JARVIS_DIR"' in BOT_SH, "bot.sh must cd to JARVIS_DIR at startup"
    # The cd must come before the bg helpers are spawned, else it's useless.
    cd_idx = BOT_SH.index('cd "$JARVIS_DIR"')
    helper_idx = BOT_SH.index("python3 -m core.heartbeat_loop")
    assert cd_idx < helper_idx, "cd to JARVIS_DIR must precede `python3 -m core.*` helpers"


def test_restart_sh_cds_before_launching_bot():
    """RC defense-in-depth: restart.sh must cd to JARVIS_DIR before launching bot.sh."""
    launch = '["bash", f"{jarvis_dir}/bot.sh"]'
    assert launch in RESTART_SH
    prefix = RESTART_SH[: RESTART_SH.index(launch)]
    # The cd must be in start_bot(), i.e. the closest cd before the launch line.
    assert 'cd "$JARVIS_DIR"' in prefix, "restart.sh must cd to JARVIS_DIR before launching bot.sh"
    assert "start_new_session=True" in RESTART_SH


def test_restart_sh_settles_before_clearing_deploy_guard():
    """restart.sh must own the post-start verdict instead of racing daemon checks."""
    assert "settle_bot()" in RESTART_SH
    assert "python3 -m core.components --critical" in RESTART_SH
    assert "Settling bot for ${seconds}s" in RESTART_SH
    full = RESTART_SH[
        RESTART_SH.index("governed_deploy()"):
        RESTART_SH.index('case "${1:-}"')
    ]
    assert full.index("_set_deploy_guard") < full.index("kill_bot")
    assert full.index("start_bot") < full.index("settle_bot")


def test_full_restart_refreshes_definitions_and_verifies_all_runtimes():
    """A full release cannot leave independently supervised residents on old
    code — and retired surfaces are torn down, never refreshed."""
    # Mobile gateway retired 2026-08-11 (REQ-120); dashboard retired
    # 2026-08-21. Their labels may appear ONLY inside the RETIRED_LABELS
    # teardown block — an installed KeepAlive job whose package is deleted
    # crash-loops with no supervision surface left to see it, so the
    # governed deploy removes leftovers in code, not in PR prose.
    retired_block = RESTART_SH[
        RESTART_SH.index("RETIRED_LABELS=("):
        RESTART_SH.index("refresh_launchd_definitions()")
    ]
    for label in ("com.pascal.jarvis.dashboard",
                  "com.pascal.jarvis.mobile-gateway"):
        assert label in retired_block
        assert RESTART_SH.count(label) == retired_block.count(label), (
            f"{label} may only appear in the RETIRED_LABELS teardown block")
    assert "launchctl bootout" in retired_block
    assert "restart_user_surfaces" not in RESTART_SH
    assert "restart_launchd_surface" not in RESTART_SH
    assert "verify_full_runtime()" in RESTART_SH
    assert "refresh_launchd_definitions()" in RESTART_SH

    refresh = RESTART_SH[
        RESTART_SH.index("refresh_launchd_definitions()"):
        RESTART_SH.index("LAUNCHD_PROBE_DETAIL=")
    ]
    assert "remove_retired_launchd_jobs" in refresh

    full = RESTART_SH[
        RESTART_SH.index("governed_deploy()"):
        RESTART_SH.index('case "${1:-}"')
    ]
    assert full.index("\n  confirm_restart") < full.index(
        "\n  refresh_launchd_definitions"
    )
    assert full.index("\n  refresh_launchd_definitions") < full.index(
        "\n  kill_bot"
    )
    assert full.index("start_bot") < full.index("settle_bot")
    assert full.index("settle_bot") < full.index("verify_full_runtime")
    # Retired components' dead runtime rows would fail every unfiltered
    # verify (the --runtime gate) forever; the deploy clears them itself.
    assert "core.deploy deregister dashboard mobile-gateway" in full
    assert full.index("deregister dashboard") < full.index(
        "verify_full_runtime")
    assert full.index("verify_full_runtime") < full.index(
        "record_release_receipt governed")

    verify = RESTART_SH[
        RESTART_SH.index("verify_full_runtime()"):
        RESTART_SH.index("# ── Main")
    ]
    assert "python3 -m core.deploy verify" in verify
    assert 'verify_args+=(--require "$component")' in verify
    assert '"${verify_args[@]}"' in verify


def test_default_release_and_full_alias_share_the_complete_deploy_path():
    """A governed code release must never leave launchd residents on old code."""
    case = RESTART_SH[RESTART_SH.index('case "${1:-}"'):]
    assert '--full|-f)\n    governed_deploy "Governed Full-Runtime Deploy"' in case
    assert '\"\")\n    governed_deploy "Governed Full-Runtime Deploy"' in case
    unknown = case[case.rindex("  *)"):]
    assert "Unknown option" in unknown
    assert "governed_deploy" not in unknown


def test_every_successful_restart_records_joined_release_evidence():
    recorder = RESTART_SH[
        RESTART_SH.index("record_release_receipt()"):
        RESTART_SH.index("verify_full_runtime()")
    ]
    assert "core.deploy receipt" in recorder
    assert "/tmp/jarvis_release_gate.json" in recorder
    assert "--mode" in recorder

    runtime_case = RESTART_SH[RESTART_SH.index('case "${1:-}"'):]
    assert "record_release_receipt runtime" in runtime_case


def test_admin_restart_handoff_uses_same_revision_runtime_path():
    """The admin restart button is operational, not an implicit code release."""
    handoff = '"--runtime", "--yes"'
    assert handoff in HEARTBEAT_LOOP
    assert '["bash", str(jd / "restart.sh"), "--yes"]' not in HEARTBEAT_LOOP


def test_lark_sidecar_config_restart_does_not_request_a_code_deploy():
    assert "./restart.sh --runtime --yes" in LARK_SIDECAR


def test_full_restart_skips_launchd_work_when_launchctl_is_unavailable():
    """Linux/manual installs still get the daemon + bot full-restart path."""
    refresh = RESTART_SH[
        RESTART_SH.index("refresh_launchd_definitions()"):
        RESTART_SH.index("LAUNCHD_PROBE_DETAIL=")
    ]
    guard = "if ! command -v launchctl >/dev/null 2>&1; then"
    assert guard in refresh
    assert "definition refresh skipped" in refresh


def test_launchd_installer_refreshes_only_requested_definition(tmp_path):
    """A release can reload updated UI definitions without touching other jobs."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    stale = destination / "com.pascal.jarvis.daemon.plist"
    stale.write_text("stale definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
[ "$1" != "print" ] || echo 'state = running'
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    installed = stale.read_text(encoding="utf-8")
    assert installed != "stale definition\n"
    assert "__JARVIS_DIR__" not in installed
    assert str(ROOT) in installed
    assert not (destination / "com.pascal.jarvis.caffeinate.plist").exists()
    assert not (
        destination / "com.jarvis.session-backup.plist"
    ).exists()
    calls = launchctl_log.read_text(encoding="utf-8")
    assert "bootout " in calls
    assert "com.pascal.jarvis.daemon" in calls
    assert "bootstrap " in calls


def test_launchd_installer_removes_retired_jobs_and_definitions(tmp_path):
    """2026-08-21: a leftover dashboard/mobile-gateway KeepAlive definition
    for a deleted package would crash-loop every ~10s. Any install removes
    retired definitions (and boots out their jobs) before installing what
    should exist — collaborator machines included."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    for retired in ("com.pascal.jarvis.dashboard",
                    "com.pascal.jarvis.mobile-gateway"):
        (destination / f"{retired}.plist").write_text(
            "retired leftover\n", encoding="utf-8")
    # The fake launchctl reports every job as loaded, so the requested
    # definition must pre-exist or the installer refuses (no rollback copy).
    daemon_plist = destination / "com.pascal.jarvis.daemon.plist"
    daemon_plist.write_text("previous daemon definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
[ "$1" != "print" ] || echo 'state = running'
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    calls = launchctl_log.read_text(encoding="utf-8")
    for retired in ("com.pascal.jarvis.dashboard",
                    "com.pascal.jarvis.mobile-gateway"):
        assert not (destination / f"{retired}.plist").exists()
        assert f"bootout gui/{os.getuid()}/{retired}" in calls
        assert f"removed retired launchd service {retired}" in result.stdout
    # The requested definition still installs normally.
    assert daemon_plist.read_text(
        encoding="utf-8") != "previous daemon definition\n"


def test_launchd_installer_retries_transient_bootstrap_eio(tmp_path):
    """macOS can return EIO briefly after bootout; retry only that signature."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    if grep -q '^loaded$' "$SERVICE_STATE"; then
      echo 'state = running'
      exit 0
    fi
    echo 'Could not find service' >&2
    exit 1
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    count=0
    [ ! -f "$BOOTSTRAP_COUNT" ] || count=$(cat "$BOOTSTRAP_COUNT")
    count=$((count + 1))
    printf '%s\n' "$count" > "$BOOTSTRAP_COUNT"
    if [ "$count" -eq 1 ]; then
      echo 'Bootstrap failed: 5: Input/output error' >&2
      exit 5
    fi
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SERVICE_STATE": str(state),
        "BOOTSTRAP_COUNT": str(bootstrap_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_BOOTSTRAP_INTERVAL": "0",
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert bootstrap_count.read_text(encoding="utf-8").strip() == "2"
    assert installed.read_text(encoding="utf-8") != "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"


def test_launchd_installer_does_not_retry_other_error_codes(tmp_path):
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    grep -q '^loaded$' "$SERVICE_STATE" || exit 1
    echo 'state = running'
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    count=0
    [ ! -f "$BOOTSTRAP_COUNT" ] || count=$(cat "$BOOTSTRAP_COUNT")
    count=$((count + 1))
    printf '%s\n' "$count" > "$BOOTSTRAP_COUNT"
    if [ "$count" -eq 1 ]; then
      echo 'Bootstrap failed: 55: unsupported operation' >&2
      exit 55
    fi
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SERVICE_STATE": str(state),
        "BOOTSTRAP_COUNT": str(bootstrap_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_BOOTSTRAP_INTERVAL": "0",
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert bootstrap_count.read_text(encoding="utf-8").strip() == "2"
    assert installed.read_text(encoding="utf-8") == "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"


def test_launchd_installer_rejects_unknown_requested_definition(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.does-not-exist",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unknown launchd service" in result.stderr


def test_launchd_installer_allows_an_empty_optional_selection(tmp_path):
    """Bash 3.2 + set -u must not reject a fully filtered optional batch."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    stale = destination / "com.pascal.jarvis.taskline.plist"
    stale.write_text("stale optional definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "WORK_DIR": _existing_work_dir(tmp_path),
        "TASKLINE_DIR": str(tmp_path / "taskline-not-installed"),
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.taskline",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "optional Taskline binary not installed" in result.stdout
    assert stale.exists()


def test_launchd_installer_restores_loaded_job_after_bootstrap_failure(
    tmp_path,
):
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    if grep -q '^loaded$' "$SERVICE_STATE"; then
      echo 'state = running'
      exit 0
    fi
    exit 1
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    count=0
    [ ! -f "$BOOTSTRAP_COUNT" ] || count=$(cat "$BOOTSTRAP_COUNT")
    count=$((count + 1))
    printf '%s\n' "$count" > "$BOOTSTRAP_COUNT"
    if [ "$count" -eq 1 ]; then
      exit 5
    fi
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "SERVICE_STATE": str(state),
        "BOOTSTRAP_COUNT": str(bootstrap_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert installed.read_text(encoding="utf-8") == "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"
    assert "previous state restored" in result.stderr
    calls = launchctl_log.read_text(encoding="utf-8")
    assert calls.count("bootstrap ") == 2


def test_launchd_installer_retries_transient_eio_during_rollback(tmp_path):
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    grep -q '^loaded$' "$SERVICE_STATE" || exit 1
    echo 'state = running'
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    count=0
    [ ! -f "$BOOTSTRAP_COUNT" ] || count=$(cat "$BOOTSTRAP_COUNT")
    count=$((count + 1))
    printf '%s\n' "$count" > "$BOOTSTRAP_COUNT"
    case "$count" in
      1)
        echo 'Bootstrap failed: 9: Operation not permitted' >&2
        exit 9
        ;;
      2)
        echo 'Bootstrap failed: 5: Input/output error' >&2
        exit 5
        ;;
    esac
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SERVICE_STATE": str(state),
        "BOOTSTRAP_COUNT": str(bootstrap_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_BOOTSTRAP_INTERVAL": "0",
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert bootstrap_count.read_text(encoding="utf-8").strip() == "3"
    assert installed.read_text(encoding="utf-8") == "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"
    assert "previous state restored" in result.stderr


def test_launchd_installer_reports_an_incomplete_file_recovery(tmp_path):
    """A failed rollback move must never be reported as restored."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    if grep -q '^loaded$' "$SERVICE_STATE"; then
      echo 'state = running'
      exit 0
    fi
    exit 1
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    count=0
    [ ! -f "$BOOTSTRAP_COUNT" ] || count=$(cat "$BOOTSTRAP_COUNT")
    count=$((count + 1))
    printf '%s\n' "$count" > "$BOOTSTRAP_COUNT"
    if [ "$count" -eq 1 ]; then
      exit 5
    fi
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    move_count = tmp_path / "move-count"
    move_wrapper = bin_dir / "mv"
    move_wrapper.write_text(
        """#!/bin/sh
count=0
[ ! -f "$MOVE_COUNT" ] || count=$(cat "$MOVE_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$MOVE_COUNT"
[ "$count" -eq 1 ] || exit 9
exec /bin/mv "$@"
""",
        encoding="utf-8",
    )
    move_wrapper.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SERVICE_STATE": str(state),
        "BOOTSTRAP_COUNT": str(bootstrap_count),
        "MOVE_COUNT": str(move_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "batch recovery was incomplete" in result.stderr
    assert "previous state restored" not in result.stderr
    assert installed.read_text(encoding="utf-8") != "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"
    assert list(destination.glob("*.rollback.*"))


def test_launchd_installer_fails_closed_on_ambiguous_probe(tmp_path):
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / "com.pascal.jarvis.daemon.plist"
    installed.write_text("previous definition\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
echo 'Operation not permitted' >&2
exit 1
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "WORK_DIR": _existing_work_dir(tmp_path),
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            "com.pascal.jarvis.daemon",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert installed.read_text(encoding="utf-8") == "previous definition\n"
    assert "cannot inspect" in result.stderr
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 1
    assert calls[0].startswith("print ")


def test_launchd_installer_rolls_back_the_entire_selected_batch(tmp_path):
    """A later bootstrap failure must also undo earlier successful updates."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    labels = (
        "com.pascal.jarvis.caffeinate",
        "com.pascal.jarvis.daemon",
    )
    for label in labels:
        (destination / f"{label}.plist").write_text(
            f"previous definition for {label}\n",
            encoding="utf-8",
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for label in labels:
        (state_dir / label).write_text("loaded\n", encoding="utf-8")
    fail_marker = tmp_path / "daemon-failed-once"
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    label=${2##*/}
    if grep -q '^loaded$' "$STATE_DIR/$label"; then
      echo 'state = running'
      exit 0
    fi
    exit 1
    ;;
  bootout)
    label=${2##*/}
    printf 'unloaded\n' > "$STATE_DIR/$label"
    ;;
  bootstrap)
    label=$(basename "$3" .plist)
    if [ "$label" = "com.pascal.jarvis.daemon" ] \
        && [ ! -f "$FAIL_MARKER" ]; then
      : > "$FAIL_MARKER"
      exit 5
    fi
    printf 'loaded\n' > "$STATE_DIR/$label"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "STATE_DIR": str(state_dir),
        "FAIL_MARKER": str(fail_marker),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            *labels,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "previous state restored" in result.stderr
    assert "failed to install com.pascal.jarvis.daemon" in result.stderr
    for label in labels:
        assert (destination / f"{label}.plist").read_text(
            encoding="utf-8"
        ) == f"previous definition for {label}\n"
        assert (state_dir / label).read_text(encoding="utf-8") == "loaded\n"
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    for label in labels:
        bootstraps = [
            call for call in calls
            if call.startswith("bootstrap ") and f"{label}.plist" in call
        ]
        assert len(bootstraps) == 2


def test_launchd_installer_rolls_back_after_plist_validation_failure(
    tmp_path,
):
    """A later invalid rendered plist retains batch atomicity."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    labels = (
        "com.pascal.jarvis.caffeinate",
        "com.pascal.jarvis.daemon",
    )
    for label in labels:
        (destination / f"{label}.plist").write_text(
            f"previous definition for {label}\n",
            encoding="utf-8",
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    for label in labels:
        (state_dir / label).write_text("loaded\n", encoding="utf-8")
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    label=${2##*/}
    if grep -q '^loaded$' "$STATE_DIR/$label"; then
      echo 'state = running'
      exit 0
    fi
    exit 1
    ;;
  bootout)
    label=${2##*/}
    printf 'unloaded\n' > "$STATE_DIR/$label"
    ;;
  bootstrap)
    label=$(basename "$3" .plist)
    printf 'loaded\n' > "$STATE_DIR/$label"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    plutil_count = tmp_path / "plutil-count"
    plutil_wrapper = bin_dir / "plutil"
    plutil_wrapper.write_text(
        """#!/bin/sh
count=0
[ ! -f "$PLUTIL_COUNT" ] || count=$(cat "$PLUTIL_COUNT")
count=$((count + 1))
printf '%s\n' "$count" > "$PLUTIL_COUNT"
[ "$count" -lt 2 ] || exit 7
exit 0
""",
        encoding="utf-8",
    )
    plutil_wrapper.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STATE_DIR": str(state_dir),
        "PLUTIL_COUNT": str(plutil_count),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            *labels,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "previous state restored" in result.stderr
    for label in labels:
        assert (destination / f"{label}.plist").read_text(
            encoding="utf-8"
        ) == f"previous definition for {label}\n"
        assert (state_dir / label).read_text(encoding="utf-8") == "loaded\n"
    assert not list(destination.glob("*.tmp"))
    assert not list(destination.glob("*.rollback.*"))


def test_launchd_installer_bootstraps_an_up_to_date_unloaded_job(tmp_path):
    """Matching plist bytes do not mean the supervised process is running."""
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    label = "com.pascal.jarvis.daemon"

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("unloaded\n", encoding="utf-8")
    launchctl_log = tmp_path / "launchctl.log"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$LAUNCHCTL_LOG"
case "$1" in
  print)
    if grep -q '^loaded$' "$SERVICE_STATE"; then
      echo 'state = running'
      exit 0
    fi
    echo 'Could not find service' >&2
    exit 1
    ;;
  bootout)
    printf 'unloaded\n' > "$SERVICE_STATE"
    ;;
  bootstrap)
    printf 'loaded\n' > "$SERVICE_STATE"
    ;;
esac
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LAUNCHCTL_LOG": str(launchctl_log),
        "SERVICE_STATE": str(state),
        "WORK_DIR": _existing_work_dir(tmp_path),
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }
    command = [
        "bash",
        str(ROOT / "scripts" / "launchd" / "install.sh"),
        label,
    ]

    subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    installed = destination / f"{label}.plist"
    current_definition = installed.read_text(encoding="utf-8")

    state.write_text("unloaded\n", encoding="utf-8")
    launchctl_log.write_text("", encoding="utf-8")
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert installed.read_text(encoding="utf-8") == current_definition
    assert state.read_text(encoding="utf-8") == "loaded\n"
    assert "up-to-date" in result.stdout
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("bootstrap ") for call in calls) == 1
    assert not any(call.startswith("bootout ") for call in calls)

    launchctl_log.write_text("", encoding="utf-8")
    idempotent = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "up-to-date" in idempotent.stdout
    calls = launchctl_log.read_text(encoding="utf-8").splitlines()
    assert not any(call.startswith("bootstrap ") for call in calls)
    assert not any(call.startswith("bootout ") for call in calls)


def test_restart_treats_ambiguous_launchd_probe_as_an_error():
    assert "LAUNCHD_PROBE_DETAIL" in RESTART_SH
    assert '*"Could not find service"*' in RESTART_SH
    refresh = RESTART_SH[
        RESTART_SH.index("refresh_launchd_definitions()"):
        RESTART_SH.index("LAUNCHD_PROBE_DETAIL=")
    ]
    assert 'if [ "$probe_rc" -eq 2 ]' in refresh
    assert "Cannot inspect" in refresh


def test_chinese_stop_words_recognized():
    """User-facing: Chinese「结束/停/停止/取消」must hit the stop/cancel bypass."""
    for word in ("结束", "停", "停止", "取消"):
        assert word in BOT_SH, f"stop bypass must recognize Chinese「{word}」"


def test_143_message_gated_on_watchdog_marker():
    """143 = SIGTERM. Only a genuine 6000s watchdog kill should tell the user 「继续」.

    A restart/external SIGTERM also yields 143; emitting the「说继续」nag there is
    the exact loop the incident produced. The watchdog drops a marker file; the
    user-facing 143 message must be gated on it.
    """
    assert '"${ANSWER_FILE}.watchdog"' in BOT_SH, "watchdog must drop a marker on a real timeout"
    assert "_watchdog_killed" in BOT_SH, "143 branch must distinguish watchdog vs external kill"
    # The「继续」message must be conditioned on the watchdog flag, not on 143 alone.
    nag = "进度已存入 session"
    idx = BOT_SH.index(nag)
    window = BOT_SH[max(0, idx - 400) : idx]
    assert "_watchdog_killed" in window, "the 「继续」 nag must be gated on _watchdog_killed"


def test_lark_listener_reconnects_without_exiting_bot():
    """The Lark long-connection is allowed to drop or be replaced.

    bot.sh must restart the listener instead of letting the foreground pipe end,
    because an EXIT cleanup kills admin.py and takes :3456 down.
    """
    assert "run_lark_listener_once()" in BOT_SH
    assert "Lark listener exited" in BOT_SH
    assert "reconnecting in 5s" in BOT_SH
    assert BOT_SH.index("while true; do\n  _listener_rc=0") > \
        BOT_SH.index("run_lark_listener_once()")


def test_lark_listener_reconnect_captures_nonzero_exit():
    """A listener failure must be logged/reconnected, not fall through ambiguously."""
    assert "run_lark_listener_once || _listener_rc=$?" in BOT_SH
    bad = "while true; do\n  run_lark_listener_once\n  _listener_rc=$?"
    assert bad not in BOT_SH


def test_sigterm_cleanup_exits_bot():
    """SIGTERM must not merely run cleanup and continue the reconnect loop."""
    assert "trap cleanup EXIT" in BOT_SH
    assert "trap 'cleanup; exit 0' INT TERM" in BOT_SH
    assert "_CLEANED_UP" in BOT_SH


def test_restart_force_reaps_runtime_helpers_after_grace_period():
    """A graceful helper must not survive into the next release.

    2026-08-19: an old ``core.ef_stream_loop`` survived a restart and fought
    the new stream for the singleton server connection until both degraded.
    """
    kill_bot = RESTART_SH[
        RESTART_SH.index("kill_bot()") : RESTART_SH.index("kill_daemon()")
    ]
    assert 'pkill -f "eigenflux stream"' in kill_bot
    assert kill_bot.index("sleep 2") < kill_bot.index("force_kill_pattern()")
    for label, pattern in (
        ("heartbeat loop", "core\\\\.heartbeat_loop"),
        ("EigenFlux loop", "core\\\\.ef_stream_loop"),
        ("EigenFlux stream child", "eigenflux stream"),
        ("Lark sidecar", "lark-cli event|lark_event_sidecar"),
        ("admin process", '$JARVIS_DIR/admin\\\\.py'),
    ):
        assert f'force_kill_pattern "{label}" "{pattern}"' in kill_bot


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
