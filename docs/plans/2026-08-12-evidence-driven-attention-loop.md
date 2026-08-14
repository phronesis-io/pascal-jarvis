# 2026-08-12 注意力闭环自改进 PRD

状态：实现完成，待发布验证
证据窗口：2026-08-05 至 2026-08-12
对应 Taskline：`1e2b1ebf-4b65-433b-bcab-a475987500ed`

> 2026-08-14 修订：本文的 25/day 排队溢出规则已被
> `docs/plans/2026-08-14-audited-product-closure.md` 取代。普通主动投递
> 现在硬限制为 9/day，溢出直接转为 ledger-only，不进入次日积压；
> 4/10-minute burst queue 保持不变。

## 1. 为什么做

本轮回看了近期真实飞书交互的聚合结果、匿名化投递指标、`conversation_audit`、
运行日志、心跳调度记录和奏折账本。原始证据只保留在本地私有运行记录中，不进入公开仓库。
产品当前不是“没有能力”，
而是有四种注意力闭环失真：发送量统计少算、卡片集中倾倒、提醒被误写成待批、
一件事转入对话后原卡仍继续等待。与此同时，EigenFlux 的预装同步在普通 checkout
能运行，在独立 git worktree 中却静默退出，导致 agent 无法验证新 skill 是否真正预装。

用户结果只有一个：Jarvis 要主动，但每一次打扰都应当有价值、说人话、能结束，
并且工程系统必须能从权威数据证明这一点。

## 2. 实证

| 现象 | 实证 | 判断 |
|---|---|---|
| presence 少算 | 统一投递库与旁路奏折事件的近期聚合计数不一致；缺旁路事件的成功卡片被漏算 | P0，统计事实源错误 |
| 早晨爆发 | 近期运行窗口内出现数秒连续投递和十分钟两位数投递 | P0，缺少短时预算 |
| 日总量越线 | 多个近期自然日超过产品约定的 12-25 张目标；旧全局上限远高于目标 | P0，目标不是执行约束 |
| 假待批 | `intention-check` 没有写 OPTIONS 时仍自动套 `done/later/stop`；普通通知因此进入待批 | P0，来源默认值覆盖真实语义 |
| 生命周期分裂 | 同一事项在短时间内重复生成 memorial；用户进入奏折线程后，原卡状态不会自动退出待批 | P1，对话与卡片状态未收敛 |
| 用户文案泄漏 | 近期仍有内部生命周期术语和英文执行结果进入主动卡片 | P1，提示词约束没有确定性兜底 |
| 旧截断 WIP | PR #52 已把全文续发、续传提交和回归测试合入主线，合入后未再复现 | 已完成，不移植旧 stash |
| EigenFlux skill 漂移 | 上游 broadcast/profile 已到 0.10.3/0.2.7，Jarvis 仍为 0.10.2/0.2.6；worktree 下脚本找错相邻仓库并 exit 0 | P1，预装验证不可信 |

## 3. 产品契约

### REQ-124 投递事实只有一个

presence 的“真正到飞书”必须读取 `delivery_envelopes` 中已经获得 Lark 成功回执的
card envelope，而不是依赖可缺失的奏折 `sent` 旁路事件。测试、deploy smoke、回复和
非 card 文本不得污染卡量。数据库不存在时才回退旧 ledger，保证升级过程不断档。

### REQ-125 双层注意力预算

普通主动投递实行两层预算：本地自然日最多 25 张，任意滚动 10 分钟最多 4 张。
第 26 张或爆发中的第 5 张不丢弃、不伪装成 suppressed，而是保留在统一 SQLite 队列，
到下一个可用时刻自动重试。用户回复、奏折专属对话、urgent 和 alert 不受这两层预算
阻塞；现有 per-metric 与 per-source 反复告警上限继续生效。

预算按“实际送达”计数，不按创建计数；并发发送必须通过现有 SQLite reservation
原子抢占，不能两个 worker 同时拿到最后一个名额。`deploy-smoke` 和显式
`bypass_throttle` 不占产品预算。

### REQ-126 决策必须显式

`intention-check` 和 `intentions` 不是天然决策源。没有 OPTIONS、没有原生行动按钮、
调用方也没有显式指定 `attention=decision` 时，一律是“知道就行”。真正的闭环问句
继续由 `intentions_post.py` 写出明确的三枚关闭按钮；模型写出 OPTIONS 的问句仍是待批。
`exercise-week` 的“下周多动一次”是反馈，不得把整张健康周报升级成待批。

### REQ-127 对话接管卡片生命周期

当 Pascal 在某张奏折的飞书 thread 中回复，且 Jarvis 的回复已经获得送达确认，原奏折
自动记为“已转入对话”，退出待批和每日回访。只在成功送达后收敛；模型失败或回复仍在
队列时不提前关闭。重复调用幂等，旧卡同步失败不影响本地终态。

### REQ-128 用户可见文案兜底

主动投递的最后一道边界把已知内部词翻成人话：`Closure recorded`、`Intent cancelled`、
“匣子”“留中”“硬顶”“台账”不得原样到达用户。动作处理器本身也返回中文结果，避免
卡片 toast、action result 和后续上下文再次带出英文内部状态。

### REQ-129 EigenFlux 预装可在任意 worktree 验证

预装脚本接受 `EIGENFLUX_MAIN_DIR` 与 `EIGENFLUX_PLUGIN_DIR` 显式覆盖，默认行为保持不变。
agent 在独立 worktree 中可指定 canonical clone，同步上游 skill、应用 Jarvis overlay、
运行完整验证并留下明确 sentinel。Jarvis 本轮同步到上游 broadcast 0.10.3、profile 0.2.7，
包括 runtime identity `settings push` 契约。共享 Claude project 定位也必须从 linked
worktree 的 `.git/commondir` 回到正式 checkout，不能为临时分支另造一份 heartbeat memory。

## 4. 验收

1. 临时 DB 写入 7 张 card、若干 text/reply/deploy-smoke 后，presence 精确返回 7。
2. 注入固定时钟并并发提交 5 张普通卡，最多 4 张送达；第 5 张保持 queued，10 分钟后自动送达。
3. 同一天送达 25 张后，第 26 张保持 queued，次日预算窗口自动释放；alert 与 reply 仍即时送达。
4. 无 OPTIONS 的 intention-check 卡为 notice；带 OPTIONS 的问句和 closure 原生按钮仍为 decision；exercise-week 为 notice。
5. 奏折 thread 中只有在 Jarvis 回复送达成功后才 resolve；失败路径保持 pending。
6. 主动文本和 card 中不出现六个已知内部词；合法业务正文不触发错误面拦截。
7. 在独立 worktree 指定两个上游目录运行 preinstall，输出 `PREINSTALL_OK` 或 `PREINSTALL_CHANGES`，skill 版本与上游一致，相关契约测试通过。
8. 针对性测试、全量测试、独立代码审查、PR CI、main CI、release gate、kickstart 和运行 smoke 全部留下证据。
9. 在 linked worktree 中，heartbeat memory 仍指向正式 checkout 对应的 project slug。

## 5. 明确不在本批

以下问题保留为下一轮候选，不能在本轮报告成已完成：heartbeat 任务单飞租约、批量 envelope
故障隔离、Guardian/alert 独立 incident 生命周期、self-diagnostic 主路径、业务语义级跨来源
dedup、完整投递时刻表和“可接着做的事”清单卡。本轮双预算会先降低它们对人的影响，
但不替代各自的领域修复。

PR #56 的四日自动归档由另一个 agent 独立实现，已完成复核、隐私清理并合入本轮基线。
Phase-0 delegation 自动晋级、backup2 凭据等配置依赖保持原状。
