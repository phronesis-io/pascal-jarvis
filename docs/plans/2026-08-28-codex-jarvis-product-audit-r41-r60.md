# Codex 前台 / Jarvis 后台：R41-R60 产品审计

**状态：** 候选分支已实现可自动验证部分；尚未发布或通过真实使用准出
**日期：** 2026-08-28
**前置：** `2026-08-28-first-principles-interruption-audit-r21-r40.md`

## 总目标

这轮不问“Jarvis 还能加什么”，而问一个更残酷的问题：

> 已经有桌面和手机 Codex 时，Jarvis 哪些价值仍不可替代？

答案只有五类：正确时机、跨任务连续性、受托异步返回、权限与权威收口、以及 Pascal
主动保留的一两个陪伴节奏。普通提问、研究、写作、代码、文件和长分析都从 Codex
开始。若一个能力不能增加上述价值，应转为静默基础设施、由 Codex 替代或退役。

Jarvis 的目标不是让 Pascal 多跟它说话。最好的状态可以是 Pascal 长时间只用 Codex，
而 Jarvis 仍在正确保存决定、等待触发、验证外部效果，并只在确有必要时出现。

## R41-R60

| 轮次 | 审计问题 | 结论 / 实现 | 当前证据 |
|---|---|---|---|
| R41 生存测试 | 独立 Codex 已经能做好时，Jarvis 是否还应出现 | 不应。只有连续性、时机、异步、权限/收口和保留节奏能让 Jarvis 存活 | `PRODUCT.md` 原则 20；本 PRD |
| R42 默认入口 | Pascal 主动想做事时从哪里开始 | 桌面和手机都开正常 Codex 任务；新目标开新任务 | 插件默认提示、日常指南和 MCP instructions 已对齐 |
| R43 可发现性 | 新 Codex 任务怎样知道这套分工 | 新增版本化只读 `jarvis_operating_model`，不再依赖模型临场发挥 | MCP E2E、纯函数不可变副本测试 |
| R44 Matter 阈值 | 是否每个对话都要进 Jarvis | 否。只有跨任务/设备/时间/执行者，或有外部效果证据时才建 Matter | skill 决策表及 create/continue 测试 |
| R45 Session 边界 | 新 Session 会不会再次积累成无限上下文 | Session 是短执行窗；Matter 是长对象；一次只允许一个 acquire-run-release 租约 | Matter Run、租约、恢复、收据测试 |
| R46 跨产品记忆 | Codex 如何知道 Claude Code / Lark 的旧决定 | 编译当前有效、带来源的 claim；原始长对话只作审计证据 | Memory Compiler 与冲突/来源测试；长期真实回放待积累 |
| R47 续接体验 | 继续旧事是否要求用户记协议或 ID | 用户自然说“继续”；唯一匹配直接续，歧义只问一个人话问题 | `jarvis_matter_continue` 测试；桌面/手机验收仍为 0/20 |
| R48 Git 权威 | Jarvis 是否重建代码任务系统 | 不重建。Git/GitHub 管源码、diff、PR、CI、review、merge；Matter 只链接结果所需证据 | 产品与架构契约完成 |
| R49 主动理由 | Jarvis 为什么有资格先说话 | 只允许 deadline、重要 external change、requested result、judgment/authority、保留节奏或决策批次 | `core.interruption` 和显式生产入口测试 |
| R50 发信门禁 | 一条消息发出前要证明什么 | 证明 why-now、已完成工作、不可替代 owner need、一个最小动作，以及不发的真实损失 | `owner_need` / `why_now` / work receipt 失败关闭测试 |
| R51 说人话 | 用户几秒内能否懂发生了什么 | 消息顺序固定为结果、已做、为什么现在、只剩什么、最小动作；不展示 harness、provider、任务 ID 和日志 | 契约完成；真实文案质量需上线观察 |
| R52 长内容 | 手机飞书看不完怎么办 | 长正文、证据、diff、文件留 Codex；Lark 只给独立可懂的唤醒和可续读全文 | 卡片全文续读已有测试；Codex 手机迁移未验收 |
| R53 直接飞书 | 什么时候直接和 Jarvis 聊 | 仅快速捕捉、回复刚到的时限/授权、飞书原生动作、Codex 故障降级 | `jarvis_operating_model` 与日常指南 |
| R54 手机协作 | 手机是否需要另一个 Jarvis App / Tailscale | 不需要。手机 Codex 是前台，Lark 是唤醒；旧网页、3458、Tailscale 全退役 | 退役门禁已实现；手机真实验收 0/20 |
| R55 四平面 | 系统、执行器、模型、发布是否混在一起 | 分开：Jarvis 管产品状态；Codex/Claude 是本人启动的执行器；模型路由独立；发布只认 exact-SHA authority/evidence | `core.model_runtime`、Matter 与 release gate |
| R56 Harness 选择 | 无人值守改进能否用 Codex 或 Claude Code | 不能。真实 Seatbelt 探针证明子进程可另建 launchd job，无法证明 mutation 已全部结束；无人值守编码退役 | 退役 ADR、能力清单 absence gate |
| R57 失败重放 | Codex 改到一半失败后能否自动让 Claude 再做一遍 | 不能。编码只存在于本人启动的任务；失败留在该任务中恢复，不由后台换执行器重放 | Matter 连续性与任务收据 |
| R58 发布权限 | 自进化 Agent 能否测试绿后自行上线 | 不能。Jarvis 后台只观察和提案；push/PR/merge/deploy/restart 仍需独立审查和最终 SHA Owner 授权 | iteration-observe 与 release gate |
| R59 功能去留 | 怎样防止功能越积越多 | 能被以后一次 Codex 提问无损替代的功能标 `replace-with-codex`；后台必要但不应打扰的标 `quiet`；新增能力未分类则 CI 失败 | 生成 inventory 和 policy gate；发布前重算数量 |
| R60 产品准出 | 什么才叫这次重新设计完成 | 代码/测试、发布运行、真实验收三条证据分开；桌面和手机各 20 次，且绑定、背景、完成、重复动作和重讲率达标 | instrumentation 完成；真实样本均 0/20，故产品未完成 |

## 日常用户流程

1. Pascal 想做任何事，先在桌面或手机 Codex 开一个正常任务。
2. 这次能做完，就只用 Codex，不创建 Matter，不产生 Jarvis 活动。
3. 结果必须跨任务、设备、日期或执行者时，Codex 自然接到一个 Matter；协议留在工具层。
4. 新目标开新 Codex 任务；同一长期结果可复用同一 Matter，不复用无限增长的 Session。
5. 代码证据留 Git/GitHub；长内容和产物留 Codex；Jarvis 只保存必要的决定、来源、权限、
   结果收据和下一步。
6. Jarvis 只有在 R49 的理由成立且通过 R50 门禁时主动唤醒；否则写账、等待以后查询。
7. Pascal 明确说完成后，Jarvis 一次性收掉同 Matter 的旧 Item、Intent 和 Handoff；
   Agent 的“做完了”、测试绿或 Result Receipt 都不能替代本人收口。

## 仍未准出的事实

- 该候选尚未独立审查、取得最终 SHA Owner 发布授权、合并、部署或重启。
- Codex connector `0.4.0` 的桌面/手机真实验收均为 `0/20`。
- “说人话”“少打扰”“无需重讲”仍需发布后四周数据，不能由测试伪造。
- 最终保留哪 1-2 个陪伴 Routine 必须来自真实使用选择，不能由 Agent 自说自话。
- 在这些证据到齐前，不退役对应的可靠 Lark 路径，也不宣称迁移完成。
