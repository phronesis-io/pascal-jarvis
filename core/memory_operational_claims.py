"""Migrate a small allowlist of obsolete operational claims in memory."""

from __future__ import annotations

import os
import re
from pathlib import Path


INDEX_TODO = (
    "- 【warm 记忆索引模式 · 已上线】生产默认使用 `JARVIS_WARM_MEMORY_MODE=index`；"
    "有文件读取能力的调用按索引取知识，无工具/受限调用继续内联 full，避免知识不可达。"
)
INDEX_THREAD = (
    "  另：PR #100 的按需索引模式已上线；有文件读取能力的调用用 index，"
    "无工具或受限调用保留 full。"
)
INDEX_DIGEST = (
    "- 记忆索引模式已上线（生产默认 index；无工具/受限调用保留 full），"
    "注入规模由运行时指标持续复核。"
)
RESTART_SECTION = """## 重启 Jarvis 的正确方式
生产发布只走 `./restart.sh --full --yes`：它先校验 release gate 和 Owner
收据，再由 launchd kickstart，并在启动后运行 `core.deploy verify`。不要手动
`pkill` 或直接运行 `launchctl kickstart`；同版本恢复用 `./restart.sh --runtime
--yes`，普通状态检查用 `./restart.sh --status`。
"""


def _replace_known(path: Path) -> bool:
    try:
        original = path.read_text(encoding="utf-8")
    except OSError:
        return False
    text = original
    lines = []
    for line in text.splitlines():
        if "warm 记忆索引模式" in line and "生产仍是关的" in line:
            line = INDEX_TODO
        elif "PR #100" in line and "索引模式仍是关的" in line:
            line = INDEX_THREAD
        elif "记忆索引模式已做好" in line and "开关要写进 bot.sh" in line:
            line = INDEX_DIGEST
        line = line.replace(
            "详见 warm/interests.md「投资内容边界」（2026-06-07）",
            "具体边界以私有 triage_profile 与当前有效画像为准",
        )
        lines.append(line)
    text = "\n".join(lines) + ("\n" if original.endswith("\n") else "")
    text = re.sub(
        r"## 重启 Jarvis 的正确方式\n.*?(?=\n## |\Z)",
        RESTART_SECTION.rstrip(),
        text,
        flags=re.DOTALL,
    )
    if text == original:
        return False
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return True


def reconcile_operational_claims(memory_dir: str | Path) -> list[str]:
    root = Path(memory_dir)
    candidates = (
        root / "system" / "todos.md",
        root / "system" / "open_threads.md",
        root / "timeline" / "longterm_digest.md",
        root / "hot" / "behavioral_rules.md",
        root / "hot" / "feedback_rules.md",
        root / "open_threads.md",
    )
    changed = []
    for path in candidates:
        if path.is_file() and not path.is_symlink() and _replace_known(path):
            changed.append(str(path.relative_to(root)))
    return changed
