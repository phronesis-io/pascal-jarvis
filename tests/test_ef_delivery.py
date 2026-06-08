"""Tests for the EigenFlux quiet-hours delivery gate (tasks/_ef_delivery.py)
and its integration into eigenflux_feed_post.py.

Pascal asked (2026-06-07): hold EigenFlux pushes at night, deliver one
consolidated card in the morning, let only urgent items break through.
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TASKS = Path(__file__).resolve().parent.parent / "tasks"
sys.path.insert(0, str(TASKS))
import _ef_delivery as efd  # noqa: E402

FEED_POST = TASKS / "eigenflux_feed_post.py"


def _run_feed(payload: str, tmp_path, extra_env=None) -> str:
    env = {"JARVIS_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, str(FEED_POST)], input=payload,
                       capture_output=True, text=True, env=env)
    return r.stdout


# ---- in_quiet_hours -------------------------------------------------------

def test_quiet_window_wraps_midnight():
    assert efd.in_quiet_hours(datetime(2026, 6, 7, 23, 0))   # 23:00 quiet
    assert efd.in_quiet_hours(datetime(2026, 6, 7, 2, 0))    # 02:00 quiet
    assert efd.in_quiet_hours(datetime(2026, 6, 7, 8, 59))   # 08:59 quiet
    assert not efd.in_quiet_hours(datetime(2026, 6, 7, 9, 0))    # 09:00 awake
    assert not efd.in_quiet_hours(datetime(2026, 6, 7, 14, 0))   # 14:00 awake
    assert not efd.in_quiet_hours(datetime(2026, 6, 7, 21, 59))  # 21:59 awake


def test_override_env(monkeypatch):
    monkeypatch.setenv("JARVIS_EF_QUIET_OVERRIDE", "quiet")
    assert efd.in_quiet_hours(datetime(2026, 6, 7, 14, 0))
    monkeypatch.setenv("JARVIS_EF_QUIET_OVERRIDE", "awake")
    assert not efd.in_quiet_hours(datetime(2026, 6, 7, 2, 0))


# ---- hold / drain ---------------------------------------------------------

def test_hold_and_drain_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    efd.hold("first card [a](https://x.com/1)", "eigenflux-feed")
    efd.hold("second card [b](https://x.com/2)", "eigenflux-research")
    held = efd.drain()
    assert len(held) == 2
    assert held[0]["message"].startswith("first")
    assert held[1]["source"] == "eigenflux-research"
    # drain clears the backlog
    assert efd.drain() == []


def test_hold_ignores_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    efd.hold("   ", "eigenflux-feed")
    assert efd.drain() == []


# ---- render_digest_body ---------------------------------------------------

def test_render_strips_chrome_and_dedups():
    held = [
        {"message": "📡 知会\n• 点 A 很重要 → 你可以看看\n📡 Powered by EigenFlux"},
        {"message": "📡 知会\n• 点 A 很重要 → 你可以看看\n• 点 B 不一样\n📡 Powered by EigenFlux"},
    ]
    body, n_total = efd.render_digest_body(held)
    assert n_total == 2  # deduped "点 A" + "点 B"
    assert body.count("点 A 很重要") == 1
    assert "点 B 不一样" in body
    assert "📡 知会" not in body  # section header stripped
    assert body.strip().endswith("📡 Powered by EigenFlux")


def test_render_caps_lines():
    held = [{"message": "\n".join(f"• 第{i}条" for i in range(20))}]
    body, n_total = efd.render_digest_body(held, cap_lines=5)
    assert n_total == 20
    assert "另有 15 条已存档" in body


# ---- integration: feed_post quiet-hours gate ------------------------------

def test_feed_post_holds_at_night(tmp_path):
    payload = json.dumps({"user_message": "夜里的一条 [l](https://example.com/a)"})
    out = _run_feed(payload, tmp_path, {"JARVIS_EF_QUIET_OVERRIDE": "quiet"})
    # nothing pushed to Lark
    assert out.strip() == ""
    # but it landed in the backlog
    backlog = tmp_path / "eigenflux" / "feed_backlog.jsonl"
    assert backlog.exists()
    assert "夜里的一条" in backlog.read_text()


def test_feed_post_urgent_breaks_through_at_night(tmp_path):
    payload = json.dumps({
        "user_message": "紧急 [l](https://example.com/a)",
        "urgent": True,
    })
    out = _run_feed(payload, tmp_path, {"JARVIS_EF_QUIET_OVERRIDE": "quiet"})
    assert "紧急" in out  # delivered immediately despite night
    backlog = tmp_path / "eigenflux" / "feed_backlog.jsonl"
    assert not backlog.exists()


def test_feed_post_delivers_when_awake(tmp_path):
    payload = json.dumps({"user_message": "白天的一条 [l](https://example.com/a)"})
    out = _run_feed(payload, tmp_path, {"JARVIS_EF_QUIET_OVERRIDE": "awake"})
    assert "白天的一条" in out


def test_morning_digest_flushes_backlog(tmp_path):
    # Seed a backlog as if held overnight.
    eg = tmp_path / "eigenflux"
    eg.mkdir(parents=True)
    (eg / "feed_backlog.jsonl").write_text(
        json.dumps({"ts": "t", "source": "eigenflux-feed",
                    "message": "📡 知会\n• 攒下的一条 → 你看看"}) + "\n",
        encoding="utf-8")
    # Awake + force flush window: no new feed items (empty stdin payload still
    # runs main()), should emit the digest card and clear the backlog.
    out = _run_feed("", tmp_path, {
        "JARVIS_EF_QUIET_OVERRIDE": "awake",
        "JARVIS_EF_FLUSH_HOUR": "0",  # any awake hour counts as flush window
    })
    assert "早报" in out
    assert "攒下的一条" in out
    assert not (eg / "feed_backlog.jsonl").exists()  # drained
