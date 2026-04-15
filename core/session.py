"""Session management — tracks Claude Code sessions with auto-rotation."""

import json
import uuid
from pathlib import Path

NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


class SessionManager:
    """Manages session IDs with auto-rotation when session files get too large."""

    def __init__(self, tracker_path: str | Path, session_dir: str | Path,
                 max_size: int = 512000):
        self.tracker_path = Path(tracker_path)
        self.session_dir = Path(session_dir)
        self.max_size = max_size
        self._tracker = self._load()

    def _load(self) -> dict:
        if self.tracker_path.exists():
            return json.loads(self.tracker_path.read_text())
        return {}

    def _save(self):
        self.tracker_path.parent.mkdir(parents=True, exist_ok=True)
        self.tracker_path.write_text(json.dumps(self._tracker, indent=2))

    def get_session(self, conv_key: str) -> tuple[str, bool]:
        """Get or create a session ID for a conversation key.

        Returns (session_id, rotated) where rotated=True if a new session
        was created due to size overflow.
        """
        entry = self._tracker.get(conv_key, {})
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
            self._tracker[conv_key] = {"session_id": sid, "counter": counter}
            self._save()

        return sid, rotated

    def get_counter(self, conv_key: str) -> int:
        return self._tracker.get(conv_key, {}).get("counter", 0)


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
        ts = obj.get("timestamp", "")[:16]
        turns.append({"role": role, "text": text, "ts": ts})

    return turns[-limit:]


def format_recent_turns(turns: list[dict], max_msg_chars: int = 400,
                        max_total_chars: int = 6000) -> str:
    """Format turns into a markdown block for system prompt injection."""
    if not turns:
        return ""

    formatted = []
    total = 0
    for turn in reversed(turns):
        snippet = turn["text"][:max_msg_chars]
        if len(turn["text"]) > max_msg_chars:
            snippet += "..."
        line = f"[{turn['ts']}] **{turn['role']}**: {snippet}"
        if total + len(line) > max_total_chars:
            break
        formatted.append(line)
        total += len(line)
    formatted.reverse()

    lines = ["## Recent Turns", ""]
    lines.extend(f"{line}\n" for line in formatted)
    return "\n".join(lines)


def build_recent_turns(session_dir: str | Path, session_id: str,
                       counter: int, conv_key: str,
                       limit: int = 20) -> str:
    """Build recent turns with cross-session backfill."""
    turns = read_tail_turns(session_dir, session_id, limit)

    # Backfill from previous session if current is too young
    if len(turns) < 5 and counter > 1:
        prev_sid = str(uuid.uuid5(NAMESPACE, f"{conv_key}-{counter - 1}"))
        prev_turns = read_tail_turns(session_dir, prev_sid, limit - len(turns))
        turns = prev_turns + turns

    return format_recent_turns(turns)


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts).strip()
    return ""
