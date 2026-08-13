#!/usr/bin/env python3
"""Write and verify a private Jarvis backup manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path


MANIFEST = "MANIFEST.sha256"
METADATA = "backup_metadata.json"


def _entries(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if (p.is_file() or p.is_symlink()) and p.name != MANIFEST
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _symlink_sha256(path: Path) -> str:
    return hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()


def _manifest_key(path: Path, root: Path) -> str:
    kind = "L" if path.is_symlink() else "F"
    # ASCII JSON escapes preserve even filesystem surrogate code points.
    relative = json.dumps(str(path.relative_to(root)))
    return f"{kind}:{relative}"


def _count_jsonl(root: Path) -> int:
    return sum(1 for _ in root.rglob("*.jsonl")) if root.is_dir() else 0


def _count_memory_files(home: Path) -> int:
    roots = [
        home / ".claude" / "projects",
        home / ".Codex" / "projects",
    ]
    return sum(
        1
        for root in roots if root.is_dir()
        for path in root.glob("*/memory/**/*") if path.is_file()
    )


def write_manifest(backup: Path, source: Path, home: Path) -> None:
    source_dbs = list(source.glob("*.db")) + list((source / "data").glob("*.db"))
    metadata = {
        "schema_version": 1,
        "source": str(source),
        "classes": {
            "claude_sessions": {
                "source_count": _count_jsonl(home / ".claude" / "projects"),
                "backup_count": _count_jsonl(backup / "claude_sessions"),
            },
            "codex_sessions": {
                "source_count": _count_jsonl(home / ".codex" / "sessions"),
                "backup_count": _count_jsonl(backup / "codex_sessions"),
            },
            "memory": {
                "source_count": _count_memory_files(home),
                "backup_count": sum(
                    1 for path in (backup / "memory").rglob("*")
                    if path.is_file()
                ) if (backup / "memory").is_dir() else 0,
            },
            "sqlite": {
                "source_count": len(source_dbs),
                "backup_count": len(list((backup / "databases").glob("*.db"))),
            },
            "code_assets": {
                "present": (backup / "code" / "assets.json").is_file(),
            },
            "config": {
                "source_present": (source / "jarvis.yaml").is_file(),
                "backup_present": (backup / "state" / "jarvis.yaml").is_file(),
            },
        },
    }
    (backup / METADATA).write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    # Keep owner execute/read/write bits and remove every group/world bit.
    for path in [backup, *backup.rglob("*")]:
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        path.chmod(mode & 0o700)
    lines = []
    for path in _entries(backup):
        digest = _symlink_sha256(path) if path.is_symlink() else _sha256(path)
        lines.append(f"{digest}  {_manifest_key(path, backup)}")
    (backup / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (backup / MANIFEST).chmod(0o600)


def verify(backup: Path) -> list[str]:
    errors: list[str] = []
    try:
        metadata = json.loads((backup / METADATA).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["backup metadata missing or invalid"]
    for name, values in metadata.get("classes", {}).items():
        if "source_count" in values and values["backup_count"] < values["source_count"]:
            errors.append(
                f"{name} incomplete: {values['backup_count']}/{values['source_count']}")
        if "source_present" in values and values["source_present"] != values["backup_present"]:
            errors.append(f"{name} presence mismatch")
        if "present" in values and not values["present"]:
            errors.append(f"{name} missing")

    try:
        lines = (backup / MANIFEST).read_text(encoding="utf-8").splitlines()
    except OSError:
        return errors + ["checksum manifest missing"]
    manifested: set[str] = set()
    for line in lines:
        expected, sep, payload = line.partition("  ")
        if not sep:
            errors.append("malformed manifest entry")
            continue
        if payload.startswith(("F:", "L:")):
            kind, encoded = payload[:1], payload[2:]
            try:
                relative = json.loads(encoded)
            except (TypeError, ValueError):
                errors.append("malformed manifest path")
                continue
        else:
            # Compatibility with snapshots written before typed paths.
            kind, relative = "F", payload
        path = backup / relative
        key = f"{kind}:{relative}"
        manifested.add(key)
        if kind == "L":
            if not path.is_symlink():
                errors.append(f"manifest symlink missing: {relative}")
                continue
            try:
                path.resolve(strict=True).relative_to(backup.resolve())
            except (OSError, ValueError):
                errors.append(f"unsafe backup symlink: {relative}")
            if _symlink_sha256(path) != expected:
                errors.append(f"symlink checksum mismatch: {relative}")
        elif not path.is_file() or path.is_symlink():
            errors.append(f"manifest file missing: {relative}")
        elif _sha256(path) != expected:
            errors.append(f"checksum mismatch: {relative}")
    actual = {
        f"{'L' if path.is_symlink() else 'F'}:{path.relative_to(backup)}"
        for path in _entries(backup)
    }
    for key in sorted(actual - manifested):
        errors.append(f"unmanifested asset: {key[2:]}")
    for key in sorted(manifested - actual):
        errors.append(f"manifested asset disappeared: {key[2:]}")

    for path in [backup, *backup.rglob("*")]:
        if not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            errors.append(f"permissions too broad: {path.relative_to(backup)}")
    for database in (backup / "databases").glob("*.db"):
        try:
            # ``mode=ro`` alone still creates -wal/-shm for a database whose
            # persisted journal_mode is WAL, mutating the snapshot during its
            # own verification. immutable=1 makes this a genuinely read-only
            # image check and keeps the checksum file set stable.
            with sqlite3.connect(
                f"file:{database}?mode=ro&immutable=1", uri=True,
            ) as db:
                result = db.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                errors.append(f"sqlite integrity failed: {database.name}")
        except sqlite3.Error as exc:
            errors.append(f"sqlite unreadable: {database.name} ({type(exc).__name__})")

    assets_path = backup / "code" / "assets.json"
    try:
        assets = json.loads(assets_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        errors.append("code asset report missing or invalid")
        assets = {}
    if assets.get("failures"):
        errors.append("code asset snapshot reports failures")
    bundle_heads: dict[str, set[str]] = {}
    bundles = backup / "code" / "bundles"
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-bundle-verify-") as temp:
            subprocess.run(
                ["git", "init", "--bare", "-q", temp], check=True,
                capture_output=True,
            )
            for bundle in bundles.glob("*.bundle"):
                checked = subprocess.run(
                    ["git", "-C", temp, "bundle", "verify", str(bundle)],
                    capture_output=True,
                )
                if checked.returncode != 0:
                    errors.append(f"git bundle invalid: {bundle.name}")
                listed = subprocess.run(
                    ["git", "bundle", "list-heads", str(bundle)],
                    capture_output=True, text=True,
                )
                if listed.returncode != 0:
                    errors.append(f"git bundle unreadable: {bundle.name}")
                    bundle_heads[bundle.name] = set()
                else:
                    bundle_heads[bundle.name] = {
                        line.split()[0] for line in listed.stdout.splitlines()
                        if line.split()
                    }
    except (OSError, subprocess.SubprocessError):
        errors.append("git bundle verification unavailable")
    for record in assets.get("repositories", []):
        bundle = str(record.get("bundle", ""))
        head = str(record.get("head", ""))
        if not bundle or bundle not in bundle_heads:
            errors.append(f"worktree bundle missing: {record.get('path', '')}")
        elif head and head not in bundle_heads[bundle]:
            errors.append(f"worktree HEAD absent from bundle: {record.get('path', '')}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--source", default="")
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args(argv)
    backup = Path(args.backup).resolve()
    if args.write:
        if not args.source:
            parser.error("--write requires --source")
        write_manifest(backup, Path(args.source).resolve(), Path(args.home).resolve())
    errors = verify(backup)
    if errors:
        for error in errors:
            print(f"[backup-verify] ERROR: {error}")
        return 1
    print(f"[backup-verify] ok: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
