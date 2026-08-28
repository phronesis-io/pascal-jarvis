"""Deterministic privacy boundary for unattended external messages."""

from __future__ import annotations

import re


# Category-based synthetic patterns only. Personal values belong in private
# configuration, never in this public repository.
_OUTBOUND_BLOCKLIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal-host", re.compile(
        r"\blocalhost\b|127\.0\.0\.1|\b0\.0\.0\.0\b"
        r"|\b192\.168\.\d{1,3}\.\d{1,3}\b|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
        r"|\b(?:aliap|aliapst|aliapmo)\b|\btailscale\b")),
    ("internal-port", re.compile(r":(?:1200|3456|3457|3458)\b")),
    ("credential", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password"
        r"|passwd|secret[_-]?key|private[_-]?key|credentials?\.json)\b"
        r"|\bBearer\s+[A-Za-z0-9_\-.]{8,}")),
    ("schedule-or-health", re.compile(
        r"日程|行程|会议安排|体检|医院|就诊|病历|复诊|手术|康复|健康状况"
        r"|吃药|服药")),
    ("personal-contact", re.compile(
        r"住址|家庭地址|手机号|电话号码|微信号|身份证")),
    ("business-metric", re.compile(
        r"用户数|注册用户|日活|月活|营收|收入|增长率|留存率"
        r"|\bDAU\b|\bMAU\b|\bARR\b|\bMRR\b|agent\s*数")),
)


def outbound_content_gate(content: str) -> str:
    """Return the privacy category hit by content, or ``""`` when safe."""
    text = str(content or "")
    for rule, pattern in _OUTBOUND_BLOCKLIST:
        if pattern.search(text):
            return rule
    return ""
