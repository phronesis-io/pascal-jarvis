from __future__ import annotations

import json
from datetime import timedelta


def _isolate(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "INFLIGHT_FILE", tmp_path / "data" / "inflight.json")
    monkeypatch.setattr(mod, "BREACH_QUEUE", tmp_path / "data" / "breach.jsonl")


def test_infrastructure_failure_restores_attempt_and_never_breaches(
    tmp_path, monkeypatch,
):
    import core.intentions as mod
    from core.timeutil import now_local

    _isolate(mod, tmp_path, monkeypatch)
    when = (now_local() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    iid = mod.create_intent(
        name="provider outage must not spend retry",
        trigger_type="date",
        trigger_config={"datetime": when},
        category="hard",
    )
    assert mod.mark_triggered(iid)
    mod.write_inflight([iid])

    result = mod.defer_inflight_infrastructure("quota")

    row = mod.get_intent(iid)
    assert result == {"deferred": [iid]}
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert "infrastructure failure" in row["last_error"]
    assert mod.peek_breaches() == []
    assert not mod._inflight_path().exists()


def test_sleep_elapsed_time_does_not_replace_real_content_attempts(
    tmp_path, monkeypatch,
):
    import core.intentions as mod
    from core.timeutil import now_local

    _isolate(mod, tmp_path, monkeypatch)
    when = (now_local() - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")
    iid = mod.create_intent(
        name="wake retry",
        trigger_type="date",
        trigger_config={"datetime": when},
        category="hard",
    )
    mod.mark_triggered(iid)
    mod.write_inflight([iid])

    result = mod.reconcile_inflight([])

    assert result["retried"] == [iid]
    assert result["expired"] == []
    assert mod.get_intent(iid)["status"] == "pending"


def test_breach_queue_obeys_attention_policy(tmp_path, monkeypatch):
    import core.intentions as mod
    from core.timeutil import now_local

    _isolate(mod, tmp_path, monkeypatch)
    base = {
        "name": "missed", "prompt": "do it", "purpose": "test",
        "attempt": mod.MAX_ATTEMPTS, "trigger_type": "date",
        "trigger_config": json.dumps({"datetime": now_local().isoformat()}),
    }
    assert mod._queue_breach({**base, "id": "hard", "category": "hard"}, now_local())
    assert not mod._queue_breach(
        {**base, "id": "healing", "category": "healing"}, now_local())
    assert [entry["id"] for entry in mod.peek_breaches()] == ["hard"]


def test_heartbeat_ack_protocol_distinguishes_call_failure(tmp_path, monkeypatch):
    from core.heartbeat import HeartbeatRunner

    heartbeat = tmp_path / "HEARTBEAT.md"
    heartbeat.write_text(
        "### intention-check\n- interval: 1m\n- post: post.py\n- prompt: x\n",
        encoding="utf-8",
    )
    memory = tmp_path / "memory"
    memory.mkdir()
    runner = HeartbeatRunner(
        jarvis_dir=tmp_path,
        heartbeat_file=heartbeat,
        state_file=tmp_path / "state.json",
        memory_dir=memory,
    )
    calls = []
    monkeypatch.setattr(
        runner, "run_script",
        lambda path, stdin_data="": calls.append((path, stdin_data)) or "",
    )
    monkeypatch.setattr(runner, "claude_call", lambda *args, **kwargs: "")

    runner.run_cycle(force=True)

    assert calls == [("post.py", "__CALL_FAILED__")]
