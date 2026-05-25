"""Session management — tracks Claude Code sessions with auto-rotation.

The tracker file (active_sessions.json) is the single source of truth that
maps Lark conv_key → current session_id. Writes are guarded by a file lock
(fcntl) to survive concurrent access from bot.sh event loop + any future
multi-threaded callers. Each save is atomic (temp + rename).
"""

import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from core.textutil import extract_text as _extract_text

NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


@contextmanager
def _locked(path: Path):
    """Exclusive file lock around operations on `path`.

    Uses a sibling `.lock` file (never replaced) as the lock target so the
    lock survives atomic-rename operations on the data file. The lock file
    is created if missing; we do not lock the data file itself because
    os.replace() gives it a new inode on every write — any flock() on the
    old inode would let a concurrent thread (which opened the new inode)
    acquire its own lock on the same path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _atomic_write_json(path: Path, data: dict) -> None:
    """Temp+rename to prevent partial files. Must be called while holding the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use a PID+tid-scoped temp name so concurrent lock holders (if any slip
    # through) don't clobber each other's tmp file.
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


class SessionManager:
    """Manages session IDs with auto-rotation when session files get too large."""

    def __init__(self, tracker_path: str | Path, session_dir: str | Path,
                 max_size: int = 512000):
        self.tracker_path = Path(tracker_path)
        self.session_dir = Path(session_dir)
        self.max_size = max_size

    def _read(self) -> dict:
        """Read tracker without holding the lock (for read-only queries)."""
        if not self.tracker_path.exists():
            return {}
        try:
            return json.loads(self.tracker_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def get_session(self, conv_key: str) -> tuple[str, bool]:
        """Get or create a session ID for a conversation key.

        Returns (session_id, rotated) where rotated=True if a new session
        was created due to size overflow. Runs under a sibling-file lock
        so concurrent callers can't race on the tracker.
        """
        with _locked(self.tracker_path):
            # Always re-read under the lock — disk is the source of truth
            if self.tracker_path.exists():
                try:
                    tracker = json.loads(self.tracker_path.read_text() or "{}")
                except json.JSONDecodeError:
                    tracker = {}
            else:
                tracker = {}

            entry = tracker.get(conv_key, {})
            sid = entry.get("session_id", "")
            rotated = False

            needs_new = not sid
            if sid:
                session_file = self.session_dir / f"{sid}.jsonl"
                if session_file.exists() and session_file.stat().st_size > self.max_size:
                    needs_new = True
                    rotated = True

            if needs_new:
                counter = entry.get("counter", 0) + 1
                sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter}"))
                tracker[conv_key] = {"session_id": sid, "counter": counter}
                _atomic_write_json(self.tracker_path, tracker)

        return sid, rotated

    def get_counter(self, conv_key: str) -> int:
        return self._read().get(conv_key, {}).get("counter", 0)


def _utc_to_local(ts_raw: str) -> str:
    """Convert a UTC ISO timestamp (e.g. '2026-04-23T03:10:33.939Z') to local time.

    Returns 'YYYY-MM-DD HH:MM' in the system's local timezone.
    Falls back to truncated UTC if parsing fails.
    """
    if not ts_raw:
        return ""
    try:
        # Import here to avoid circular imports
        from core.timeutil import _LOCAL_TZ
        # Strip trailing Z and parse as UTC
        clean = ts_raw.rstrip("Z")[:19]  # "2026-04-23T03:10:33"
        dt_utc = datetime.fromisoformat(clean).replace(tzinfo=timezone.utc)
        if _LOCAL_TZ is not None:
            dt_local = dt_utc.astimezone(_LOCAL_TZ)
        else:
            dt_local = dt_utc
        return dt_local.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts_raw[:16]


def read_tail_turns(session_dir: str | Path, session_id: str,
                    limit: int = 20, max_msg_chars: int = 400,
                    max_total_chars: int = 6000) -> list[dict]:
    """Read the last N user/assistant turns from a session file.

    Returns list of {"role": str, "text": str, "ts": str} dicts.
    """
    path = Path(session_dir) / f"{session_id}.jsonl"
    if not path.exists():
        return []

    turns = []
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message", {})
        role = msg.get("role", obj.get("type", ""))
        content = msg.get("content", "")
        text = _extract_text(content)
        if not text:
            continue
        ts = _utc_to_local(obj.get("timestamp", ""))
        turns.append({"role": role, "text": text, "ts": ts})

    return turns[-limit:]


def format_recent_turns(turns: list[dict], max_msg_chars: int = 400,
                        max_total_chars: int = 6000) -> str:
    """Format turns into a markdown block for system prompt injection.

    Inserts date separators (e.g. '--- 昨天 2026-05-09 ---') when turns span
    multiple days, so Claude can clearly distinguish today vs past messages.
    """
    if not turns:
        return ""

    formatted = []
    total = 0
    for turn in reversed(turns):
        snippet = turn["text"][:max_msg_chars]
        if len(turn["text"]) > max_msg_chars:
            snippet += "..."
        role_label = turn["role"]
        if turn.get("source") == "heartbeat":
            role_label = "assistant (heartbeat)"
        line = f"[{turn['ts']}] **{role_label}**: {snippet}"
        if total + len(line) > max_total_chars:
            break
        formatted.append(line)
        total += len(line)
    formatted.reverse()

    # Insert date separators between days
    from core.timeutil import now_local
    today_str = now_local().strftime("%Y-%m-%d")
    lines = ["## Recent Turns", ""]
    prev_date = None
    for line in formatted:
        # Extract date from "[YYYY-MM-DD HH:MM]" prefix
        turn_date = line[1:11] if len(line) > 11 and line[0] == "[" else None
        if turn_date and turn_date != prev_date:
            if turn_date == today_str:
                lines.append(f"\n{'═' * 50}")
                lines.append(f"📅 今天 {turn_date} — 以下是今天的对话")
                lines.append(f"⚠️ 上方所有内容都是历史，不要当作今天发生的事")
                lines.append(f"{'═' * 50}\n")
            else:
                lines.append(f"\n--- 过去 {turn_date} (不是今天) ---\n")
            prev_date = turn_date
        lines.append(f"{line}\n")
    return "\n".join(lines)


def read_heartbeat_outbox(outbox_path: str | Path, limit: int = 10) -> list[dict]:
    """Read recent heartbeat messages (checkins, alerts) sent to the user.

    These are messages generated by the heartbeat loop (separate Claude call)
    that were sent to Lark but never entered the main session. We merge them
    into recent_turns so the main session Claude knows what it said.
    """
    path = Path(outbox_path)
    if not path.exists():
        return []
    entries = []
    for line in path.open(encoding="utf-8", errors="ignore"):
        try:
            obj = json.loads(line)
            entries.append({
                "role": obj.get("role", "assistant"),
                "text": obj.get("text", ""),
                "ts": obj.get("ts", ""),
                "source": obj.get("source", "heartbeat"),
            })
        except Exception:
            continue
    return entries[-limit:]


def build_recent_turns(session_dir: str | Path, session_id: str,
                       counter: int, conv_key: str,
                       limit: int = 20) -> str:
    """Build recent turns with cross-session backfill + heartbeat outbox merge."""
    turns = read_tail_turns(session_dir, session_id, limit)

    # Backfill from previous session if current is too young
    if len(turns) < 5 and counter > 1:
        prev_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter - 1}"))
        prev_turns = read_tail_turns(session_dir, prev_sid, limit - len(turns))
        turns = prev_turns + turns

    # Merge heartbeat outbox (checkins, calendar alerts, etc.)
    jarvis_dir = os.environ.get("JARVIS_DIR", "")
    if jarvis_dir:
        outbox_path = Path(jarvis_dir) / "heartbeat_outbox.jsonl"
        hb_turns = read_heartbeat_outbox(outbox_path, limit=10)
        if hb_turns:
            turns = turns + hb_turns
            # Sort by timestamp so heartbeat messages appear in chronological order
            turns.sort(key=lambda t: t.get("ts", ""))
            turns = turns[-limit:]

    return format_recent_turns(turns)



# _extract_text is imported from core.textutil
