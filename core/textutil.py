"""Shared text utilities for Jarvis core modules."""


def extract_text(content) -> str:
    """Flatten a Claude Code message.content into a single plain-text string.

    Handles the three shapes produced by Claude Code sessions:
      - str (plain assistant text)
      - list of blocks (text / tool_use / tool_result) — only text blocks extracted
      - anything else → empty string
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts).strip()
    return ""
