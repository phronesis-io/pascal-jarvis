"""cross_session_pre.sh high-water mark (2026-07-07 stale-claim amplifier).

The pre-hook used to re-read the same rolling-24h transcript window every 10
minutes with no memory of what it had already fed to Claude — a stale morning
"PR 等批" turn re-entered the digest prompt up to 144x/day and got re-pushed to
Pascal 8 times in one evening. These tests pin the watermark contract: new
turns surface exactly once (plus a small [context] tail), no-new-data runs emit
nothing (healthy empty_pre), and corrupt/lost state degrades to a single
re-read, never a re-digest loop.
"""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRE = ROOT / "tasks" / "cross_session_pre.sh"


def _setup(tmp_path: Path) -> Path:
    proj = tmp_path / "home" / ".claude" / "projects" / "-tmp-testproj"
    proj.mkdir(parents=True)
    return proj / "s1.jsonl"


def _turn(role: str, text: str, ts: str) -> str:
    return json.dumps({"type": role, "message": {"content": text},
                       "timestamp": ts}, ensure_ascii=False) + "\n"


def _run_pre(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(tmp_path / "home"),
        "MEMORY_DIR": str(tmp_path / "mem"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
    }
    return subprocess.run(["bash", str(PRE)], capture_output=True,
                          text=True, env=env)


def _seen_file(tmp_path: Path) -> Path:
    return tmp_path / "mem" / "system" / "cross_session_seen.json"


def test_first_run_emits_then_watermark_suppresses(tmp_path):
    session = _setup(tmp_path)
    session.write_text(
        _turn("user", "帮我看下修复进展", "2026-07-07T10:00:00Z")
        + _turn("assistant", "三个分支已推上去，测试全绿", "2026-07-07T10:01:00Z"))
    r1 = _run_pre(tmp_path)
    assert r1.returncode == 0, r1.stderr
    assert "修复进展" in r1.stdout and "测试全绿" in r1.stdout
    # Second run over the unchanged file: nothing new -> emit nothing at all
    # (a healthy empty_pre; re-emitting is the stale-claim amplifier)
    r2 = _run_pre(tmp_path)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == ""
    assert _seen_file(tmp_path).exists()


def test_appended_turn_emits_only_new_plus_context(tmp_path):
    session = _setup(tmp_path)
    session.write_text(
        _turn("user", "帮我看下修复进展", "2026-07-07T10:00:00Z")
        + _turn("assistant", "三个分支已推上去，测试全绿", "2026-07-07T10:01:00Z"))
    _run_pre(tmp_path)
    with session.open("a", encoding="utf-8") as f:
        f.write(_turn("user", "都合并完了，收工", "2026-07-07T12:00:00Z"))
    r = _run_pre(tmp_path)
    assert "都合并完了" in r.stdout
    # Already-seen turns come back only as marked context, never as news
    for line in r.stdout.splitlines():
        if "修复进展" in line or "测试全绿" in line:
            assert line.startswith("[context] "), line


def test_corrupt_watermark_degrades_to_full_reread(tmp_path):
    session = _setup(tmp_path)
    session.write_text(_turn("user", "只有一条消息", "2026-07-07T10:00:00Z"))
    seen = _seen_file(tmp_path)
    seen.parent.mkdir(parents=True)
    seen.write_text("{corrupt json!!")
    r = _run_pre(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "只有一条消息" in r.stdout


def test_truncated_session_file_resets_watermark(tmp_path):
    session = _setup(tmp_path)
    session.write_text(
        _turn("user", "第一条消息内容", "2026-07-07T10:00:00Z")
        + _turn("assistant", "第二条消息内容", "2026-07-07T10:01:00Z"))
    _run_pre(tmp_path)
    # Rotation/truncation: fewer bytes than the recorded watermark
    session.write_text(_turn("user", "重写后的新内容", "2026-07-07T11:00:00Z"))
    r = _run_pre(tmp_path)
    assert "重写后的新内容" in r.stdout


def test_unwritable_watermark_warns_loudly_then_recovers(tmp_path):
    # 2026-07-08 red-team: sha and watermark live in ONE file, so a failed
    # state write loses BOTH — a persistently unwritable state file re-digests
    # every cycle. The contract is now honesty, not magic: every failed write
    # screams to stderr (heartbeat logs script stderr into jarvis.log), and
    # recovery costs exactly one re-read once the path is writable again.
    session = _setup(tmp_path)
    session.write_text(_turn("user", "唯一的一条消息", "2026-07-07T10:00:00Z"))
    mem = tmp_path / "mem"
    mem.mkdir()
    mem.chmod(0o555)  # system/ cannot be created -> state write fails
    try:
        r1 = _run_pre(tmp_path)
        assert r1.returncode == 0, r1.stderr
        assert "唯一的一条消息" in r1.stdout  # still emits — degrade, don't die
        assert "WATERMARK UNWRITABLE" in r1.stderr
        assert str(_seen_file(tmp_path)) in r1.stderr  # path named in the alert
    finally:
        mem.chmod(0o755)
    # Writable again: the lost watermark costs one re-read, then suppression.
    r2 = _run_pre(tmp_path)
    assert "唯一的一条消息" in r2.stdout
    assert _seen_file(tmp_path).exists()
    r3 = _run_pre(tmp_path)
    assert r3.stdout.strip() == ""
