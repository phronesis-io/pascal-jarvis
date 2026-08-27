"""What leaves this machine for a public network, and what does not."""

import json

import pytest

from core import podcasts


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(podcasts, "STATE_PATH", tmp_path / "podcast_state.json")
    monkeypatch.setattr(podcasts, "TRANSCRIPT_DIR", tmp_path / "podcasts")
    monkeypatch.setattr(podcasts, "WATCHLIST_PATH", tmp_path / "watchlist.json")
    return tmp_path


GOOD_CONTENT = (
    "Digest of one episode, read from the official transcript.\n"
    "- 0:42:10 - they put a hard number on inference spend: $23 per task on "
    "the cheap model against $550 on the expensive one, same task.\n"
    "- 2:58:12 - the orchestrator runs the model but execution happens in an "
    "isolated VM, so untrusted content from pull requests never reaches model "
    "context.\n"
    "Discount it here: the sample is a single greenfield project with no "
    "existing users, which is the most favourable case for the claim.\n"
    "Ask me for a digest of a specific episode."
)
GOOD_SUMMARY = "Transcript-grounded digest: isolated-VM execution and per-task model cost"


def test_publishable_digest_clears_the_bar(isolated_state):
    assert podcasts.broadcast_reject_reason("vid1", GOOD_CONTENT, GOOD_SUMMARY) == ""


def test_thin_digest_is_refused(isolated_state):
    reason = podcasts.broadcast_reject_reason("vid1", "They talked about agents.", GOOD_SUMMARY)
    assert "too thin" in reason


def test_digest_without_two_timestamps_is_refused(isolated_state):
    body = GOOD_CONTENT.replace("0:42:10", "early on").replace("2:58:12", "later")
    assert "timestamped" in podcasts.broadcast_reject_reason("vid1", body, GOOD_SUMMARY)


@pytest.mark.parametrize("leak", ["Pascal asked for this", "built into Jarvis", "见飞书文档"])
def test_private_markers_never_reach_the_network(isolated_state, leak):
    body = GOOD_CONTENT + "\n" + leak
    assert "private marker" in podcasts.broadcast_reject_reason("vid1", body, GOOD_SUMMARY)


def test_oversized_summary_is_refused(isolated_state):
    assert "summary" in podcasts.broadcast_reject_reason("vid1", GOOD_CONTENT, "x" * 101)


def test_same_episode_is_never_broadcast_twice(isolated_state, monkeypatch):
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        return 0, '{"item_id": "351234388815511552"}', ""

    monkeypatch.setattr(podcasts, "_eigenflux", lambda: "/usr/bin/eigenflux")
    monkeypatch.setattr(podcasts, "_run", fake_run)

    first = podcasts.broadcast("vid1", "An episode", GOOD_CONTENT, GOOD_SUMMARY,
                               url="https://example.invalid/watch")
    assert first == {"item_id": "351234388815511552"}
    notes = json.loads(calls[0][calls[0].index("--notes") + 1])
    assert notes["type"] == "supply"
    assert notes["summary"] == GOOD_SUMMARY
    assert "--accept-reply" in calls[0]

    second = podcasts.broadcast("vid1", "An episode", GOOD_CONTENT, GOOD_SUMMARY)
    assert second == {"skipped": "already broadcast"}
    assert len(calls) == 1


def test_publish_failure_is_reported_not_recorded(isolated_state, monkeypatch):
    monkeypatch.setattr(podcasts, "_eigenflux", lambda: "/usr/bin/eigenflux")
    monkeypatch.setattr(podcasts, "_run", lambda cmd, timeout: (1, "", "validation error"))
    result = podcasts.broadcast("vid1", "An episode", GOOD_CONTENT, GOOD_SUMMARY)
    assert "validation error" in result["error"]
    assert "vid1" not in (podcasts.load_state().get("broadcast") or {})


def test_missing_item_id_is_not_a_success(isolated_state, monkeypatch):
    monkeypatch.setattr(podcasts, "_eigenflux", lambda: "/usr/bin/eigenflux")
    monkeypatch.setattr(podcasts, "_run", lambda cmd, timeout: (0, "Broadcast published", ""))
    result = podcasts.broadcast("vid1", "An episode", GOOD_CONTENT, GOOD_SUMMARY)
    assert "no item_id" in result["error"]
    assert "vid1" not in (podcasts.load_state().get("broadcast") or {})


def test_missing_cli_is_a_skip_not_a_crash(isolated_state, monkeypatch):
    monkeypatch.setattr(podcasts, "_eigenflux", lambda: None)
    assert podcasts.broadcast("vid1", "t", GOOD_CONTENT, GOOD_SUMMARY)["skipped"]


def test_vtt_becomes_timestamped_paragraphs():
    vtt = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.000\nhello there\n\n"
        "00:00:03.000 --> 00:00:05.000\nhello there\n\n"
        "00:00:05.000 --> 00:00:07.000\n<c>second line</c>\n"
    )
    text = podcasts.vtt_to_text(vtt, cues_per_para=2)
    assert text.startswith("[00:00:01] hello there second line")
    assert text.count("hello there") == 1  # rolling-caption repeat dropped


@pytest.mark.parametrize("raw,minutes", [("5:15:51", 315), ("48:12", 48), ("612", 10), ("", 0)])
def test_duration_parsing(raw, minutes):
    assert podcasts._duration_minutes(raw) == minutes


def test_marking_records_the_doc(isolated_state):
    podcasts.mark("vid1", "https://example.invalid/doc")
    state = podcasts.load_state()
    assert state["seen"] == ["vid1"]
    assert state["delivered"]["vid1"]["doc"] == "https://example.invalid/doc"
