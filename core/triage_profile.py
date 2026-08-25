"""Bounded, explicitly configured relevance context for isolated triage.

Untrusted feed, friend and mail bodies run without personal memory or tools.
They may receive this small allowlisted projection from private jarvis.yaml;
no mailbox addresses, phone numbers, URLs, free-form notes or memory files are
read here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from core.config import Config

PROFILE_FIELDS = {
    "domains": "关注领域",
    "projects": "当前项目",
    "organizations": "相关组织",
    "portfolio_sectors": "持仓板块",
}
MAX_VALUES_PER_FIELD = 12
MAX_VALUE_CHARS = 80
_UNSAFE_VALUE_RE = re.compile(
    r"(?:https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\b1[3-9]\d{9}\b|\b(?:api[_ -]?key|token|password|secret)\b)",
    re.I,
)


def _safe_values(value) -> list[str]:
    if not isinstance(value, list):
        return []
    clean: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())[:MAX_VALUE_CHARS]
        if not text or _UNSAFE_VALUE_RE.search(text):
            continue
        if text not in clean:
            clean.append(text)
        if len(clean) >= MAX_VALUES_PER_FIELD:
            break
    return clean


def load_profile(config: Config | None = None) -> dict[str, list[str]]:
    """Return only the four allowlisted profile fields from private config."""
    config = config or Config()
    raw = config.get("triage_profile", {})
    if not isinstance(raw, dict):
        return {}
    return {
        field: values
        for field in PROFILE_FIELDS
        if (values := _safe_values(raw.get(field)))
    }


def render_profile(config: Config | None = None) -> str:
    """Render a short DATA block; absence is explicit rather than invented."""
    profile = load_profile(config)
    lines = ["=== RELEVANCE PROFILE (owner-configured, bounded) ==="]
    if not profile:
        lines.append("未配置；只按内容本身判断，不猜用户关系、持仓或项目。")
        return "\n".join(lines)
    for field, label in PROFILE_FIELDS.items():
        if profile.get(field):
            lines.append(f"{label}: {'、'.join(profile[field])}")
    return "\n".join(lines)


def main() -> int:
    runtime_root = os.environ.get("JARVIS_DIR", "").strip()
    config = (
        Config(Path(runtime_root) / "jarvis.yaml")
        if runtime_root
        else Config()
    )
    print(render_profile(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
