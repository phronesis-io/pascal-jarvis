"""Pure composition and rendering helpers for memorial cards.

The module has no repository root and performs no I/O.  Runtime hooks and
product policy remain owned by :mod:`core.memorial` and are passed in by its
compatibility wrappers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from core.card import build_card


def markdown_protected_lines(
    lines: list[str],
    *,
    fence_open_re: re.Pattern,
    list_fence_open_re: re.Pattern,
) -> set[int]:
    """Return directive-looking lines that are Markdown content."""
    protected: set[int] = set()
    fence_char = ""
    fence_len = 0
    fence_close_indent = 3
    lazy_container = False

    def indent_columns(value: str) -> int:
        columns = 0
        for char in value:
            if char == " ":
                columns += 1
            elif char == "\t":
                columns += 4 - (columns % 4)
            else:
                break
        return columns

    for index, line in enumerate(lines):
        if fence_char:
            protected.add(index)
            stripped = line.lstrip(" \t")
            if (indent_columns(line) <= fence_close_indent
                    and re.fullmatch(
                        rf"{re.escape(fence_char)}{{{fence_len},}}[ \t]*",
                        stripped)):
                fence_char, fence_len, fence_close_indent = "", 0, 3
            continue
        if not line.strip():
            lazy_container = False
            continue
        if lazy_container:
            protected.add(index)
            continue
        fence = fence_open_re.match(line)
        list_fence = list_fence_open_re.match(line)
        if fence and not (
                fence.group(1).startswith("`") and "`" in fence.group(2)):
            marker = fence.group(1)
            fence_char, fence_len = marker[0], len(marker)
            protected.add(index)
            continue
        if list_fence and not (
                list_fence.group(2).startswith("`")
                and "`" in list_fence.group(3)):
            marker = list_fence.group(2)
            fence_char, fence_len = marker[0], len(marker)
            fence_close_indent = indent_columns(list_fence.group(1)) + 3
            protected.add(index)
            continue
        if line.lstrip().startswith(">"):
            protected.add(index)
            lazy_container = True
            continue
        if re.match(r"^ {0,3}(?:[-+*]|\d{1,9}[.)])[ \t]+", line):
            protected.add(index)
            lazy_container = True
            continue
        if indent_columns(line) >= 4:
            protected.add(index)
    return protected


def extract_inline_options(
    text: str,
    *,
    protected_lines: Callable[[list[str]], set[int]],
    options_line_re: re.Pattern,
    options_split_re: re.Pattern,
    max_label_chars: int,
    max_options: int,
) -> tuple[str, list[dict] | None]:
    lines = str(text or "").splitlines()
    protected = protected_lines(lines)
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        if index in protected:
            return text, None
        match = options_line_re.match(lines[index])
        if not match:
            return text, None
        labels: list[str] = []
        for part in options_split_re.split(match.group(1)):
            part = part.strip().strip("「」\"'")
            if part and part not in labels:
                labels.append(part[:max_label_chars])
        if not labels:
            return text, None
        body = "\n".join(lines[:index]).rstrip()
        return body, [
            {"key": f"r{offset}", "label": label, "action": None,
             "reply": True}
            for offset, label in enumerate(labels[:max_options], 1)
        ]
    return text, None


def split_authored_card_blocks(
    text: str,
    *,
    protected_lines: Callable[[list[str]], set[int]],
    any_title_line_re: re.Pattern,
) -> list[str]:
    raw = str(text or "")
    lines = raw.splitlines()
    protected = protected_lines(lines)
    title_positions = [
        index for index, line in enumerate(lines)
        if index not in protected and any_title_line_re.match(line)
    ]
    if len(title_positions) < 2:
        return [raw]
    prefix = lines[:title_positions[0]]
    blocks: list[str] = []
    for offset, start in enumerate(title_positions):
        end = (title_positions[offset + 1]
               if offset + 1 < len(title_positions) else len(lines))
        block_lines = list(lines[start:end])
        if offset == 0 and any(line.strip() for line in prefix):
            block_lines = [block_lines[0], *prefix, *block_lines[1:]]
        blocks.append("\n".join(block_lines).strip())
    return blocks


def scrub_embedded_authoring_directives(
    text: str,
    *,
    protected_lines: Callable[[list[str]], set[int]],
    any_options_line_re: re.Pattern,
    any_recommend_line_re: re.Pattern,
    any_title_line_re: re.Pattern,
) -> str:
    lines = str(text or "").splitlines()
    protected = protected_lines(lines)
    cleaned: list[str] = []
    for index, line in enumerate(lines):
        if index in protected:
            cleaned.append(line)
            continue
        if any_options_line_re.match(line) or any_recommend_line_re.match(line):
            continue
        title_match = any_title_line_re.match(line)
        if title_match:
            value = title_match.group(1).strip()
            if value:
                cleaned.append(f"**{value}**")
        else:
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def extract_recommendation(
    text: str,
    *,
    protected_lines: Callable[[list[str]], set[int]],
    recommend_line_re: re.Pattern,
    any_recommend_line_re: re.Pattern,
    recommend_split_re: re.Pattern,
    max_label_chars: int,
    max_why_chars: int,
) -> tuple[str, dict | None]:
    lines = str(text or "").splitlines()
    protected = protected_lines(lines)
    seen = 0
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].strip():
            continue
        if index in protected:
            return text, None
        seen += 1
        match = recommend_line_re.match(lines[index])
        if match:
            parts = recommend_split_re.split(match.group(1), maxsplit=1)
            label = parts[0].strip().strip("「」\"'")
            why = parts[1].strip() if len(parts) > 1 else ""
            body = "\n".join(lines[:index] + lines[index + 1:]).rstrip()
            if not label or not why:
                return body, None
            return body, {
                "label": label[:max_label_chars],
                "why": why[:max_why_chars],
            }
        if any_recommend_line_re.match(lines[index]):
            body = "\n".join(lines[:index] + lines[index + 1:]).rstrip()
            return body, None
        if seen >= 3:
            break
    return text, None


def normalize_recommendation(
    recommend: dict | None,
    options: list[dict],
    *,
    max_why_chars: int,
) -> dict | None:
    if not isinstance(recommend, dict):
        return None
    label = str(recommend.get("label", "")).strip()
    why = str(recommend.get("why", "")).strip()
    if not label or not why:
        return None
    for option in options:
        if label in (str(option.get("label", "")).strip(),
                     str(option.get("key", "")).strip()):
            return {
                "key": str(option.get("key", "")),
                "label": str(option.get("label", "")),
                "why": why[:max_why_chars],
            }
    return None


def normalize_options(
    options: list[dict] | None,
    preset: str | None,
    *,
    presets: dict[str, list[dict]],
    reserved_keys: set[str],
) -> list[dict]:
    if options is not None:
        normalized = []
        seen: set[str] = set()
        for index, option in enumerate(options, 1):
            key = str(option.get("key", "") or f"opt{index}").strip()
            label = str(option.get("label", "")).strip()
            if not label:
                raise ValueError(f"option #{index} has no label")
            if key in reserved_keys:
                raise ValueError(f"option key '{key}' is reserved")
            if key in seen:
                raise ValueError(f"duplicate option key: {key}")
            seen.add(key)
            item = {
                "key": key,
                "label": label,
                "action": option.get("action") or None,
            }
            if option.get("reply"):
                item["reply"] = True
            normalized.append(item)
        return normalized
    name = preset or "fyi"
    if name not in presets:
        raise ValueError(
            f"unknown preset: {name} (have: {', '.join(sorted(presets))})")
    return [dict(option) for option in presets[name]]


def normalize_extra_buttons(buttons: list[dict] | None) -> list[dict]:
    normalized: list[dict] = []
    for index, button in enumerate(buttons or [], 1):
        text = str(button.get("text", "")).strip()
        if not text:
            raise ValueError(f"extra button #{index} has no text")
        item = {"text": text}
        if button.get("url"):
            item["url"] = str(button["url"])
        elif isinstance(button.get("value"), dict):
            item["value"] = dict(button["value"])
        else:
            raise ValueError(f"extra button #{index} needs url or value")
        normalized.append(item)
    return normalized


def header(state: dict, source_emoji: dict[str, str]) -> str:
    emoji = source_emoji.get(state["source"], "")
    return " ".join(part for part in ("📜", emoji, state["title"]) if part)


def button_groups(
    state: dict,
    *,
    include_options: bool,
    include_chat: bool,
    chat_button_label: str,
    chat_opt_key: str,
    confused_opt_key: str,
) -> list[list[dict]]:
    groups: list[list[dict]] = []
    if include_options and state.get("options"):
        recommended = str((state.get("recommend") or {}).get("key", ""))
        keys = [str(option.get("key", "")) for option in state["options"]]
        primary = keys.index(recommended) if recommended in keys else 0
        groups.append([
            {
                "text": option.get("label", option.get("key", "")),
                "type": "primary" if index == primary else "default",
                "value": {
                    "action": "memorial",
                    "id": state["id"],
                    "opt": option.get("key", ""),
                },
            }
            for index, option in enumerate(state["options"])
        ])
    if state.get("extra_buttons"):
        groups.append([
            {**dict(button), "type": "default"}
            for button in state["extra_buttons"]
        ])
    if include_chat:
        groups.append([
            {
                "text": chat_button_label,
                "type": "default",
                "value": {
                    "action": "memorial",
                    "id": state["id"],
                    "opt": chat_opt_key,
                },
            },
            {
                "text": "🤔 看不懂",
                "type": "default",
                "value": {
                    "action": "memorial",
                    "id": state["id"],
                    "opt": confused_opt_key,
                },
            },
        ])
    return groups


def cut_at_boundary(text: str, limit: int) -> str:
    """Cut on a line/space boundary and never inside a Markdown link."""
    cut = text[:limit]
    for separator in ("\n", " "):
        if separator in cut[limit // 2:]:
            cut = cut.rsplit(separator, 1)[0]
            break
    last_open = cut.rfind("[")
    if last_open != -1:
        after = cut[last_open:]
        close_bracket = after.find("]")
        if close_bracket == -1 or after.find(")", close_bracket) == -1:
            cut = cut[:last_open].rstrip()
    return cut.rstrip()


def display_body(
    body: str,
    *,
    max_lines: int,
    max_chars: int,
    clip_notice: str,
) -> str:
    raw = str(body or "").strip()
    lines = raw.splitlines()
    clipped = len(lines) > max_lines
    text = "\n".join(lines[:max_lines]).strip()
    if len(text) > max_chars:
        text = cut_at_boundary(text, max_chars)
        clipped = True
    if clipped:
        text += "\n\n" + clip_notice
    return text


def body_was_clipped(
    body: str,
    *,
    max_lines: int,
    max_chars: int,
    clip_notice: str,
) -> bool:
    return display_body(
        body,
        max_lines=max_lines,
        max_chars=max_chars,
        clip_notice=clip_notice,
    ).endswith(clip_notice)


def render_card(
    state: dict,
    *,
    body: str | None,
    status_line: str,
    include_options: bool,
    include_chat: bool,
    source_emoji: dict[str, str],
    chat_button_label: str,
    chat_opt_key: str,
    confused_opt_key: str,
    max_lines: int,
    max_chars: int,
    clip_notice: str,
    alert_attention: str,
    requires_decision: Callable[[dict], bool],
) -> str:
    audit_text = state.get("authoring_audit_text")
    if audit_text is not None:
        from core.safety import IDLE_SENTINEL, sentinel_present
        if sentinel_present(audit_text):
            return ""
        escaped_title = header(state, source_emoji).replace(
            IDLE_SENTINEL, r"HEARTBEAT\_OK")
    else:
        escaped_title = header(state, source_emoji)
    content = display_body(
        state["body"] if body is None else body,
        max_lines=max_lines,
        max_chars=max_chars,
        clip_notice=clip_notice,
    )
    if audit_text is not None:
        content = content.replace(IDLE_SENTINEL, r"HEARTBEAT\_OK")
    recommendation = state.get("recommend") or {}
    if (include_options and recommendation.get("label")
            and recommendation.get("why")):
        content += (
            f"\n\n**建议：{recommendation['label']}** — "
            f"{recommendation['why']}"
        )
    if state.get("status") == "pending":
        attention = str(state.get("attention", ""))
        if attention == alert_attention:
            role = "⚡ 即时提醒 · 不用批"
        elif requires_decision(state):
            role = "🎯 等你拍一个"
        else:
            role = "ℹ️ 知道就行"
        content = f"{role}\n\n{content}"
    if status_line:
        content += "\n\n" + status_line
    return build_card(
        escaped_title,
        content,
        button_groups=button_groups(
            state,
            include_options=include_options,
            include_chat=include_chat,
            chat_button_label=chat_button_label,
            chat_opt_key=chat_opt_key,
            confused_opt_key=confused_opt_key,
        ),
    )


def replacement_card(
    rendered: str,
    state: dict,
    *,
    web_desk_url: Callable[[str], str],
    chat_button_label: str,
    chat_opt_key: str,
) -> dict:
    if rendered:
        try:
            card = json.loads(rendered)
            if isinstance(card, dict):
                return card
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    buttons = [{
        "text": chat_button_label,
        "type": "default",
        "value": {
            "action": "memorial",
            "id": state["id"],
            "opt": chat_opt_key,
        },
    }]
    desk = web_desk_url(f"/items/{state['id']}")
    if desk:
        buttons.insert(0, {"text": "打开事项", "url": desk})
    fallback = build_card(
        "Jarvis · 事项",
        ("状态已更新。完整记录在下面的「打开事项」里。" if desk
         else "状态已更新。完整记录已存档，随时可以问我。"),
        button_groups=[buttons],
    )
    return json.loads(fallback)
