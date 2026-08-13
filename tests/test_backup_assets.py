from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from scripts.snapshot_code_assets import snapshot
from scripts.verify_backup import verify, write_manifest


def test_code_snapshot_keeps_local_commit_diff_and_untracked(tmp_path):
    root = tmp_path / "workspace"
    repo = root / "repos" / "project"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "tracked.txt").write_text("two\n")
    (repo / "draft.md").write_text("private draft\n")

    destination = tmp_path / "backup" / "code"
    report = snapshot(root, destination)

    assert report["failures"] == []
    assert list((destination / "bundles").glob("*.bundle"))
    records = report["repositories"]
    assert records[0]["working_patch"] is True
    assert records[0]["untracked_files"] == 1
    saved = list((destination / "worktrees").glob("*/untracked/draft.md"))
    assert saved[0].read_text() == "private draft\n"


def test_code_snapshot_records_untracked_symlink_without_following_it(tmp_path):
    root = tmp_path / "workspace"
    repo = root / "repos" / "project"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True,
                   capture_output=True)
    secret = tmp_path / "outside-secret"
    secret.write_text("do not copy\n")
    (repo / "outside-link").symlink_to(secret)

    destination = tmp_path / "backup" / "code"
    report = snapshot(root, destination)

    assert report["failures"] == []
    saved = list(destination.glob(
        "worktrees/*/untracked/outside-link.symlink"))
    assert len(saved) == 1
    assert saved[0].read_text() == str(secret)
    assert "do not copy" not in saved[0].read_text()


def test_code_snapshot_bundle_contains_detached_worktree_head(tmp_path):
    root = tmp_path / "workspace"
    repo = root / "repos" / "project"
    detached = root / "worktrees" / "detached"
    repo.mkdir(parents=True)
    detached.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo)
    (repo / "tracked.txt").write_text("one\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(detached), "HEAD"],
        cwd=repo, check=True, capture_output=True,
    )
    (detached / "detached.txt").write_text("unique commit\n")
    subprocess.run(["git", "add", "detached.txt"], cwd=detached, check=True)
    subprocess.run(["git", "commit", "-m", "detached"], cwd=detached,
                   check=True, capture_output=True)
    detached_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=detached, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    destination = tmp_path / "backup" / "code"
    report = snapshot(root, destination)

    assert report["failures"] == []
    bundle = next((destination / "bundles").glob("*.bundle"))
    listed = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)], check=True,
        capture_output=True, text=True,
    ).stdout
    assert detached_head in listed


def test_backup_manifest_checks_sessions_sqlite_permissions_and_tamper(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "repo"
    backup = tmp_path / "backup"
    (home / ".claude" / "projects" / "p").mkdir(parents=True)
    (home / ".codex" / "sessions" / "2026").mkdir(parents=True)
    (home / ".claude" / "projects" / "p" / "a.jsonl").write_text("{}\n")
    (home / ".codex" / "sessions" / "2026" / "b.jsonl").write_text("{}\n")
    (source / "data").mkdir(parents=True)
    (source / "jarvis.yaml").write_text("lark: {}\n")
    with sqlite3.connect(source / "data" / "jarvis.db") as db:
        db.execute("create table proof(value text)")
    (backup / "claude_sessions" / "p").mkdir(parents=True)
    (backup / "codex_sessions" / "2026").mkdir(parents=True)
    (backup / "claude_sessions" / "p" / "a.jsonl").write_text("{}\n")
    (backup / "codex_sessions" / "2026" / "b.jsonl").write_text("{}\n")
    (backup / "databases").mkdir()
    with sqlite3.connect(backup / "databases" / "data__jarvis.db") as db:
        db.execute("create table proof(value text)")
    (backup / "state").mkdir()
    (backup / "state" / "jarvis.yaml").write_text("lark: {}\n")
    (backup / "code").mkdir()
    (backup / "code" / "assets.json").write_text(json.dumps({"repositories": []}))
    (backup / "code" / "assets-link").symlink_to("assets.json")

    write_manifest(backup, source, home)
    assert verify(backup) == []
    assert backup.stat().st_mode & 0o077 == 0

    (backup / "code" / "assets.json").write_text("tampered")
    assert any("checksum mismatch" in error for error in verify(backup))


def test_backup_manifest_rejects_retargeted_or_external_symlink(tmp_path):
    home = tmp_path / "home"
    source = tmp_path / "repo"
    backup = tmp_path / "backup"
    home.mkdir()
    (source / "data").mkdir(parents=True)
    (backup / "code").mkdir(parents=True)
    (backup / "code" / "assets.json").write_text("{}")
    link = backup / "code" / "current"
    link.symlink_to("assets.json")

    write_manifest(backup, source, home)
    assert verify(backup) == []

    link.unlink()
    external = tmp_path / "external-secret"
    external.write_text("secret")
    link.symlink_to(external)
    errors = verify(backup)
    assert any("unsafe backup symlink" in error for error in errors)
    assert any("symlink checksum mismatch" in error for error in errors)
