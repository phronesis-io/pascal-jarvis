from __future__ import annotations

import os
import subprocess
from pathlib import Path

from core.components import check_components


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "jarvis-tests@example.invalid")
    _git(tmp_path, "config", "user.name", "Jarvis Tests")
    (tmp_path / "core").mkdir()
    source = tmp_path / "core" / "worker.py"
    source.write_text("VERSION = 1\n", encoding="utf-8")
    (tmp_path / "runtime_sources.txt").write_text(
        "core\nruntime_sources.txt\n", encoding="utf-8"
    )
    _git(tmp_path, "add", "core", "runtime_sources.txt")
    _git(tmp_path, "commit", "-qm", "fixture")
    head = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / ".bot.pid").write_text(
        f"{os.getpid()} 123 {head}\n", encoding="utf-8"
    )
    manifest = tmp_path / "components.yaml"
    manifest.write_text(
        "components:\n"
        "  - name: watchdog-armed\n"
        "    check: runtime_source\n"
        "    pid_path: .bot.pid\n"
        "    paths_file: runtime_sources.txt\n"
        "    critical: true\n",
        encoding="utf-8",
    )
    return manifest, source


def _check(tmp_path: Path, manifest: Path) -> dict:
    (result,) = check_components(manifest_path=manifest, root=tmp_path)
    return result


def test_runtime_source_reports_armed_for_exact_clean_boot_revision(tmp_path):
    manifest, _ = _fixture(tmp_path)

    result = _check(tmp_path, manifest)

    assert result["ok"] is True
    assert "watchdog armed" in result["detail"]


def test_runtime_source_detects_protected_worktree_change(tmp_path):
    manifest, source = _fixture(tmp_path)
    source.write_text("VERSION = 2\n", encoding="utf-8")

    result = _check(tmp_path, manifest)

    assert result["ok"] is False
    assert "runtime source modified" in result["detail"]
    assert "core/worker.py" in result["detail"]


def test_runtime_source_detects_clean_checkout_after_bot_boot(tmp_path):
    manifest, source = _fixture(tmp_path)
    source.write_text("VERSION = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "core/worker.py")
    _git(tmp_path, "commit", "-qm", "new runtime")

    result = _check(tmp_path, manifest)

    assert result["ok"] is False
    assert "bot loaded a different revision" in result["detail"]


def test_runtime_source_requires_revision_receipt_from_running_bot(tmp_path):
    manifest, _ = _fixture(tmp_path)
    (tmp_path / ".bot.pid").write_text(
        f"{os.getpid()} 123\n", encoding="utf-8"
    )

    result = _check(tmp_path, manifest)

    assert result["ok"] is False
    assert "governed restart required" in result["detail"]


def test_bot_and_component_share_one_runtime_source_manifest():
    root = Path(__file__).resolve().parent.parent
    bot = (root / "bot.sh").read_text(encoding="utf-8")
    manifest = (root / "components.yaml").read_text(encoding="utf-8")
    protected = (root / "runtime_sources.txt").read_text(encoding="utf-8")

    assert '_RUNTIME_GIT_PATHS_FILE="$JARVIS_DIR/runtime_sources.txt"' in bot
    assert 'echo "$$ $_BOOT_TS $_BOOT_GIT_HEAD" > "$PIDFILE"' in bot
    assert "paths_file: runtime_sources.txt" in manifest
    assert "runtime_sources.txt" in protected.splitlines()
