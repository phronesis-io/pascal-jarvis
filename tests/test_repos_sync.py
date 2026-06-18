"""REQ-52 repos-sync fast pre-script + detached worker contract."""

import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRE = ROOT / "tasks" / "repos_sync_pre.sh"
WORKER = ROOT / "tasks" / "repos_sync_worker.sh"


def _write_fake_worker(jarvis_dir: Path, body: str | None = None) -> Path:
    tasks = jarvis_dir / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    worker = tasks / "repos_sync_worker.sh"
    worker.write_text(
        body
        or (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "echo spawned >> \"$JARVIS_DIR/spawned.log\"\n"
        ),
        encoding="utf-8",
    )
    worker.chmod(0o755)
    return worker


def _run_pre(jarvis_dir: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "JARVIS_DIR": str(jarvis_dir)}
    return subprocess.run(
        ["bash", str(PRE)],
        capture_output=True,
        text=True,
        env=env,
        timeout=2,
    )


def _wait_for(path: Path, timeout_s: float = 1.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if path.exists():
            return True
        time.sleep(0.02)
    return path.exists()


def test_repos_sync_pre_spawns_worker_when_product_missing(tmp_path):
    """The pre-hook must stay fast: it spawns the slow worker and emits no
    prompt text until a worker product exists."""
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    _write_fake_worker(jarvis_dir)

    result = _run_pre(jarvis_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert _wait_for(jarvis_dir / "spawned.log")


def test_repos_sync_pre_emits_each_product_once(tmp_path):
    """A fresh worker product is emitted exactly once; subsequent cycles stay
    empty so heartbeat skips repos-sync instead of re-summarizing stale data."""
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    _write_fake_worker(jarvis_dir)
    product = jarvis_dir / ".repos_sync_product.txt"
    product.write_text("Repos sync:\n  demo: changed\n", encoding="utf-8")

    first = _run_pre(jarvis_dir)
    second = _run_pre(jarvis_dir)

    assert first.returncode == 0, first.stderr
    assert first.stdout == "Repos sync:\n  demo: changed\n"
    assert (jarvis_dir / ".repos_sync_consumed").exists()
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""


def test_repos_sync_pre_refreshes_stale_consumed_product_without_reemitting(tmp_path):
    """If the last product is stale and already consumed, pre only kicks the
    worker; it does not make Claude analyze old repo activity again."""
    jarvis_dir = tmp_path / "jarvis"
    jarvis_dir.mkdir()
    _write_fake_worker(jarvis_dir)
    product = jarvis_dir / ".repos_sync_product.txt"
    consumed = jarvis_dir / ".repos_sync_consumed"
    product.write_text("old product\n", encoding="utf-8")
    consumed.write_text("", encoding="utf-8")
    old = time.time() - 6 * 60 * 60
    os.utime(product, (old, old))
    os.utime(consumed, (old, old))

    result = _run_pre(jarvis_dir)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert _wait_for(jarvis_dir / "spawned.log")


def test_repos_sync_worker_standalone_finds_sibling_repos(tmp_path):
    """Standalone fallback must scan WORK_DIR/repos, not repos/repos. A local
    git repo with no upstream will be reported as PULL FAILED if discovered."""
    work_dir = tmp_path / "work"
    jarvis_dir = work_dir / "repos" / "pascal-jarvis"
    demo_repo = work_dir / "repos" / "demo"
    jarvis_dir.mkdir(parents=True)
    demo_repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=demo_repo, check=True, capture_output=True)

    env = {**os.environ, "JARVIS_DIR": str(jarvis_dir)}
    result = subprocess.run(
        ["bash", str(WORKER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    product = jarvis_dir / ".repos_sync_product.txt"
    assert result.returncode == 0, result.stderr
    assert product.exists()
    text = product.read_text(encoding="utf-8")
    assert "demo" in text
    assert "PULL FAILED" in text
