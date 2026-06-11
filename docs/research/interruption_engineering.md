# 打扰工程：文献对照与参数决策（2026-06-11）

本地实证锚点：docs/research/engagement_hourly_baseline.md（0-9h ≤6-9%、黄金窗 10-11/14/18/21h 70%+、总回复率 28% 远高于行业 push 基准 7.8% [Airship 2025]——总量未入伤害区，优化点在时机与分层）。

## 文献裁决

| 机制 | 裁决 | 依据 |
|---|---|---|
| 深夜静默排队 | 支持 | 夜间通知有实证睡眠代价 [PMC6191085]；Slack DND 惯例 |
| 批量 digest | 支持但要扩展到白天 | Fitz 2019 RCT (n=237)：**每日 3 批最优；每小时一批≈无效；完全压制反而焦虑+FoMO 上升** |
| 同文 6h 去重 | 支持，可升级为同主题 24h | over-notification 行为代价（42% 改设置/39% 全关/8% 删 app）[mobiloud] |
| 纯 wall-clock 投递 | 反对 | defer-to-breakpoint 平均仅递延 88.6s 即显著降 frustration [Iqbal & Bailey CHI'08]；"刚结束一段手机活动"是最佳时机 [Fischer MobileHCI'11] |
| 内容不分层 | 反对 | general-interest frustration 4.98 vs 任务相关 3.59 (p<0.001) [Iqbal & Bailey '08] |

## 已实施参数（2026-06-11 上线）

1. **静默窗 23:30→10:00**（原 09:30；9 点档实测仍 6%）
2. **白天批量窗 10:00 / 13:30 / 17:30**：general-interest 源（eigenflux-feed-triage / content-recommend / personal-site）白天也进队，按窗合并放行；任务相关源（checkin/intents/calendar/phronesis）即时直发——内容分层 + 每日 3 批，同时满足 Fitz 与 Iqbal & Bailey
3. **断点放行**：用户 ≤5 分钟内发过消息（/tmp/jarvis-last-msg mtime）→ 立即放行整批（用户在屏幕前=天然断点）；静默窗内不放行

## 待做（PRD 已记）
- 同主题 24h 降级去重（需 topic 标注，可挂 haiku）
- general-interest 周配额（行业 opt-out 阈值：营销类 2-5 条/周触发 46% 退订）

来源：Fitz et al. 2019 [ScienceDirect S0747563219302596] · Iqbal & Bailey CHI 2008 [interruptions.net PDF] · Fischer MobileHCI 2011 · [PMC6191085] · [mobiloud 统计] · [Airship 基准]
