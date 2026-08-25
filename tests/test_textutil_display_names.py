"""The shared task-id → plain-Chinese display map (2026-08-24 card-style fix).

Guardian, watermarks and brain_health all speak to the owner through this one
map, so a rename lands everywhere at once and an unmapped id degrades to the
raw id — never to a truncated fragment (「c…」was undecodable on a live card).
"""

from core.heartbeat import parse_heartbeat
from core.textutil import (
    TASK_DISPLAY_NAMES,
    middle_ellipsize,
    task_display_name,
)


def test_mapped_ids_translate():
    assert task_display_name("intention-check") == "定时提醒"
    assert task_display_name("reply-followup") == "按钮跟进"
    assert task_display_name("cross-session-sync") == "跨会话同步"
    assert task_display_name("eigenflux-inbox-reconcile") == "EigenFlux 收件核对"
    assert task_display_name("guardian-daemon") == "系统守护"


def test_unmapped_id_falls_back_to_raw_untruncated():
    long_id = "some-brand-new-task-with-a-very-long-name"
    assert task_display_name(long_id) == long_id
    assert task_display_name("") == ""
    assert task_display_name(None) == ""


def test_routine_sources_show_the_users_own_routine_name():
    assert task_display_name("routine:午间拉伸") == "例程「午间拉伸」"
    assert task_display_name("routine:") == "例程"


def test_combined_delivery_sources_are_each_humanized():
    assert task_display_name("calendar-sync,intention-check") == \
        "日历同步、定时提醒"


def test_every_known_production_non_task_source_has_a_display_name():
    expected = {
        "manual", "mobile-onboarding", "pgc-improvement",
        "pgc_pulse", "release-canary", "test",
    }
    assert expected <= TASK_DISPLAY_NAMES.keys()


def test_pgc_metric_source_never_leaks_its_internal_id():
    assert task_display_name("pgc_pulse") == "PGC 指标日报"


def test_middle_ellipsize_keeps_title_subject_and_distinguishing_suffix():
    value = "跨会话动态：这是很长的一段说明但结尾是董事责任保险"
    result = middle_ellipsize(value, 20)
    assert len(result) == 20
    assert result.startswith("跨会话动态")
    assert result.endswith("董事责任保险")
    assert "…" in result


def test_every_heartbeat_task_has_a_display_name():
    """New HEARTBEAT.md tasks must join the map — otherwise their raw id
    reaches the owner's cards again the first time they fail."""
    import pathlib
    hb = pathlib.Path(__file__).resolve().parent.parent / "HEARTBEAT.md"
    missing = [t["name"] for t in parse_heartbeat(hb)
               if t["name"] not in TASK_DISPLAY_NAMES]
    assert missing == []


def test_display_names_are_chinese_or_product_names():
    """Values must be boss-readable: no bare snake/kebab ids as values."""
    for task_id, display in TASK_DISPLAY_NAMES.items():
        assert display.strip(), task_id
        assert not set(display) & {"_"}, (task_id, display)
        # Every display name carries at least one CJK char except pure
        # product names (EigenFlux).
        has_cjk = any("一" <= ch <= "鿿" for ch in display)
        assert has_cjk or display == "EigenFlux", (task_id, display)
