"""Regression tests for the P1 delivery-assurance + quiet-hours batch.

Covers docs/prd_interaction_quality.md REQ-11 (send retry, delivery ledger,
aggregate alert) and REQ-13 (night queue + morning digest flush).
"""

import json
import time

import core.heartbeat_loop as hbl
from core.card import build_card
from core.heartbeat_loop import (
    DELIVERY_ALERT_COOLDOWN,
    DELIVERY_ALERT_THRESHOLD,
    DELIVERY_STATE_FILE,
    NIGHT_QUEUE_FILE,
    _flush_night_queue,
    _in_quiet_hours,
    _is_urgent,
    _note_delivery,
    _queue_for_morning,
)


# ── REQ-13: quiet hours window ───────────────────────────────────────


def test_quiet_hours_boundaries():
    assert _in_quiet_hours(23 * 60 + 30)      # 23:30 — starts
    assert _in_quiet_hours(0)                 # midnight
    assert _in_quiet_hours(9 * 60 + 29)       # 09:29 — still quiet
    assert not _in_quiet_hours(9 * 60 + 30)   # 09:30 — opens (2026-07-18)
    assert not _in_quiet_hours(10 * 60)       # 10:00 — golden window
    assert not _in_quiet_hours(13 * 60)       # 13:00 — golden window
    assert not _in_quiet_hours(23 * 60 + 29)  # 23:29 — still open


def test_quiet_hour_runtime_override_is_shared_by_heartbeat_and_delivery(
        monkeypatch):
    from datetime import datetime

    from core.delivery import _next_awake_epoch, _quiet_now

    monkeypatch.setenv("JARVIS_QUIET_START", "12:15")
    monkeypatch.setenv("JARVIS_QUIET_END", "13:45")
    moment = datetime(2026, 7, 25, 12, 30)

    assert _in_quiet_hours(12 * 60 + 30)
    assert _quiet_now(moment)
    assert datetime.fromtimestamp(_next_awake_epoch(moment)).strftime(
        "%H:%M") == "13:45"


def test_invalid_quiet_hour_override_falls_back_for_every_surface(monkeypatch):
    from core.attention_policy import quiet_window_labels

    monkeypatch.setenv("JARVIS_QUIET_START", "99:99")
    monkeypatch.setenv("JARVIS_QUIET_END", "not-a-time")

    assert quiet_window_labels() == ("23:30", "09:30")
    assert _in_quiet_hours(9 * 60 + 29)
    assert not _in_quiet_hours(9 * 60 + 30)


def test_urgent_source_parsing():
    assert _is_urgent("intention-check")
    assert _is_urgent("eigenflux-feed-triage, calendar-sync")
    assert not _is_urgent("eigenflux-feed-triage,content-recommend")
    assert not _is_urgent("")


def test_night_queue_roundtrip(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    _queue_for_morning("深夜推荐内容 A", tmp_path)
    # sidecar consumed so the queued message isn't double-counted as sent
    assert not (tmp_path / ".heartbeat_last_source").exists()
    _queue_for_morning("深夜推荐内容 B", tmp_path)

    sent = []
    monkeypatch.setattr(
        hbl,
        "_lark_send_text",
        lambda text, uid, **kw: sent.append((text, kw)) or True,
    )
    assert _flush_night_queue(tmp_path, "ou_test")

    assert len(sent) == 1  # ONE digest, not N messages
    assert "深夜推荐内容 A" in sent[0][0] and "深夜推荐内容 B" in sent[0][0]
    assert "2" in sent[0][0]  # count in header
    assert sent[0][1]["idempotency_key"].startswith("night:")
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()  # cleared after flush


def _memorial_card(mid: str = "mem_test") -> str:
    return build_card(
        "📜 测试奏折", "一张卡说清一件事",
        buttons=[{"text": "已阅", "value": {
            "action": "memorial", "id": mid, "opt": "read"}},
                 {"text": "💬 聊聊这个", "value": {
                     "action": "memorial", "id": mid, "opt": "chat"}}],
    )


def test_memorial_queues_and_flushes_as_intact_card(tmp_path, monkeypatch):
    card = _memorial_card()
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")
    _queue_for_morning("CARD:" + card, tmp_path)

    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()
    queued = [json.loads(line) for line in
              (tmp_path / hbl.MEMORIAL_QUEUE_FILE).read_text().splitlines()]
    assert queued[0]["card_json"] == card
    assert queued[0]["memorial_id"] == "mem_test"

    sent_cards = []
    sent_texts = []
    monkeypatch.setattr(
        hbl, "_lark_send_card",
        lambda payload, uid, log_file, **kw:
        sent_cards.append((payload, kw)) or True)
    monkeypatch.setattr(
        hbl, "_lark_send_text",
        lambda payload, uid, **kw: sent_texts.append(payload) or True)

    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert sent_cards[0][0] == card
    assert sent_cards[0][1]["idempotency_key"] == "memorial:mem_test"
    assert sent_texts == []
    assert not (tmp_path / hbl.MEMORIAL_QUEUE_FILE).exists()
    # The outbox keeps readable text, while the sent payload kept its buttons.
    assert "一张卡说清一件事" in (tmp_path / "heartbeat_outbox.jsonl").read_text()
    assert "memorial-card-queue" in (tmp_path / "engagement_log.jsonl").read_text()


def test_memorial_flush_only_consumes_ids_from_its_own_send(
        tmp_path, monkeypatch):
    card = _memorial_card("mem_scoped")
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")
    _queue_for_morning("CARD:" + card, tmp_path)
    hbl._LAST_SENT_IDS[:] = ["om_previous"]

    def send_card(*_args, **_kwargs):
        hbl._LAST_SENT_IDS.append("om_memorial")
        return True

    monkeypatch.setattr(hbl, "_lark_send_card", send_card)
    monkeypatch.setattr(hbl, "_record_sent_lark_id", lambda *_args: None)

    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert hbl._LAST_SENT_IDS == ["om_previous"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "engagement_log.jsonl").read_text().splitlines()
    ]
    assert rows[0]["message_ids"] == ["om_memorial"]
    hbl._LAST_SENT_IDS.clear()


def test_memorial_flush_selects_at_most_one_card_per_source(
        tmp_path, monkeypatch):
    for source, mid in (
        ("eigenflux-feed-triage", "mem_feed_a"),
        ("eigenflux-feed-triage", "mem_feed_b"),
        ("routine-run", "mem_routine"),
    ):
        hbl._append_memorial_queue_entry(
            tmp_path, mid, _memorial_card(mid), source
        )
    sent = []
    monkeypatch.setattr(
        hbl, "_lark_send_card",
        lambda card, *_a, **_kw: sent.append(card) or True,
    )

    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert len(sent) == 2
    retained = [
        json.loads(line)
        for line in (tmp_path / hbl.MEMORIAL_QUEUE_FILE).read_text().splitlines()
    ]
    assert [row["memorial_id"] for row in retained] == ["mem_feed_b"]


def test_failed_memorial_flush_retains_exact_card(tmp_path, monkeypatch):
    card = _memorial_card("mem_retry")
    (tmp_path / ".heartbeat_last_source").write_text("mail-triage")
    _queue_for_morning(card, tmp_path)
    monkeypatch.setattr(hbl, "_lark_send_card", lambda *a, **kw: False)

    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_RETRYABLE
    kept = json.loads(
        (tmp_path / hbl.MEMORIAL_QUEUE_FILE).read_text().splitlines()[0])
    assert kept["card_json"] == card
    assert kept["retries"] == 1
    assert not (tmp_path / "heartbeat_outbox.jsonl").exists()


def test_memorial_card_timeout_can_be_treated_as_retryable(monkeypatch):
    def timeout(*args, **kwargs):
        raise hbl.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(hbl.subprocess, "run", timeout)
    assert not hbl._lark_send_card(
        _memorial_card(), "ou_test", "", assume_delivered_on_timeout=False)


def test_night_queue_kept_when_send_fails(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("heartbeat")
    _queue_for_morning("消息", tmp_path)
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid, **kw: False)
    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_RETRYABLE
    # retained for retry, now carrying a retry count (backlog #4)
    kept = [json.loads(l) for l in
            (tmp_path / NIGHT_QUEUE_FILE).read_text().splitlines()]
    assert len(kept) == 1 and kept[0]["retries"] == 1


def test_flush_empty_queue_is_noop(tmp_path):
    assert not _flush_night_queue(tmp_path, "ou_test")


# ── REQ-11: delivery ledger + aggregate alert ────────────────────────


def _fails(tmp_path):
    try:
        return json.loads((tmp_path / DELIVERY_STATE_FILE).read_text())
    except FileNotFoundError:
        return {}


def test_alert_fires_once_past_threshold(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid, **kw: alerts.append(text) or True)

    t0 = time.time()
    for i in range(DELIVERY_ALERT_THRESHOLD + 2):
        _note_delivery(tmp_path, ok=False, user_id="ou_test", now=t0 + i)

    # Threshold crossed once → exactly one alert (cooldown blocks repeats)
    assert len(alerts) == 1
    assert "送达" in alerts[0]
    assert _fails(tmp_path)["consec_fails"] == DELIVERY_ALERT_THRESHOLD + 2


def test_alert_repeats_after_cooldown(tmp_path, monkeypatch):
    alerts = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid, **kw: alerts.append(text) or True)

    t0 = time.time()
    for i in range(DELIVERY_ALERT_THRESHOLD):
        _note_delivery(tmp_path, ok=False, user_id="u", now=t0 + i)
    _note_delivery(tmp_path, ok=False, user_id="u", now=t0 + DELIVERY_ALERT_COOLDOWN + 60)
    assert len(alerts) == 2


def test_success_resets_counter(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_lark_send_text", lambda text, uid, **kw: True)
    _note_delivery(tmp_path, ok=False, user_id="u")
    _note_delivery(tmp_path, ok=False, user_id="u")
    _note_delivery(tmp_path, ok=True, user_id="u")
    assert _fails(tmp_path)["consec_fails"] == 0


def test_send_text_retries_then_succeeds(monkeypatch):
    attempts = []

    class _R:
        stdout = ""

        def __init__(self, rc):
            self.returncode = rc

    def fake_run(cmd, **kw):
        attempts.append(cmd)
        return _R(1 if len(attempts) < 3 else 0)

    monkeypatch.setattr(hbl.subprocess, "run", fake_run)
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)

    assert hbl._lark_send_text("hello", "ou_test")
    assert len(attempts) == 3  # failed twice, succeeded on final retry


def test_send_text_gives_up_after_retries(monkeypatch):
    attempts = []

    class _R:
        returncode = 1

    monkeypatch.setattr(hbl.subprocess, "run", lambda cmd, **kw: attempts.append(cmd) or _R())
    monkeypatch.setattr(hbl.time, "sleep", lambda s: None)

    assert not hbl._lark_send_text("hello", "ou_test")
    assert len(attempts) == 1 + len(hbl.SEND_RETRY_DELAYS)


def test_flush_dedups_and_records_engagement(tmp_path, monkeypatch):
    for text in ["重复内容", "重复内容", "另一条"]:
        (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
        _queue_for_morning(text, tmp_path)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")

    assert sent[0].count("重复内容") == 1  # duplicate collapsed
    assert "2 条消息" in sent[0]
    # queued sources are visible to engagement-analyze after the flush
    elog = (tmp_path / "engagement_log.jsonl").read_text()
    entries = [json.loads(l) for l in elog.splitlines()]
    assert any(e["type"] == "sent" and e["source"] == "content-recommend"
               and e.get("via") == "night-digest" for e in entries)


def test_text_flush_only_consumes_ids_from_its_own_send(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    _queue_for_morning("自己的攒批消息", tmp_path)
    hbl._LAST_SENT_IDS[:] = ["om_previous"]

    def send_text(*_args, **_kwargs):
        hbl._LAST_SENT_IDS.append("om_digest")
        return True

    monkeypatch.setattr(hbl, "_lark_send_text", send_text)

    assert _flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert hbl._LAST_SENT_IDS == ["om_previous"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "engagement_log.jsonl").read_text().splitlines()
    ]
    assert rows[0]["message_ids"] == ["om_digest"]
    hbl._LAST_SENT_IDS.clear()


def test_record_engagement_includes_prompt_variant_sidecar(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("checkin")
    (tmp_path / hbl.PROMPT_VARIANTS_FILE).write_text(json.dumps({
        "checkin": {
            "prompt_experiment": "checkin-choice-v1",
            "prompt_variant": "choice_first",
        }
    }))

    hbl._record_engagement(tmp_path)

    row = json.loads((tmp_path / "engagement_log.jsonl").read_text().splitlines()[0])
    assert row["source"] == "checkin"
    assert row["prompt_experiment"] == "checkin-choice-v1"
    assert row["prompt_variant"] == "choice_first"
    assert not (tmp_path / hbl.PROMPT_VARIANTS_FILE).exists()


def test_night_digest_preserves_prompt_variant_metadata(tmp_path, monkeypatch):
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    (tmp_path / hbl.PROMPT_VARIANTS_FILE).write_text(json.dumps({
        "content-recommend": {
            "prompt_experiment": "recommend-tone-v1",
            "prompt_variant": "compact",
        }
    }))
    _queue_for_morning("推荐内容", tmp_path)

    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")

    row = json.loads((tmp_path / "engagement_log.jsonl").read_text().splitlines()[0])
    assert row["source"] == "content-recommend"
    assert row["via"] == "night-digest"
    assert row["prompt_experiment"] == "recommend-tone-v1"
    assert row["prompt_variant"] == "compact"


# ── Daytime batching + breakpoint release (R3 research round) ────────


def test_should_queue_general_interest_in_daytime(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("eigenflux-feed-triage")
    assert hbl._should_queue(tmp_path, minutes_of_day=14 * 60)  # 14:00 daytime


def test_should_not_queue_task_relevant_in_daytime(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("phronesis-monitor")
    assert not hbl._should_queue(tmp_path, minutes_of_day=14 * 60)
    # mixed batch containing task-relevant content sends immediately
    (tmp_path / ".heartbeat_last_source").write_text("eigenflux-feed-triage, phronesis-monitor")
    assert not hbl._should_queue(tmp_path, minutes_of_day=14 * 60)


def test_should_queue_everything_nonurgent_at_night(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("phronesis-monitor")
    assert hbl._should_queue(tmp_path, minutes_of_day=2 * 60)  # 02:00
    (tmp_path / ".heartbeat_last_source").write_text("intention-check")
    assert not hbl._should_queue(tmp_path, minutes_of_day=2 * 60)  # urgent bypasses


def test_should_flush_at_window_and_not_before(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: False)
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text('{"text":"x"}\n')
    now = time.time()
    # last flush long ago; 13:29 is before the 13:30 window (and past 10:00,
    # but a 10:00 flush already happened — stamp it 30min ago)
    hbl._stamp_flush(tmp_path, now=now - 1800)
    assert not hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 29, now=now)
    # 13:31 — the 13:30 window opened after the last flush
    assert hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 31, now=now)
    # but not twice: flush stamped just now → no re-flush at 13:35
    hbl._stamp_flush(tmp_path, now=now)
    assert not hbl._should_flush(tmp_path, minutes_of_day=13 * 60 + 35, now=now + 240)


def test_breakpoint_release_flushes_outside_quiet_hours(tmp_path, monkeypatch):
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text('{"text":"x"}\n')
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    assert hbl._should_flush(tmp_path, minutes_of_day=15 * 60)
    # never during quiet hours, even if the user is active
    assert not hbl._should_flush(tmp_path, minutes_of_day=2 * 60)


def test_no_flush_with_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    assert not hbl._should_flush(tmp_path, minutes_of_day=15 * 60)


def test_extract_message_id_shapes():
    assert hbl._extract_message_id('{"message_id":"om_abc"}') == "om_abc"
    assert hbl._extract_message_id('{"data":{"message_id":"om_xyz"}}') == "om_xyz"
    assert hbl._extract_message_id("not json") == ""
    assert hbl._extract_message_id("") == ""
    assert hbl._extract_message_id('{"message_id":123}') == ""


# ── Permanently silent housekeeping tasks (SILENT_SOURCES) ───────────
# behavioral_rules.md: daily-plan / self-diagnostic / thinking-review never
# surface — not directly, not via the batch queue. Regression for 2026-06-12:
# a daily-plan card (built on truncated calendar data) was queued at 08:12
# and pushed to the user inside the 10:00 digest.


def test_silent_source_never_enters_batch_queue(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("daily-plan")
    _queue_for_morning("🌅 今日 plan card", tmp_path)
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()  # dropped, not queued
    assert not (tmp_path / ".heartbeat_last_source").exists()  # sidecar consumed


def test_all_silent_tasks_are_dropped_at_queue(tmp_path):
    for name in sorted(hbl.SILENT_SOURCES):
        (tmp_path / ".heartbeat_last_source").write_text(name)
        _queue_for_morning(f"output of {name}", tmp_path)
    assert not (tmp_path / NIGHT_QUEUE_FILE).exists()


def test_normal_source_still_queues(tmp_path):
    (tmp_path / ".heartbeat_last_source").write_text("content-recommend")
    _queue_for_morning("正常推荐内容", tmp_path)
    lines = (tmp_path / NIGHT_QUEUE_FILE).read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["source"] == "content-recommend"


def test_flush_scrubs_legacy_silent_entries(tmp_path, monkeypatch):
    # Entries queued BEFORE the silence gate existed must not reach the digest
    # (fresh ts so the backlog-#4 age expiry doesn't kick in first)
    ts = hbl.now_local_str("%Y-%m-%d %H:%M")
    queue = tmp_path / NIGHT_QUEUE_FILE
    queue.write_text(
        json.dumps({"ts": ts, "text": "🌅 今日 plan card",
                    "source": "daily-plan"}, ensure_ascii=False) + "\n"
        + json.dumps({"ts": ts, "text": "深夜推荐",
                      "source": "content-recommend"}, ensure_ascii=False) + "\n")
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")

    assert len(sent) == 1
    assert "plan card" not in sent[0]
    assert "深夜推荐" in sent[0]
    assert "1 条消息" in sent[0]  # silent entry not counted either
    # engagement rows must not credit the silent source with a send
    entries = [json.loads(l) for l in
               (tmp_path / "engagement_log.jsonl").read_text().splitlines()]
    assert all(e["source"] != "daily-plan" for e in entries)


def test_flush_with_only_silent_entries_sends_nothing(tmp_path, monkeypatch):
    queue = tmp_path / NIGHT_QUEUE_FILE
    queue.write_text(json.dumps(
        {"ts": "2026-06-12 08:12", "text": "plan", "source": "daily-plan"}) + "\n")
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert not _flush_night_queue(tmp_path, "ou_test")
    assert sent == []
    assert not queue.exists()  # scrubbed queue is cleared, not retried forever


# ── Flush-time fair truncation (6/12 feed-triage card cut at 600) ────


def test_single_long_entry_not_truncated_at_600(tmp_path, monkeypatch):
    # One queued message with three bullets (~1500 chars) used to be cut to
    # 600 at queue time even though the whole 3800 digest budget was free.
    text = "\n\n".join(f"• 条目{i} " + "内容" * 200 for i in range(3))  # ~1300 chars
    (tmp_path / ".heartbeat_last_source").write_text("eigenflux-feed-triage")
    _queue_for_morning(text, tmp_path)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")
    assert "截断" not in sent[0]
    assert "条目2" in sent[0]  # last bullet survives


def test_many_entries_share_budget_with_floor(tmp_path, monkeypatch):
    # Six long entries: each gets at least the 600-char floor, digest stays
    # within the Lark budget, truncation lands on a newline boundary.
    for i in range(6):
        (tmp_path / ".heartbeat_last_source").write_text("checkin")
        _queue_for_morning(f"头{i}\n" + "\n".join("行" * 50 for _ in range(30)), tmp_path)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert _flush_night_queue(tmp_path, "ou_test")
    assert len(sent[0]) <= hbl.NIGHT_DIGEST_MAX_CHARS + 200  # header slack
    assert "截断" in sent[0]


def test_truncate_entry_prefers_newline_boundary():
    text = "第一行内容\n[原文](https://example.com/very-long-url-path)"
    cut = hbl._truncate_entry(text, len(text) - 5)
    # never leaves a dangling half link — cut back to the newline
    assert "https" not in cut
    assert cut.endswith("…(截断)")


def test_truncate_entry_noop_when_short():
    assert hbl._truncate_entry("短", 600) == "短"
