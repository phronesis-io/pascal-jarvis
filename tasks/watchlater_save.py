#!/usr/bin/env python3
"""Save a content item to the watch-later list.

Usage:
    python3 watchlater_save.py <title> <url> [source]
    echo '{"title":"...","url":"..."}' | python3 watchlater_save.py

Appends to $MEMORY_DIR/system/watchlater.jsonl.
Deduplicates by URL. Caps at 50 entries.
Prints a confirmation message on stdout.
"""

import json
import fcntl
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.timeutil import now_local_str
from core.log import log as _structured_log

MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", Path.home() / ".jarvis" / "memory"))
STORE_FILE = MEMORY_DIR / "system" / "watchlater.jsonl"
MAX_ENTRIES = 50


def load_entries() -> list[dict]:
    entries = []
    if STORE_FILE.exists():
        for line in STORE_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict):
                    entries.append(entry)
            except json.JSONDecodeError:
                continue
    return entries


def save_entries(entries: list[dict]) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STORE_FILE.parent, 0o700)
    body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{STORE_FILE.name}.",
        suffix=".tmp",
        dir=STORE_FILE.parent,
    )
    tmp = Path(tmp_name)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, STORE_FILE)
        os.chmod(STORE_FILE, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _store_lock():
    """Serialize the watch-later read/deduplicate/write transaction."""
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STORE_FILE.parent, 0o700)
    lock_path = STORE_FILE.with_suffix(STORE_FILE.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    if STORE_FILE.exists():
        os.chmod(STORE_FILE, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _save_to_sqlite(title: str, url: str, source: str) -> bool:
    """Also save to the shared SQLite store (if available)."""
    try:
        from core.db import bookmark_add
        bookmark_add(title=title, url=url, source=source)
        return True
    except Exception as exc:
        _structured_log(
            "watchlater-save",
            "sqlite_dual_write_failed",
            level="warn",
            error_type=type(exc).__name__,
        )
        return False


def main() -> int:
    title = ""
    url = ""
    source = "natural"

    # Parse from args or stdin
    if len(sys.argv) >= 3:
        title = sys.argv[1]
        url = sys.argv[2]
        source = sys.argv[3] if len(sys.argv) >= 4 else "natural"
    else:
        raw = sys.stdin.read().strip()
        if raw:
            try:
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("JSON input must be an object")
                title = str(data.get("title") or "")
                url = str(data.get("url") or "")
                source = str(data.get("source") or "natural")
            except json.JSONDecodeError:
                print("Error: invalid JSON input", file=sys.stderr)
                return 1
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

    if not url:
        print("Error: URL is required", file=sys.stderr)
        return 1

    try:
        with _store_lock():
            entries = load_entries()

            existing_urls = {e.get("url", "") for e in entries}
            if url in existing_urls:
                print(f"已在收藏列表中: {title or url}")
                return 0

            entries.append({
                "ts": now_local_str("%Y-%m-%d %H:%M"),
                "title": title,
                "url": url,
                "status": "pending",
                "source": source,
            })
            save_entries(entries[-MAX_ENTRIES:])
    except Exception as exc:
        _structured_log(
            "watchlater-save",
            "jsonl_write_failed",
            level="error",
            error_type=type(exc).__name__,
        )
        print("Error: 收藏写入失败", file=sys.stderr)
        return 1

    # Dual-write to the shared SQLite store
    _save_to_sqlite(title, url, source)

    print(f"已收藏: {title or url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
