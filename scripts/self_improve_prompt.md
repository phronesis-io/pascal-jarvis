self improve —— 这是三天一次的例行自进化轮（由 heartbeat 的 self-improve-cycle 任务分离拉起，无人值守）。

授权与边界（Pascal 2026-08-07 亲口定的，MEMORY.md 里有对应记忆）：
- 题目必须来自**真实价值数据**，不许拍脑袋：批阅率与噪声源（memorials.jsonl / engagement_log.jsonl，哪类卡发得多批得少）、死路按钮、presence 哨兵与投递断流、heartbeat 饥饿、以及上一轮 self-improve 记忆里挂的账。
- 「有些自进化不用打扰我」：内部、可逆、方向已定的改进（降噪、合并重复、修死路、补哨兵、修 bug）直接做完，不发卡请示；收尾时在记忆里记账即可。
- 需要 Pascal 拍板的（方向性取舍、不可逆、对外可见行为变化）：不做，写成一张 propose 级奏折卡留给他，或记入 open_threads。
- 单轮预算约 2 小时；到点就收尾，宁可少做不烂尾。

流程按仓库 CLAUDE.md 的 Change Lifecycle 全套走：实证复盘→窄边界修→回归测试→全量 localtest→PR→CI 绿后按 admin-owner 规则合并（决定绑定 SHA 记录理由）→部署重启相关常驻进程→运行时验证。并发纪律：动手前查有没有别的 claude/Codex 会话在改仓库（ps + git status），只 stage 自己的文件，commit 前重查 HEAD。

收尾硬要求（每轮更新必收拾烂尾）：更新 auto-memory（本轮做了什么/剩什么/教训），MEMORY.md 索引加行；如果这轮什么值得做的都没挖到，也要在记忆里记一笔"本轮无题"和依据，不许静默结束。
