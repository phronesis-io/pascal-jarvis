"""cross_session_post user_message gates (2026-07-07 stale-PR-nag incident).

PRs #71/#73/#75 auto-merged 08:18–08:55, yet "3 个 PR 等你批" was pushed to
Pascal 8 more times until 21:24 — the LLM reworded the ping every 10-min cycle,
so the exact-match outbox dedup never fired, and nothing verified the claim
against live PR state. These tests pin the three gates that close it:
live gh verification of pending-PR claims (fail closed, auto-merge repos
exempt wholesale) and trigram dedup against recently-sent messages. In every
suppression case the digest write must still proceed — an early return would
break digest continuity.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POST = ROOT / "tasks" / "cross_session_post.py"


def _fake_gh(tmp_path: Path, body: str) -> Path:
    """Install a fake `gh` on PATH; returns the bin dir to prepend."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text("#!/bin/sh\n" + body + "\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run_post(tmp_path: Path, digest: str, user_message: str = "",
              gh_body: str = "exit 1") -> subprocess.CompletedProcess:
    bin_dir = _fake_gh(tmp_path, gh_body)
    env = {
        **os.environ,
        "MEMORY_DIR": str(tmp_path / "mem"),
        "JARVIS_DIR": str(tmp_path),  # sandbox outbox — never the live one
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    payload = {"digest": digest}
    if user_message:
        payload["user_message"] = user_message
    return subprocess.run(
        [sys.executable, str(POST)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, text=True, env=env,
    )


def _digest_file(tmp_path: Path) -> Path:
    return tmp_path / "mem" / "system" / "cross_session_digest.md"


def test_plain_user_message_printed_and_recorded(tmp_path):
    result = _run_post(tmp_path, "### proj\n- 修好了日历同步",
                       user_message="隔壁 session 把日历同步修好了，链路已恢复")
    assert result.returncode == 0, result.stderr
    assert "TITLE: 📡 跨会话动态" in result.stdout
    assert "WORKED:" in result.stdout
    assert _digest_file(tmp_path).exists()
    sent = tmp_path / "mem" / "system" / "cross_session_sent.jsonl"
    entries = [json.loads(l) for l in sent.read_text().splitlines()]
    assert len(entries) == 1 and "日历同步" in entries[0]["message"]


def test_user_message_survives_strict_work_receipt_gate(
        tmp_path, monkeypatch):
    """The ambient finding must reach its ledger, not only the digest file."""
    result = _run_post(
        tmp_path,
        "### proj\n- 修好了日历同步",
        user_message="隔壁 session 把日历同步修好了，链路已恢复",
    )
    assert result.returncode == 0, result.stderr

    import core.memorial as memorial

    monkeypatch.setattr(memorial, "JARVIS_DIR", tmp_path)
    rendered = memorial.memorialize_output(
        result.stdout,
        "cross-session-sync",
        require_work_receipt=True,
    )

    assert rendered == ""  # ambient notice: ledger-only, never realtime Lark
    states = memorial.list_memorials()
    assert len(states) == 1
    assert states[0]["delivery_status"] == "ledger_only"
    assert "跨产品会话" in states[0]["work_receipt"]


def test_reworded_repeat_suppressed_but_digest_written(tmp_path):
    msg_a = "隔壁 session 把三个修复分支都推上去了，测试全绿，今晚可以收尾"
    msg_b = "隔壁 session 把三个修复分支都推上去了，测试全绿，明天可以收尾"
    r1 = _run_post(tmp_path, "### proj\n- 第一轮进展", user_message=msg_a)
    assert "📡" in r1.stdout
    r2 = _run_post(tmp_path, "### proj\n- 完全不同的第二轮：接入了新数据源并部署",
                   user_message=msg_b)
    assert "📡" not in r2.stdout, "reworded repeat must not reach Pascal"
    assert "similarity" in r2.stderr
    # The digest write must survive the suppression
    assert "新数据源" in _digest_file(tmp_path).read_text()
    # Suppressed messages must not be recorded (would poison future dedup)
    sent = tmp_path / "mem" / "system" / "cross_session_sent.jsonl"
    assert len(sent.read_text().splitlines()) == 1


def test_dissimilar_message_still_sent(tmp_path):
    _run_post(tmp_path, "### a\n- 进展一", user_message="修复分支全部推上去了，测试全绿")
    r2 = _run_post(tmp_path, "### b\n- 进展二",
                   user_message="信箱那边有一封需要你亲自回的邮件")
    assert "📡" in r2.stdout


def test_pending_pr_claim_automerge_repo_dropped_without_gh(tmp_path):
    # eigenflux-pgc auto-merges: "等批" reminders about it are false by
    # construction — dropped before gh is even consulted.
    marker = tmp_path / "gh_called"
    r = _run_post(
        tmp_path, "### eigenflux-pgc\n- 三个修复 PR",
        user_message="eigenflux-pgc 有三个 PR 挂着等你网页端批",
        gh_body=f"touch {marker}; echo '[]'",
    )
    assert "📡" not in r.stdout
    assert "auto-merges" in r.stderr
    assert not marker.exists(), "exempt repo must not trigger a gh call"
    assert _digest_file(tmp_path).exists()


def test_pending_pr_claim_gh_failure_fails_closed(tmp_path):
    r = _run_post(tmp_path, "### pascal-jarvis\n- 开了个 PR",
                  user_message="pascal-jarvis 仓库有个 PR 挂着等你批",
                  gh_body="exit 1")
    assert "📡" not in r.stdout, "gh failure must DROP the claim, not push it"
    assert "gh verification failed" in r.stderr
    assert _digest_file(tmp_path).exists()


def test_pending_pr_claim_zero_open_dropped(tmp_path):
    r = _run_post(tmp_path, "### pascal-jarvis\n- 开了个 PR",
                  user_message="pascal-jarvis 仓库有个 PR 挂着等你批",
                  gh_body="echo '[]'")
    assert "📡" not in r.stdout
    assert "0 open PRs" in r.stderr


def test_pending_pr_claim_verified_open_is_sent(tmp_path):
    r = _run_post(tmp_path, "### pascal-jarvis\n- 开了个 PR",
                  user_message="pascal-jarvis 仓库有个 PR 挂着等你批",
                  gh_body='echo \'[{"number": 99}]\'')
    assert "📡" in r.stdout


def test_pending_pr_claim_unknown_repo_dropped(tmp_path):
    r = _run_post(tmp_path, "### misc\n- 某个仓库开了 PR",
                  user_message="某个新仓库有个 PR 挂着等你批",
                  gh_body="echo '[]'")
    assert "📡" not in r.stdout
    assert "no known repo" in r.stderr


def test_pending_pr_mixed_exempt_repo_still_verified_and_sent(tmp_path):
    # 2026-07-08 red-team: eigenflux PRs about PGC naturally co-mention pgc;
    # "ANY slug exempt → suppress" ate the live eigenflux#67 reminder. Mixed
    # mentions must gh-verify the NON-exempt repo and let the message through.
    calls = tmp_path / "gh_calls"
    r = _run_post(
        tmp_path, "### eigenflux\n- 面板 PR",
        user_message="PGC 的 NewsAPI 上限面板 PR（eigenflux#67）还挂着等你网页端批",
        gh_body=f"echo \"$@\" >> {calls}; echo '[{{\"number\": 67}}]'",
    )
    assert "📡" in r.stdout, r.stderr
    assert "eigenflux#67" in r.stdout
    called = calls.read_text()
    assert "--repo phronesis-io/eigenflux " in called
    assert "eigenflux-pgc" not in called, "exempt repo must not be gh-verified"


def test_non_pr_ask_with_incidental_pr_mention_sent_intact(tmp_path):
    # 2026-07-08 red-team: the ask is Pascal registering a token (standing
    # thread); the PR mention is incidental and in a DIFFERENT clause — the
    # whole-message AND used to classify this as a pending-PR claim and
    # suppress it via the pgc exemption.
    marker = tmp_path / "gh_called"
    r = _run_post(
        tmp_path, "### pgc\n- 比分适配器",
        user_message="等你注册 football-data.org token；相关 PR 已就绪(pgc#39)",
        gh_body=f"touch {marker}; echo '[]'",
    )
    assert "📡" in r.stdout, r.stderr
    assert "football-data.org" in r.stdout and "pgc#39" in r.stdout
    assert not marker.exists(), "no pending-PR clause → nothing to verify"


def test_merged_pr_news_not_classified_pending(tmp_path):
    # Past-tense/resolved contexts ("已获 approve 并合并上线") are news, not
    # pending claims — the bare 'approv' branch used to arm the gate, gh saw
    # 0 open PRs, and fresh good news was dropped as "stale".
    marker = tmp_path / "gh_called"
    r = _run_post(tmp_path, "### eigenflux\n- PR#62 合并",
                  user_message="eigenflux PR#62 已获 approve 并合并上线",
                  gh_body=f"touch {marker}; echo '[]'")
    assert "📡" in r.stdout, r.stderr
    assert not marker.exists()


def test_stale_pr_segment_removed_rest_of_message_sent(tmp_path):
    # Only the stale pending-PR clause is removed; other substantive
    # call-to-action content still reaches Pascal.
    r = _run_post(
        tmp_path, "### jarvis\n- 邮件提醒",
        user_message="pascal-jarvis 有个 PR 挂着等你批；另外邮箱有一封需要你亲自回的邮件",
        gh_body="echo '[]'")
    assert "📡" in r.stdout, r.stderr
    assert "邮箱" in r.stdout
    assert "等你批" not in r.stdout
    assert "0 open PRs" in r.stderr


def test_unconfirmed_old_sent_cache_entry_ignored(tmp_path):
    # 2026-07-08 red-team: the sent-cache records at PRINT time; if the Lark
    # delivery then fails, the entry must not suppress the retry for 24h.
    # No engagement_log row + older than the 30-min grace = never delivered.
    from datetime import timedelta

    from core.timeutil import now_local
    sent = tmp_path / "mem" / "system" / "cross_session_sent.jsonl"
    sent.parent.mkdir(parents=True)
    old_ts = (now_local().replace(tzinfo=None)
              - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    msg = "隔壁 session 把三个修复分支都推上去了，测试全绿，今晚可以收尾"
    sent.write_text(json.dumps({"ts": old_ts, "message": msg},
                               ensure_ascii=False) + "\n")
    r = _run_post(tmp_path, "### proj\n- 进展", user_message=msg)
    assert "📡" in r.stdout, "failed delivery must not suppress the retry"


def test_delivery_confirmed_sent_cache_entry_still_suppresses(tmp_path):
    # Same aged entry, but engagement_log has a cross-session "sent" row a few
    # minutes after the print — delivery confirmed, dedup must hold.
    from datetime import timedelta

    from core.timeutil import now_local
    now_dt = now_local().replace(tzinfo=None)
    sent = tmp_path / "mem" / "system" / "cross_session_sent.jsonl"
    sent.parent.mkdir(parents=True)
    printed = now_dt - timedelta(hours=2)
    msg = "隔壁 session 把三个修复分支都推上去了，测试全绿，今晚可以收尾"
    sent.write_text(json.dumps(
        {"ts": printed.strftime("%Y-%m-%d %H:%M"), "message": msg},
        ensure_ascii=False) + "\n")
    (tmp_path / "engagement_log.jsonl").write_text(json.dumps(
        {"ts": (printed + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M"),
         "source": "cross-session-sync", "type": "sent", "epoch": 0}) + "\n")
    r = _run_post(tmp_path, "### proj\n- 进展", user_message=msg)
    assert "📡" not in r.stdout
    assert "similarity" in r.stderr


def test_different_item_numbers_not_deduped(tmp_path):
    # 2026-07-08 red-team: templated reminders differing by one item number
    # ("#67 面板" vs "#72 告警", sim 0.75) are DIFFERENT items — the second
    # one is new work and must reach Pascal.
    gh = 'echo \'[{"number": 67}, {"number": 72}]\''
    r1 = _run_post(tmp_path, "### eigenflux\n- 面板 PR",
                   user_message="eigenflux 仓有 1 个 PR 等你在网页端批准合并（#67 面板）",
                   gh_body=gh)
    assert "📡" in r1.stdout, r1.stderr
    r2 = _run_post(tmp_path, "### eigenflux\n- 告警 PR",
                   user_message="eigenflux 仓有 1 个 PR 等你在网页端批准合并（#72 告警）",
                   gh_body=gh)
    assert "📡" in r2.stdout, "new item sharing the template must not be eaten"


def test_same_item_reworded_still_deduped(tmp_path):
    gh = 'echo \'[{"number": 71}]\''
    r1 = _run_post(tmp_path, "### eigenflux\n- 阈值 PR",
                   user_message="eigenflux 仓有 1 个 PR 等你在网页端批准合并（#71 面板阈值）",
                   gh_body=gh)
    assert "📡" in r1.stdout, r1.stderr
    r2 = _run_post(tmp_path, "### eigenflux\n- 阈值 PR 又提了一次",
                   user_message="eigenflux 仓还有 1 个 PR 挂着等你在网页端批准合并（#71 面板阈值）",
                   gh_body=gh)
    assert "📡" not in r2.stdout, "same #71 reworded is still a repeat"
    assert "similarity" in r2.stderr


def test_outbox_new_title_prefix_also_deduped(tmp_path):
    # Rows written AFTER the 2026-08-24 rename carry「跨会话动态」— the
    # outbox dedup must strip that prefix too.
    from core.timeutil import now_local_str
    outbox = tmp_path / "heartbeat_outbox.jsonl"
    sent_text = ("📡 跨会话动态：隔壁把三个修复分支都推上去了，测试全绿，今晚可以收尾")
    outbox.write_text(json.dumps(
        {"role": "assistant", "text": sent_text,
         "ts": now_local_str("%Y-%m-%d %H:%M"), "source": "heartbeat"},
        ensure_ascii=False) + "\n")
    r = _run_post(tmp_path, "### proj\n- 进展",
                  user_message="隔壁把三个修复分支都推上去了，测试全绿，明天可以收尾")
    assert "📡" not in r.stdout
    assert "similarity" in r.stderr


def test_outbox_rewording_deduped_across_batch_segments(tmp_path):
    # A near-identical line already delivered via the outbox (possibly embedded
    # in a batched multi-task message, '---'-separated) must suppress a fresh
    # rewording even with an empty sent-cache — covers pre-sent-cache history.
    # The 2026-08-24 pre-rename prefix stays covered: old rows still carry it.
    from core.timeutil import now_local_str
    outbox = tmp_path / "heartbeat_outbox.jsonl"
    sent_text = ("其他任务的内容在前面\n---\n📡 跨 Session 动态："
                 "隔壁把三个修复分支都推上去了，测试全绿，今晚可以收尾")
    outbox.write_text(json.dumps(
        {"role": "assistant", "text": sent_text,
         "ts": now_local_str("%Y-%m-%d %H:%M"), "source": "heartbeat"},
        ensure_ascii=False) + "\n")
    r = _run_post(tmp_path, "### proj\n- 进展",
                  user_message="隔壁把三个修复分支都推上去了，测试全绿，明天可以收尾")
    assert "📡" not in r.stdout
    assert "similarity" in r.stderr
