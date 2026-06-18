"""Prompt A/B experiment selection for heartbeat tasks.

Experiments live in memory, not in HEARTBEAT.md, so new prompt variants can be
proposed/reviewed as data before they influence production behavior.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPERIMENTS_FILE = "system/prompt_experiments.json"


@dataclass(frozen=True)
class PromptVariant:
    experiment_id: str
    variant_id: str
    instruction: str

    def to_log(self) -> dict[str, str]:
        return {
            "prompt_experiment": self.experiment_id,
            "prompt_variant": self.variant_id,
        }


def choose_variant(memory_dir: str | Path, task_name: str,
                   now: float | None = None) -> PromptVariant | None:
    """Return the active variant for a task, or None when no experiment applies.

    Selection is deterministic per local day + task + experiment id, so a task
    does not flap variants within a day while still giving the framework enough
    rotation for engagement measurement over time.
    """
    cfg = _load_config(Path(memory_dir) / EXPERIMENTS_FILE)
    if not cfg:
        return None
    for exp in cfg.get("experiments", []):
        if not isinstance(exp, dict):
            continue
        if exp.get("enabled") is False or exp.get("status") in {"draft", "paused", "disabled"}:
            continue
        if str(exp.get("task") or "").strip() != task_name:
            continue
        variants = _valid_variants(exp.get("variants", []))
        if not variants:
            continue
        chosen = _weighted_pick(exp_id=str(exp.get("id") or task_name),
                                task_name=task_name, variants=variants,
                                now=now)
        if chosen is None:
            continue
        return PromptVariant(
            experiment_id=str(exp.get("id") or task_name)[:80],
            variant_id=chosen["id"][:80],
            instruction=chosen["instruction"][:2000],
        )
    return None


def inject_variant(prompt: str, variant: PromptVariant | None) -> str:
    """Append experiment steering to a task prompt without hiding the base prompt."""
    if variant is None or not variant.instruction.strip():
        return prompt
    return (
        f"{prompt.rstrip()}\n\n"
        "[Prompt experiment]\n"
        f"Experiment: {variant.experiment_id}\n"
        f"Variant: {variant.variant_id}\n"
        "Apply this variant as a soft style/structure steering layer. "
        "Do not mention the experiment to Pascal.\n"
        f"{variant.instruction.strip()}"
    )


def _load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_variants(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    variants = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        vid = str(item.get("id") or "").strip()
        instruction = str(item.get("instruction") or item.get("prompt") or "").strip()
        if not vid:
            continue
        try:
            weight = max(0, int(item.get("weight", 1)))
        except (TypeError, ValueError):
            weight = 1
        if weight <= 0:
            continue
        variants.append({"id": vid, "instruction": instruction, "weight": weight})
    return variants


def _weighted_pick(exp_id: str, task_name: str, variants: list[dict[str, Any]],
                   now: float | None = None) -> dict[str, Any] | None:
    total = sum(v["weight"] for v in variants)
    if total <= 0:
        return None
    now = time.time() if now is None else now
    local_day = time.strftime("%Y-%m-%d", time.localtime(now))
    key = f"{local_day}:{task_name}:{exp_id}"
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % total
    cursor = 0
    for variant in variants:
        cursor += variant["weight"]
        if bucket < cursor:
            return variant
    return variants[-1]
