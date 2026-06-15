"""REQ-64 reply-based closure classifier."""
from core import reply_closure as rc


def test_done_signals():
    for t in ["约了周四下午", "搞定", "做完了 ✅", "已经约了", "去了", "嗯"]:
        assert rc.classify_reply(t) == "done", t


def test_recorded_signals():
    for t in ["没做，改天吧", "还没空", "没有", "下次再说", "忘了"]:
        assert rc.classify_reply(t) == "recorded", t


def test_na_signals():
    for t in ["不用追了", "算了", "取消这个吧", "别管了"]:
        assert rc.classify_reply(t) == "na", t


def test_ambiguous_returns_none():
    for t in ["今天天气不错", "", "你觉得呢", "凯瑞老师那边"]:
        assert rc.classify_reply(t) is None, t


def test_strips_quote_marker():
    assert rc.classify_reply("[Replying to: <card>约上了吗</card>] 约了") == "done"


def test_short_result_strips_marker():
    r = rc.short_result("[Replying to: <card>x</card>] 约了周四下午茶")
    assert "Replying to" not in r and "约了" in r


def test_na_beats_negation():
    # '不用追了' must be na, not recorded (the negation 不 is present)
    assert rc.classify_reply("这个不用追了") == "na"
