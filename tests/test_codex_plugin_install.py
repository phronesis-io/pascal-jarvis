"""Installer tests for the repo-owned Codex plugin."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_plugin_and_mcp_share_the_acceptance_connector_version():
    from core.frontstage_acceptance import CONNECTOR_VERSION

    plugin_root = ROOT / "plugins" / "jarvis-matters"
    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(
            encoding="utf-8"))
    mcp_source = (ROOT / "core" / "codex_mcp.py").read_text(encoding="utf-8")
    skill = (plugin_root / "skills" / "jarvis-matter" / "SKILL.md").read_text(
        encoding="utf-8")

    assert manifest["version"] == CONNECTOR_VERSION
    assert "version=CONNECTOR_VERSION" in mcp_source
    assert "什么时候需要 Jarvis" in manifest["interface"]["defaultPrompt"][0]
    assert "how Codex and Jarvis divide work" in skill
    assert "jarvis_operating_model" in skill


def _fake_codex(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "marketplace-added"
    log = tmp_path / "codex.log"
    executable = bin_dir / "codex"
    executable.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_CODEX_LOG"
if [[ "$*" == "plugin marketplace list --json" ]]; then
  if [[ -f "$FAKE_CODEX_STATE" ]]; then
    printf '{"marketplaces":[{"root":"%s"}]}\\n' "$FAKE_REPO"
  else
    printf '{"marketplaces":[]}\\n'
  fi
elif [[ "$1 $2 $3" == "plugin marketplace add" ]]; then
  touch "$FAKE_CODEX_STATE"
elif [[ "$1 $2" == "plugin add" ]]; then
  :
else
  exit 9
fi
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, log


def test_codex_installer_registers_repo_privately_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir, log = _fake_codex(tmp_path)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_CODEX_LOG": str(log),
        "FAKE_CODEX_STATE": str(tmp_path / "marketplace-added"),
        "FAKE_REPO": str(ROOT),
    }

    first = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_codex_integration.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    second = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_codex_integration.sh")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    repo_path = home / ".jarvis" / "repo-path"
    assert repo_path.read_text(encoding="utf-8").strip() == str(ROOT)
    assert stat.S_IMODE(repo_path.stat().st_mode) == 0o600
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls.count(f"plugin marketplace add {ROOT}") == 1
    assert calls.count("plugin add jarvis-matters@pascal-jarvis") == 2
