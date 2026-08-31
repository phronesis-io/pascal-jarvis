from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from core.release_gate import (
    ReleaseGate,
    ReleaseGateError,
    ReleaseGateUnavailable,
    _repo_name,
)


SHA = "a" * 40
HEAD_SHA = "b" * 40


def _responses(**overrides):
    responses = {
        ("git", "branch", "--show-current"): "main",
        ("git", "rev-parse", "HEAD"): SHA,
        ("git", "fetch", "--quiet", "origin", "main"): "",
        ("git", "rev-parse", "origin/main"): SHA,
        ("git", "status", "--porcelain", "--untracked-files=all"): "",
        ("git", "remote", "get-url", "origin"):
            "git@github.com:phronesis-io/pascal-jarvis.git",
        (
            "gh", "api",
            "repos/phronesis-io/pascal-jarvis/branches/main/protection",
        ): {
            "enforce_admins": {"enabled": True},
            "required_status_checks": {"strict": True, "contexts": ["test"]},
            "required_pull_request_reviews": {"required_approving_review_count": 0},
            "required_conversation_resolution": {"enabled": True},
        },
        (
            "gh", "api", "-H", "Accept: application/vnd.github+json",
            f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/pulls",
        ): [{
            "number": 42,
            "merged_at": "2026-07-24T12:00:00Z",
            "base": {"ref": "main"},
            "user": {"login": "author"},
            "head": {"sha": HEAD_SHA},
        }],
        (
            "gh", "api",
            f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs",
        ): {
            "check_runs": [{
                "name": "test", "status": "completed", "conclusion": "success",
            }],
        },
        (
            "gh", "api",
            f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/status",
        ): {"statuses": []},
        (
            "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
        ): [{
            "user": {"login": "review-bot"},
            "author_association": "MEMBER",
            "state": "APPROVED",
            "commit_id": HEAD_SHA,
        }],
        (
            "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
        ): [],
    }
    responses.update(overrides)
    return responses


def _gate(monkeypatch, responses, **kwargs):
    gate = ReleaseGate("/tmp/repo", **kwargs)

    def fake_run(command, **_kwargs):
        key = tuple(command)
        if (
            key not in responses
            and command[:4] == ["gh", "api", "--paginate", "--slurp"]
        ):
            key = ("gh", "api", command[4])
        if key not in responses:
            raise AssertionError(f"unexpected command: {command}")
        return responses[key]

    monkeypatch.setattr(gate, "_run", fake_run)
    return gate


def test_repo_name_accepts_ssh_and_https():
    assert _repo_name("git@github.com:owner/repo.git") == "owner/repo"
    assert _repo_name("https://github.com/owner/repo") == "owner/repo"


def test_release_gate_accepts_merged_reviewed_checked_main(monkeypatch):
    result = _gate(monkeypatch, _responses()).verify()

    assert result["ok"] is True
    assert result["pr"] == 42
    assert result["required_checks"] == ["test"]
    assert result["review_evidence"] == ["review:review-bot:APPROVED"]


def test_resilient_gate_reuses_only_fresh_exact_sha_live_evidence(
    tmp_path, monkeypatch,
):
    cache = tmp_path / "private" / "release-gate.json"
    live = _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_000.0,
    ).verify_resilient()

    assert live["evidence_source"] == "github_live"
    assert live["stale"] is False
    assert live["cache_persisted"] is True
    assert cache.stat().st_mode & 0o077 == 0

    offline = _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_120.0,
    )
    monkeypatch.setattr(
        offline,
        "verify",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReleaseGateUnavailable("network down")
        ),
    )

    result = offline.verify_resilient()

    assert result["ok"] is True
    assert result["sha"] == SHA
    assert result["stale"] is True
    assert result["evidence_source"] == "cached_live_verification"
    assert result["cache_age_seconds"] == 120.0


@pytest.mark.parametrize(
    ("now", "replacement", "message"),
    [
        (90_000.0, {}, "stale"),
        (
            1_120.0,
            {
                ("git", "rev-parse", "HEAD"): "c" * 40,
                ("git", "rev-parse", "origin/main"): "c" * 40,
            },
            "another revision",
        ),
    ],
)
def test_resilient_gate_rejects_stale_or_different_sha_cache(
    tmp_path, monkeypatch, now, replacement, message,
):
    cache = tmp_path / "release-gate.json"
    _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_000.0,
    ).verify_resilient()
    responses = _responses()
    responses.update(replacement)
    offline = _gate(
        monkeypatch,
        responses,
        cache_path=cache,
        now=lambda: now,
    )
    monkeypatch.setattr(
        offline,
        "verify",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReleaseGateUnavailable("network down")
        ),
    )

    with pytest.raises(ReleaseGateError, match=message):
        offline.verify_resilient()


def test_resilient_gate_rejects_world_readable_cache(tmp_path, monkeypatch):
    cache = tmp_path / "release-gate.json"
    _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_000.0,
    ).verify_resilient()
    os.chmod(cache, 0o644)
    offline = _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_010.0,
    )
    monkeypatch.setattr(
        offline,
        "verify",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReleaseGateUnavailable("network down")
        ),
    )

    with pytest.raises(ReleaseGateError, match="permissions are unsafe"):
        offline.verify_resilient()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("required_checks", [], "no required checks"),
        ("branch_protection", [], "branch-protection evidence is invalid"),
        ("review_evidence", [], "approval evidence is incomplete"),
    ],
)
def test_resilient_gate_rejects_incomplete_cached_evidence(
    tmp_path, monkeypatch, field, value, message,
):
    cache = tmp_path / "release-gate.json"
    _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_000.0,
    ).verify_resilient()
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["result"][field] = value
    cache.write_text(json.dumps(payload), encoding="utf-8")
    offline = _gate(
        monkeypatch,
        _responses(),
        cache_path=cache,
        now=lambda: 1_010.0,
    )
    monkeypatch.setattr(
        offline,
        "verify",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReleaseGateUnavailable("network down")
        ),
    )

    with pytest.raises(ReleaseGateError, match=message):
        offline.verify_resilient()


def test_resilient_gate_never_uses_cache_for_policy_failure(
    tmp_path, monkeypatch,
):
    gate = _gate(
        monkeypatch,
        _responses(),
        cache_path=tmp_path / "release-gate.json",
    )
    monkeypatch.setattr(
        gate,
        "verify",
        lambda **_kwargs: (_ for _ in ()).throw(
            ReleaseGateError("required checks are not successful")
        ),
    )

    with pytest.raises(ReleaseGateError, match="required checks"):
        gate.verify_resilient()


def test_network_errors_are_the_only_remote_failures_marked_unavailable():
    def network_runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, "", "Could not resolve host: api.github.com"
        )

    with pytest.raises(ReleaseGateUnavailable):
        ReleaseGate("/tmp/repo", runner=network_runner)._run(
            ["gh", "api", "repos/org/repo"]
        )

    def auth_runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "HTTP 401: Bad credentials")

    with pytest.raises(ReleaseGateError) as captured:
        ReleaseGate("/tmp/repo", runner=auth_runner)._run(
            ["gh", "api", "repos/org/repo"]
        )
    assert not isinstance(captured.value, ReleaseGateUnavailable)


def test_release_gate_reads_review_evidence_from_later_pages(monkeypatch):
    endpoint = "repos/phronesis-io/pascal-jarvis/pulls/42/reviews"
    responses = _responses()
    responses[("gh", "api", "--paginate", "--slurp", endpoint)] = [
        [{
            "user": {"login": "review-bot"},
            "author_association": "MEMBER",
            "state": "COMMENTED",
            "body": "Earlier review.",
        }],
        [{
            "user": {"login": "review-bot"},
            "author_association": "MEMBER",
            "state": "APPROVED",
            "commit_id": HEAD_SHA,
        }],
    ]

    result = _gate(monkeypatch, responses).verify()

    assert result["review_evidence"] == ["review:review-bot:APPROVED"]


def test_release_gate_reads_required_check_from_later_page(monkeypatch):
    endpoint = (
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs"
    )
    responses = _responses()
    responses[("gh", "api", "--paginate", "--slurp", endpoint)] = [
        {"check_runs": [{
            "name": "other",
            "status": "completed",
            "conclusion": "success",
        }]},
        {"check_runs": [{
            "name": "test",
            "status": "completed",
            "conclusion": "success",
        }]},
    ]

    result = _gate(monkeypatch, responses).verify()

    assert result["required_checks"] == ["test"]


def test_release_gate_accepts_required_classic_commit_status(monkeypatch):
    runs_key = (
        "gh", "api",
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs",
    )
    status_key = (
        "gh", "api",
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/status",
    )
    responses = _responses()
    responses[runs_key] = {"check_runs": []}
    responses[status_key] = {
        "statuses": [{"context": "test", "state": "success"}],
    }

    result = _gate(monkeypatch, responses).verify()

    assert result["required_checks"] == ["test"]


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({("git", "branch", "--show-current"): "feature"}, "local main"),
        ({("git", "rev-parse", "origin/main"): "b" * 40}, "origin/main"),
        ({
            ("git", "status", "--porcelain", "--untracked-files=all"):
                " M core/actions.py",
        }, "worktree"),
        ({
            ("git", "status", "--porcelain", "--untracked-files=all"):
                "?? sitecustomize.py",
        }, "worktree"),
    ],
)
def test_release_gate_rejects_invalid_git_state(
    monkeypatch, replacement, message,
):
    responses = _responses()
    responses.update(replacement)
    with pytest.raises(ReleaseGateError, match=message):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_weak_branch_protection(monkeypatch):
    key = (
        "gh", "api",
        "repos/phronesis-io/pascal-jarvis/branches/main/protection",
    )
    responses = _responses()
    responses[key] = {
        **responses[key],
        "enforce_admins": {"enabled": False},
    }

    with pytest.raises(ReleaseGateError, match="admin bypass"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_protection_without_required_checks(monkeypatch):
    key = (
        "gh", "api",
        "repos/phronesis-io/pascal-jarvis/branches/main/protection",
    )
    responses = _responses()
    responses[key] = {
        **responses[key],
        "required_status_checks": {
            "strict": True,
            "contexts": [],
            "checks": [],
        },
    }

    with pytest.raises(ReleaseGateError, match="no required checks"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_failed_required_check(monkeypatch):
    key = (
        "gh", "api",
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs",
    )
    responses = _responses()
    responses[key] = {
        "check_runs": [{
            "name": "test", "status": "completed", "conclusion": "failure",
        }],
    }

    with pytest.raises(ReleaseGateError, match="not successful: test"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_matches_required_check_to_github_app(monkeypatch):
    protection_key = (
        "gh", "api",
        "repos/phronesis-io/pascal-jarvis/branches/main/protection",
    )
    runs_key = (
        "gh", "api",
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs",
    )
    responses = _responses()
    responses[protection_key] = {
        **responses[protection_key],
        "required_status_checks": {
            "strict": True,
            "contexts": ["test"],
            "checks": [{"context": "test", "app_id": 42}],
        },
    }
    responses[runs_key] = {
        "check_runs": [
            {
                "name": "test",
                "status": "completed",
                "conclusion": "success",
                "app": {"id": 99},
            },
            {
                "name": "test",
                "status": "completed",
                "conclusion": "failure",
                "app": {"id": 42},
            },
        ]
    }

    with pytest.raises(ReleaseGateError, match=r"test@app:42"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_accepts_required_check_from_configured_app(monkeypatch):
    protection_key = (
        "gh", "api",
        "repos/phronesis-io/pascal-jarvis/branches/main/protection",
    )
    runs_key = (
        "gh", "api",
        f"repos/phronesis-io/pascal-jarvis/commits/{SHA}/check-runs",
    )
    responses = _responses()
    responses[protection_key] = {
        **responses[protection_key],
        "required_status_checks": {
            "strict": True,
            "contexts": ["test"],
            "checks": [{"context": "test", "app_id": 42}],
        },
    }
    responses[runs_key] = {
        "check_runs": [{
            "name": "test",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 42},
        }]
    }

    result = _gate(monkeypatch, responses).verify()

    assert result["required_checks"] == ["test@app:42"]


def test_release_gate_rejects_unreviewed_or_self_reviewed_pr(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    responses = _responses()
    responses[reviews_key] = [
        {"user": {"login": "author"}, "state": "APPROVED"},
        {"user": {"login": "review-bot"}, "state": "PENDING"},
    ]
    responses[comments_key] = [
        {"user": {"login": "other"}, "body": "looks plausible"},
    ]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_accepts_explicit_independent_attestation(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "body": f"REVIEW-GATE: PASS {SHA}\nNo blocking findings.",
    }]

    result = _gate(monkeypatch, responses).verify()
    assert result["review_evidence"] == ["attestation:review-bot"]


def test_release_gate_rejects_generic_commented_review(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    responses = _responses()
    responses[reviews_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "state": "COMMENTED",
        "body": "Found a blocking defect.",
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_requires_attestation_on_its_own_line(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "body": f"This is not REVIEW-GATE: PASS {SHA} because findings remain.",
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_review_of_an_older_commit(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    responses = _responses()
    responses[reviews_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "state": "APPROVED",
        "commit_id": "c" * 40,
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_review_bound_only_to_merge_commit(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    responses = _responses()
    responses[reviews_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "state": "APPROVED",
        "commit_id": SHA,
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_unbound_pass_attestation(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "MEMBER",
        "body": "REVIEW-GATE: PASS",
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_rejects_public_attestation_without_repo_permission(
    monkeypatch,
):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    permission_key = (
        "gh",
        "api",
        "repos/phronesis-io/pascal-jarvis/collaborators/stranger/permission",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "stranger"},
        "author_association": "NONE",
        "body": f"REVIEW-GATE: PASS {SHA}",
    }]
    responses[permission_key] = {"permission": "read", "role_name": "read"}

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_release_gate_accepts_trusted_bot_by_repository_permission(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    permission_key = (
        "gh",
        "api",
        "repos/phronesis-io/pascal-jarvis/collaborators/review-bot/permission",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "review-bot"},
        "author_association": "NONE",
        "body": f"REVIEW-GATE: PASS {SHA}",
    }]
    responses[permission_key] = {"permission": "write", "role_name": "write"}

    result = _gate(monkeypatch, responses).verify()

    assert result["review_evidence"] == ["attestation:review-bot"]


def test_release_gate_accepts_explicit_admin_owner_decision(monkeypatch):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    permission_key = (
        "gh",
        "api",
        "repos/phronesis-io/pascal-jarvis/collaborators/author/permission",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "author"},
        "author_association": "MEMBER",
        "created_at": "2026-07-24T12:01:00Z",
        "body": (
            f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
            "REASON: Pascal explicitly requested this verified release."
        ),
    }]
    responses[permission_key] = {
        "permission": "admin",
        "role_name": "admin",
    }

    result = _gate(monkeypatch, responses).verify()

    assert result["approval_mode"] == "owner_release_decision"
    assert result["review_evidence"] == []
    assert result["owner_release_decisions"] == [{
        "actor": "author",
        "reason": "Pascal explicitly requested this verified release.",
    }]


@pytest.mark.parametrize(
    ("comment_body", "permission", "review_policy", "created_at"),
    [
        (
            f"RELEASE-GATE: OWNER-APPROVED {SHA}",
            "admin",
            {"required_approving_review_count": 0},
            "2026-07-24T12:01:00Z",
        ),
        (
            (
                f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
                "REASON: Pascal explicitly requested this verified release."
            ),
            "write",
            {"required_approving_review_count": 0},
            "2026-07-24T12:01:00Z",
        ),
        (
            (
                f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
                "REASON: Pascal explicitly requested this verified release."
            ),
            "admin",
            {"required_approving_review_count": 1},
            "2026-07-24T12:01:00Z",
        ),
        (
            (
                f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
                "REASON: Pascal explicitly requested this verified release."
            ),
            "admin",
            {
                "required_approving_review_count": 0,
                "require_code_owner_reviews": True,
            },
            "2026-07-24T12:01:00Z",
        ),
        (
            (
                f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
                "REASON: Pascal explicitly requested this verified release."
            ),
            "admin",
            {
                "required_approving_review_count": 0,
                "require_last_push_approval": True,
            },
            "2026-07-24T12:01:00Z",
        ),
        (
            (
                f"RELEASE-GATE: OWNER-APPROVED {SHA}\n"
                "REASON: Pascal explicitly requested this verified release."
            ),
            "admin",
            {"required_approving_review_count": 0},
            "2026-07-24T11:59:00Z",
        ),
    ],
)
def test_release_gate_rejects_invalid_owner_decision(
    monkeypatch,
    comment_body,
    permission,
    review_policy,
    created_at,
):
    reviews_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
    )
    comments_key = (
        "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
    )
    permission_key = (
        "gh",
        "api",
        "repos/phronesis-io/pascal-jarvis/collaborators/author/permission",
    )
    protection_key = (
        "gh", "api",
        "repos/phronesis-io/pascal-jarvis/branches/main/protection",
    )
    responses = _responses()
    responses[reviews_key] = []
    responses[comments_key] = [{
        "user": {"login": "author"},
        "author_association": "MEMBER",
        "created_at": created_at,
        "body": comment_body,
    }]
    responses[permission_key] = {
        "permission": permission,
        "role_name": permission,
    }
    responses[protection_key] = {
        **responses[protection_key],
        "required_pull_request_reviews": review_policy,
    }

    with pytest.raises(ReleaseGateError, match="owner release decision"):
        _gate(monkeypatch, responses).verify()


def test_restart_runs_release_gate_before_touching_deploy_guard(tmp_path):
    script = (
        __import__("pathlib").Path(__file__).parent.parent / "restart.sh"
    ).read_text(encoding="utf-8")
    deploy = script[
        script.index("governed_deploy()"):
        script.index('case "${1:-}"')
    ]
    assert deploy.index("_verify_release_gate") < deploy.index(
        "_verify_retired_self_improve_quiescent"
    ) < deploy.index("_set_deploy_guard")

    from scripts.retired_self_improve_preflight import inspect_retired_worker

    clean = inspect_retired_worker(
        tmp_path,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "  1 /sbin/launchd\n", ""
        ),
        platform="test",
    )
    assert clean["ok"] is True
    state = tmp_path / "data" / "self_improve_cycle.json"
    state.parent.mkdir()
    state.write_text('{"status":"running","pid":42}', encoding="utf-8")
    active = inspect_retired_worker(
        tmp_path,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "  42 python -m core.self_improve_cycle run si-1\n", ""
        ),
        platform="test",
    )
    assert active["ok"] is False
    assert "legacy run is unresolved:running" in active["issues"]
    assert "recorded legacy pid is alive:42" in active["issues"]

    state.write_text('{"status":"mystery","pid":"broken"}', encoding="utf-8")
    uncertain = inspect_retired_worker(
        tmp_path,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
        platform="test",
    )
    assert uncertain["ok"] is False
    assert "legacy state status is unknown:mystery" in uncertain["issues"]
    assert "legacy state pid is invalid" in uncertain["issues"]
    assert (
        "process table inspection returned no parseable rows"
        in uncertain["issues"]
    )

    state.write_text('{"status":"succeeded","pid":0}', encoding="utf-8")
    launchd_uncertain = inspect_retired_worker(
        tmp_path,
        runner=lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            "  1 /sbin/launchd\n" if command[0] == "/bin/ps" else "",
            "",
        ),
        platform="darwin",
    )
    assert launchd_uncertain["ok"] is False
    assert (
        "launchd inspection returned unparseable output"
        in launchd_uncertain["issues"]
    )

    case = script[script.index('case "${1:-}"'):]
    assert '--full|-f)\n    governed_deploy "Governed Full-Runtime Deploy"' in case
    assert '\"\")\n    governed_deploy "Governed Full-Runtime Deploy"' in case


def test_official_launchd_kickstart_is_inside_joined_release_verification():
    root = Path(__file__).parent.parent
    script = (root / "restart.sh").read_text(encoding="utf-8")
    governed = script[
        script.index("governed_deploy()"):
        script.index('case "${1:-}"')
    ]
    claims = (root / "core" / "memory_operational_claims.py").read_text(
        encoding="utf-8"
    )

    assert governed.index("_verify_release_gate") < governed.index(
        "restart_daemon"
    )
    assert governed.index("restart_daemon") < governed.index(
        "verify_full_runtime"
    )
    assert "launchctl kickstart -k" in script
    assert "不要手动" in claims
    assert "直接运行 `launchctl kickstart`" in claims


def test_runtime_only_restart_requires_governed_same_revision():
    script = (Path(__file__).parent.parent / "restart.sh").read_text(
        encoding="utf-8"
    )
    gate = script[
        script.index("_verify_runtime_only_gate()"):
        script.index('case "${1:-}" in')
    ]
    runtime = script[
        script.index("--runtime|-r)"):
        script.index("--help|-h)")
    ]

    assert 'git -C "$JARVIS_DIR" status --porcelain --untracked-files=all' in gate
    assert "python3 -m core.deploy verify" in gate
    assert "--allow-config-changes" in gate
    assert "--require bot --require heartbeat-loop" in gate
    assert "_verify_runtime_only_gate" in runtime
    assert "_verify_release_gate" in runtime
    assert "_verify_retired_self_improve_quiescent" in runtime
    assert runtime.index("_verify_release_gate") < runtime.index(
        "_verify_retired_self_improve_quiescent"
    ) < runtime.index(
        "_verify_runtime_only_gate"
    )
    assert runtime.index("_verify_runtime_only_gate") < runtime.index(
        "_set_deploy_guard"
    )
