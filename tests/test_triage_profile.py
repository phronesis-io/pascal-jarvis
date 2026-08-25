from __future__ import annotations

import os
import subprocess
import sys

from core.triage_profile import load_profile, render_profile


class _Config:
    def __init__(self, value):
        self.value = value

    def get(self, key, default=None):
        return self.value if key == "triage_profile" else default


def test_profile_is_allowlisted_bounded_and_deduplicated():
    config = _Config({
        "domains": ["agent systems", "agent systems", "recommenders"],
        "projects": ["EigenFlux", "person@example.com", "token abc"],
        "organizations": "not-a-list",
        "portfolio_sectors": [f"sector-{index}" for index in range(20)],
        "private_notes": ["must never appear"],
    })

    profile = load_profile(config)

    assert profile["domains"] == ["agent systems", "recommenders"]
    assert profile["projects"] == ["EigenFlux"]
    assert len(profile["portfolio_sectors"]) == 12
    assert "organizations" not in profile
    assert "private_notes" not in profile


def test_empty_profile_is_honest_and_contains_no_personal_guess():
    text = render_profile(_Config({}))

    assert "未配置" in text
    assert "不猜用户关系" in text


def test_rendered_profile_contains_only_safe_categories():
    text = render_profile(_Config({
        "domains": ["agent infrastructure"],
        "projects": ["EigenFlux"],
        "organizations": ["Research Team"],
        "portfolio_sectors": ["semiconductors"],
    }))

    assert "关注领域: agent infrastructure" in text
    assert "当前项目: EigenFlux" in text
    assert "相关组织: Research Team" in text
    assert "持仓板块: semiconductors" in text


def test_triage_profile_cli_is_honest_when_unconfigured(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "core.triage_profile"],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "JARVIS_DIR": str(tmp_path)},
    )

    assert "RELEVANCE PROFILE" in result.stdout
    assert "未配置" in result.stdout
