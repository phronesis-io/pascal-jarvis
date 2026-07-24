import json
import subprocess

import pytest

from core.iteration_loop import DailyObserver, IterationError, IterationStore


def _store(tmp_path, now=None):
    clock = now or [1000.0]
    return IterationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: clock[0],
    )


def _signal(store, **overrides):
    values = {
        "source": "components",
        "category": "health",
        "key": "dashboard",
        "severity": "major",
        "summary": "Dashboard unavailable",
        "evidence": {"component": "dashboard", "ok": False},
    }
    values.update(overrides)
    return store.record_signal(**values)


def _proposal(store, signal):
    return store.create_proposal(
        signal_fingerprint=signal["fingerprint"],
        title="Restore dashboard",
        problem="Dashboard is repeatedly unavailable.",
        goal="Keep the dashboard reachable.",
        non_goals=["No redesign"],
        product_direction="Do not interrupt unless recovery fails.",
        technical_direction="Add deterministic supervision and smoke checks.",
        acceptance=["Health endpoint stays green", "Regression test passes"],
        priority=80,
        baseline={"failures": 2},
        expected={"failures": 0},
    )[0]


def test_signal_is_deduplicated_and_accumulates_occurrences(tmp_path):
    store = _store(tmp_path)
    first = _signal(store)
    second = _signal(store, evidence={"component": "dashboard", "ok": False, "n": 2})
    assert first["id"] == second["id"]
    assert second["occurrence_count"] == 2
    assert second["evidence"]["n"] == 2


def test_major_signal_needs_repetition_before_proposal(tmp_path):
    store = _store(tmp_path)
    first = _signal(store)
    assert store.propose_from_signal(first) is None
    second = _signal(store)
    proposal, created = store.propose_from_signal(second)
    assert created is True
    assert proposal["status"] == "pending"
    assert proposal["priority"] == 80


def test_critical_signal_can_propose_once(tmp_path):
    store = _store(tmp_path)
    signal = _signal(store, severity="critical")
    proposal, _ = store.propose_from_signal(signal)
    assert proposal["priority"] == 100


def test_pending_proposal_is_deduplicated(tmp_path):
    store = _store(tmp_path)
    signal = _signal(store)
    first = _proposal(store, signal)
    second, created = store.create_proposal(
        signal_fingerprint=signal["fingerprint"],
        title="Duplicate",
        problem="Duplicate",
        goal="Duplicate",
        non_goals=[],
        product_direction="",
        technical_direction="",
        acceptance=[],
        priority=1,
    )
    assert created is False
    assert second["id"] == first["id"]


def test_rejection_never_creates_taskline_work(monkeypatch, tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, _signal(store))
    called = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: called.append(args) or None,
    )
    rejected = store.review(
        proposal["id"], approved=False, actor="owner", reason="not valuable"
    )
    assert rejected["status"] == "rejected"
    assert called == []


def test_approval_requires_valid_taskline_receipt(monkeypatch, tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, _signal(store))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "offline"
        ),
    )
    with pytest.raises(IterationError, match="offline"):
        store.review(proposal["id"], approved=True, actor="owner")
    assert store.get(proposal["id"])["status"] == "approved"


def test_approved_proposal_enters_external_taskline(monkeypatch, tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, _signal(store))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"id": "task-123"}), ""
        )

    monkeypatch.setattr(subprocess, "run", run)
    queued = store.review(proposal["id"], approved=True, actor="owner")
    assert queued["status"] == "queued"
    assert queued["taskline_id"] == "task-123"
    assert commands[0][:4] == ["taskline", "task", "list", "--project"]
    assert commands[1][:4] == ["taskline", "task", "create", "--project"]
    assert "l3-proposal" in commands[1]


def test_ambiguous_taskline_timeout_recovers_by_label_without_duplicate(
    monkeypatch, tmp_path
):
    clock = [1_000.0]
    store = IterationStore(
        root=tmp_path,
        db_path=tmp_path / "jarvis.db",
        now=lambda: clock[0],
    )
    proposal = _proposal(store, _signal(store))
    calls = []

    def uncertain(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["task", "list"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"tasks": None}), ""
            )
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(subprocess, "run", uncertain)
    with pytest.raises(IterationError, match="outcome is unknown"):
        store.review(proposal["id"], approved=True, actor="owner")
    assert store.get(proposal["id"])["status"] == "queueing"

    clock[0] += 61

    def recovered(command, **kwargs):
        calls.append(command)
        assert command[1:3] == ["task", "list"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"tasks": [{"id": "task-existing"}]}),
            "",
        )

    monkeypatch.setattr(subprocess, "run", recovered)
    queued = store.review(proposal["id"], approved=True, actor="owner")

    assert queued["status"] == "queued"
    assert queued["taskline_id"] == "task-existing"


def test_taskline_lookup_rejects_malformed_task_rows(monkeypatch, tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, _signal(store))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"tasks": ["not-an-object"]}), ""
        ),
    )

    with pytest.raises(IterationError, match="invalid task receipt"):
        store.review(proposal["id"], approved=True, actor="owner")

    assert store.get(proposal["id"])["status"] == "approved"


def test_rejected_signal_does_not_create_a_daily_repeat_proposal(tmp_path):
    store = _store(tmp_path)
    signal = _signal(store, severity="critical")
    proposal, _ = store.propose_from_signal(signal)
    store.review(
        proposal["id"],
        approved=False,
        actor="owner",
        reason="not valuable",
        queue=False,
    )

    repeated = _signal(store, severity="critical")

    assert store.propose_from_signal(repeated)[1] is False
    assert len(store.list()) == 1


def test_post_release_outcome_can_close_or_reopen_loop(monkeypatch, tmp_path):
    store = _store(tmp_path)
    proposal = _proposal(store, _signal(store))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"id": "task-123"}), ""
        ),
    )
    queued = store.review(proposal["id"], approved=True, actor="owner")
    shipped = store.mark_shipped(
        queued["id"], release_sha="abcdef123456", actor="deploy"
    )
    assert shipped["status"] == "shipped"
    followup = store.verify_outcome(
        shipped["id"], actual={"failures": 1}, matched=False
    )
    assert followup["status"] == "needs_followup"


def test_successful_outcome_resolves_original_signal(monkeypatch, tmp_path):
    store = _store(tmp_path)
    signal = _signal(store)
    proposal = _proposal(store, signal)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps({"id": "task-456"}), ""
        ),
    )
    queued = store.review(proposal["id"], approved=True, actor="owner")
    shipped = store.mark_shipped(
        queued["id"], release_sha="abcdef123456", actor="deploy"
    )
    verified = store.verify_outcome(
        shipped["id"], actual={"failures": 0}, matched=True
    )
    assert verified["status"] == "verified"
    with store._connect() as db:
        row = db.execute(
            "SELECT status FROM iteration_signals WHERE id=?", (signal["id"],)
        ).fetchone()
    assert row["status"] == "resolved"


def test_signal_rejects_secret_material(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(IterationError, match="secret"):
        _signal(store, summary="Authorization: Bearer abc")


def test_daily_observer_preserves_human_gate(tmp_path, monkeypatch):
    store = _store(tmp_path)

    class Observer(DailyObserver):
        def _component_signals(self):
            return [{
                "source": "components",
                "category": "health",
                "key": "dashboard",
                "severity": "critical",
                "summary": "Dashboard unavailable",
                "evidence": {"ok": False},
            }]

        def _delegation_signals(self):
            return []

        def _conversation_signals(self):
            return []

    monkeypatch.setattr(
        "core.iteration_loop.sync_proposal_item",
        lambda proposal, **kwargs: "mem-1",
    )
    result = Observer(store).run()
    assert len(result["proposals"]) == 1
    assert store.get(result["proposals"][0])["status"] == "pending"


def _queued_observed_proposal(store, monkeypatch):
    signal = _signal(store, severity="critical")
    proposal, _ = store.propose_from_signal(signal)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            (
                json.dumps({"tasks": []})
                if command[1:3] == ["task", "list"]
                else json.dumps({"id": "task-123"})
            ),
            "",
        ),
    )
    return store.review(
        proposal["id"], approved=True, actor="owner", queue=True
    )


def test_daily_observer_closes_taskline_release_after_deployed_sha(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    queued = _queued_observed_proposal(store, monkeypatch)
    release_sha = "d" * 40

    def readback(command, **_kwargs):
        if command[:3] == ["taskline", "task", "get"]:
            payload = {
                "id": "task-123",
                "state": "done",
                "links": [{
                    "url": "https://github.com/org/repo/pull/9",
                }],
            }
        elif command[:3] == ["gh", "pr", "view"]:
            payload = {
                "mergedAt": "2026-07-24T12:00:00Z",
                "mergeCommit": {"oid": release_sha},
            }
        elif command == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 0, release_sha + "\n", "")
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", readback)

    class Observer(DailyObserver):
        def _component_signals(self):
            return []

        def _delegation_signals(self):
            return []

        def _conversation_signals(self):
            return []

    result = Observer(store).run()
    closed = store.get(queued["id"])

    assert closed["status"] == "verified"
    assert closed["release_sha"] == release_sha
    assert closed["actual"] == {"signal_open": False}
    assert result["reconciliation"] == {
        "shipped": 1,
        "verified": 1,
        "needs_followup": 0,
        "errors": [],
    }


def test_daily_observer_waits_until_merged_sha_is_deployed(
    tmp_path, monkeypatch,
):
    store = _store(tmp_path)
    queued = _queued_observed_proposal(store, monkeypatch)
    release_sha = "d" * 40

    def readback(command, **_kwargs):
        if command[:3] == ["taskline", "task", "get"]:
            payload = {
                "id": "task-123",
                "state": "done",
                "links": [{
                    "url": "https://github.com/org/repo/pull/9",
                }],
            }
            output = json.dumps(payload)
        elif command[:3] == ["gh", "pr", "view"]:
            output = json.dumps({
                "mergedAt": "2026-07-24T12:00:00Z",
                "mergeCommit": {"oid": release_sha},
            })
        elif command == ["git", "rev-parse", "HEAD"]:
            output = "e" * 40 + "\n"
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", readback)

    class Observer(DailyObserver):
        def _component_signals(self):
            return []

        def _delegation_signals(self):
            return []

        def _conversation_signals(self):
            return []

    result = Observer(store).run()

    assert store.get(queued["id"])["status"] == "queued"
    assert result["reconciliation"]["shipped"] == 0
