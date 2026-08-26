"""Exact cache-block assembly for stateless Heartbeat model calls."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path


PromptCache = dict[tuple, tuple[float, str]]


def build_prompt_pair(
    *,
    persona: str,
    memory_dir: str | Path,
    prompt: str,
    acting_section: str,
    restrict_tools: bool,
    allow_tools: bool,
    full_memory: bool,
    memory_purpose: str,
    mem_budget: int | None,
    warm_mode: str,
    cacheable_provider: bool,
    cache: PromptCache,
    load_memory: Callable,
    now_string: Callable[[str], str],
) -> tuple[str, str]:
    """Return an exact system block plus a time-current user request.

    Claude caches complete content blocks. Reordering memory or appending a
    changing clock inside one system string rewrites the whole block. Primary
    calls therefore reuse one bounded snapshot per trust/tool profile; live
    task DATA and time remain in the uncached user request.
    """
    cacheable = (
        cacheable_provider
        and not full_memory
        and memory_purpose == "inbound"
    )
    cache_key = (
        bool(restrict_tools), bool(allow_tools), warm_mode, persona,
    )
    try:
        cache_ttl = max(
            0, int(os.environ.get("HEARTBEAT_SYSTEM_PROMPT_TTL", "3600")))
    except ValueError:
        cache_ttl = 3600
    cached = cache.get(cache_key)
    cache_age = time.monotonic() - cached[0] if cached else None
    if (cacheable and cache_ttl > 0 and cached and cache_age is not None
            and cache_age < cache_ttl):
        system_prompt = cached[1]
    else:
        # Focus ordering helps constrained providers preserve relevant notes.
        # On the primary path it only makes the system block task-dependent.
        mem_kwargs = {
            "max_chars": mem_budget,
            "focus_text": prompt if mem_budget is not None else "",
        }
        if memory_purpose != "inbound":
            mem_kwargs["purpose"] = memory_purpose
        if warm_mode != "full":
            mem_kwargs["warm_mode"] = warm_mode
        try:
            memory = load_memory(memory_dir, **mem_kwargs)
        except TypeError as exc:
            if not any(name in str(exc) for name in (
                    "max_chars", "focus_text", "warm_mode", "purpose")):
                raise
            if memory_purpose == "outbound":
                print("[heartbeat] outbound memory filter unavailable; "
                      "withholding memory for this call", file=sys.stderr)
                memory = (
                    "(personal memory withheld: outbound filter unavailable)"
                )
            else:
                print("[heartbeat] load_tiered_memory signature mismatch; "
                      f"falling back to the legacy one-argument call "
                      f"(memory budget and warm_mode dropped): {exc}",
                      file=sys.stderr)
                memory = load_memory(memory_dir)
        if restrict_tools:
            memory = "(personal memory withheld for untrusted-input isolation)"
        system_prompt = f"""You are {persona}, a personal AI assistant and life mentor.
## 主动输出＝先判断注意力（任务指定了 JSON 格式的仍按任务格式）
- 需要 Pascal 明确选择：才是待批奏折，必须给真实分支的 OPTIONS。
- 紧急告警：可以不带 OPTIONS，系统会推飞书但不算待批。
- 纯周知：省略 OPTIONS，仍会推一张飞书知会卡并占当天额度；
  确实值得 Pascal 现在知道才写。
- 一次只说一件事；确有多件独立事，用单独一行 "---" 分隔。
- 第一句就是结论；背景能省就省。正文最多三行：什么事、为什么现在说、
  Pascal 要做什么。不需要他做什么就明确写「知道就行」。
- 每件事第一行写 `TITLE: 一句话说清这件事`（≤40字）。这是他扫一眼决定
  点不点开的唯一依据，不写就退回「Intent」这类按来源起的泛标题。
- 最后一行写 `OPTIONS: 回复1 | 回复2`（2-4 个，每个=他会打的那句回复本身，
  第一人称≤14字，覆盖真实分支含「不做」）。不要为了获得推送而虚构选项。
- 正文说人话：无 SLA/HTTP 码/内部黑话。

{acting_section}

You have access to the user's memory below. Use it to personalize your responses.

{memory}

The user request carries the authoritative current time."""
        if cacheable and cache_ttl > 0:
            cache[cache_key] = (time.monotonic(), system_prompt)

    now_ts = now_string("%Y-%m-%d %H:%M %A")
    request_prompt = f"{prompt.rstrip()}\n\nCurrent time: {now_ts}"
    return system_prompt, request_prompt
