from __future__ import annotations

import pytest

from core.release_gate import ReleaseGate, ReleaseGateError, _repo_name


SHA = "a" * 40


def _responses(**overrides):
    responses = {
        ("git", "branch", "--show-current"): "main",
        ("git", "rev-parse", "HEAD"): SHA,
        ("git", "fetch", "--quiet", "origin", "main"): "",
        ("git", "rev-parse", "origin/main"): SHA,
        ("git", "status", "--porcelain", "--untracked-files=no"): "",
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
            "gh", "api", "repos/phronesis-io/pascal-jarvis/pulls/42/reviews",
        ): [{
            "user": {"login": "review-bot"},
            "state": "APPROVED",
        }],
        (
            "gh", "api", "repos/phronesis-io/pascal-jarvis/issues/42/comments",
        ): [],
    }
    responses.update(overrides)
    return responses


def _gate(monkeypatch, responses):
    gate = ReleaseGate("/tmp/repo")

    def fake_run(command, **_kwargs):
        key = tuple(command)
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


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ({("git", "branch", "--show-current"): "feature"}, "local main"),
        ({("git", "rev-parse", "origin/main"): "b" * 40}, "origin/main"),
        ({
            ("git", "status", "--porcelain", "--untracked-files=no"):
                " M core/actions.py",
        }, "tracked worktree"),
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
        "body": "REVIEW-GATE: PASS\nNo blocking findings.",
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
        "body": "This is not REVIEW-GATE: PASS because findings remain.",
    }]

    with pytest.raises(ReleaseGateError, match="independent review evidence"):
        _gate(monkeypatch, responses).verify()


def test_restart_runs_release_gate_before_touching_deploy_guard():
    script = (
        __import__("pathlib").Path(__file__).parent.parent / "restart.sh"
    ).read_text(encoding="utf-8")
    full = script.index('  --full|-f)')
    normal = script.index('  *)', full)

    assert script.index("_verify_release_gate", full) < script.index(
        "_set_deploy_guard", full
    )
    assert script.index("_verify_release_gate", normal) < script.index(
        "_set_deploy_guard", normal
    )
