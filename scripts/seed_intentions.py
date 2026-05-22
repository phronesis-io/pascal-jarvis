#!/usr/bin/env python3
"""Seed initial intentions from Pascal's memory and schedule.

Run once to bootstrap the intention system with known future actions.
Idempotent: skips intents whose tag already exists.

Usage:
    python3 scripts/seed_intentions.py
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.intentions import create_intent, list_intents


def _intent_exists(tag: str) -> bool:
    """Check if an intent with this tag already exists."""
    existing = list_intents(status="pending")
    for i in existing:
        import json
        tags = json.loads(i["tags"]) if isinstance(i["tags"], str) else (i["tags"] or [])
        if tag in tags:
            return True
    return False


def seed():
    created = []

    # ── 1. 天目山活动准备 (6/4 提醒，6/6-7活动) ──
    if not _intent_exists("seed:tianmushan-prep"):
        iid = create_intent(
            name="天目山活动准备提醒",
            trigger_type="date",
            trigger_config={"datetime": "2026-06-04T09:00:00"},
            prompt="""天目山活动（6/6-7）后天出发。提醒 Pascal 准备：
1. 找一个家里不用的小玩具/物件（交换闲置物件传统）
2. 翻衣柜找卡通元素的衣服（T恤/帽子/墨镜都行，卡通 dress code）
3. 确认和 Weici 的出行安排（爱德文车活动）
4. 三个传统：交换物件、聊天不提年龄/学校/公司、卡通穿搭""",
            purpose="6/6-7 天目山活动准备，需提前 2 天准备物件和衣服",
            tags=["seed:tianmushan-prep", "activity", "social"],
            source="seed",
            action_type="notify",
            expires_at="2026-06-06T08:00:00",
        )
        created.append(f"天目山准备 → {iid}")

    # ── 2. 明天 (5/22) 发发11 会前准备 ──
    if not _intent_exists("seed:fafa11-prep"):
        iid = create_intent(
            name="发发11 会前准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-22T13:30:00"},
            prompt="发发11 在 14:00 开始。快速回顾：这个会的目的是什么？有什么需要讨论的议题？",
            purpose="会前 30 分钟准备",
            tags=["seed:fafa11-prep", "calendar-prep"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-22T14:00:00",
        )
        created.append(f"发发11 prep → {iid}")

    # ── 3. 明天周会准备 ──
    if not _intent_exists("seed:weekly-meeting-prep"):
        iid = create_intent(
            name="周会准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-22T14:30:00"},
            prompt="""周会 15:00 开始。准备：
- 本周做了什么（白皮书进展、Jarvis 改进、意图系统上线）
- 要讨论的问题
- 需要其他人帮忙的事""",
            purpose="周会前 30 分钟整理思路",
            tags=["seed:weekly-meeting-prep", "calendar-prep", "work"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-22T15:00:00",
        )
        created.append(f"周会 prep → {iid}")

    # ── 4. 明晚攀岩提醒 ──
    if not _intent_exists("seed:climbing-prep"):
        iid = create_intent(
            name="攀岩准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-22T18:30:00"},
            prompt="""今晚 19:30 和王席文去粉抱攀岩。提醒：
- 带攀岩鞋（如果有的话）
- 吃点东西垫肚子
- 王席文是高中同学，上次看了她的毕业论文（Patience Agbabi 诗歌的分形分析）""",
            purpose="攀岩前准备提醒",
            tags=["seed:climbing-prep", "calendar-prep", "social", "health"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-22T19:30:00",
        )
        created.append(f"攀岩 prep → {iid}")

    # ── 5. 周六康复课准备 ──
    if not _intent_exists("seed:rehab-prep-0523"):
        iid = create_intent(
            name="康复课准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-23T13:00:00"},
            prompt="""复动肌骨康复课 14:00 开始（华山路1568号2层）。
- 准备问教练的问题（脊柱侧弯/腰突/颈突进展）
- 臀肌训练动作（臀桥/蚌式开合/趴着抬腿）这周做了没？
- 如果没做，今天课上跟教练重点练""",
            purpose="康复课前整理问题和进展",
            tags=["seed:rehab-prep-0523", "calendar-prep", "health"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-23T14:00:00",
        )
        created.append(f"康复课 prep → {iid}")

    # ── 6. 周六晚 online workshop 准备 ──
    if not _intent_exists("seed:workshop-prep-0523"):
        iid = create_intent(
            name="Online Workshop 准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-23T19:30:00"},
            prompt="Online workshop 20:00 开始。确认链接和内容。",
            purpose="Workshop 前准备",
            tags=["seed:workshop-prep-0523", "calendar-prep"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-23T20:00:00",
        )
        created.append(f"workshop prep → {iid}")

    # ── 7. 每周三心理咨询后 check-in (recurring) ──
    if not _intent_exists("seed:therapy-checkin"):
        iid = create_intent(
            name="心理咨询后 check-in",
            trigger_type="cron",
            trigger_config={"expression": "0 11 * * 3"},  # Wed 11:00
            prompt="""Pascal 今天上午有心理咨询（10:00-10:50）。
咨询后他可能在处理情绪。这时候：
- 不要推高强度任务
- 如果他想聊，用 MI 式倾听
- 可以温和地问"今天聊了什么？有什么想记下来的吗？"
- 帮他记录想法（therapy_notes）""",
            purpose="咨询后情绪处理期，调整交互模式",
            tags=["seed:therapy-checkin", "health", "wellbeing", "recurring"],
            source="seed",
            action_type="notify",
            conditions=[{"type": "user_awake", "wake": "08:00", "sleep": "23:30"}],
        )
        created.append(f"咨询后 checkin → {iid}")

    # ── 8. 每日 21:00 康复训练提醒 (recurring) ──
    if not _intent_exists("seed:daily-rehab"):
        iid = create_intent(
            name="每日康复训练",
            trigger_type="cron",
            trigger_config={"expression": "0 21 * * *"},  # 21:00 daily
            prompt="""21:00 康复训练时间。
今天的动作：根据习惯轮转制（周二=围棋死活题，周三=音乐，周四=围棋），先做康复再做轮转项目。
提醒 Pascal 开始康复训练。""",
            purpose="每日固定康复训练时间",
            tags=["seed:daily-rehab", "health", "recurring"],
            source="seed",
            action_type="notify",
        )
        created.append(f"每日康复 → {iid}")

    # ── 9. 早晨 anchor 提醒 (recurring) ──
    if not _intent_exists("seed:morning-anchor"):
        iid = create_intent(
            name="早晨 anchor",
            trigger_type="cron",
            trigger_config={"expression": "15 8 * * *"},  # 08:15 daily
            prompt="""早上好。前一晚钉死的 anchor 任务时间到了。
提醒 Pascal：泡沫轴 → 实体棋盘死活题（或读书/健身/小任务轮转）。
不要给选择——直接说"泡沫轴→死活题，开始吧"。
手机放远一点。""",
            purpose="早晨结构化 anchor，防止滑向手机",
            tags=["seed:morning-anchor", "health", "recurring"],
            source="seed",
            action_type="notify",
            conditions=[{"type": "user_awake", "wake": "07:30", "sleep": "23:30"}],
        )
        created.append(f"早晨 anchor → {iid}")

    # ── 10. 下周三 Dr Stretch (5/27) ──
    if not _intent_exists("seed:drstretch-prep-0527"):
        iid = create_intent(
            name="Dr Stretch 准备",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-27T19:30:00"},
            prompt="""Dr Stretch 拉伸课 20:00 开始。
- 这周身体哪里特别紧？
- 教练上次说的代偿模式有改善吗？
- 可以提前做 5 分钟热身""",
            purpose="拉伸课前准备",
            tags=["seed:drstretch-prep-0527", "calendar-prep", "health"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-27T20:00:00",
        )
        created.append(f"Dr Stretch prep → {iid}")

    # ── 11. 下周心理咨询 (5/28) ──
    if not _intent_exists("seed:therapy-prep-0528"):
        iid = create_intent(
            name="心理咨询素材整理",
            trigger_type="date",
            trigger_config={"datetime": "2026-05-27T21:00:00"},
            prompt="""明天 10:00 有心理咨询。帮 Pascal 整理这周的素材：
- 查看 therapy_notes_202605.md 里的议题
- 这周有什么新的情绪/身体信号？
- 咬指甲/紧绷/浅呼吸这些旧模式有变化吗？
- 有什么想跟咨询师讨论的新话题？""",
            purpose="咨询前一晚整理素材",
            tags=["seed:therapy-prep-0528", "calendar-prep", "health", "wellbeing"],
            source="seed",
            action_type="notify",
            expires_at="2026-05-28T10:00:00",
        )
        created.append(f"咨询素材整理 → {iid}")

    # ── 12. 婚礼提醒 (9月底) ──
    if not _intent_exists("seed:wedding-reminder"):
        iid = create_intent(
            name="曾逸帆婚礼准备提醒",
            trigger_type="date",
            trigger_config={"datetime": "2026-09-25T09:00:00"},
            prompt="""曾逸帆婚礼（10/11）还有 2 周多。提醒 Pascal：
- 2000 元红包准备好了吗？（对等回礼）
- 确认具体时间（午宴/晚宴）
- 确认地点（本地/异地，需不需要订机酒）
- 是否带老婆""",
            purpose="婚礼提前准备",
            tags=["seed:wedding-reminder", "social"],
            source="seed",
            action_type="notify",
            expires_at="2026-10-11T00:00:00",
        )
        created.append(f"婚礼提醒 → {iid}")

    print(f"Seeded {len(created)} intents:")
    for c in created:
        print(f"  ✅ {c}")
    if not created:
        print("  (all intents already exist)")


if __name__ == "__main__":
    seed()
