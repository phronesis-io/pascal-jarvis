# 什么时候需要 Jarvis：R61-R80 再审计

**状态：** 产品契约与显式节奏订阅已在本地候选实现；对抗审查和真实沙箱实验推翻无人值守编码的安全假设，该能力已退役；待最终 SHA 复审、发布和真实使用验收
**日期：** 2026-08-28
**前置：** R21-R40 主动交流审计、R41-R60 Codex 前台审计

## 结论

Pascal 不需要为了让 Jarvis 存在而跟它说话。主动想做事时，电脑和手机都从一个新的
Codex 任务开始。Jarvis 只有在单个前台任务之外仍有不可替代价值时才存在：正确时机、
跨任务连续性、异步返回、重要外部变化、本人权限与收口，以及明确保留的一两个节奏。

Jarvis 主动发消息也不是为了交流本身，而只服务五个目标：

1. 保护即将过期的时间、机会或安全；
2. 返回 Pascal 已明确托付的异步结果；
3. 把已经核验、与当前目标相关的重要外部变化带回来；
4. 把能代劳的工作完成后，只交付一个本人判断或授权；
5. 履行 Pascal 主动保留、可忽略也可停用的少数陪伴节奏。

如果没有任何目标成立，零消息是健康状态。若一件普通前台工作能在独立 Codex 任务中
无损完成，Jarvis 应当缺席。

## 二十轮结果

| 轮次 | 审计问题 | 结论与动作 | 当前证据 |
|---|---|---|---|
| R61 交流目的 | 系统是不是应该促进聊天 | 否；促进人的结果、时间和判断，engagement 不是目标 | `operating_model` 1.2、PRODUCT 原则 27 |
| R62 默认入口 | Pascal 想做事时先找谁 | 桌面/手机均开 Codex 新任务 | 默认入口契约、插件与日常指南 |
| R63 生存条件 | 已有 Codex 后 Jarvis 为什么还存在 | 只为时机、连续性、异步、外部变化、权威收口和保留节奏 | 六类 `jarvis_is_needed_when` |
| R64 消息目标 | Jarvis 先说话要达到什么 | 限定为五个用户目标，系统活动不算 | `proactive_message_goals` |
| R65 五问门禁 | 文档原则是否真的进代码 | 从两字段提升为五字段：need、receipt、now、action、silence cost | `core.interruption`、Memorial v2 gate |
| R66 历史兼容 | 新规则会不会伪造旧卡 | 不会；旧显式卡按原 gate 审计，新卡才强制 v2 | `message_gate_version` 与回归测试 |
| R67 沉默含义 | Pascal 不回复代表什么 | 不代表同意、拒绝、验收或关系下降；不能追问制造互动 | 既有 acceptance/closure 契约保留 |
| R68 零消息 | 很久没消息是否一定坏了 | 否；无欠付结果是健康，已承诺却未送达才是 delivery debt | 日常指南、presence/delivery debt 既有机制 |
| R69 陪伴边界 | 为了“更像助手”能否自动加日常卡 | 不能；最多保留本人明确选择的 1-2 个节奏，默认全部关闭 | 私有 `retained_rhythms` 配置、pre/post 双门禁 |
| R70 陪伴质量 | 保留节奏怎样不变成打卡压力 | 先采集本轮证据，可忽略、暂停、退订，不把生活评分 | Routine 五字段生产入口 |
| R71 外部变化 | 邮件/EigenFlux 有新东西就发吗 | 只发重要且相关、已核验去重的变化；传输活动不算变化 | 邮件、EigenFlux v2 gate |
| R72 托付结果 | 所有 Agent 结果都要推吗 | 只有本人明确托付的异步工作；普通执行结果留当前 Codex | `requested_result` 契约，真实样本待观察 |
| R73 现在性 | `why_now` 是否等于“刚检测到” | 否；必须说明晚发的真实代价或哪项托付刚结束 | `why_now` + `silence_cost` 强制字段 |
| R74 最小动作 | 卡片能不能把问题原样扔给人 | 不能；先做完可逆工作，只留一个判断、授权、知悉或继续动作 | `owner_action` 强制字段与生产入口迁移 |
| R75 长内容 | Lark 是否复制 Codex 长结果 | 不复制；Lark 只给独立可懂的唤醒，正文、证据、Diff 留 Codex | DESIGN 长内容规则，手机验收待完成 |
| R76 手机入口 | 手机是否要 Jarvis 自建 App | 不要；手机 Codex 是工作台，Lark 是唤醒/原生动作 | Tailscale/3458 退役门禁，20 次验收待完成 |
| R77 Session 边界 | 为什么不一直跟同一个 Jarvis 聊 | Session 是短执行窗，Matter 才是跨产品长期对象 | Matter acquire-run-release 契约 |
| R78 回复闭环 | 回复一张卡后旧状态怎么办 | 一次回答应收掉同 Matter 的重复卡、Intent、提醒和 Handoff | 既有 closure/reconciliation 测试 |
| R79 成功指标 | 消息数少了怎么判断价值 | 看有用闭环、重讲率、注意力成本、欠付结果和显式保留，不看活跃度 | PRODUCT success measures、审计 CLI v2 统计 |
| R80 杀死测试 | 哪些主动功能最终应删除 | 连续四周不能产生合法目标、总可由以后问 Codex 替代者静默或退役 | capability policy；真实四周数据待积累 |

## 实现变化

- 所有 `core/` 与 `tasks/` 直接 Memorial 生产入口由 AST 门禁要求声明五字段；漏一项
  即测试失败。
- 新显式消息写入 `message_gate_version=2`；折叠台账、投递 metadata 和审计均保留
  `owner_action` 与 `silence_cost`。
- `core.interruption.audit` 区分 v2 可见消息和历史显式消息，并按用户目标统计，方便
  发布后判断系统是在闭环还是只在产生活动。
- Operating Model 升到 1.2，Codex 的只读工具可以稳定回答“什么时候需要 Jarvis”。
- `checkin`、`daily_reflect`、`exercise_week` 默认全部关闭，只有私有 `jarvis.yaml` 中
  exact boolean `true` 才算显式保留；同时打开超过两个会全部 fail closed。旧的“20 小时
  没说话就欠一张”与 `companion-voice` 超时告警已经退役。
- 多轮独立对抗审查曾逐步修补 process birth token、double-fork、cwd/lease、launchd job
  identity 和 coalition 回收；最终复审指出 harness 仍可请求 launchd 创建边界外新 job。
- 真实无害探针证实 Codex `workspace-write` Seatbelt 内的 `launchctl submit` 可以成功。
  这推翻了“controller 能证明全部 mutation 已结束”的前提，因此没有继续堆清理补丁。
- `self-improve-cycle` heartbeat、可变更 coding harness、coalition/process supervisor、
  prompt、pre-hook、策略项和对应测试全部退役。后台只保留 `iteration-observe` 的观察、
  聚合、去重和 Proposal；代码工作从 Pascal 主动开启的 Codex/Claude Code 任务开始。
- Codex、Claude Code、多模型 runtime 和 provider fallback 仍然保留；退役的只是无人值守
  code mutation。Git/GitHub 与 exact-SHA 发布门禁不变。
- 最终独立复审又发现普通 Heartbeat 仍能经旧参数取得工具，以及 legacy worker preflight
  对未知状态、坏 PID 和空系统输出不够严格。现已在 `core.heartbeat` 与
  `core.heartbeat_model` 双层强制所有后台模型只读，GPT fallback 不再进入 agentic tool
  loop；发布前检查对未知 schema、畸形身份和不可解析的 `ps`/launchd 证据一律阻断。
- 重生成清单后共有 186 项真实能力：keep 82、quiet 88、replace-with-codex 16、
  unreviewed 0。退役后的全量验证为 `3674 passed, 6 skipped`；statement 82.2%、
  branch 74.1%，shell、shellcheck、维护性和全部模块覆盖预算通过。

## 仍未完成的真实证据

代码和文档不能证明产品迁移成功。发布后仍需：桌面 Codex 20 次、手机 Codex 20 次
Matter 继续验收；四周主动消息有用率；托付结果是否准时返回；零消息期间是否确实没有
delivery debt；以及本人最终保留哪 1-2 个节奏。沉默不能替代这些验收。
