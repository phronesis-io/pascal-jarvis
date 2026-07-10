"""Tests for core.memorial — the 奏折 (memorial) card framework.

Covers: create / ledger fold / decide idempotence / action execution via
ActionProcessor / chat injection into bot.sh's pending-merge channel /
CLI parsing / preset expansion / card JSON structure (button value
round-trip). All lark-cli sends are mocked — nothing real is sent.
"""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.memorial as memorial


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated JARVIS_DIR + mocked send channels. Returns a recorder."""
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rec = SimpleNamespace(dir=tmp_path, cards=[], texts=[], send_ok=True)

    def fake_send_card(card_json_str, chat_id=""):
        rec.cards.append((card_json_str, chat_id))
        return rec.send_ok

    def fake_send_text(text, chat_id=""):
        rec.texts.append((text, chat_id))
        return rec.send_ok

    monkeypatch.setattr(memorial, "_send_card", fake_send_card)
    monkeypatch.setattr(memorial, "_send_text", fake_send_text)
    monkeypatch.setattr(memorial, "_resolve_user_id", lambda: "ou_test")
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: False)
    # Most tests care about chat semantics, not thread scheduling. Keep them
    # deterministic; the dedicated async test below covers non-blocking send.
    monkeypatch.setattr(memorial, "_send_opener_async",
                        lambda text, chat_id: memorial._deliver_opener(text, chat_id))
    return rec


def _ledger_events(tmp_path) -> list[dict]:
    p = tmp_path / "memorials.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# ── create ───────────────────────────────────────────────────────────────


def test_create_writes_ledger_sends_card_and_mirrors_outbox(env):
    mid, sent = memorial.create("mail", "测试标题", "正文内容", preset="decision")

    assert mid.startswith("mem_")
    assert sent is True
    events = _ledger_events(env.dir)
    assert [e["ev"] for e in events] == ["create", "delivery"]
    ev = events[0]
    assert ev["ev"] == "create" and ev["id"] == mid
    assert ev["source"] == "mail" and ev["title"] == "测试标题"
    assert [o["key"] for o in ev["options"]] == ["approve", "defer", "reject"]
    assert events[1]["status"] == "delivered"
    # direct send mirrored into the outbox so the main session knows
    outbox = (env.dir / "heartbeat_outbox.jsonl").read_text()
    assert "测试标题" in outbox and '"source": "memorial"' in outbox


def test_create_card_structure_and_button_value_round_trip(env):
    mid, _ = memorial.create("mail", "标题", "正文", preset="decision")
    card = json.loads(env.cards[0][0])

    assert card["header"]["title"]["content"] == "📜 📬 标题"
    assert card["elements"][0]["text"]["content"] == "正文"
    actions = card["elements"][1]["actions"]
    # 3 preset options + framework-appended 聊聊这个
    assert len(actions) == 4
    assert actions[0]["type"] == "primary"
    assert actions[-1]["text"]["content"] == "💬 聊聊这个"
    for a, opt in zip(actions, ("approve", "defer", "reject", "chat")):
        # the value dict must round-trip exactly as the sidecar will see it
        v = json.loads(json.dumps(a["value"]))
        assert v == {"action": "memorial", "id": mid, "opt": opt}


def test_create_defaults_to_fyi_preset(env):
    mid, _ = memorial.create("selfmon", "t", "b")
    st = memorial.get_memorial(mid)
    assert [o["label"] for o in st["options"]] == ["已阅", "重要，持续盯"]


def test_create_rejects_reserved_chat_key_and_unknown_preset(env):
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b",
                        options=[{"key": "chat", "label": "撞框架保留键"}])
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b", preset="nonsense")
    with pytest.raises(ValueError):
        memorial.create("x", "t", "b",
                        options=[{"key": "a", "label": "一"},
                                 {"key": "a", "label": "二"}])
    assert _ledger_events(env.dir) == []  # nothing recorded on bad input


def test_create_send_failure_still_ledgered(env):
    env.send_ok = False
    mid, sent = memorial.create("mail", "t", "b", preset="fyi")
    assert sent is False
    assert memorial.get_memorial(mid)["status"] == "pending"
    # no outbox mirror for an unsent card
    assert not (env.dir / "heartbeat_outbox.jsonl").exists()
    # card_json lets the caller re-deliver through another channel
    card = json.loads(memorial.card_json(mid))
    assert card["header"]["title"]["content"].startswith("📜")


# ── decide ───────────────────────────────────────────────────────────────


def test_decide_records_and_replaces_card(env):
    mid, _ = memorial.create("mail", "标题", "正文", preset="decision")
    payload = memorial.decide(mid, "approve")

    assert payload["toast"]["type"] == "success"
    assert "已批：准（去做）" in payload["toast"]["content"]
    assert payload["card"]["type"] == "raw"
    card = payload["card"]["data"]
    body = card["elements"][0]["text"]["content"]
    assert "正文" in body and "✅ 已批：准（去做）" in body
    # buttons removed on the replacement card
    assert all(el.get("tag") != "action" for el in card["elements"])
    st = memorial.get_memorial(mid)
    assert st["status"] == "decided" and st["decided_opt"] == "approve"


def test_decide_is_idempotent(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.decide(mid, "read")
    payload = memorial.decide(mid, "watch")  # second tap, different button

    assert payload["toast"]["type"] == "info"
    assert "已批过" in payload["toast"]["content"]
    decides = [e for e in _ledger_events(env.dir) if e["ev"] == "decide"]
    assert len(decides) == 1 and decides[0]["opt"] == "read"


def test_decide_unknown_id_and_unknown_opt(env):
    payload = memorial.decide("mem_nope", "read")
    assert payload["toast"]["type"] == "info"
    assert "card" not in payload

    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    payload = memorial.decide(mid, "nonsense")
    assert payload["toast"]["type"] == "info"
    assert memorial.get_memorial(mid)["status"] == "pending"


def test_decide_runs_action_through_action_processor(env, monkeypatch):
    calls = []

    class FakeAP:
        def __init__(self, **kw):
            pass

        def _do_intent_close(self, raw):
            calls.append(raw)
            return "Closure recorded"

    import core.actions as actions
    monkeypatch.setattr(actions, "ActionProcessor", FakeAP)

    mid, _ = memorial.create(
        "intent", "t", "b",
        options=[{"key": "done", "label": "做了",
                  "action": {"type": "intent_close",
                             "params": {"id": "int_1", "outcome": "done"}}}])
    payload = memorial.decide(mid, "done")

    assert calls == ["id=int_1|outcome=done"]
    assert payload["toast"]["type"] == "success"
    results = [e for e in _ledger_events(env.dir) if e["ev"] == "action_result"]
    assert results[0]["result"] == "Closure recorded"
    # result surfaced on the replacement card
    assert "Closure recorded" in payload["card"]["data"]["elements"][0]["text"]["content"]


def test_decide_action_failure_still_records_with_info_toast(env, monkeypatch):
    class FakeAP:
        def __init__(self, **kw):
            pass

        def _do_boom(self, raw):
            raise RuntimeError("boom")

    import core.actions as actions
    monkeypatch.setattr(actions, "ActionProcessor", FakeAP)

    mid, _ = memorial.create(
        "x", "t", "b",
        options=[{"key": "go", "label": "去",
                  "action": {"type": "boom", "params": {}}}])
    payload = memorial.decide(mid, "go")

    assert payload["toast"]["type"] == "info"
    assert "出错" in payload["toast"]["content"]
    results = [e for e in _ledger_events(env.dir) if e["ev"] == "action_result"]
    assert results[0]["result"].startswith("FAILED")
    assert memorial.get_memorial(mid)["status"] == "decided"


# ── chat ─────────────────────────────────────────────────────────────────


def test_chat_sends_opener_and_injects_pending_merge(env):
    mid, _ = memorial.create("mail", "邮件标题", "正文" * 200,
                             preset="fyi", context="来自 alice 的邮件")
    payload = memorial.chat(mid)

    # 1. opener message sent (body truncated to ~200 chars)
    opener = env.texts[0][0]
    assert opener.startswith("📜 聊聊：邮件标题")
    assert "你想怎么处理这件事" in opener
    assert len(opener) < 300

    # 2. context injected into bot.sh's pending-merge channel
    pm = env.dir / "jobs" / "pending_merge.jsonl"
    entry = json.loads(pm.read_text().splitlines()[0])
    assert entry["conv_key"] == "ou_test"
    assert entry["job_id"] == f"memorial:{mid}"
    assert entry["summary"].startswith("[奏折上下文]")
    assert "邮件标题" in entry["summary"]
    assert "来自 alice" in entry["summary"]
    assert "待批" in entry["summary"]
    assert len(entry["summary"]) <= 1500

    # 3. ledger event + replacement card keeps remaining options, drops 聊聊
    assert [e["ev"] for e in _ledger_events(env.dir)] == [
        "create", "delivery", "chat"
    ]
    card = payload["card"]["data"]
    body = card["elements"][0]["text"]["content"]
    assert "💬 聊天中" in body
    actions = card["elements"][1]["actions"]
    labels = [a["text"]["content"] for a in actions]
    assert labels == ["已阅", "重要，持续盯"]  # no 聊聊这个 button


def test_chat_after_decide_shows_status_no_buttons(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.decide(mid, "read")
    payload = memorial.chat(mid)

    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    assert "已批：已阅" in entry["summary"]
    card = payload["card"]["data"]
    assert all(el.get("tag") != "action" for el in card["elements"])


def test_chat_unknown_id(env):
    payload = memorial.chat("mem_nope")
    assert payload["toast"]["type"] == "info"
    assert env.texts == []


def test_chat_group_card_uses_chat_id_as_conv_key(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi", chat_id="oc_group1")
    memorial.chat(mid)
    assert env.texts[0][1] == "oc_group1"  # opener goes to the group
    entry = json.loads(
        (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()[0])
    assert entry["conv_key"] == "oc_group1"


def test_chat_retap_does_not_duplicate_opener_or_injection(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    memorial.chat(mid)
    payload = memorial.chat(mid)

    assert payload["toast"]["type"] == "info"
    assert len(env.texts) == 1
    pending = (env.dir / "jobs" / "pending_merge.jsonl").read_text().splitlines()
    assert len(pending) == 1
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "chat"]) == 1


# ── ledger fold / list ───────────────────────────────────────────────────


def test_list_memorials_and_pending_filter(env):
    m1, _ = memorial.create("mail", "一", "b", preset="fyi")
    m2, _ = memorial.create("mail", "二", "b", preset="fyi")
    memorial.decide(m1, "read")

    assert {s["id"] for s in memorial.list_memorials()} == {m1, m2}
    pending = memorial.list_memorials(pending_only=True)
    assert [s["id"] for s in pending] == [m2]


def test_fold_skips_malformed_ledger_lines(env):
    mid, _ = memorial.create("mail", "t", "b", preset="fyi")
    with open(env.dir / "memorials.jsonl", "a") as f:
        f.write("not json at all\n")
    assert memorial.get_memorial(mid)["status"] == "pending"


def test_ids_are_unique(env):
    ids = {memorial.create("x", "t", f"b{i}", preset="fyi")[0]
           for i in range(5)}
    assert len(ids) == 5


def test_duplicate_delivered_memorial_is_not_resent(env):
    first, sent = memorial.create("x", "t", "b", preset="fyi")
    second, resent = memorial.create("x", "t", "b", preset="fyi")

    assert sent is True and resent is True
    assert second == first
    assert len(env.cards) == 1


def test_duplicate_failed_memorial_retries_same_ledger_entry(env):
    env.send_ok = False
    first, sent = memorial.create("x", "t", "b", preset="fyi")
    env.send_ok = True
    second, resent = memorial.create("x", "t", "b", preset="fyi")

    assert sent is False and resent is True
    assert second == first
    assert len([e for e in _ledger_events(env.dir) if e["ev"] == "create"]) == 1
    assert memorial.get_memorial(first)["delivery_status"] == "delivered"


def test_quiet_hours_queue_records_delivery_without_direct_send(env, monkeypatch):
    monkeypatch.setattr(memorial, "_quiet_hours_now", lambda: True)
    mid, queued = memorial.create("x", "t", "b", preset="fyi")

    assert queued is True
    assert env.cards == []
    assert memorial.get_memorial(mid)["delivery_status"] == "queued"
    assert mid in (env.dir / "night_queue.jsonl").read_text()


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_send_prints_id(env, capsys):
    rc = memorial.main(["send", "--source", "mail", "--title", "T",
                        "--body", "B", "--preset", "fyi"])
    assert rc == 0
    mid = capsys.readouterr().out.strip()
    assert mid.startswith("mem_")
    assert memorial.get_memorial(mid)["title"] == "T"


def test_cli_option_spec_parsing(env, capsys):
    rc = memorial.main([
        "send", "--source", "intent", "--title", "T", "--body", "B",
        "--option", "准=intent_close:id=xxx,outcome=done",
        "--option", "缓",
    ])
    assert rc == 0
    mid = capsys.readouterr().out.strip()
    opts = memorial.get_memorial(mid)["options"]
    assert opts[0] == {"key": "opt1", "label": "准",
                       "action": {"type": "intent_close",
                                  "params": {"id": "xxx", "outcome": "done"}}}
    assert opts[1] == {"key": "opt2", "label": "缓", "action": None}


def test_cli_send_failure_returns_1_but_prints_id(env, capsys):
    env.send_ok = False
    rc = memorial.main(["send", "--source", "mail", "--title", "T",
                        "--body", "B", "--preset", "fyi"])
    assert rc == 1
    assert capsys.readouterr().out.strip().startswith("mem_")


def test_cli_list_pending(env, capsys):
    m1, _ = memorial.create("mail", "一", "b", preset="fyi")
    m2, _ = memorial.create("mail", "二", "b", preset="fyi")
    memorial.decide(m1, "read")
    capsys.readouterr()  # drain

    rc = memorial.main(["list", "--pending"])
    assert rc == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x]
    assert [s["id"] for s in lines] == [m2]


def test_cli_bad_option_spec_is_usage_error(env, capsys):
    rc = memorial.main(["send", "--source", "x", "--title", "t",
                        "--body", "b", "--option", "  "])
    assert rc == 2
    assert "ERROR" in capsys.readouterr().err
    assert _ledger_events(env.dir) == []


def test_send_timeout_retries_and_never_claims_delivery(monkeypatch):
    calls = []

    def timeout(*args, **kwargs):
        calls.append((args, kwargs))
        raise memorial.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(memorial.subprocess, "run", timeout)
    monkeypatch.setattr(memorial.time, "sleep", lambda _seconds: None)

    assert memorial._send(["--user-id", "ou_test", "--markdown", "x"]) is False
    assert len(calls) == 3


# ── mail triage integration (post script carrier swap) ──────────────────


def test_mail_post_emits_memorial_card(tmp_path, monkeypatch, capsys):
    """mail_triage_post push path now goes through memorial.create; the
    quiet-hours / triaged bookkeeping is exercised by test_mail_triage.py —
    here we only pin the carrier swap at the module level."""
    import importlib.util
    root = Path(__file__).parent.parent
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("JARVIS_EF_QUIET_OVERRIDE", "awake")
    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    spec = importlib.util.spec_from_file_location(
        "mail_triage_post_t", root / "tasks" / "mail_triage_post.py")
    post = importlib.util.module_from_spec(spec)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "triage": [{"event_id": "a1", "decision": "push"}],
        "user_message": "📬 来自 X 的邮件", "urgent": False})))
    spec.loader.exec_module(post)
    assert post.main() == 0

    card = json.loads(capsys.readouterr().out)
    assert card["header"]["title"]["content"] == "📜 📬 邮件"
    assert "来自 X 的邮件" in card["elements"][0]["text"]["content"]
    labels = [a["text"]["content"] for a in card["elements"][1]["actions"]]
    assert labels == ["已阅", "重要，持续盯", "💬 聊聊这个"]
