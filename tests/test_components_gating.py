"""Optional-feature gating in core/components.py (2026-07-13 audit).

A collaborator's default install alarmed [critical] forever on ef-stream /
lark-sidecar / admin — features doctor.sh explicitly calls optional. The
manifest now declares preconditions (requires_cmd / requires_file /
requires_config); an unmet precondition means SKIPPED (ok, never ⚠️).
"""

import json
import subprocess
from pathlib import Path

import core.components as components
from core.components import check_components, format_report


def _manifest(tmp_path, body: str):
    p = tmp_path / "components.yaml"
    p.write_text("components:\n" + body)
    return p


def test_requires_cmd_missing_skips(tmp_path):
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "core.ef_stream_loop"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True
    assert r["skipped"] is True
    assert "not installed" in r["detail"]


def test_requires_file_missing_skips(tmp_path):
    m = _manifest(tmp_path, """
  - name: dashboard
    check: http
    url: "http://127.0.0.1:1/"
    critical: true
    requires_file: no/such/plist.plist
""")
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True and r["skipped"] is True


def test_requires_config_unset_skips_and_enabled_arms(tmp_path):
    body = """
  - name: admin
    check: file_age
    path: does-not-exist
    max_age_hours: 1
    critical: true
    requires_config: admin.enabled
"""
    m = _manifest(tmp_path, body)
    # No jarvis.yaml at root → default admin.enabled=False → skipped
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["ok"] is True and r["skipped"] is True

    # Enabled in config → gate opens → the underlying check runs (and fails,
    # proving we reached it)
    armed = tmp_path / "armed"
    armed.mkdir()
    (armed / "jarvis.yaml").write_text("admin:\n  enabled: true\n")
    m2 = _manifest(armed, body)
    (r2,) = check_components(manifest_path=m2, root=armed)
    assert r2["ok"] is False
    assert "missing" in r2["detail"]


def test_requires_config_equality(tmp_path):
    body = """
  - name: lark-sidecar
    check: file_age
    path: does-not-exist
    max_age_hours: 1
    requires_config: lark.event_backend=sidecar
"""
    (tmp_path / "jarvis.yaml").write_text("lark:\n  event_backend: cli\n")
    m = _manifest(tmp_path, body)
    (r,) = check_components(manifest_path=m, root=tmp_path)
    assert r["skipped"] is True

    other = tmp_path / "sidecar"
    other.mkdir()
    (other / "jarvis.yaml").write_text("lark:\n  event_backend: sidecar\n")
    m2 = _manifest(other, body)
    (r2,) = check_components(manifest_path=m2, root=other)
    assert "skipped" not in r2
    assert r2["ok"] is False  # gate open, real check ran


def test_format_report_marks_skipped_not_warned(tmp_path):
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "x"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
""")
    report = format_report(check_components(manifest_path=m, root=tmp_path))
    assert "○ ef-stream" in report
    assert "⚠️" not in report
    assert "skipped" in report


def test_daemon_critical_probe_ignores_skipped(tmp_path):
    """daemon._probe_manifest_criticals only alerts on ok=False — a skipped
    component must present ok=True through the critical_only path too."""
    m = _manifest(tmp_path, """
  - name: ef-stream
    check: pgrep
    pattern: "x"
    critical: true
    requires_cmd: definitely-not-a-real-binary-xyz
  - name: bot
    check: pid
    path: .bot.pid
    critical: true
""")
    results = check_components(critical_only=True, manifest_path=m,
                               root=tmp_path)
    by_name = {r["name"]: r for r in results}
    assert by_name["ef-stream"]["ok"] is True     # skipped → never pages
    assert by_name["bot"]["ok"] is False          # ungated check still real


def test_taskline_component_checks_readiness_not_launchd_registration(
    tmp_path, monkeypatch,
):
    binary = tmp_path / "taskline-server"
    binary.write_text("binary")
    manifest = _manifest(tmp_path, f"""
  - name: taskline
    check: taskline
    requires_file: {binary}
""")

    monkeypatch.setattr(
        components.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"healthy": False, "registered": True}),
            "",
        ),
    )
    (unhealthy,) = check_components(
        manifest_path=manifest, root=tmp_path
    )
    assert unhealthy["ok"] is False

    monkeypatch.setattr(
        components.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"healthy": True, "registered": True}),
            "",
        ),
    )
    (healthy,) = check_components(
        manifest_path=manifest, root=tmp_path
    )
    assert healthy["ok"] is True


def test_launchd_installer_skips_optional_taskline_before_bootstrap():
    script = (
        Path(__file__).parent.parent / "scripts" / "launchd" / "install.sh"
    ).read_text(encoding="utf-8")
    guard = script.index(
        '[[ "$label" == "com.pascal.jarvis.taskline"'
    )
    bootstrap = script.index("launchctl bootstrap")

    assert guard < bootstrap
    assert "optional Taskline binary not installed" in script
