from __future__ import annotations

import subprocess

from core.log_maintenance import ManagedLog, maintain_logs, rotate_managed_log


def _runner(calls, *, fail=()):
    def run(command, **_kwargs):
        calls.append(command)
        action = command[1] if len(command) > 1 else ""
        return subprocess.CompletedProcess(
            command,
            1 if action in fail else 0,
            stdout="",
            stderr=f"{action} failed" if action in fail else "",
        )
    return run


def _spec(tmp_path):
    label = "com.example.worker"
    plist = tmp_path / "Library" / "LaunchAgents" / f"{label}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    log = tmp_path / "worker.log"
    log.write_text("old log\n", encoding="utf-8")
    return ManagedLog(label, (log,)), plist, log


def test_below_threshold_does_not_touch_service(tmp_path, monkeypatch):
    spec, plist, _log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    calls = []

    result = rotate_managed_log(
        spec, max_bytes=1000, runner=_runner(calls),
    )

    assert result["status"] == "below_threshold"
    assert calls == []


def test_rotation_stops_swaps_and_then_restarts(tmp_path, monkeypatch):
    spec, plist, log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    calls = []

    result = rotate_managed_log(
        spec, max_bytes=1, keep=2, uid=501, runner=_runner(calls),
    )

    assert result["ok"] is True
    assert result["status"] == "rotated"
    assert log.read_text() == ""
    assert (tmp_path / "worker.log.1").read_text() == "old log\n"
    assert [command[1] for command in calls] == [
        "print", "bootout", "bootstrap", "print",
    ]
    assert calls[1][-1] == "gui/501/com.example.worker"


def test_bootout_failure_never_swaps_live_inode(tmp_path, monkeypatch):
    spec, plist, log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    calls = []

    result = rotate_managed_log(
        spec, max_bytes=1, runner=_runner(calls, fail={"bootout"}),
    )

    assert result["status"] == "stop_failed_recovered"
    assert log.read_text() == "old log\n"
    assert not (tmp_path / "worker.log.1").exists()
    assert [command[1] for command in calls] == [
        "print",
        "bootout",
        "bootstrap",
        "print",
    ]


def test_restart_failure_is_reported_after_rotation(tmp_path, monkeypatch):
    spec, plist, log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    calls = []

    print_count = 0

    def failed_restart(command, **_kwargs):
        nonlocal print_count
        calls.append(command)
        action = command[1]
        if action == "print":
            print_count += 1
            return subprocess.CompletedProcess(
                command, 0 if print_count == 1 else 1, "", "not loaded"
            )
        return subprocess.CompletedProcess(
            command,
            1 if action == "bootstrap" else 0,
            "",
            "bootstrap failed" if action == "bootstrap" else "",
        )

    result = rotate_managed_log(spec, max_bytes=1, runner=failed_restart)

    assert result["status"] == "restart_failed"
    assert result["ok"] is False
    assert log.exists()
    assert (tmp_path / "worker.log.1").exists()


def test_bootout_timeout_attempts_recovery_before_returning(
    tmp_path, monkeypatch
):
    spec, plist, log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    calls = []

    def timeout_then_recover(command, **_kwargs):
        calls.append(command)
        if command[1] == "bootout":
            raise subprocess.TimeoutExpired(command, 15)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = rotate_managed_log(
        spec, max_bytes=1, runner=timeout_then_recover
    )

    assert result["status"] == "stop_failed_recovered"
    assert result["ok"] is False
    assert log.read_text() == "old log\n"
    assert [command[1] for command in calls] == [
        "print",
        "bootout",
        "bootstrap",
        "print",
    ]


def test_maintenance_aggregates_failure(tmp_path, monkeypatch):
    spec, plist, _log = _spec(tmp_path)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: plist),
    )
    result = maintain_logs(
        max_bytes=1,
        specs=(spec,),
        runner=_runner([], fail={"bootout"}),
        lock_path=tmp_path / "maintenance.lock",
    )
    assert result["ok"] is False
