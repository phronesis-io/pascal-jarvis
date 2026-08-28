from pathlib import Path

from core import resource_health


def test_fd_health_thresholds():
    healthy = resource_health.evaluate_fd_health(1, count=30, limit=100)
    warning = resource_health.evaluate_fd_health(1, count=60, limit=100)
    critical = resource_health.evaluate_fd_health(1, count=80, limit=100)
    assert healthy.state == "healthy"
    assert warning.state == "warning"
    assert critical.state == "critical"
    assert "⚠️" in critical.line()


def test_fd_health_unknown_is_not_a_false_alarm(monkeypatch):
    monkeypatch.setattr(resource_health, "open_fd_count", lambda _pid: None)
    assert resource_health.evaluate_fd_health(123, limit=256) is None


def test_lsof_output_counts_only_numeric_fd_rows(monkeypatch):
    isolated_memory = Path.home() / ".jarvis" / "memory"
    assert isolated_memory.is_dir()

    class MissingProcFd:
        def is_dir(self):
            return False

    # Replace only resource_health's constructor binding. Patching
    # pathlib.Path.is_dir on the shared class also changes pytest's runtime
    # write guard and can make existing directories disappear at teardown.
    monkeypatch.setattr(resource_health, "Path", lambda _value: MissingProcFd())
    monkeypatch.setattr(resource_health.shutil, "which", lambda _name: "/usr/sbin/lsof")

    class Result:
        returncode = 0
        stdout = "p123\nfcwd\nftxt\nf0\nf1\nf42\n"

    monkeypatch.setattr(
        resource_health.subprocess, "run", lambda *_args, **_kwargs: Result()
    )
    assert resource_health.open_fd_count(123) == 3
    assert isolated_memory.is_dir()


def test_macos_uses_launchd_service_limit(monkeypatch):
    monkeypatch.setattr(resource_health.sys, "platform", "darwin")
    monkeypatch.setattr(
        resource_health.resource,
        "getrlimit",
        lambda _kind: (1_048_575, 1_048_575),
    )
    monkeypatch.setattr(
        resource_health.shutil, "which", lambda name: f"/bin/{name}"
    )

    class Result:
        returncode = 0
        stdout = "maxfiles 256 unlimited"

    monkeypatch.setattr(
        resource_health.subprocess, "run", lambda *_args, **_kwargs: Result()
    )
    assert resource_health.soft_fd_limit() == 256
