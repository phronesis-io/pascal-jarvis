"""Suggested-reply taps must lead somewhere — the reply-followup loop.

Owner (2026-08-07): tapped「现在授权」on the auth-outage card and nothing
happened until he typed「怎么授权」himself. A reply-only button labeled with
an action verb parked the sentence in pending_merge and waited for HIM to
speak first. The tap must (1) queue a proactive follow-up turn, (2) toast an
honest promise, and (3) not make the conversation act a second time after
the follow-up already acted.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import memorial  # noqa: E402
from core.heartbeat import parse_heartbeat  # noqa: E402
from core.jsonl import read_jsonl  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    monkeypatch.setattr(memorial, "_desk_reachable", lambda: True)
    monkeypatch.setattr(memorial, "_send_card", lambda *a, **k: "om_test")
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    yield


def _reply_card(label="现在授权"):
    mid, _ = memorial.create(
        source="heartbeat", title="飞书授权掉了", body="修法是……",
        options=[{"key": "r1", "label": label, "action": None, "reply": True},
                 {"key": "r2", "label": "晚点再弄", "action": None,
                  "reply": True}],
        send=False)
    return mid


def test_reply_tap_enqueues_a_proactive_followup():
    mid = _reply_card()
    memorial.decide(mid, "r1")
    rows = read_jsonl(memorial._reply_followup_queue_path())
    assert len(rows) == 1
    assert rows[0]["memorial_id"] == mid
    assert rows[0]["label"] == "现在授权"
    assert int(rows[0]["taken_at"]) == 0


def test_reply_tap_toast_promises_proactive_action_not_waiting():
    mid = _reply_card()
    out = memorial.decide(mid, "r1")
    assert out["toast"]["type"] == "success"
    # The old wording「下条消息我接着这个说」meant HE had to speak first.
    assert "下条消息" not in out["toast"]["content"]
    assert "接手" in out["toast"]["content"]


def test_action_option_does_not_enqueue_followup(monkeypatch):
    """A tap whose real action ran needs no second responder."""
    monkeypatch.setattr(memorial, "_execute_action",
                        lambda action, **kw: "✅ 已发授权链接")
    mid, _ = memorial.create(
        source="selfmon", title="t", body="b",
        options=[{"key": "auth", "label": "现在授权",
                  "action": {"type": "lark_auth_login", "params": {}}}],
        send=False)
    memorial.decide(mid, "auth")
    assert read_jsonl(memorial._reply_followup_queue_path()) == []


def test_claim_stamps_and_complete_drops():
    mid_a = _reply_card()
    mid_b = _reply_card(label="晚点再弄")
    memorial.decide(mid_a, "r1")
    memorial.decide(mid_b, "r2")

    first = memorial.reply_followup_claim(now_epoch=1000)
    assert first["memorial_id"] == mid_a
    second = memorial.reply_followup_claim(now_epoch=1001)
    assert second["memorial_id"] == mid_b

    memorial.reply_followup_complete(mid_a)
    left = read_jsonl(memorial._reply_followup_queue_path())
    assert [r["memorial_id"] for r in left] == [mid_b]


def test_stale_claim_is_retaken_a_tap_is_never_swallowed():
    mid = _reply_card()
    memorial.decide(mid, "r1")
    assert memorial.reply_followup_claim(now_epoch=1000) is not None
    # Within the retake window the claim holds…
    assert memorial.reply_followup_claim(now_epoch=1300) is None
    # …after it, the request is retaken instead of lost.
    retaken = memorial.reply_followup_claim(
        now_epoch=1000 + memorial.REPLY_FOLLOWUP_RETAKE_S + 1)
    assert retaken["memorial_id"] == mid


def test_settle_rewrites_the_decision_injection_no_double_action():
    mid = _reply_card()
    memorial.decide(mid, "r1")
    pending = read_jsonl(memorial._pending_merge_path())
    assert any("照它行动" in r.get("summary", "") for r in pending)

    memorial.settle_decision_context(mid, "[奏折回复·已接手] 已发授权链接。")
    pending = read_jsonl(memorial._pending_merge_path())
    entry = next(r for r in pending
                 if r["job_id"] == f"memorial-decision:{mid}")
    # The conversation still learns the decision — but is told it was
    # handled, not instructed to act on it again.
    assert "已接手" in entry["summary"]
    assert "照它行动" not in entry["summary"]


def test_post_hook_delivers_settles_and_defuses(monkeypatch, capsys):
    import tasks.reply_followup_post as post

    mid = _reply_card()
    memorial.decide(mid, "r1")
    memorial.reply_followup_claim()

    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(f"[reply-followup {mid}] 链接已发到你飞书，点开即可。"))
    assert post.main() == 0

    out = capsys.readouterr().out
    assert "链接已发" in out
    assert f"[reply-followup {mid}]" not in out  # marker stripped
    assert read_jsonl(memorial._reply_followup_queue_path()) == []
    entry = next(r for r in read_jsonl(memorial._pending_merge_path())
                 if r["job_id"] == f"memorial-decision:{mid}")
    assert "已接手" in entry["summary"]


def test_post_hook_empty_output_leaves_claim_for_retake(monkeypatch):
    import tasks.reply_followup_post as post

    mid = _reply_card()
    memorial.decide(mid, "r1")
    memorial.reply_followup_claim()
    monkeypatch.setattr(sys, "stdin", io.StringIO("HEARTBEAT_OK"))
    assert post.main() == 0
    # Still queued — an unanswered tap must be retaken, never eaten.
    assert len(read_jsonl(memorial._reply_followup_queue_path())) == 1


PRE_SH = ROOT / "tasks" / "reply_followup_pre.sh"

_SETUP = """
from core import memorial
mid, _ = memorial.create(
    source="heartbeat", title="T", body="B",
    options=[{"key": "r1", "label": "现在授权", "action": None,
              "reply": True}], send=False)
memorial.decide(mid, "r1")
print(mid)
"""


def _subenv(tmp_path):
    import os
    env = os.environ.copy()
    env["JARVIS_DIR"] = str(tmp_path)
    env["USER_ID"] = "ou_test"
    env["PYTHONPATH"] = str(ROOT)
    return env


def _sub_setup(tmp_path, env):
    import subprocess
    r = subprocess.run([sys.executable, "-c", _SETUP], capture_output=True,
                       text=True, env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_pre_hook_emits_the_claimed_tap(tmp_path):
    import subprocess
    env = _subenv(tmp_path)
    mid = _sub_setup(tmp_path, env)
    out = subprocess.run(["bash", str(PRE_SH)], capture_output=True,
                         text=True, env=env, cwd=ROOT, timeout=60)
    assert f"[reply-followup {mid}]" in out.stdout
    assert "现在授权" in out.stdout


def test_pre_hook_drops_taps_the_conversation_already_took(tmp_path):
    """Pascal spoke before the task ran: bot.sh consumed the injection and
    the session acted — answering again would be a double response."""
    import subprocess
    env = _subenv(tmp_path)
    _sub_setup(tmp_path, env)
    for pm in tmp_path.rglob("pending_merge.jsonl"):
        pm.unlink()  # simulate bot.sh having consumed the injection
    out = subprocess.run(["bash", str(PRE_SH)], capture_output=True,
                         text=True, env=env, cwd=ROOT, timeout=60)
    assert "[reply-followup" not in out.stdout
    queue = list(tmp_path.rglob("reply_followup_queue.jsonl"))
    assert not queue or all(
        not line.strip() for line in queue[0].read_text().splitlines())


def test_heartbeat_registers_the_reply_followup_task():
    tasks = {t["name"]: t for t in parse_heartbeat(ROOT / "HEARTBEAT.md")}
    task = tasks["reply-followup"]
    assert task["pre"] == "tasks/reply_followup_pre.sh"
    assert task["post"] == "tasks/reply_followup_post.py"
    assert task["interval"] == 120
    assert "core.lark_auth start" in task["prompt"]


def test_selfmon_auth_warning_binds_the_real_action():
    import tasks.self_diagnostic_post as sdp

    with_auth = "⚠️ 日历 user token 探针失败 — 点「现在授权」修复\n⚠️ 别的"
    options = sdp._options_for(with_auth)
    assert options is not None
    auth = next(o for o in options if o["label"] == "现在授权")
    assert auth["action"] == {"type": "lark_auth_login", "params": {}}
    assert not auth.get("reply"), "the fix button must be an action, not chat"
    assert sdp._options_for("⚠️ 磁盘快满了") is None


def test_action_processor_wires_lark_auth_login(tmp_path, monkeypatch):
    import core.lark_auth as lark_auth
    from core.actions import ActionProcessor

    ap = ActionProcessor(jarvis_dir=tmp_path, memory_dir=tmp_path,
                         jobs_dir=tmp_path)
    monkeypatch.setattr(lark_auth, "start_device_flow",
                        lambda **kw: "已把授权链接发到你的飞书私聊")
    assert "授权链接" in ap._do_lark_auth_login("")

    def boom(**kw):
        raise RuntimeError("lark-cli auth login failed")
    monkeypatch.setattr(lark_auth, "start_device_flow", boom)
    assert ap._do_lark_auth_login("").startswith("FAILED")
