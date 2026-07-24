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


def test_initial_probe_timeout_isolated_from_later_services(
    tmp_path, monkeypatch
):
    first, first_plist, _ = _spec(tmp_path / "first")
    second, second_plist, second_log = _spec(tmp_path / "second")
    first = ManagedLog("com.example.first", first.paths)
    second = ManagedLog("com.example.second", second.paths)
    plists = {
        first.label: first_plist,
        second.label: second_plist,
    }
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda self: plists[self.label]),
    )

    def runner(command, **_kwargs):
        if command[1] == "print" and command[-1].endswith(first.label):
            raise subprocess.TimeoutExpired(command, 15)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = maintain_logs(
        max_bytes=1,
        specs=(first, second),
        runner=runner,
        lock_path=tmp_path / "maintenance.lock",
    )

    assert result["ok"] is False
    assert [item["status"] for item in result["results"]] == [
        "probe_failed",
        "rotated",
    ]
    assert second_log.read_text() == ""


def test_absent_optional_service_rotates_stale_unowned_log(
    tmp_path, monkeypatch
):
    log = tmp_path / "optional.log"
    log.write_text("stale optional output", encoding="utf-8")
    missing_plist = tmp_path / "missing.plist"
    spec = ManagedLog("com.example.optional", (log,), optional=True)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: missing_plist),
    )
    calls = []

    def explicitly_absent(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            113,
            "",
            'Could not find service "com.example.optional" in domain for user',
        )

    result = rotate_managed_log(
        spec,
        max_bytes=1,
        runner=explicitly_absent,
    )

    assert result["ok"] is True
    assert result["status"] == "optional_absent_rotated"
    assert log.read_text() == ""
    assert (tmp_path / "optional.log.1").read_text() == "stale optional output"
    assert [command[1] for command in calls] == ["print"]


def test_optional_service_ambiguous_probe_failure_does_not_rotate(
    tmp_path, monkeypatch
):
    log = tmp_path / "optional.log"
    log.write_text("possibly live output", encoding="utf-8")
    spec = ManagedLog("com.example.optional", (log,), optional=True)
    monkeypatch.setattr(
        ManagedLog,
        "plist",
        property(lambda _self: tmp_path / "missing.plist"),
    )

    result = rotate_managed_log(
        spec,
        max_bytes=1,
        runner=_runner([], fail={"print"}),
    )

    assert result["ok"] is False
    assert result["status"] == "probe_failed"
    assert log.read_text() == "possibly live output"
    assert not (tmp_path / "optional.log.1").exists()


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
