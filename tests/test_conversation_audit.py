import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

from core import conversation_audit as audit
from core.timeutil import now_local


def test_audit_ingests_logs_and_derives_issues(tmp_path):
    log = tmp_path / "jarvis.log"
    base = now_local() - timedelta(minutes=10)
    ts = [(base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(8)]
    log.write_text(
        "\n".join([
            f"[{ts[0]}] [INFO] Event: msg_type=text content_len=2 mid=om_1 chat_type=p2p content_head=hi",
            f"[{ts[1]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Calling primary Claude Code model=opus",
            f"[{ts[2]}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Final empty/error answer from Claude (74 chars after 1 attempts)",
            f"[{ts[3]}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Suppressed content: You've hit your monthly spend limit",
            f"[{ts[4]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (30s)",
            f"[{ts[5]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (60s)",
            f"[{ts[6]}] [INFO] [37cad35c-f395-5cdd-babe-97fbef249e1c] Session busy, waiting... (90s)",
            f"[{ts[7]}] [INFO] Event: msg_type=text content_len=12 mid=om_2 chat_type=p2p content_head=这个我也没有收到，这也太差了",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)
    conn = audit.connect(paths.db_path)
    completed_at = conn.execute(
        "SELECT completed_at FROM audit_runs WHERE id=?",
        (run_id,),
    ).fetchone()["completed_at"]
    conn.close()

    assert completed_at
    assert "Provider/account-limit text reached" in report
    assert "same-session" in report or "Same-session" in report
    assert "important signals were not surfaced" in report
    assert "Issues derived: 3" in report


def test_default_paths_honor_jarvis_dir_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))

    paths = audit.default_paths()

    assert paths.jarvis_dir == tmp_path
    assert paths.db_path == tmp_path / "data" / "conversation_audit.db"


def test_report_can_be_written_from_cli(tmp_path, monkeypatch):
    log = tmp_path / "jarvis.log"
    log.write_text(
        "[2026-06-16 14:01:14] [WARN] [sid] Suppressed content: You've hit your monthly spend limit\n",
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )
    monkeypatch.setattr(audit, "default_paths", lambda: paths)
    report = tmp_path / "report.md"

    assert audit.main(["--hours", "48", "--report", str(report)]) == 0
    assert report.exists()
    assert "Conversation Audit PRD" in report.read_text(encoding="utf-8")


def _empty_reply_fixture(tmp_path, *, replied: bool):
    """A session that emitted 'No response requested.' — optionally one that
    actually delivered a reply. Session id must match between the transcript
    file stem and the `[id] Replied (n chars)` log line."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "37cad35c-f395-5cdd-babe-97fbef249e1c"
    session = session_dir / f"{sid}.jsonl"
    now = datetime.now(timezone.utc)
    session.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "No response requested."},
            "timestamp": (now - timedelta(minutes=9)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    log_paths = []
    if replied:
        log = tmp_path / "jarvis.log"
        stamp = (now_local() - timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S")
        log.write_text(f"[{stamp}] [INFO] [{sid}] Replied (22 chars)\n",
                       encoding="utf-8")
        log_paths = [log]
    return audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=log_paths,
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )


def test_empty_reply_needs_delivery_evidence_to_be_called_user_visible(tmp_path):
    """2026-07-27: two P0s were raised for 'No response requested.' turns in
    local Claude Code CLI sessions that never sent anything. The delivery
    ledger held zero matching rows. A transcript is not a delivery record."""
    paths = _empty_reply_fixture(tmp_path, replied=False)

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "empty_reply_user_visible" not in report


def test_empty_reply_is_still_flagged_when_the_session_did_reply(tmp_path):
    """The corroboration gate must not silence the real defect."""
    paths = _empty_reply_fixture(tmp_path, replied=True)

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "empty_reply_user_visible" in report


def _provider_error_fixture(tmp_path, *, replied: bool):
    """A session whose transcript recorded a provider error, optionally one
    that actually delivered a reply. Mirrors _empty_reply_fixture."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "443f9880-a592-400c-a2dd-e1d3544d22fd"
    session = session_dir / f"{sid}.jsonl"
    now = datetime.now(timezone.utc)
    session.write_text(
        json.dumps({
            "type": "assistant",
            "message": {"role": "assistant",
                        "content": "API Error: 403 无权访问 vip_1_max_cheap 分组"},
            "timestamp": (now - timedelta(minutes=9)).strftime(
                "%Y-%m-%dT%H:%M:%S.000Z"),
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    log_paths = []
    if replied:
        log = tmp_path / "jarvis.log"
        stamp = (now_local() - timedelta(minutes=9)).strftime("%Y-%m-%d %H:%M:%S")
        log.write_text(f"[{stamp}] [INFO] [{sid}] Replied (22 chars)\n",
                       encoding="utf-8")
        log_paths = [log]
    return audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=log_paths,
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )


def test_provider_error_needs_delivery_evidence_to_be_called_user_visible(tmp_path):
    """The 2026-07-27 corroboration fix was applied to the empty-reply detector
    only, leaving its symmetrical twin raising P0s about local Claude Code CLI
    transcripts that were never sent (open findings #261/#265/#274/#283 across
    audit runs 50-56, every flagged session with reply_sent=0)."""
    paths = _provider_error_fixture(tmp_path, replied=False)

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "provider_error_in_assistant_transcript" not in report


def test_provider_error_is_still_flagged_when_the_session_did_reply(tmp_path):
    """The gate must not silence a provider error in a session that answered."""
    paths = _provider_error_fixture(tmp_path, replied=True)

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "provider_error_in_assistant_transcript" in report


def test_audit_flags_user_visible_provider_and_empty_replies(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "37cad35c-f395-5cdd-babe-97fbef249e1c"
    session = session_dir / f"{sid}.jsonl"
    now = datetime.now(timezone.utc)
    log = tmp_path / "jarvis.log"
    log.write_text(
        f"[{(now_local() - timedelta(minutes=9)).strftime('%Y-%m-%d %H:%M:%S')}] "
        f"[INFO] [{sid}] Replied (22 chars)\n",
        encoding="utf-8",
    )
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "🔧 You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            },
            "timestamp": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "No response requested."},
            "timestamp": (now - timedelta(minutes=9)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
    ]
    session.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "progress_provider_error_leak" in report
    assert "empty_reply_user_visible" in report


def test_audit_flags_recent_interaction_self_evolution_signals(tmp_path):
    log = tmp_path / "jarvis.log"
    base = now_local() - timedelta(minutes=10)
    ts = [(base + timedelta(seconds=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(5)]
    log.write_text(
        "\n".join([
            f"[{ts[0]}] [INFO] Event: msg_type=text content_len=10 mid=om_model chat_type=p2p content_head=我想知道你现在到底是什么模型",
            f"[{ts[1]}] [INFO] Event: msg_type=text content_len=10 mid=om_done chat_type=p2p content_head=codex 干完了吗，这个啥情况",
            f"[{ts[2]}] [INFO] Event: msg_type=text content_len=10 mid=om_copy chat_type=p2p content_head=用人话简洁明了地说，这个文案不是中文",
            f"[{ts[3]}] [INFO] Event: msg_type=text content_len=10 mid=om_pgc chat_type=p2p content_head=我们在说PGC信源问题，为什么让他这么晚收到，时效性差",
            f"[{ts[4]}] [INFO] Event: msg_type=text content_len=10 mid=om_research chat_type=p2p content_head=查呀，全查了，把所有东西都查明白",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "model_transparency_requested" in report
    assert "status_uncertainty" in report
    assert "awkward_progress_copy" in report
    assert "pgc_latency_quality" in report
    assert "external PGC handoff signal" in report
    assert "needs_deeper_research" in report


def test_audit_ingests_daemon_instability(tmp_path):
    daemon = tmp_path / "daemon.log"
    base = now_local() - timedelta(minutes=10)
    ts = [(base + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S") for i in range(3)]
    daemon.write_text(
        "\n".join([
            f"[{ts[0]}] [WARN] Observed component DOWN: admin :3456",
            f"[{ts[1]}] [WARN] Health check failed (2x): ['bot.sh is not running']",
            f"[{ts[2]}] [WARN] BRAIN-DEAD heartbeat: cross-session-sync last_success stale",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[daemon],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=48)
    report = audit.render_report(paths.db_path, run_id)

    assert "guardian_runtime_instability" in report
    assert "admin :3456" in report
    assert "Status: `open`" in report


def test_session_ingest_filters_messages_by_timestamp(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    sid = "37cad35c-f395-5cdd-babe-97fbef249e1c"
    session = session_dir / f"{sid}.jsonl"
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    new_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    # The empty-reply probe below is only "user visible" with delivery
    # evidence in the same session (2026-07-27); this keeps the test about
    # timestamp filtering rather than about the corroboration gate.
    log = tmp_path / "jarvis.log"
    log.write_text(
        f"[{(now_local() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')}] "
        f"[INFO] [{sid}] Replied (22 chars)\n",
        encoding="utf-8",
    )
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": "You've hit your monthly spend limit · raise it at claude.ai/settings/usage",
            },
            "timestamp": old_ts,
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "No response requested."},
            "timestamp": new_ts,
        },
    ]
    session.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[session_dir],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "empty_reply_user_visible" in report
    assert "provider_error_as_answer" not in report
    assert "Issues derived: 1" in report


def test_log_ingest_interprets_timestamps_as_local_time(tmp_path):
    log = tmp_path / "jarvis.log"
    old_ts = (now_local() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
    new_ts = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        "\n".join([
            f"[{old_ts}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Suppressed content: You've hit your monthly spend limit",
            f"[{new_ts}] [WARN] [37cad35c-f395-5cdd-babe-97fbef249e1c] Final empty/error answer from Claude (74 chars after 1 attempts)",
        ]),
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "restart_syntax_regression" not in report
    assert "provider_error_as_answer" not in report
    assert "No issues detected" in report


def test_timestamped_shell_errors_still_flag_restart_regressions(tmp_path):
    log = tmp_path / "jarvis.log"
    ts = (now_local() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(
        f"[{ts}] [ERROR] /repo/bot.sh: line 1735: syntax error near unexpected token `}}'\n",
        encoding="utf-8",
    )
    paths = audit.AuditPaths(
        jarvis_dir=tmp_path,
        log_paths=[log],
        session_dirs=[],
        db_path=tmp_path / "audit.db",
    )

    run_id = audit.run_audit(paths, hours=1)
    report = audit.render_report(paths.db_path, run_id)

    assert "restart_syntax_regression" in report


# ===========================================================================
# REQ-104 — card leak sentinel: internal residue on delivered cards becomes
# an open P0 by itself (HEARTBEAT_OK / raw JSON / task framing / bare send /
# raw OPTIONS line)
# ===========================================================================

def _ledger_line(ts, body, title="卡", source="checkin", mid="mem_1",
                 authoring_protocol=None, authoring_audit_text=None):
    row = {"ev": "create", "id": mid, "ts": ts, "title": title,
           "body": body, "source": source, "options": [],
           "context": "", "epoch": 1}
    if authoring_protocol is not None:
        row["authoring_protocol"] = authoring_protocol
    if authoring_audit_text is not None:
        row["authoring_audit_text"] = authoring_audit_text
    return json.dumps(row, ensure_ascii=False)


def _paths(tmp_path):
    return audit.AuditPaths(jarvis_dir=tmp_path, log_paths=[],
                            session_dirs=[], db_path=tmp_path / "audit.db")


def test_card_leak_sentinel_flags_residue(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    old = (now_local() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text("\n".join([
        _ledger_line(ts, "分析完毕，没什么可说的。HEARTBEAT_OK", mid="m1"),
        _ledger_line(ts, '{"response": "今天不错", "action": "notify"}', mid="m2"),
        _ledger_line(ts, "[CHECKIN]\n\n昨晚你聊到很晚。", mid="m3"),
        _ledger_line(ts, "send", mid="m4"),
        _ledger_line(ts, "试讲顺利吗？\n\nOPTIONS: 聊聊 | 知道了", mid="m5"),
        _ledger_line(ts, "这是一张干净的卡片，正文完全正常。", mid="m6"),
        _ledger_line(old, "HEARTBEAT_OK", mid="m7"),  # outside window
    ]) + "\n", encoding="utf-8")
    run_id = audit.run_audit(_paths(tmp_path), hours=24)
    conn = audit.connect(tmp_path / "audit.db")
    rows = conn.execute(
        "SELECT evidence FROM audit_issues WHERE run_id=? AND "
        "issue_type='card_template_leak'", (run_id,)).fetchall()
    conn.close()
    got = " ".join(r["evidence"] for r in rows)
    assert len(rows) == 5
    for mid in ("m1", "m2", "m3", "m4", "m5"):
        assert f"memorial={mid}" in got
    assert "m6" not in got and "m7" not in got


def test_card_leak_ignores_non_create_events(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(json.dumps(
        {"ev": "decide", "id": "m1", "ts": ts, "label": "HEARTBEAT_OK"}) + "\n")
    run_id = audit.run_audit(_paths(tmp_path), hours=24)
    conn = audit.connect(tmp_path / "audit.db")
    n = conn.execute("SELECT COUNT(*) FROM audit_issues WHERE run_id=? AND "
                     "issue_type='card_template_leak'", (run_id,)).fetchone()[0]
    conn.close()
    assert n == 0


# ===========================================================================
# REQ-105 — closure workflow: open-findings view, resolve, and carry-forward
# ===========================================================================

def test_resolve_and_carry_forward(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(
        _ledger_line(ts, "HEARTBEAT_OK", mid="m1") + "\n")
    paths = _paths(tmp_path)
    audit.run_audit(paths, hours=24)

    findings = audit.open_findings(paths.db_path, days=7)
    assert len(findings) == 1
    fid = findings[0]["id"]

    # resolve requires a note
    try:
        audit.resolve_findings(paths.db_path, "  ", issue_id=fid)
        assert False, "empty note must be refused"
    except ValueError:
        pass
    n = audit.resolve_findings(paths.db_path, "fixed in commit abc123",
                               issue_id=fid)
    assert n == 1
    assert audit.open_findings(paths.db_path, days=7) == []

    # a second run re-derives the same evidence → carried forward as resolved
    audit.run_audit(paths, hours=24)
    assert audit.open_findings(paths.db_path, days=7) == []
    conn = audit.connect(paths.db_path)
    statuses = [r["status"] for r in conn.execute(
        "SELECT status FROM audit_issues WHERE issue_type='card_template_leak'"
    ).fetchall()]
    conn.close()
    assert statuses.count("resolved") == 2


def test_resolve_by_type_and_open_findings_dedup(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(
        _ledger_line(ts, "HEARTBEAT_OK", mid="m1") + "\n")
    paths = _paths(tmp_path)
    audit.run_audit(paths, hours=24)
    audit.run_audit(paths, hours=24)  # same evidence twice, two runs

    findings = audit.open_findings(paths.db_path, days=7)
    assert len(findings) == 1  # deduped by (type, evidence)

    n = audit.resolve_findings(paths.db_path, "false positive — test",
                               issue_type="card_template_leak")
    assert n == 2  # both open rows closed
    assert audit.open_findings(paths.db_path, days=7) == []


def test_cli_open_findings_and_resolve(tmp_path, monkeypatch, capsys):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(
        _ledger_line(ts, "HEARTBEAT_OK", mid="m1") + "\n")
    paths = _paths(tmp_path)
    audit.run_audit(paths, hours=24)

    rc = audit.main(["open-findings", "--db", str(paths.db_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "card_template_leak" in out

    rc = audit.main(["resolve", "--type", "card_template_leak",
                     "--note", "wad", "--db", str(paths.db_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "resolved 1" in out

    rc = audit.main(["open-findings", "--db", str(paths.db_path)])
    out = capsys.readouterr().out
    assert "none 🎉" in out


def test_card_leak_task_framing_ignores_cjk_timeline(tmp_path):
    """Red-team 7/20 #4: a card quoting '[ts] 中文' timeline lines is not a
    leak."""
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(_ledger_line(
        ts, "昨天的节奏：\n[2026-07-19 07:30] 起床锻炼\n[2026-07-19 14:00] 试讲",
        mid="m1") + "\n", encoding="utf-8")
    run_id = audit.run_audit(_paths(tmp_path), hours=24)
    conn = audit.connect(tmp_path / "audit.db")
    n = conn.execute("SELECT COUNT(*) FROM audit_issues WHERE run_id=? AND "
                     "issue_type='card_template_leak'", (run_id,)).fetchone()[0]
    conn.close()
    assert n == 0


def test_card_leak_ignores_options_inside_markdown_examples(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    bodies = [
        "协议例子：\n```text\nOPTIONS: 甲 | 乙\n```",
        "对方原文：\n> OPTIONS: 甲 | 乙",
        "缩进代码：\n    OPTIONS: 甲 | 乙",
        "空行后的缩进代码：\n\n    OPTIONS: 甲 | 乙",
        "协议例子：\n```text\nRECOMMEND: 甲 — 示例\nTITLE: 示例\n```",
        '卡片例子：\n```json\n{"response":"示例"}\n```',
    ]
    (tmp_path / "memorials.jsonl").write_text(
        "\n".join(_ledger_line(ts, body, mid=f"m{i}")
                  for i, body in enumerate(bodies)) + "\n",
        encoding="utf-8",
    )
    paths = _paths(tmp_path)
    run_id = audit.run_audit(paths, hours=24)
    conn = audit.connect(paths.db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM audit_issues WHERE run_id=? AND "
        "issue_type='card_template_leak'", (run_id,)).fetchone()[0]
    conn.close()
    assert count == 0


def test_card_leak_respects_authoring_provenance(tmp_path):
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    body = ("TITLE: 引用标题\n正文\nOPTIONS: 引用选项\n"
            'HEARTBEAT_OK\n{"response":"引用 JSON"}')
    (tmp_path / "memorials.jsonl").write_text("\n".join([
        _ledger_line(ts, body, source="mail", mid="quoted",
                     authoring_protocol=False),
        _ledger_line(ts, body, source="heartbeat", mid="leaked",
                     authoring_protocol=True),
        _ledger_line(ts, body, source="eigenflux", mid="segmented",
                     authoring_protocol=True,
                     authoring_audit_text="这段本地分析是干净的"),
    ]) + "\n", encoding="utf-8")
    paths = _paths(tmp_path)
    run_id = audit.run_audit(paths, hours=24)
    conn = audit.connect(paths.db_path)
    rows = conn.execute(
        "SELECT evidence FROM audit_issues WHERE run_id=? AND "
        "issue_type='card_template_leak'", (run_id,)).fetchall()
    conn.close()
    assert len(rows) == 1
    assert "memorial=leaked" in rows[0]["evidence"]


def test_resolve_by_id_closes_twin_rows(tmp_path):
    """Red-team 7/20 #2: the daily runner's 25h overlap derives the same
    evidence into two open rows; resolve --id must close both or the older
    twin resurfaces in open-findings immediately."""
    ts = now_local().strftime("%Y-%m-%d %H:%M")
    (tmp_path / "memorials.jsonl").write_text(
        _ledger_line(ts, "HEARTBEAT_OK", mid="m1") + "\n")
    paths = _paths(tmp_path)
    audit.run_audit(paths, hours=25)
    audit.run_audit(paths, hours=25)
    findings = audit.open_findings(paths.db_path, days=7)
    assert len(findings) == 1
    n = audit.resolve_findings(paths.db_path, "fixed", issue_id=findings[0]["id"])
    assert n == 2
    assert audit.open_findings(paths.db_path, days=7) == []


def test_connect_uses_wal_and_bounded_lock_wait(tmp_path):
    connection = audit.connect(tmp_path / "conversation_audit.db")
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()
