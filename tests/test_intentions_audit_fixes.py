"""Tests for the F2 audit fixes in tasks/intentions_post.py (2026-07-08).

F2: report intents (小时报/日报) were marked executed on contentless envelopes
— a dead Claude call / spend-limit fallback husk closed the occurrence with
zero delivered product and health stayed green for ~23h. Coverage here:

  1. contentless guard — executed is blocked on empty/husk responses
     (incl. the '```json' fence husk) and the row is left for reconcile;
  2. re-fire semantics — the uncovered row goes back to 'pending' through
     the REAL reconcile_inflight against a tmp SQLite DB;
  3. deterministic product write — the opted-in intent's content is appended
     to its declared target log by the post script itself, and ONLY for it.

All state (DB, inflight manifest, breach queue, ledger, product log) lives in
tmp_path; sched_events emission is stubbed — tests never touch live files.
"""

import importlib.util
import json
import sqlite3
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "intentions_post_audit", ROOT / "tasks" / "intentions_post.py"
)
ip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ip)


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


class _FakeTime:
    """Deterministic clock for the product-log stamp only — every other
    format string falls through to the real strftime (ledger timestamps)."""
    STAMP = "2026-07-08 15:00"

    @staticmethod
    def strftime(fmt, *a):
        if fmt == "%Y-%m-%d %H:%M":
            return _FakeTime.STAMP
        return time.strftime(fmt, *a)


def _record_marks(monkeypatch):
    """Stub the DB-touching mark functions; return the call recorders."""
    executed, failed = [], []
    monkeypatch.setattr(ip, "mark_executed",
                        lambda iid, result="": executed.append(iid))
    monkeypatch.setattr(ip, "mark_failed",
                        lambda iid, error="": failed.append(iid))
    return executed, failed


# ── 1. contentless guard (unit) ────────────────────────────────────────────

@pytest.mark.parametrize("husk", [
    "", "   \n\t ", "```json", "```json\n```", "``````", "```",
    "HEARTBEAT_OK", "```\nHEARTBEAT_OK\n```", None,
])
def test_executed_blocked_on_empty_and_husk(monkeypatch, husk):
    executed, failed = _record_marks(monkeypatch)
    msgs = []
    resolved = ip._apply_action("int_x", response=husk, action="notify",
                                user_messages=msgs)
    assert resolved is False
    assert executed == [] and failed == []
    assert msgs == []


@pytest.mark.parametrize("action", ["notify", "silent", "chain"])
def test_guard_applies_to_every_action(monkeypatch, action):
    executed, _ = _record_marks(monkeypatch)
    resolved = ip._apply_action("int_x", response="```json", action=action,
                                user_messages=[])
    assert resolved is False
    assert executed == []


def test_status_token_still_executes(monkeypatch):
    """The guard is deliberately narrower than _is_contentless: internal
    intents reporting a bare 'sent' must still count as executed — otherwise
    every quiet occurrence of 27 unrelated intents becomes a retry storm."""
    executed, _ = _record_marks(monkeypatch)
    msgs = []
    resolved = ip._apply_action("int_x", response="sent", action="silent",
                                user_messages=msgs)
    assert resolved is True
    assert executed == ["int_x"]
    assert msgs == []          # still never carded (pre-existing behavior)


def test_real_content_still_executes_and_cards(monkeypatch):
    executed, _ = _record_marks(monkeypatch)
    msgs = []
    resolved = ip._apply_action("int_x", response="14:00 开会，记得带电脑",
                                action="notify", user_messages=msgs)
    assert resolved is True
    assert executed == ["int_x"]
    assert msgs == ["14:00 开会，记得带电脑"]


def test_closure_row_exempt_from_guard(monkeypatch):
    """A follow-up recording onto its parent may carry an empty response —
    the record_closure write IS the product; it must not be retried."""
    executed, _ = _record_marks(monkeypatch)
    calls = []
    monkeypatch.setattr(ip, "record_closure",
                        lambda parent, outcome="done", result="", via="cli":
                        calls.append((parent, outcome)))
    resolved = ip._apply_action(
        "int_fu", response="", action="silent", user_messages=[],
        closure={"parent": "int_parent", "outcome": "done", "result": "搞定"})
    assert resolved is True
    assert executed == ["int_fu"]
    assert calls == [("int_parent", "done")]


def test_failed_action_still_resolves(monkeypatch):
    executed, failed = _record_marks(monkeypatch)
    resolved = ip._apply_action("int_x", response="boom", action="failed",
                                user_messages=[])
    assert resolved is True
    assert failed == ["int_x"] and executed == []


# ── 3. deterministic product write (unit) ──────────────────────────────────

def _opt_in(monkeypatch, tmp_path, intent_id="int_hb", name="小时报"):
    target = tmp_path / "timeline" / "hourly_log.md"
    monkeypatch.setattr(ip, "PRODUCT_LOGS", {intent_id: target})
    monkeypatch.setattr(ip, "get_intent", lambda iid: {"name": name})
    monkeypatch.setattr(ip, "time", _FakeTime)
    return target


def test_product_append_for_opted_in_intent(monkeypatch, tmp_path):
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    msgs = []
    resolved = ip._apply_action("int_hb", response="15:00 帮 Pascal 修了看板",
                                action="silent", user_messages=msgs)
    assert resolved is True
    assert executed == ["int_hb"]
    text = target.read_text(encoding="utf-8")
    assert "### 2026-07-08 15:00 小时报" in text
    assert "帮 Pascal 修了看板" in text
    assert msgs == []          # silent stays silent toward the user


def test_no_append_for_non_opted_intent(monkeypatch, tmp_path):
    """ONLY the opted-in intent gets the deterministic write — 27+ unrelated
    intents flow through _apply_action and must never be blanket-appended."""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)   # opt-in maps int_hb only
    resolved = ip._apply_action("int_other", response="给凌安回了消息",
                                action="silent", user_messages=[])
    assert resolved is True
    assert executed == ["int_other"]
    assert not target.exists()
    assert list(tmp_path.rglob("*.md")) == []


def test_product_append_strips_fences(monkeypatch, tmp_path):
    _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    ip._apply_action("int_hb", response="```\n15:00 做了X\n```",
                     action="silent", user_messages=[])
    text = target.read_text(encoding="utf-8")
    assert "15:00 做了X" in text
    assert "```" not in text


@pytest.mark.parametrize("in_call_entry", [
    # F-16: ALL heading shapes the model actually writes in-call must dedup —
    # the old rule ('### YYYY-MM-DD HH' prefix + name in the SAME line) only
    # matched the first shape, so healthy hours got double entries.
    "\n### 2026-07-08 15:02 [小时报]\n模型自己写的\n",       # date-time + name
    "\n### 2026-07-08 15:02 小时报\n模型自己写的\n",         # unbracketed name
    "\n### 小时报 15:09 (2026-07-08)\n模型自己写的\n",       # name-first
    "\n### 2026-07-08 (小时报 15:29)\n模型自己写的\n",       # name in parens
    "\n### 2026-07-08 15:02\n[小时报] 模型自己写的\n",       # label on next line
])
def test_product_dedup_matches_real_in_call_shapes(monkeypatch, tmp_path,
                                                   in_call_entry):
    """A Claude run that DID write in-call already left this hour's entry —
    no double entry, but the occurrence still counts as executed."""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(in_call_entry, encoding="utf-8")
    resolved = ip._apply_action("int_hb", response="15:00 重复内容",
                                action="silent", user_messages=[])
    assert resolved is True
    assert executed == ["int_hb"]
    text = target.read_text(encoding="utf-8")
    assert text.count("### ") == 1
    assert "重复内容" not in text


def test_product_dedup_by_content_identity(monkeypatch, tmp_path):
    """The report text itself already on disk (first 40 chars) dedups even
    under a heading shape none of the parsers recognize — the near-identical
    double-entry case F-16 observed in the live archive."""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    report = "15:00 帮 Pascal 盯着部署，修复了两个看板异常，其余时间后台安静"
    target.write_text(f"\n### 某个完全不规则的标题\n{report}\n",
                      encoding="utf-8")
    resolved = ip._apply_action("int_hb", response=report,
                                action="silent", user_messages=[])
    assert resolved is True
    assert executed == ["int_hb"]
    assert target.read_text(encoding="utf-8").count("盯着部署") == 1


def test_other_entry_same_hour_does_not_dedup(monkeypatch, tmp_path):
    """An unrelated writer's entry for the same hour (e.g. the memory task's
    HOURLY INDEX) must not swallow the 小时报 append — dedup needs the intent
    name (or the report text), never the hour alone."""
    _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("\n### 2026-07-08 15:01 [HOURLY INDEX 14:15~15:00]\n索引\n",
                      encoding="utf-8")
    ip._apply_action("int_hb", response="15:00 真实小时报",
                     action="silent", user_messages=[])
    text = target.read_text(encoding="utf-8")
    assert "真实小时报" in text


def test_bare_hourly_index_mentioning_name_does_not_dedup(monkeypatch, tmp_path):
    """The nastiest real shape (today's live hourly_log.md): memory task's
    BARE '### YYYY-MM-DD HH:MM' heading whose body merely MENTIONS 小时报
    deeper down. Hour-only dedup would swallow the real report behind it;
    the name must lead the body's FIRST line to count as ours."""
    _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        "\n### 2026-07-08 15:01\n"
        "[HOURLY INDEX — 2026-07-08 14:15~15:00]\n"
        "- 跨-session 播报误弹，已核实\n"
        "> ⚠️ 小时报中断补记：昨夜没有小时报\n",
        encoding="utf-8")
    ip._apply_action("int_hb", response="15:00 真实小时报",
                     action="silent", user_messages=[])
    text = target.read_text(encoding="utf-8")
    assert "真实小时报" in text


def test_same_name_earlier_hour_does_not_dedup(monkeypatch, tmp_path):
    """Last hour's 小时报 entry never blocks this hour's append."""
    _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("\n### 2026-07-08 14:05 小时报\n上一个小时的\n",
                      encoding="utf-8")
    ip._apply_action("int_hb", response="15:00 真实小时报",
                     action="silent", user_messages=[])
    assert "真实小时报" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("no_product", ["", "```json", "``", "已写入", "sent",
                                        "done"])
def test_opted_in_blocks_on_no_appendable_product(monkeypatch, tmp_path, no_product):
    """For the file-product intent even a bare status token is no product —
    a claim of having written is not a write; gate executed on the file.
    ('``' pins the _is_empty_product half of the F-11 combined gate: a
    backtick husk _is_contentless alone would let through.)"""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    resolved = ip._apply_action("int_hb", response=no_product,
                                action="silent", user_messages=[])
    assert resolved is False
    assert executed == []
    assert not target.exists()


@pytest.mark.parametrize("status", ["已写入", "sent", "done"])
def test_opted_in_status_accepts_verified_in_call_product(
        monkeypatch, tmp_path, status):
    """A status word is not proof by itself, but an existing current-hour
    product on disk is deterministic evidence and must not trigger another
    paid model attempt."""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True)
    original = "\n### 2026-07-08 15:02 小时报\n模型已经写好的小时报\n"
    target.write_text(original, encoding="utf-8")

    resolved = ip._apply_action(
        "int_hb", response=status, action="silent", user_messages=[])

    assert resolved is True
    assert executed == ["int_hb"]
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("sentinel", [
    "HEARTBEAT_OK", "```\nHEARTBEAT_OK\n```", "`HEARTBEAT_OK`",
])
@pytest.mark.parametrize("action", ["silent", "notify"])
def test_opted_in_heartbeat_ok_is_quiet_hour_done(monkeypatch, tmp_path,
                                                  sentinel, action):
    """Design choice for F-11's quiet-hour semantics: HEARTBEAT_OK in the
    file-product intent's slot is its DOCUMENTED sentinel (the 小时报 prompt:
    「…否则静默累积、HEARTBEAT_OK」) — a model alive enough to build a valid
    envelope is reporting 'nothing this hour'. The occurrence is legitimately
    done-with-NO-product: executed, but

      - NOTHING is appended (the 7/7-7/8 leak wrote a bare 'HEARTBEAT_OK'
        line into the prompt-loaded timeline AND that junk line dedup-blocked
        the hour's real report forever), and
      - nothing ever reaches a card, even on action=notify.

    A husk from a dead call/fallback carrying the same in-slot shape is
    indistinguishable and closes that one hour with no product — a bounded
    loss deliberately preferred over re-firing every genuinely quiet hour
    for 6h at intention-check's 1-minute cadence (F-14's burn)."""
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    msgs = []
    resolved = ip._apply_action("int_hb", response=sentinel, action=action,
                                user_messages=msgs)
    assert resolved is True             # occurrence closes — no endless re-fire
    assert executed == ["int_hb"]
    assert not target.exists()          # no junk entry, nothing to dedup-block
    assert msgs == []                   # protocol token never carded


def test_file_product_is_internal_even_when_model_requests_notify(
    monkeypatch, tmp_path,
):
    executed, _ = _record_marks(monkeypatch)
    target = _opt_in(monkeypatch, tmp_path)
    msgs = []

    resolved = ip._apply_action(
        "int_hb",
        response="18:00 记录备用通道恢复和当前运行状态",
        action="notify",
        user_messages=msgs,
    )

    assert resolved is True
    assert executed == ["int_hb"]
    assert "备用通道恢复" in target.read_text(encoding="utf-8")
    assert msgs == []


def test_non_product_heartbeat_ok_still_blocks(monkeypatch):
    """The sentinel semantics are scoped to the file-product intent ONLY —
    for every other intent a HEARTBEAT_OK slot stays a husk (F2 behavior:
    not executed, left for reconcile retry)."""
    executed, _ = _record_marks(monkeypatch)
    resolved = ip._apply_action("int_x", response="HEARTBEAT_OK",
                                action="silent", user_messages=[])
    assert resolved is False
    assert executed == []


def test_opted_in_blocks_on_write_failure(monkeypatch, tmp_path, capsys):
    executed, _ = _record_marks(monkeypatch)
    _opt_in(monkeypatch, tmp_path)
    # Make the append fail: the target's parent path is occupied by a file.
    (tmp_path / "timeline").write_text("not a dir")
    resolved = ip._apply_action("int_hb", response="15:00 真内容",
                                action="silent", user_messages=[])
    assert resolved is False
    assert executed == []
    assert "product log append failed" in capsys.readouterr().err


def test_canonical_target_is_hourly_log():
    """Pin the opt-in roster: exactly the 小时报, targeting the fresh-write
    buffer that memory_daily_post rotates into hourly_archive.md."""
    assert set(ip.PRODUCT_LOGS) == {"int_6362ae1606"}
    assert str(ip.PRODUCT_LOGS["int_6362ae1606"]).endswith(
        "timeline/hourly_log.md")


# ── 2. re-fire semantics (E2E through a real tmp SQLite) ──────────────────

def _wire_e2e(tmp_path, monkeypatch):
    import core.intentions as mod

    db_path = tmp_path / "data" / "jarvis.db"
    db_path.parent.mkdir(parents=True)

    def _test_get_db():
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    monkeypatch.setattr(mod, "_get_db", _test_get_db)
    monkeypatch.setattr(mod, "_table_ready", False)
    mod._init()

    monkeypatch.setattr(mod, "INFLIGHT_FILE",
                        tmp_path / "data" / ".intention_inflight.json")
    monkeypatch.setattr(mod, "BREACH_QUEUE",
                        tmp_path / "data" / ".intent_breach_queue.jsonl")
    monkeypatch.setattr(mod, "_emit_intent", lambda *a, **k: None)
    monkeypatch.setattr(ip, "CARD_LEDGER",
                        tmp_path / "data" / ".intent_card_ledger.jsonl")
    return mod


def test_contentless_envelope_refires_e2e(tmp_path, monkeypatch, capsys):
    """The 7/8 日报 shape: the envelope covers the intent but with a husk.
    The row must go back to 'pending' (re-fires next cycle) instead of being
    fake-executed, and nothing reaches the user."""
    mod = _wire_e2e(tmp_path, monkeypatch)
    from core.intentions import create_intent, mark_triggered, get_intent

    iid = create_intent(
        name="每日日报",
        trigger_type="cron",
        trigger_config={"expression": "0 9 * * *"},
        prompt="写并送达每日日报",
        intent_id="int_e2e_daily",
    )
    mark_triggered(iid)
    mod.write_inflight([iid])

    envelope = json.dumps(
        {"intents": {iid: {"response": "```json", "action": "silent"}}})
    monkeypatch.setattr("sys.stdin", _Stdin(envelope))
    ip.main()

    row = get_intent(iid)
    assert row["status"] == "pending"          # re-fires: next_fire_at unmoved
    assert row["executed_at"] is None          # NOT fake-executed
    assert "retry" in (row.get("last_error") or "").lower()
    assert not mod.INFLIGHT_FILE.exists()      # manifest reconciled + cleared
    assert capsys.readouterr().out == ""       # no husk card to the user


def test_product_write_e2e_opted_in_only(tmp_path, monkeypatch, capsys):
    """Full path: one opted-in 小时报 + one unrelated silent intent in the
    same envelope. The 小时报 content lands in the target log via the post
    script itself; the other intent executes without any file write."""
    mod = _wire_e2e(tmp_path, monkeypatch)
    from core.intentions import create_intent, mark_triggered, get_intent

    hb = create_intent(
        name="小时报",
        trigger_type="cron",
        trigger_config={"expression": "0 9-23 * * *"},
        prompt="写一条小时报",
        intent_id="int_e2e_hb",
    )
    other = create_intent(
        name="unrelated silent",
        trigger_type="cron",
        trigger_config={"expression": "0 * * * *"},
        prompt="internal chore",
        intent_id="int_e2e_other",
    )
    for iid in (hb, other):
        mark_triggered(iid)
    mod.write_inflight([hb, other])

    target = tmp_path / "memory" / "timeline" / "hourly_log.md"
    monkeypatch.setattr(ip, "PRODUCT_LOGS", {hb: target})
    monkeypatch.setattr(ip, "time", _FakeTime)

    envelope = json.dumps({"intents": {
        hb: {"response": "15:00 帮 Pascal 盯着部署，无异常", "action": "silent"},
        other: {"response": "内部检查跑完了，一切正常", "action": "silent"},
    }}, ensure_ascii=False)
    monkeypatch.setattr("sys.stdin", _Stdin(envelope))
    ip.main()

    # 小时报: executed AND its product verifiable on disk
    assert get_intent(hb)["executed_at"] is not None
    text = target.read_text(encoding="utf-8")
    assert "### 2026-07-08 15:00 小时报" in text
    assert "盯着部署" in text
    # unrelated intent: executed, but its content appears in NO file
    assert get_intent(other)["executed_at"] is not None
    assert "内部检查" not in text
    assert [p for p in tmp_path.rglob("*.md")] == [target]
    # both were silent — nothing user-facing
    assert capsys.readouterr().out == ""


def test_quiet_hour_sentinel_e2e(tmp_path, monkeypatch, capsys):
    """Full path for a quiet hour (F-11): the 小时报 slot carries the
    documented HEARTBEAT_OK sentinel — the occurrence is legitimately done
    (cron row resets for the next hour), nothing lands in the timeline, and
    nothing reaches the user."""
    mod = _wire_e2e(tmp_path, monkeypatch)
    from core.intentions import create_intent, mark_triggered, get_intent

    hb = create_intent(
        name="小时报",
        trigger_type="cron",
        trigger_config={"expression": "0 9-23 * * *"},
        prompt="写一条小时报",
        intent_id="int_e2e_quiet",
    )
    mark_triggered(hb)
    mod.write_inflight([hb])

    target = tmp_path / "memory" / "timeline" / "hourly_log.md"
    monkeypatch.setattr(ip, "PRODUCT_LOGS", {hb: target})
    monkeypatch.setattr(ip, "time", _FakeTime)

    envelope = json.dumps(
        {"intents": {hb: {"response": "HEARTBEAT_OK", "action": "silent"}}})
    monkeypatch.setattr("sys.stdin", _Stdin(envelope))
    ip.main()

    row = get_intent(hb)
    assert row["executed_at"] is not None      # quiet hour = legitimately done
    assert row["status"] == "pending"          # cron reset for next occurrence
    assert row["attempt"] == 0                 # fresh budget — no re-fire loop
    assert not target.exists()                 # no junk entry in the timeline
    assert not mod.INFLIGHT_FILE.exists()      # manifest reconciled + cleared
    assert capsys.readouterr().out == ""       # nothing user-facing


def test_cron_refire_capped_after_max_attempts_e2e(tmp_path, monkeypatch):
    """F-14: a fast degraded fallback answering with husks must not convert
    one cron occurrence into a retry every cycle for 6h (intention-check runs
    at a 1-minute cadence — hundreds of paid calls per occurrence). After
    MAX_ATTEMPTS husk cycles the OCCURRENCE is retired loudly: an
    intent_occurrence_skipped event is emitted (the skip digest folds it into
    the 停摆汇总/补发 card — never silently closed), and the row returns to
    pending with a fresh attempt budget for the NEXT occurrence."""
    mod = _wire_e2e(tmp_path, monkeypatch)
    events = []
    monkeypatch.setattr(mod, "_emit_intent",
                        lambda ev, iid, **f: events.append((ev, iid, f)))
    from core.intentions import create_intent, mark_triggered, get_intent

    iid = create_intent(
        name="小时报",
        trigger_type="cron",
        trigger_config={"expression": "0 * * * *"},
        prompt="写一条小时报",
        intent_id="int_e2e_cap",
    )

    for attempt in range(1, mod.MAX_ATTEMPTS + 1):
        mark_triggered(iid)
        mod.write_inflight([iid])
        result = mod.reconcile_inflight([])    # husk cycle — nothing covered
        assert get_intent(iid)["status"] == "pending"   # row itself survives
        if attempt < mod.MAX_ATTEMPTS:
            assert result["retried"] == [iid]
            assert result["skipped"] == []
        else:
            assert result["skipped"] == [iid]  # budget exhausted → retired
            assert result["retried"] == []

    skipped = [(ev, iid_) for ev, iid_, _f in events
               if ev == "intent_occurrence_skipped"]
    assert skipped == [("intent_occurrence_skipped", iid)]  # surfaced once
    row = get_intent(iid)
    assert row["attempt"] == 0                 # fresh budget, next occurrence
    assert row["executed_at"] is None          # never fake-executed
    assert "skip digest" in (row["last_error"] or "")
