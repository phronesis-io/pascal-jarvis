from __future__ import annotations


class _Response:
    status = 204

    def close(self):
        pass


def _config(root, *, enabled=True, url="https://monitor.test/secret-token"):
    (root / "jarvis.yaml").write_text(
        "ops:\n"
        "  deadman:\n"
        f"    enabled: {'true' if enabled else 'false'}\n"
        f"    url: {url}\n"
        "    interval_seconds: 300\n",
        encoding="utf-8",
    )


def test_disabled_is_safe_noop(tmp_path):
    from core.deadman import ping_due, status

    _config(tmp_path, enabled=False)
    assert ping_due(tmp_path).status == "disabled"
    assert status(tmp_path).status == "disabled"


def test_ping_is_rate_limited_and_status_uses_success_stamp(tmp_path):
    from core.deadman import ping_due, status

    _config(tmp_path)
    seen = []

    def opener(request, timeout):
        seen.append((request.full_url, timeout))
        return _Response()

    assert ping_due(tmp_path, now=1000, opener=opener).status == "ok"
    assert ping_due(tmp_path, now=1100, opener=opener).status == "not_due"
    assert len(seen) == 1
    assert status(tmp_path, now=1200).status == "ok"


def test_failure_never_leaks_token(tmp_path):
    from core.deadman import ping_due

    secret = "private-monitor-token"
    _config(tmp_path, url=f"https://monitor.test/{secret}")

    def opener(request, timeout):
        raise RuntimeError(f"failed at {request.full_url}")

    result = ping_due(tmp_path, now=1000, opener=opener)
    assert result.status == "failed"
    assert secret not in result.detail


def test_non_tls_remote_endpoint_is_rejected(tmp_path):
    from core.deadman import ping_due

    _config(tmp_path, url="http://monitor.test/token")
    result = ping_due(tmp_path, now=1000)
    assert result.status == "failed"
    assert "invalid" in result.detail


def test_external_deadman_component_reports_verified_success(tmp_path):
    import time

    from core import components
    from core.deadman import SUCCESS_STAMP

    _config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / SUCCESS_STAMP).write_text(str(time.time()))
    manifest = tmp_path / "components.yaml"
    manifest.write_text(
        "components:\n"
        "  - name: external-deadman\n"
        "    check: deadman\n"
        "    requires_config: ops.deadman.enabled\n"
    )
    components._config_cache.clear()

    result = components.check_components(
        manifest_path=manifest, root=tmp_path)[0]

    assert result["name"] == "external-deadman"
    assert result["ok"] is True
    assert result.get("skipped") is not True
