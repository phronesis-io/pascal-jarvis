# REQ-16 长任务异步化设计（2026-06-11 调研定稿）

实证痛点：4/23 "3 小时任务阻塞 bot 完全不可用"。现状：同会话经 `.session_lock` 串行，6000s watchdog；jobs 雏形（core/jobs.py + `[ACTION:bg]` 路径）已存在，UUID bug 已修。

## 架构（每项有出处）

- **进程模型**：后台 job 独立 `claude -p` 进程（headless 文档：`-p` 内 background bash 任务在结果返回 ~5s 后被杀，必须独立进程）。lane 设计对齐 OpenClaw：session lane 串行（现有锁）、后台 lane 并行。
- **上下文继承**：`claude -p --resume <主会话sid> --fork-session`——拿全部历史、不污染主会话、复用 prompt cache（官方 CLI flag）。
- **自动提升**：复用 bot.sh watchdog 循环（已逐行解析 stream-json tool_use）——elapsed >120s 时登记 job、发任务卡、**释放 session lock**、该会话 session_id 切新 fork；原 handler 继续 wait 走完成卡路径。
- **进度**：tool_use 事件 → jobs/<id>/progress.txt → ≥60s 节流原地 PATCH 任务卡（Telegram editMessageText / Discord deferred-PATCH 模式）。
- **结果归并**：完成时写 jobs/pending_merge.jsonl，该会话下一条消息 prepend `[后台任务完成]` 摘要（Claude sub-agents / OpenClaw 共同收敛的"摘要注入"模式，而非拼 transcript）。
- **对账 sweeper**：每 60s——running 但 PID 死 → lost+失败卡（5min grace）；>30min 无新输出 → stale 警告。挂 heartbeat loop。

## MVP 切分与状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| MVP-1 | `--resume --fork-session` 继承上下文 + pending_merge 归并 + 超时 3600→6000 | ✅ 2026-06-11 已实现上线 |
| MVP-2 | watchdog 120s 自动提升 + 释放锁 + 后续消息 fork（**消掉原始痛点的关键刀**） | 待做（动主消息路径，需专门 session 实现+真机验证） |
| MVP-3 | 进度卡 PATCH + sweeper 对账 | 待做 |

衔接点：bot.sh:537（锁）、:610（watchdog/提升点）、:798（run_background_job）、core/jobs.py（progress/card_message_id/sweep）、core/heartbeat_loop.py（sweeper）。

来源：[Headless docs](https://code.claude.com/docs/en/headless) · [CLI reference](https://code.claude.com/docs/en/cli-reference) · [Sub-agents](https://code.claude.com/docs/en/sub-agents) · [OpenClaw queue](https://docs.openclaw.ai/concepts/queue) · [OpenClaw tasks](https://docs.openclaw.ai/automation/tasks)
