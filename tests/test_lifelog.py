"""Tests for core.lifelog (REQ-114/115/116) + the task hooks built on it.

Everything external is mocked / isolated: lifelog paths are redirected to
tmp_path via monkeypatched JARVIS_DIR (module attr for in-process tests, env
var for subprocess script tests — memorial tests especially need JARVIS_DIR
isolation per the 7/10 lesson), calendar input is synthetic text, and no
lark-cli send ever happens (memorial is used with send=False only).
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

import core.lifelog as lifelog

ROOT = Path(__file__).resolve().parent.parent
CHECKIN_POST = ROOT / "tasks" / "checkin_post.py"
ANCHOR_PRE = ROOT / "tasks" / "morning_anchor_pre.sh"
ANCHOR_POST = ROOT / "tasks" / "morning_anchor_post.py"
EXWEEK_PRE = ROOT / "tasks" / "exercise_week_pre.sh"
EXWEEK_POST = ROOT / "tasks" / "exercise_week_post.py"

NOW = datetime(2026, 7, 21, 12, 0)  # Tuesday


@pytest.fixture
def lifedir(tmp_path, monkeypatch):
    """Redirect all lifelog data paths into tmp_path."""
    monkeypatch.setattr(lifelog, "JARVIS_DIR", tmp_path)
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    return tmp_path


def _sub_env(tmp_path) -> dict:
    """Env for script subprocesses: JARVIS_DIR + MEMORY_DIR fully isolated."""
    (tmp_path / "jarvis.yaml").write_text(
        "retained_rhythms:\n  checkin: true\n  exercise_week: true\n",
        encoding="utf-8",
    )
    return {**os.environ,
            "JARVIS_DIR": str(tmp_path),
            "MEMORY_DIR": str(tmp_path / "memory"),
            # Belt+braces: never let a stray USER_ID make memorial resolvable
            "USER_ID": ""}


# ── script hygiene ───────────────────────────────────────────────────────


@pytest.mark.parametrize("script", [ANCHOR_PRE, EXWEEK_PRE])
def test_pre_script_syntax(script):
    r = subprocess.run(["bash", "-n", str(script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── diet append + normalization (REQ-114) ────────────────────────────────


def test_diet_append_normalizes_and_appends(lifedir):
    entry = lifelog.diet_append({"meal": "午饭", "items": ["牛肉面", " 青菜 ", ""],
                                 "source": "checkin", "note": "外卖"})
    assert entry["meal"] == "午"
    assert entry["items"] == ["牛肉面", "青菜"]
    assert entry["source"] == "checkin"
    rows = [json.loads(l) for l in
            (lifedir / "data" / "diet_log.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["note"] == "外卖"
    assert rows[0]["ts"]  # defaulted to now


def test_diet_append_infers_meal_bucket_from_ts_only(lifedir):
    e = lifelog.diet_append({"ts": "2026-07-21 08:00", "meal": "",
                             "items": ["粥"]})
    assert e["meal"] == "早"
    e = lifelog.diet_append({"ts": "2026-07-21 22:30", "meal": "",
                             "items": ["泡面"]})
    assert e["meal"] == "加餐"


def test_normalize_meal_aliases():
    assert lifelog.normalize_meal("早餐") == "早"
    assert lifelog.normalize_meal("夜宵") == "加餐"
    assert lifelog.normalize_meal("晚饭") == "晚"


# ── DIET: contract line parsing ─────────────────────────────────────────


def test_parse_diet_line_valid():
    e = lifelog.parse_diet_line("DIET: 午|牛肉面、青菜|外卖")
    assert e == {"meal": "午", "items": ["牛肉面", "青菜"], "note": "外卖"}


def test_parse_diet_line_rejects_bad_meal_and_empty_items():
    assert lifelog.parse_diet_line("DIET: 宇宙餐|石头") is None
    assert lifelog.parse_diet_line("DIET: 午|") is None
    assert lifelog.parse_diet_line("DIET: 午") is None
    assert lifelog.parse_diet_line("今天不错") is None


def test_split_diet_line_strips_trailing_only():
    msg, entry = lifelog.split_diet_line("今天状态不错。\n\nDIET: 晚|饺子")
    assert msg == "今天状态不错。"
    assert entry["meal"] == "晚" and entry["items"] == ["饺子"]

    # Mid-text DIET is prose, not a declaration
    msg, entry = lifelog.split_diet_line("DIET: 午|面\n后面还有正文")
    assert entry is None
    assert "后面还有正文" in msg


def test_split_diet_line_strips_malformed_line_from_card():
    # A broken contract line must never leak onto the user-facing card
    msg, entry = lifelog.split_diet_line("正文\nDIET: 火星餐|岩浆")
    assert msg == "正文"
    assert entry is None


# ── conservative chat-text parsing ───────────────────────────────────────


def test_parse_diet_mentions_clear_mention():
    entries = lifelog.parse_diet_mentions("午饭吃了牛肉面和青菜，下午继续干活")
    assert len(entries) == 1
    assert entries[0]["meal"] == "午"
    assert entries[0]["items"] == ["牛肉面", "青菜"]


def test_parse_diet_mentions_bare_ate_infers_meal_from_time():
    entries = lifelog.parse_diet_mentions("刚吃了三明治", when=datetime(2026, 7, 21, 8, 30))
    assert len(entries) == 1
    assert entries[0]["meal"] == "早"
    assert entries[0]["items"] == ["三明治"]


def test_parse_diet_mentions_never_hallucinates():
    # Idioms, vague mentions and food-free text produce NOTHING
    assert lifelog.parse_diet_mentions("这次真是吃了亏") == []
    assert lifelog.parse_diet_mentions("随便吃了点东西") == []
    assert lifelog.parse_diet_mentions("今天在改 heartbeat 的 bug") == []


# ── diet week summary ────────────────────────────────────────────────────


def test_diet_week_summary_counts_and_gaps(lifedir):
    for ts, meal, items in [
        ("2026-07-21 08:00", "早", ["粥", "鸡蛋"]),
        ("2026-07-21 12:30", "午", ["牛肉面"]),
        ("2026-07-19 19:00", "晚", ["牛肉面"]),
        ("2026-07-01 12:00", "午", ["旧数据"]),   # outside the 7d window
    ]:
        lifelog.diet_append({"ts": ts, "meal": meal, "items": items})
    s = lifelog.diet_week_summary(now=NOW)
    assert s["meals_logged"] == 3
    assert s["by_meal"] == {"早": 1, "午": 1, "晚": 1}
    assert s["common_items"][0] == {"item": "牛肉面", "count": 2}
    assert s["days_with_log"] == ["2026-07-19", "2026-07-21"]
    assert "2026-07-20" in s["gaps"] and "2026-07-15" in s["gaps"]
    assert len(s["gaps"]) == 5
    assert "旧数据" not in [i["item"] for i in s["common_items"]]


# ── exercise aggregation (REQ-116) ───────────────────────────────────────

SYNTH_CALENDAR = """# Calendar

Today (2026-07-21 Tuesday):
  09:00-10:00  周会 @ 会议室
  18:00-19:00  游泳 @ 泳池

Day 1 (2026-07-22 Wednesday):
  19:00-20:00  康复训练 (PT session)

Upcoming:
  07/25 Sat  10:00-11:00  篮球
  07/18 Sat  16:00-17:00  健身
"""


def test_calendar_exercise_events_keyword_filter():
    events = lifelog.calendar_exercise_events(now=NOW, text=SYNTH_CALENDAR)
    titles = {e["title"] for e in events}
    assert "游泳" in titles and "康复训练" in titles
    assert "篮球" in titles and "健身" in titles
    assert "周会" not in titles


def test_harvest_only_past_window_and_dedups(lifedir):
    added = lifelog.harvest_calendar_exercise(now=NOW, text=SYNTH_CALENDAR)
    activities = sorted(a["activity"] for a in added)
    # 游泳 (today) + 健身 (07/18, within last 7d) — future 康复/篮球 excluded
    assert activities == ["健身", "游泳"]
    # Second harvest is a no-op (dedup on date+time+activity)
    assert lifelog.harvest_calendar_exercise(now=NOW, text=SYNTH_CALENDAR) == []
    rows = [json.loads(l) for l in
            (lifedir / "data" / "exercise_log.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert all(r["source"] == "calendar" for r in rows)


def test_exercise_week_summary_merges_sources(lifedir):
    lifelog.harvest_calendar_exercise(now=NOW, text=SYNTH_CALENDAR)
    lifelog.exercise_append({"ts": "2026-07-20 20:00", "activity": "拉伸",
                             "source": "chat"})
    s = lifelog.exercise_week_summary(now=NOW)
    assert s["sessions"] == 3
    assert s["by_activity"] == {"游泳": 1, "健身": 1, "拉伸": 1}
    assert s["by_source"] == {"calendar": 2, "chat": 1}
    assert s["goal"] == "2-3" and (s["goal_min"], s["goal_max"]) == (2, 3)
    assert s["goal_met"] is True
    assert s["days_active"] == ["2026-07-18", "2026-07-20", "2026-07-21"]


def test_exercise_goal_from_personal_config(lifedir):
    (lifedir / "data").mkdir(exist_ok=True)
    (lifedir / "data" / "exercise_goal_personal.txt").write_text("4-5\n")
    assert lifelog.exercise_goal() == "4-5"
    assert lifelog.goal_range() == (4, 5)
    (lifedir / "data" / "exercise_goal_personal.txt").write_text("3\n")
    assert lifelog.goal_range() == (3, 3)
    (lifedir / "data" / "exercise_goal_personal.txt").write_text("whatever\n")
    assert lifelog.goal_range() == (2, 3)  # unparseable → neutral default


# ── morning anchor state (REQ-115) ───────────────────────────────────────


def test_morning_anchor_dedup_same_day(lifedir):
    assert lifelog.morning_anchor_fired(NOW) is False
    lifelog.morning_anchor_mark(NOW)
    assert lifelog.morning_anchor_fired(NOW) is True
    # Next day → due again
    assert lifelog.morning_anchor_fired(datetime(2026, 7, 22, 8, 30)) is False


def test_morning_anchor_items_default_then_personal(lifedir):
    items = lifelog.morning_anchor_items()
    assert items == lifelog.DEFAULT_ANCHOR_ITEMS  # neutral, multi-user safe
    (lifedir / "data").mkdir(exist_ok=True)
    (lifedir / "data" / "morning_anchor_personal.txt").write_text(
        "# 注释\n棋盘死活题一道\n康复 circuit\n")
    assert lifelog.morning_anchor_items() == ["棋盘死活题一道", "康复 circuit"]


def test_exercise_card_week_gate(lifedir):
    assert lifelog.exercise_card_sent_this_week(NOW) is False
    lifelog.exercise_card_mark(NOW)
    assert lifelog.exercise_card_sent_this_week(NOW) is True
    # Same ISO week (Sunday 7/26) still counts as sent
    assert lifelog.exercise_card_sent_this_week(datetime(2026, 7, 26, 19, 0)) is True
    # Next ISO week → due again
    assert lifelog.exercise_card_sent_this_week(datetime(2026, 7, 27, 19, 0)) is False


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_diet_add_and_week(lifedir, capsys):
    assert lifelog.main(["diet-add", "--meal", "早", "--items", "粥,鸡蛋"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meal"] == "早" and out["items"] == ["粥", "鸡蛋"]
    assert lifelog.main(["diet-week"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["meals_logged"] == 1


def test_cli_exercise_and_anchor_status(lifedir, capsys):
    assert lifelog.main(["exercise-add", "--activity", "游泳"]) == 0
    capsys.readouterr()
    assert lifelog.main(["exercise-week"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["sessions"] == 1 and summary["by_activity"] == {"游泳": 1}

    assert lifelog.main(["anchor-status"]) == 0
    assert capsys.readouterr().out.strip() == "due"
    lifelog.morning_anchor_mark()
    assert lifelog.main(["anchor-status"]) == 0
    assert capsys.readouterr().out.strip() == "sent"

    assert lifelog.main(["week-card-status"]) == 0
    assert capsys.readouterr().out.strip() == "due"


# ── checkin_post diet capture (subprocess, fully isolated) ──────────────


def test_checkin_post_captures_diet_line_and_strips_it(tmp_path):
    msg = "刚看到你聊起做菜的事。\n\nDIET: 午|牛肉面、青菜"
    r = subprocess.run([sys.executable, str(CHECKIN_POST)], input=msg,
                       capture_output=True, text=True, env=_sub_env(tmp_path))
    assert r.returncode == 0
    assert "DIET" not in r.stdout          # contract line never reaches the card
    assert "做菜" in r.stdout               # message itself still goes out
    rows = [json.loads(l) for l in
            (tmp_path / "data" / "diet_log.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["meal"] == "午"
    assert rows[0]["items"] == ["牛肉面", "青菜"]
    assert rows[0]["source"] == "checkin"


def test_checkin_post_without_diet_line_writes_nothing(tmp_path):
    r = subprocess.run([sys.executable, str(CHECKIN_POST)],
                       input="今天读的那本书有意思吗?",
                       capture_output=True, text=True, env=_sub_env(tmp_path))
    assert r.returncode == 0
    assert not (tmp_path / "data" / "diet_log.jsonl").exists()


# ── morning_anchor_post: one line, once per day ──────────────────────────


def _run_anchor_post(stdin: str, tmp_path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(ANCHOR_POST)], input=stdin,
                          capture_output=True, text=True,
                          env=_sub_env(tmp_path))


def test_morning_anchor_post_sends_once_per_day(tmp_path):
    msg = "早。今天的锚点：一道死活题 + 康复 circuit，各 10 分钟。"
    r1 = _run_anchor_post(msg, tmp_path)
    assert r1.returncode == 0
    card = json.loads(r1.stdout.strip())
    assert "晨间锚点" in json.dumps(card, ensure_ascii=False)
    state = json.loads((tmp_path / "data" / "morning_anchor_state.json").read_text())
    assert state["date"] == datetime.now().strftime("%Y-%m-%d")

    # Same day, second pass → dedup backstop, NO second card
    r2 = _run_anchor_post(msg, tmp_path)
    assert r2.returncode == 0
    assert r2.stdout.strip() == ""


def test_morning_anchor_post_clips_to_one_line(tmp_path):
    r = _run_anchor_post("第一行锚点提醒。\n第二行不该出现的唠叨。", tmp_path)
    card_text = r.stdout
    assert "第一行锚点提醒" in card_text
    assert "唠叨" not in card_text


def test_morning_anchor_post_silent_on_sentinel_and_error(tmp_path):
    assert _run_anchor_post("HEARTBEAT_OK", tmp_path).stdout.strip() == ""
    assert not (tmp_path / "data" / "morning_anchor_state.json").exists()


def test_morning_anchor_receipt_states_only_work_done(tmp_path):
    """Regression (2026-08-20 finding): the receipt claimed 待办 and 留中摘要
    sweeps that no code in the anchor task performs. It must describe the
    actual work (anchor items + calendar context + REQ-121 dedup)."""
    r = _run_anchor_post("早。今天的锚点：拉伸 10 分钟。", tmp_path)
    out = r.stdout
    assert "核对今日锚点事项与日历上下文" in out
    assert "待办" not in out
    assert "留中摘要" not in out
    # No ledger-only bin seeded → no digest, so no 攒批 claim either.
    assert "攒批" not in out


def test_morning_anchor_receipt_mentions_digest_only_when_present(tmp_path):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [
        {"ev": "create", "id": "mem_r1", "ts": ts, "epoch": int(time.time()),
         "source": "mail", "title": "占位判断题", "attention": "decision"},
        {"ev": "delivery", "id": "mem_r1", "status": "ledger_only", "ts": ts},
    ]
    (tmp_path / "memorials.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    out = _run_anchor_post("早。锚点两件。", tmp_path).stdout
    assert "📥" in out          # the digest footer itself rides the card
    assert "攒批一行" in out     # and the receipt owns up to attaching it


# ── exercise_week_post: one memorial card, one matter, once per week ────


def _run_exweek_post(stdin: str, tmp_path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(EXWEEK_POST)], input=stdin,
                          capture_output=True, text=True,
                          env=_sub_env(tmp_path))


def test_exercise_week_post_renders_one_card_one_matter(tmp_path):
    body = "本周运动 2 次（目标 2-3 次）。\n游泳×1、健身×1"
    r = _run_exweek_post(body, tmp_path)
    assert r.returncode == 0, r.stderr
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    assert len(lines) == 1                      # ONE card
    card = json.loads(lines[0])
    dumped = json.dumps(card, ensure_ascii=False)
    assert "本周运动" in dumped and "游泳×1" in dumped
    assert "知道了" in dumped                    # 批红 option present
    # Memorial ledger written in the ISOLATED dir, never the repo
    ledger = tmp_path / "memorials.jsonl"
    assert ledger.exists()
    events = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert events[0]["source"] == "exercise-week"
    assert events[0]["title"] == "本周运动"
    assert events[0]["authoring_protocol"] is True
    # Week stamped → second run same week emits nothing
    r2 = _run_exweek_post(body, tmp_path)
    assert r2.stdout.strip() == ""


def test_exercise_week_post_falls_back_to_deterministic_body(tmp_path):
    # Seed an isolated exercise log, then hand the post an unusable envelope:
    # the week's numbers must still ship.
    env = _sub_env(tmp_path)
    log = tmp_path / "data" / "exercise_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log.write_text(
        json.dumps({"ts": f"{today} 18:00", "activity": "游泳",
                    "source": "calendar", "note": ""}, ensure_ascii=False) + "\n")
    r = subprocess.run([sys.executable, str(EXWEEK_POST)],
                       input='{"user_message": ""}',
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    dumped = r.stdout
    assert "本周运动 1 次" in dumped
    assert "游泳×1" in dumped


def test_exercise_week_post_strips_model_authoring_directives(tmp_path):
    r = _run_exweek_post(
        "TITLE: 模型标题\n本周动了两次。\nOPTIONS: 甲 | 乙", tmp_path)
    assert r.returncode == 0, r.stderr
    event = json.loads((tmp_path / "memorials.jsonl").read_text().splitlines()[0])
    assert event["body"] == "本周动了两次。"
    assert "TITLE:" not in event["body"] and "OPTIONS:" not in event["body"]


def test_exercise_week_post_silent_on_sentinel(tmp_path):
    r = _run_exweek_post("HEARTBEAT_OK", tmp_path)
    assert r.stdout.strip() == ""
    assert not (tmp_path / "data" / "exercise_week_state.json").exists()
