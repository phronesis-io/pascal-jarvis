"""Session compaction — summarizes old sessions on rotation.

When a session file exceeds max_size and rotates, this module extracts
all turns from the old session and calls Claude (opus) to produce a
compact summary. The summary is stored per-conv_key and injected into
the new session's system prompt so context is preserved across rotations.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from core.textutil import extract_text

NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

COMPACT_DIR_NAME = "session_compacts"

COMPACT_PROMPT = """\
你是一个对话压缩器。下面是一段用户和AI助手之间的对话记录。
请提取并保留以下关键信息，生成一份简洁的摘要（中文）：

1. **当前任务** — 用户正在做什么、进展到哪里
2. **关键决定** — 已经做出的选择（方案、命名、架构等）
3. **重要上下文** — 讨论中提到的关键事实、数据、人名、链接
4. **未完成的事项** — 还在进行中或等待用户确认的内容
5. **情绪/状态** — 用户当前的情绪状态或关注点（如果明显的话）

规则：
- 不要逐条复述对话，只提取核心信息
- 总长度控制在 800 字以内
- 用结构化的 markdown 格式
- 如果对话主要是闲聊且没有重要信息，就简短说明即可

---

对话记录：

"""


def _extract_all_turns(session_dir: Path, session_id: str,
                       max_chars: int = 80000) -> str:
    """Extract all user/assistant turns from a session JSONL, formatted as text."""
    path = session_dir / f"{session_id}.jsonl"
    if not path.exists():
        return ""

    lines = []
    total = 0
    for raw_line in path.open(encoding="utf-8", errors="ignore"):
        try:
            obj = json.loads(raw_line)
        except Exception:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message", {})
        role = msg.get("role", obj.get("type", ""))
        content = msg.get("content", "")
        text = extract_text(content)
        if not text:
            continue

        # Truncate very long individual messages
        if len(text) > 2000:
            text = text[:2000] + "...(truncated)"

        entry = f"**{role}**: {text}"
        if total + len(entry) > max_chars:
            break
        lines.append(entry)
        total += len(entry)

    return "\n\n".join(lines)


def get_compact_path(jarvis_dir: str | Path, conv_key: str) -> Path:
    """Return the path where a compact summary is stored for a conv_key."""
    return Path(jarvis_dir) / COMPACT_DIR_NAME / f"{conv_key}.md"


def read_compact(jarvis_dir: str | Path, conv_key: str) -> str:
    """Read the compact summary for a conv_key, if it exists."""
    path = get_compact_path(jarvis_dir, conv_key)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def generate_compact(jarvis_dir: str | Path, session_dir: str | Path,
                     old_session_id: str, conv_key: str,
                     work_dir: str | Path | None = None) -> str:
    """Generate a compact summary of an old session using Claude opus.

    Runs synchronously. Returns the compact text, also saves to disk.
    """
    jarvis_dir = Path(jarvis_dir)
    session_dir = Path(session_dir)

    # Extract conversation content
    content = _extract_all_turns(session_dir, old_session_id)
    if not content or len(content) < 200:
        # Too little content to bother compacting
        return ""

    prompt = COMPACT_PROMPT + content

    # Call Claude via CLI (opus — Pascal 2026-06-07: 全部用最好的模型，不计 token)
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "opus",
             "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=work_dir or jarvis_dir,
        )
        if result.returncode != 0:
            print(f"[compact] Claude exited with code {result.returncode}", file=sys.stderr)
            if result.stderr.strip():
                print(f"[compact] stderr: {result.stderr.strip()[:200]}", file=sys.stderr)
        compact_text = result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("[compact] Claude call timed out (60s)", file=sys.stderr)
        return ""
    except FileNotFoundError:
        print("[compact] Claude CLI not found", file=sys.stderr)
        return ""
    except OSError as e:
        print(f"[compact] OS error calling Claude: {e}", file=sys.stderr)
        return ""

    if not compact_text or len(compact_text) < 50:
        return ""

    # Save to disk
    compact_path = get_compact_path(jarvis_dir, conv_key)
    compact_path.parent.mkdir(parents=True, exist_ok=True)
    compact_path.write_text(compact_text, encoding="utf-8")

    return compact_text


def get_old_session_id(conv_key: str, current_counter: int) -> str:
    """Compute the session ID of the previous session."""
    if current_counter <= 1:
        return ""
    prev_counter = current_counter - 1
    return str(uuid.uuid5(NAMESPACE, f"{conv_key}-{prev_counter}"))
