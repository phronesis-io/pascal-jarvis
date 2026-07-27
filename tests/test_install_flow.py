"""Portable installation and runtime-selection regressions."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


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


def test_setup_installs_and_verifies_the_complete_dependency_set():
    script = (ROOT / "setup.sh").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    assert "pip install -r requirements-dev.txt" in script
    assert 'if python3 -c "import yaml"' not in script
    assert "nicegui" in script
    assert "pywebpush" in script
    assert "qrcode" in script
    assert "lark_oapi" in script
    assert "pip check" in script
    assert "sys.version_info >= (3, 10)" in script
    assert "pytest>=8.0" in requirements
    assert "chmod -x scripts/config_env.sh scripts/runtime_env.sh" in script


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
