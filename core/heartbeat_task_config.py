"""Parse heartbeat task declarations and classify model/privacy boundaries."""

from __future__ import annotations

import re
from pathlib import Path


TASK_MODELS = {"opus", "sonnet", "haiku", "gpt"}
TASK_MODEL_RANK = {"haiku": 1, "sonnet": 2, "gpt": 2, "opus": 3}
MEMORY_PURPOSES = {"inbound", "outbound"}


def parse_interval(value: str) -> int:
    match = re.match(r"(\d+)\s*(s|m|h|d)", value.strip())
    if not match:
        return 600
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _enabled(line: str) -> bool:
    return line.split(":", 1)[1].strip().lower() in {"true", "yes", "1"}


def _new_task(name: str) -> dict:
    return {
        "name": name,
        "interval": 600,
        "pre": "",
        "post": "",
        "prompt": "",
        "heavy": False,
        "timeout": None,
        "untrusted_input": False,
        "no_tools": False,
        "full_memory": False,
        "model": None,
        "memory_purpose": "inbound",
    }


def parse_heartbeat(path: str | Path) -> list[dict]:
    """Parse HEARTBEAT.md task blocks and apply private prompt overlays."""
    source = Path(path)
    tasks: list[dict] = []
    current: dict | None = None
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            if current:
                current.pop("_in_prompt", None)
                tasks.append(current)
            current = _new_task(line[4:].strip())
            continue
        if current is None:
            continue
        if line.startswith("- interval:"):
            current["interval"] = parse_interval(line.split(":", 1)[1])
        elif line.startswith("- pre:"):
            current["pre"] = line.split(":", 1)[1].strip()
        elif line.startswith("- post:"):
            current["post"] = line.split(":", 1)[1].strip()
        elif line.startswith("- heavy:"):
            current["heavy"] = _enabled(line)
        elif line.startswith("- untrusted-input:"):
            current["untrusted_input"] = _enabled(line)
        elif line.startswith("- no-tools:"):
            current["no_tools"] = _enabled(line)
        elif line.startswith("- full-memory:"):
            current["full_memory"] = _enabled(line)
        elif line.startswith("- model:"):
            value = line.split(":", 1)[1].strip().lower()
            current["model"] = value if value in TASK_MODELS else None
        elif line.startswith("- memory-purpose:"):
            value = line.split(":", 1)[1].strip().lower()
            current["memory_purpose"] = (
                value if value in MEMORY_PURPOSES else "inbound"
            )
        elif line.startswith("- timeout:"):
            try:
                current["timeout"] = int(line.split(":", 1)[1].strip())
            except ValueError:
                current["timeout"] = None
        elif line.startswith("- prompt:"):
            value = line.split(":", 1)[1].strip()
            if value == "|":
                current["_in_prompt"] = True
            else:
                current["prompt"] = value
        elif current.get("_in_prompt"):
            if line.startswith("- "):
                current.pop("_in_prompt", None)
            else:
                current["prompt"] += line.lstrip() + "\n"
    if current:
        current.pop("_in_prompt", None)
        tasks.append(current)

    overlay_dir = source.parent / "data" / "heartbeat_overlay"
    if overlay_dir.is_dir():
        for task in tasks:
            overlay = overlay_dir / f"{task['name']}.md"
            if overlay.is_file():
                task["prompt"] += "\n" + overlay.read_text(encoding="utf-8")
    return tasks


def highest_task_model(tasks: list[dict]) -> str | None:
    """Return the strongest declared model in a compatible Claude batch."""
    declared = [
        str(task.get("model") or "")
        for task in tasks
        if str(task.get("model") or "") in TASK_MODEL_RANK
    ]
    return max(declared, key=TASK_MODEL_RANK.__getitem__) if declared else None


def shared_batch_eligible(task: dict) -> bool:
    """Whether one task can join the ordinary trusted Claude batch."""
    return not any((
        task.get("heavy"),
        task.get("untrusted_input"),
        task.get("no_tools"),
        task.get("model") == "gpt",
        task.get("memory_purpose") == "outbound",
    ))


def policy_isolation_reason(task: dict) -> str:
    """Extra isolation after heavy, untrusted, and no-tool tasks peel off."""
    if not shared_batch_eligible(task):
        if (task.get("model") == "gpt" and not task.get("heavy")
                and not task.get("untrusted_input") and not task.get("no_tools")):
            return "model-route"
        if (task.get("memory_purpose") == "outbound" and not task.get("heavy")
                and not task.get("untrusted_input") and not task.get("no_tools")):
            return "outbound-privacy"
    return ""
