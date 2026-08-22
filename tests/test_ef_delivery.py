"""Legacy EigenFlux backlog helpers + one-way migration into intact cards.

Quiet-hour policy now lives in heartbeat_loop; these helpers only drain
pre-migration backlog files without condensing them.
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

def test_feed_post_holds_nothing_locally_at_night(tmp_path):
    payload = json.dumps({"user_message": "夜里的一条 [l](https://example.com/a)"})
    out = _run_feed(payload, tmp_path, {"JARVIS_EF_QUIET_OVERRIDE": "quiet"})
    # A non-urgent item is not minted at night at all (it would only queue
    # and flush as a 09:30 burst); and the dead local backlog file must never
    # come back as a substitute hold.
    assert out.strip() == ""
    backlog = tmp_path / "eigenflux" / "feed_backlog.jsonl"
    assert not backlog.exists()


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


def test_legacy_backlog_drains_as_separate_cards(tmp_path):
    # Seed a backlog as if held overnight.
    eg = tmp_path / "eigenflux"
    eg.mkdir(parents=True)
    (eg / "feed_backlog.jsonl").write_text("\n".join([
        json.dumps({"ts": "t", "source": "eigenflux-feed",
                    "message": "第一件旧消息"}),
        json.dumps({"ts": "t", "source": "eigenflux-research",
                    "message": "第二件旧消息"}),
    ]) + "\n", encoding="utf-8")
    out = _run_feed("", tmp_path, {
        "JARVIS_EF_QUIET_OVERRIDE": "awake",
    })
    cards = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(cards) == 2
    assert "第一件旧消息" in out and "第二件旧消息" in out
    assert not (eg / "feed_backlog.jsonl").exists()  # drained
