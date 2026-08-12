"""Portable installation and runtime-selection regressions."""

from __future__ import annotations

import os
import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _localtest_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    tasks = root / "tasks"
    bin_dir = tmp_path / "bin"
    for directory in (scripts, tasks, root / "tests", bin_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (scripts / "localtest.sh").write_text(
        (ROOT / "scripts" / "localtest.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "runtime_env.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (root / "bot.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    calls = tmp_path / "python-calls.txt"
    (bin_dir / "python3").write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$PYTHON_CALLS\"\n",
        encoding="utf-8",
    )
    (bin_dir / "shellcheck").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    for executable in (bin_dir / "python3", bin_dir / "shellcheck"):
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHON_CALLS": str(calls),
    }
    return root, env, calls


def test_localtest_runtime_mode_skips_pytest_and_runs_runtime_gates(tmp_path):
    root, env, calls = _localtest_fixture(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/localtest.sh", "--runtime"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    invoked = calls.read_text(encoding="utf-8")
    assert "pytest skipped in runtime mode" in result.stdout
    assert "-m pytest" not in invoked
    assert "-m core.components" in invoked
    assert "-m core.deploy verify" in invoked
    assert "-m core.deploy smoke" in invoked


def test_localtest_normal_mode_keeps_strict_full_suite(tmp_path):
    root, env, calls = _localtest_fixture(tmp_path)

    subprocess.run(
        ["bash", "scripts/localtest.sh"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "-m pytest tests/" in calls.read_text(encoding="utf-8")


def test_localtest_runtime_mode_rejects_discarded_pytest_arguments(tmp_path):
    root, env, calls = _localtest_fixture(tmp_path)

    result = subprocess.run(
        ["bash", "scripts/localtest.sh", "--runtime", "tests/test_one.py"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "accepts no pytest arguments" in result.stderr
    assert not calls.exists()


def test_runtime_env_prefers_tcc_safe_managed_virtualenv(tmp_path):
    root = tmp_path / "Desktop" / "jarvis"
    home = tmp_path / "home"
    venv = home / ".jarvis" / "runtime-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    env = {**os.environ, "HOME": str(home), "JARVIS_DIR": str(root)}
    env.pop("JARVIS_PYTHON", None)
    env.pop("JARVIS_VENV_DIR", None)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s\\n%s\\n" "$JARVIS_PYTHON" "$PATH"',
            "bash",
            str(ROOT / "scripts" / "runtime_env.sh"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    selected, path = result.stdout.splitlines()
    assert Path(selected).parent == venv / "bin"
    assert Path(path.split(os.pathsep)[0]) == venv / "bin"


def test_runtime_env_honors_explicit_python_before_path_fallback(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_python = bin_dir / "python3"
    fake_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env = {
        **os.environ,
        "JARVIS_PYTHON": sys.executable,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s\\n" "$JARVIS_PYTHON"',
            "bash",
            str(ROOT / "scripts" / "runtime_env.sh"),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert Path(result.stdout.strip()).resolve() == Path(sys.executable).resolve()


def test_setup_installs_and_verifies_the_complete_dependency_set():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    runtime_requirements = (ROOT / "requirements.txt").read_text(
        encoding="utf-8")

    assert "pip install -r requirements-dev.txt" in script
    assert 'if python3 -c "import yaml"' not in script
    assert "nicegui" in script
    # REQ-120: Web Push and pairing QR are retired with the mobile desk —
    # their libraries must not creep back into the dependency set.
    assert "pywebpush" not in script
    assert "qrcode" not in script
    assert "pywebpush" not in runtime_requirements
    assert "qrcode" not in runtime_requirements
    assert "lark_oapi" in script
    assert "pip check" in script
    assert "sys.version_info >= (3, 10)" in script
    assert "pytest>=8.0" in requirements
    assert "nicegui==3.15.0" in runtime_requirements
    assert "chmod -x scripts/config_env.sh scripts/runtime_env.sh" in script
    assert "need_cmd python3" not in script


def test_launchd_installer_renders_configured_paths_and_selected_python(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work & notes"
    work.mkdir()
    config = tmp_path / "jarvis.yaml"
    config.write_text(f"work_dir: {str(work)!r}\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "launchd-loaded"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    if [ -f "$SERVICE_STATE" ]; then
      echo 'state = running'
      exit 0
    fi
    echo 'Could not find service' >&2
    exit 1
    ;;
  bootstrap)
    : > "$SERVICE_STATE"
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
        "JARVIS_CONFIG_FILE": str(config),
        "JARVIS_PYTHON": sys.executable,
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
        "TASKLINE_DIR": str(tmp_path / "taskline-not-installed"),
    }
    env.pop("WORK_DIR", None)

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

    installed = (
        home / "Library" / "LaunchAgents" / "com.pascal.jarvis.daemon.plist"
    )
    assert installed.exists()
    raw = installed.read_text(encoding="utf-8")
    assert "__PYTHON_BIN__" not in raw
    assert "__WORK_DIR__" not in raw
    with installed.open("rb") as handle:
        plist = plistlib.load(handle)
    assert Path(plist["ProgramArguments"][0]).resolve() == Path(sys.executable).resolve()
    assert plist["EnvironmentVariables"]["WORK_DIR"] == str(work.resolve())
    assert plist["EnvironmentVariables"]["PATH"].split(":")[0] == str(
        Path(plist["ProgramArguments"][0]).parent.resolve()
    )


@pytest.mark.parametrize(
    ("label", "running_probes"),
    [
        ("com.pascal.jarvis.daemon", 0),
        ("com.pascal.jarvis.dashboard", 0),
        ("com.pascal.jarvis.taskline", 0),
        ("com.pascal.jarvis.dashboard", 3),
    ],
)
def test_launchd_installer_rolls_back_tcc_crash_loops(
    tmp_path, label, running_probes
):
    home = tmp_path / "home"
    destination = home / "Library" / "LaunchAgents"
    destination.mkdir(parents=True)
    installed = destination / f"{label}.plist"
    installed.write_text("previous definition\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    taskline = tmp_path / "taskline"
    taskline_binary = taskline / "dist" / "taskline-server"
    taskline_binary.parent.mkdir(parents=True)
    taskline_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    taskline_binary.chmod(0o755)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.write_text("loaded\n", encoding="utf-8")
    bootstrap_count = tmp_path / "bootstrap-count"
    probe_count = tmp_path / "probe-count"
    launchctl = bin_dir / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
case "$1" in
  print)
    case "$(cat "$SERVICE_STATE")" in
      loaded) echo 'state = running'; exit 0 ;;
      crash)
        count=0
        [ ! -f "$PROBE_COUNT" ] || count=$(cat "$PROBE_COUNT")
        count=$((count + 1))
        printf '%s\n' "$count" > "$PROBE_COUNT"
        if [ "$count" -le "$RUNNING_PROBES" ]; then
          echo 'state = running'
          exit 0
        fi
        echo 'state = waiting'
        echo 'last exit code = 78'
        exit 0
        ;;
      *) echo 'Could not find service' >&2; exit 1 ;;
    esac
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
      printf 'crash\n' > "$SERVICE_STATE"
    else
      printf 'loaded\n' > "$SERVICE_STATE"
    fi
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
        "PROBE_COUNT": str(probe_count),
        "RUNNING_PROBES": str(running_probes),
        "WORK_DIR": str(work),
        "TASKLINE_DIR": str(taskline),
        "JARVIS_PYTHON": sys.executable,
        "JARVIS_LAUNCHD_SETTLE_ATTEMPTS": "8",
        "JARVIS_LAUNCHD_SETTLE_INTERVAL": "0",
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "launchd" / "install.sh"),
            label,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "status 78" in result.stderr
    assert "macOS TCC" in result.stderr
    assert "previous state restored" in result.stderr
    assert installed.read_text(encoding="utf-8") == "previous definition\n"
    assert state.read_text(encoding="utf-8") == "loaded\n"


def test_session_backup_accepts_apostrophes_in_paths(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "Jarvis user's repo"
    work = tmp_path / "owner's workspace"
    home.mkdir()
    repo.mkdir()
    work.mkdir()
    (repo / "data").mkdir()
    (repo / "active_sessions.json").write_text(
        '{"main": {"session_id": "session-1"}}',
        encoding="utf-8",
    )
    with sqlite3.connect(repo / "data" / "jarvis.db") as database:
        database.execute("CREATE TABLE proof (value TEXT)")
        database.execute("INSERT INTO proof VALUES ('wal-safe')")

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "backup_sessions.sh")],
        env={
            **os.environ,
            "HOME": str(home),
            "JARVIS_DIR": str(repo),
            "WORK_DIR": str(work),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (repo / ".last_backup_ok").exists()
    assert (work / "session_backups" / "latest").is_symlink()
    with sqlite3.connect(
        work / "session_backups" / "latest" / "jarvis.db"
    ) as backup:
        assert backup.execute("SELECT value FROM proof").fetchone() == (
            "wal-safe",
        )


def test_launchd_templates_do_not_pin_pascal_homebrew_python():
    templates = list((ROOT / "scripts" / "launchd").glob("*.plist"))
    assert templates
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert "/opt/homebrew/bin/python3" not in text
    audit_runner = (
        ROOT / "scripts" / "run_conversation_audit.sh"
    ).read_text(encoding="utf-8")
    assert "/opt/homebrew/bin/python3" not in audit_runner
    assert '"$JARVIS_PYTHON"' in audit_runner


def test_dashboard_launchd_command_delegates_to_canonical_installer():
    script = (ROOT / "dashboard" / "start.sh").read_text(encoding="utf-8")
    block = script[script.index("--install-launchd)") : script.index("--migrate)")]

    assert "scripts/launchd/install.sh" in block
    assert "com.pascal.jarvis.dashboard" in block
    assert "dashboard/launchd" not in block
