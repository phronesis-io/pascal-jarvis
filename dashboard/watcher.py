"""File system watcher — emits events when Jarvis data changes.

Watches:
- heartbeat_state.json → task completions
- memory/ → memory updates
- engagement_log.jsonl → new engagement data
- heartbeat_outbox.jsonl → outgoing messages
- eigenflux/ → feed updates
"""

import json
import os
import time
from pathlib import Path
from threading import Thread

from .event_bus import bus

# Polling interval (seconds) — lighter than inotify, works on macOS
POLL_INTERVAL = 2.0


class FileWatcher:
    """Polls key files for changes and emits bus events."""

    def __init__(self, jarvis_dir: Path):
        self.jarvis_dir = jarvis_dir
        self._running = False
        self._thread: Thread | None = None
        self._mtimes: dict[str, float] = {}
        self._watched = {
            "heartbeat_state": jarvis_dir / "heartbeat_state.json",
            "engagement": jarvis_dir / "engagement_log.jsonl",
            "outbox": jarvis_dir / "heartbeat_outbox.jsonl",
            "active_sessions": jarvis_dir / "active_sessions.json",
        }

    def start(self):
        """Start watching in a background thread."""
        if self._running:
            return
        self._running = True
        # Initialize mtimes
        for key, path in self._watched.items():
            if path.exists():
                self._mtimes[key] = path.stat().st_mtime
        self._thread = Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop watching."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            for key, path in self._watched.items():
                if not path.exists():
                    continue
                mtime = path.stat().st_mtime
                prev = self._mtimes.get(key, 0)
                if mtime > prev:
                    self._mtimes[key] = mtime
                    if prev > 0:  # Don't fire on first detection
                        bus.emit_sync(f"file:{key}", {
                            "path": str(path),
                            "mtime": mtime,
                        })
            time.sleep(POLL_INTERVAL)
