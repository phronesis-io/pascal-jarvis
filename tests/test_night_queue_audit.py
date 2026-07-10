"""F4 + F13 (7/8 audit): night-queue drop accounting + heartbeat-path
write-claim shadow audit.

F4 — the flush silently destroyed content: overflow beyond NIGHT_QUEUE_MAX
and digest-length-capped entries were expired with only an 80-char preview
(31 of 57 queued entries lost 7/6-7/8, incl. an action-tagged security
advisory). Now: length-capped entries re-queue for the next flush (bounded
by the retry budget), terminal drops keep near-full text in the audit and
are disclosed in the digest + one aggregated dead-letter row.

F13 — REQ-88's shadow auditor only hooked bot.sh replies while most real
"已记录/已写入" claims ride heartbeat sends; core.heartbeat_loop now records
them via tasks.write_claim_audit.audit_message (channel="heartbeat").

Red-team follow-ups (7/9):
- F4: because deferred entries keep the queue file alive after a SUCCESSFUL
  flush, the user-activity breakpoint re-fired _flush_night_queue on every
  10s tick (multi-digest burst) and each burst bumped the crowded-out tail's
  retries to expiry in under a minute. The breakpoint path now has a min-gap
  floor, and a length-cap deferral inside a burst no longer burns retries.
- F13/REQ-88: when extract_readable_from_output yields nothing, the shadow
  audit SKIPS instead of scanning raw envelope/card JSON no human ever saw.

All state goes through tmp_path and sends are stubbed — nothing here may
touch live queue/audit/log files.
"""

import json
import time

import pytest

import core.heartbeat_loop as hbl
from core.delivery_deadletter import DEADLETTER_FILE
from tasks.write_claim_audit import audit_message


@pytest.fixture(autouse=True)
def _isolate_claim_surfaces(monkeypatch, tmp_path):
    """Keep the write-claim surface scan away from the real ~/.claude and
    memory dirs (read-only, but tests must not depend on live state)."""
    monkeypatch.setenv("JV_CLAUDE_PROJECTS", str(tmp_path / "claude-projects"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))


def _audit_rows(tmp_path):
    p = tmp_path / hbl.QUIET_FLUSH_AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def _deadletter_rows(tmp_path):
    p = tmp_path / DEADLETTER_FILE
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def _claim_rows(jarvis_dir):
    p = jarvis_dir / "data" / "write_claim_audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines()]


def _plant(queue, texts, source="content-recommend", retries=0):
    ts = hbl.now_local_str("%Y-%m-%d %H:%M")
    epoch = int(time.time())
    with open(queue, "a") as f:
        for t in texts:
            e = {"ts": ts, "epoch": epoch, "text": t, "source": source}
            if retries:
                e["retries"] = retries
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── F4: overflow drops — full text, one dead-letter, digest disclosure ─


def test_overflow_drops_keep_full_text_and_deadletter_once(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "NIGHT_QUEUE_MAX", 2)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    long_a = "工信部安全通报：" + "细节" * 60   # >80 chars — the class 7/8 lost
    long_b = "体育适配器卡点等你：" + "补充" * 60
    _plant(queue, [long_a, long_b, "短消息一", "短消息二"])
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED

    dropped = [r for r in _audit_rows(tmp_path)
               if r.get("detail") == "queue_overflow"]
    # full text survives in the audit — the 80-char preview made the 7/7
    # overflow (13 entries) permanently unrecoverable
    assert [r["text"] for r in dropped] == [long_a, long_b]
    assert all(len(r["text"]) > 80 for r in dropped)
    # ONE aggregated dead-letter row for the whole overflow, not one per entry
    dl = _deadletter_rows(tmp_path)
    assert len(dl) == 1
    assert dl[0]["kind"] == "night_queue_expired"
    assert "2 条" in dl[0]["detail"]
    # the digest tells the user drops happened instead of hiding them
    assert "另有 2 条" in sent[0]
    assert "短消息一" in sent[0] and "短消息二" in sent[0]


def test_age_expired_entry_disclosed_and_recoverable(tmp_path, monkeypatch):
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    old_text = "旧的重要结论：" + "内容" * 50
    old = {"ts": "2026-07-06 08:00", "epoch": int(time.time()) - 49 * 3600,
           "text": old_text, "source": "checkin"}
    queue.write_text(json.dumps(old, ensure_ascii=False) + "\n")
    _plant(queue, ["新消息"])
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert "另有 1 条" in sent[0]
    exp = [r for r in _audit_rows(tmp_path) if r["status"] == "expired"]
    assert exp and exp[0]["text"] == old_text


# ── F4: length-capped entries defer to the next flush, never vanish ───


def test_length_capped_entries_requeue_not_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "NIGHT_DIGEST_MAX_CHARS", 260)
    monkeypatch.setattr(hbl, "NIGHT_ENTRY_MAX_CHARS", 100)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    texts = ["甲" * 90, "乙" * 90, "丙" * 90]
    _plant(queue, texts)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED

    # header counts DELIVERED entries (7/8 digest said 15, delivered 9)
    assert "攒批的 1 条消息" in sent[0]
    assert "还有 2 条" in sent[0]          # deferral disclosed, not hidden
    # deferred entries are back in the queue, content intact, retries bumped
    kept = [json.loads(l) for l in queue.read_text().splitlines()]
    assert [e["text"] for e in kept] == texts[1:]
    assert all(e["retries"] == 1 for e in kept)
    by_status = {}
    for r in _audit_rows(tmp_path):
        by_status.setdefault(r["status"], []).append(r)
    assert len(by_status[hbl.FLUSH_DELIVERED]) == 1
    assert all(r["detail"] == "digest_length_cap"
               for r in by_status[hbl.FLUSH_RETRYABLE])
    assert "expired" not in by_status      # deferral is not destruction
    assert not (tmp_path / DEADLETTER_FILE).exists()  # deferrals never page
    # only the delivered entry gets an engagement "sent" row this flush
    elog = [json.loads(l) for l in
            (tmp_path / "engagement_log.jsonl").read_text().splitlines()]
    assert len([r for r in elog if r["type"] == "sent"]) == 1


def test_deferred_entries_drain_across_flushes(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "NIGHT_DIGEST_MAX_CHARS", 260)
    monkeypatch.setattr(hbl, "NIGHT_ENTRY_MAX_CHARS", 100)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    texts = ["甲" * 90, "乙" * 90, "丙" * 90]
    _plant(queue, texts)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    for _ in range(3):
        assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert not queue.exists()
    all_sent = "\n".join(sent)
    for t in texts:                        # every entry reached the user
        assert t in all_sent


def test_retry_exhausted_entry_keeps_full_text(tmp_path, monkeypatch):
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    text = "屡次放不下的长内容：" + "详情" * 1200   # > AUDIT_DROP_TEXT_CHARS
    _plant(queue, [text], retries=hbl.NIGHT_FLUSH_MAX_RETRIES)
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: True)
    assert not hbl._flush_night_queue(tmp_path, "ou_test")
    rows = _audit_rows(tmp_path)
    assert rows[0]["status"] == "expired"
    assert rows[0]["text"] == text[:hbl.AUDIT_DROP_TEXT_CHARS]
    assert not queue.exists()


def test_single_oversized_entry_still_ships(tmp_path, monkeypatch):
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    _plant(queue, ["长" * (hbl.NIGHT_DIGEST_MAX_CHARS + 500)])
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert "攒批的 1 条消息" in sent[0]
    assert not queue.exists()              # a lone big entry can't wedge FIFO


# ── F4 follow-up: breakpoint flush floor + burst deferrals never burn ─


def test_breakpoint_flush_floored_after_recent_flush(tmp_path, monkeypatch):
    """The activity breakpoint used to return True unconditionally — safe
    only while a successful flush always unlinked the queue. Deferred
    entries now keep the queue alive, so a floor must stop the 10s-tick
    multi-digest burst while the user is chatting."""
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text("{}\n")
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    t = time.time()
    # no flush recorded yet — first breakpoint delivery goes out
    assert hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=t) is True
    # a flush 100s ago (deferrals kept the queue) — next ticks stay silent
    hbl._stamp_flush(tmp_path, t - 100)
    assert hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=t) is False
    # floor passed — the breakpoint may fire again
    hbl._stamp_flush(tmp_path, t - hbl.BREAKPOINT_FLUSH_MIN_GAP_S - 1)
    assert hbl._should_flush(tmp_path, minutes_of_day=11 * 60, now=t) is True


def test_window_flush_unaffected_by_activity_floor(tmp_path, monkeypatch):
    """An active user with the floor unmet must not SUPPRESS a window flush
    — the window arithmetic stays exactly as it was."""
    (tmp_path / hbl.NIGHT_QUEUE_FILE).write_text("{}\n")
    monkeypatch.setattr(hbl, "_user_recently_active", lambda now=None: True)
    t = time.time()
    hbl._stamp_flush(tmp_path, t - 600)   # 10 min ago: breakpoint floor unmet
    # 10:05 — the 10:00 window opened after the last flush → still flushes
    assert hbl._should_flush(tmp_path, minutes_of_day=10 * 60 + 5, now=t) is True


def test_burst_flush_does_not_burn_deferred_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(hbl, "NIGHT_DIGEST_MAX_CHARS", 260)
    monkeypatch.setattr(hbl, "NIGHT_ENTRY_MAX_CHARS", 100)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    texts = ["甲" * 90, "乙" * 90, "丙" * 90]
    _plant(queue, texts)
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: True)
    hbl._stamp_flush(tmp_path, time.time() - 100)   # previous flush <15min ago
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    kept = [json.loads(l) for l in queue.read_text().splitlines()]
    assert [e["text"] for e in kept] == texts[1:]
    # crowded out by position inside a burst ≠ a failed delivery — no burn
    assert all(int(e.get("retries", 0) or 0) == 0 for e in kept)


def test_burst_drain_delivers_everything_without_expiry(tmp_path, monkeypatch):
    """Replay of the red-team scenario: a deep queue drained by back-to-back
    flushes. Every entry must reach the user; none may expire to the audit
    on a retry budget burned purely by positional crowd-out."""
    monkeypatch.setattr(hbl, "NIGHT_DIGEST_MAX_CHARS", 260)
    monkeypatch.setattr(hbl, "NIGHT_ENTRY_MAX_CHARS", 100)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    texts = [f"第{i}条：" + "内" * 90 for i in range(8)]
    _plant(queue, texts)
    sent = []
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: sent.append(t) or True)
    for _ in range(10):
        if not queue.exists():
            break
        assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    assert not queue.exists()
    all_sent = "\n".join(sent)
    for t in texts:
        assert f"第{texts.index(t)}条：" in all_sent
    assert not [r for r in _audit_rows(tmp_path) if r["status"] == "expired"]
    assert not (tmp_path / DEADLETTER_FILE).exists()


def test_standalone_flush_still_bumps_deferred_retries(tmp_path, monkeypatch):
    """Outside a burst the retry budget keeps working — a never-fitting
    entry must still expire into the full-text audit eventually."""
    monkeypatch.setattr(hbl, "NIGHT_DIGEST_MAX_CHARS", 260)
    monkeypatch.setattr(hbl, "NIGHT_ENTRY_MAX_CHARS", 100)
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    _plant(queue, ["甲" * 90, "乙" * 90])
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: True)
    hbl._stamp_flush(tmp_path, time.time() - 2 * 3600)   # last flush 2h ago
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    kept = [json.loads(l) for l in queue.read_text().splitlines()]
    assert len(kept) == 1 and kept[0]["retries"] == 1


# ── F13: heartbeat-path write-claim shadow audit ─────────────────────


def test_audit_message_records_channel_and_verdict(tmp_path):
    jd = tmp_path / "jarvis"
    jd.mkdir()
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "note.md").write_text("fresh")   # inside the mtime window
    audit_message("这条已写入记忆。", str(jd), channel="heartbeat")
    rows = _claim_rows(jd)
    assert len(rows) == 1
    assert rows[0]["channel"] == "heartbeat"
    assert rows[0]["verdict"] == "confirmed"
    assert "已写入记忆" in rows[0]["claim"]


def test_audit_message_default_channel_is_reply(tmp_path):
    jd = tmp_path / "jarvis"
    jd.mkdir()
    (tmp_path / "memory").mkdir()
    audit_message("已记录到 open_threads", str(jd))
    rows = _claim_rows(jd)
    assert rows and rows[0]["channel"] == "reply"   # bot.sh path unchanged


def test_audit_message_no_claim_writes_nothing_and_never_raises(tmp_path):
    jd = tmp_path / "jarvis"
    jd.mkdir()
    audit_message("明早提醒你开会。", str(jd), channel="heartbeat")
    assert not _claim_rows(jd)
    # never-raises contract on junk input
    audit_message(None, str(jd))
    audit_message(123, str(jd))
    audit_message("已写入记忆", "")
    assert not _claim_rows(jd)


def test_shadow_audit_claims_reads_card_text_not_json(tmp_path):
    card = json.dumps({"config": {}, "header": {"title": {"content": "记忆更新"}},
                       "elements": [{"text": {"content": "刚才那条已写入记忆。"}}]},
                      ensure_ascii=False)
    hbl._shadow_audit_claims("CARD:" + card, tmp_path)
    rows = _claim_rows(tmp_path)
    assert rows and rows[0]["channel"] == "heartbeat"
    assert "已写入记忆" in rows[0]["claim"]
    assert "{" not in rows[0]["claim"]      # claim text, not raw card JSON


def test_shadow_audit_skips_when_nothing_human_visible(tmp_path):
    """Red-team follow-up: when readable extraction yields nothing, the raw
    envelope/card JSON must NOT be scanned — claim phrasing inside JSON
    string fields no human saw would inflate the REQ-88 confirmed rate."""
    raw = json.dumps({"config": {}, "elements": [
        {"tag": "button", "value": {"note": "已写入记忆"}}]},
        ensure_ascii=False)
    hbl._shadow_audit_claims("CARD:" + raw, tmp_path)   # card, no text field
    hbl._shadow_audit_claims("", tmp_path)              # empty output
    hbl._shadow_audit_claims('{"status": "已写入记忆"}', tmp_path)  # bare JSON
    assert not _claim_rows(tmp_path)


def test_shadow_audit_claims_swallows_hook_failures(tmp_path, monkeypatch):
    import tasks.write_claim_audit as wca

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(wca, "audit_message", _boom)
    hbl._shadow_audit_claims("已写入记忆", tmp_path)   # must not raise


def test_flush_digest_flows_through_shadow_audit(tmp_path, monkeypatch):
    queue = tmp_path / hbl.NIGHT_QUEUE_FILE
    _plant(queue, ["刚才的结论已写入记忆。"], source="cross-session-sync")
    monkeypatch.setattr(hbl, "_lark_send_text", lambda t, u, **kw: True)
    assert hbl._flush_night_queue(tmp_path, "ou_test") == hbl.FLUSH_DELIVERED
    rows = _claim_rows(tmp_path)
    assert rows and rows[0]["channel"] == "heartbeat"
    assert "已写入记忆" in rows[0]["claim"]
