"""Closure names / card titles carry the matter, not the mechanism.

2026-08-24 card-style audit: cards shipped titled with a stacked mechanism
prefix hard-cut mid-sentence (「闭环再问: <某服务> token 4…」-shaped) and with
doubled mechanism words (「闭环: …… 后闭环」). closure_matter strips the
machinery; _card_title cuts on a word/CJK boundary with「…」. All fixture
matters here are synthetic (repo privacy rule: fixtures never quote live
ledger content).
"""

from core.textutil import closure_matter
from tasks.intentions_post import _card_title


def test_matter_strips_mechanism_prefix_and_suffix():
    assert closure_matter("闭环: 整理书房") == "整理书房"
    assert closure_matter("和朋友吃饭 后闭环") == "和朋友吃饭"
    assert closure_matter("闭环: 和朋友吃饭 后闭环") == "和朋友吃饭"
    assert closure_matter("闭环再问: 示例服务 key 无效是否已解决") == \
        "示例服务 key 无效是否已解决"
    assert closure_matter("跟进：借书归还") == "借书归还"
    assert closure_matter("组会（事后跟进）") == "组会"


def test_matter_keeps_compound_names_containing_the_suffix_words():
    # 「后闭环」strips only as the template's own「 后闭环」tail (always
    # space-separated); a name that merely CONTAINS the characters survives.
    assert closure_matter("示例餐厅饭后闭环") == "示例餐厅饭后闭环"


def test_matter_never_returns_empty():
    # A name that IS only mechanism words survives as itself, never "".
    assert closure_matter("闭环再问") == "闭环再问"
    assert closure_matter("") == ""


def test_card_title_short_names_pass_through():
    assert _card_title("整理书房") == "整理书房"
    assert _card_title("") == "跟进"


def test_card_title_strips_legacy_mechanism_prefix():
    assert _card_title("闭环: 整理书房") == "整理书房"


def test_card_title_truncates_with_ellipsis_on_a_boundary():
    long_name = "示例服务 key 无效是否已解决以及后续的续期安排还要不要跟"
    title = _card_title(long_name)
    assert title.endswith("…")
    assert len(title) <= 24


def test_card_title_never_cuts_inside_an_ascii_word():
    # CJK run, then an ASCII word straddling the limit: the cut backs up to
    # the word boundary instead of shipping a chopped token.
    name = "这句话很长很长很长很长很长很 verylongtoken 结尾"
    title = _card_title(name, limit=20)
    assert title.endswith("…")
    body = title.rstrip("…")
    assert not (body and body[-1].isascii() and body[-1].isalnum())


def test_card_title_ascii_backoff_keeps_a_survivable_title():
    # A long ASCII token near the FRONT: word-boundary backoff must not erase
    # the whole matter — below the survivable floor it falls back to a plain
    # CJK-safe hard cut with「…」instead of a near-empty title.
    name = "查 averyveryverylongtokenvalue 的续期这件事到底办了没有"
    title = _card_title(name, limit=20)
    assert title.endswith("…")
    assert len(title.rstrip("…")) >= 8
