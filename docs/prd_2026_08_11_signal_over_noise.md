# PRD 2026-08-11 · 信号胜于噪声（REQ-119 ~ REQ-123）

授权：Pascal 2026-08-11 批准四项改动（封 web fallback / 退役 funnel 手机面 / 降噪减卡量 / 修账本口径），
并要求以 PM 视角基于历史数据重新思考。本文档即该轮 PRD。

## 0. 证据基础（三路挖掘，2026-08-11）

数据源：`data/jarvis.db`（delivery_envelopes 等）、`memorials.jsonl`、`engagement_log.jsonl`、
三个记忆目录、全部 152 条 typed response 原文。关键事实：

- **通道**：近 14 天 lark 235 张已读 225（95.7%）；web 170 张已读 3（1.8%）。web 是死通道，
  且 web transport 无条件返回成功（`core/delivery.py:268`），"delivered" 是假的。
- **手机面**：iPhone 最后一次真实访问 2026-08-04 02:48，之后归零；Tailscale funnel 公网暴露，
  8/9 被扫描器打 22 次探测（全部 401）。零使用 + 持续被扫 = 纯负资产。
- **:3457 dashboard**：近两周人类访问 ≈ 0（Chrome 仅 1-2 次连接痕迹），无访问日志无埋点。
- **时序**：01:00-08:00 投递 ghost 率（>4h 未读）50-100%；12:00 / 15:00-17:00 / 22:00 是秒读窗口
  （2 分钟内读完率 47-67%）；周日批红 7 次 vs 周一 51 次，周日是决策死区。
- **转化分层**：checkin 93% / intention-check 69% / calendar-sync 68% 批红；
  eigenflux 系 20%（170 卡 lapse 114，占全部 lapse 31%）；morning-anchor 10%；
  metrics-digest 12%（42/43 走 web）；pgc-improvement / iteration-observe 0%。
  规律：**关于他的生活、需要他动手的卡他批；信息流转发 / 系统自述 / 同构日报他不批**。
- **负反馈实锤**：confused 仅 2 次，全是运维黑话卡和催账 docket 卡；na（不用追了）5 次全是闭环追问；
  cancel 3 次全是广播草稿整点重发轰炸。
- **口径分裂**：台账 106 张未闭环 vs escrow docket 自报 14 件——escrow 只数逾期 decision，
  notice 与未到期不在内；web 假 delivered 又让 delivery 台账永远 delivered-never-read。

## 1. 宪法裁决（本轮一并落实的产品级结论）

1. **飞书是唯一投递面**（8/7 拍板执行到底）：卡片要么走飞书，要么只进台账靠晨匣一行字兜底。
   不存在第三条路。web/phone 不再是投递通道，`route_channel='web'` 从此不再新增。
2. **归档面诚实化**：dashboard :3457 保留为归档 + ops 参考（不投功能，拍板不变）；
   mobile gateway :3458 与 Tailscale funnel 退役（本 PRD REQ-120）。Tailscale 私网本体保留作维护通道。
3. **一事一卡的现行定义成文**：一个决策一卡；同类知会合并；一拍 ≤4 张；同一话题线他已读未拍时不追发。
4. **PRODUCT.md 旧宪法（phone/web = canonical surface）与 8/7 拍板冲突，本轮改正**（REQ-123）。

## 2. REQ-119 · 封死 web fallback（授权项 1）

**现状**：desk_reachable 恒 False，decision 类已自动走飞书；仍走 web 的是
`AMBIENT_SOURCES`（cross-session-sync / metrics-digest / phronesis-monitor / repos-sync，
`core/memorial.py:221`）的 notice 卡 + 显式 kind=web 投递。

**改法**：
- a. `AMBIENT_SOURCES` 语义从「路由到 web」改为「**ledger-only**：创建 memorial、不投递、
  不再生成 delivery envelope」。晨匣兜底照旧（`core/presence.py` web_only → morning_digest_line，
  ≥5 条一行）。
- b. `core/delivery.py:_route()` 默认分支（:514 落 web）改为 lark；web transport 的假成功
  （:268）删除——走不到的防御分支不许再造假账。
- c. `_deliver_existing()`（memorial.py:1389）web 路线不再建 envelope（配合 REQ-122 方案 C）。
- d. 受影响测试改写契约：`tests/test_card_delivery_closure.py`（ambient 断言反向）、
  `test_presence.py`、`test_delivery_pipeline.py`、`test_no_dead_ends.py`、`test_memorial.py`。

**验收**：新增卡片中 route_channel='web' 数量 = 0；晨匣行照常出现；presence 哨兵不误报。

## 3. REQ-120 · 退役 Tailscale funnel + mobile gateway（授权项 2）

分两步，避免路由语义与基础设施同 PR 动：

- **步骤 1（基础设施，行为零变化）**：
  - 停：`launchctl bootout gui/$UID/com.pascal.jarvis.mobile-gateway`；`tailscale funnel reset`。
  - 删：LaunchAgent plist（本机 + repo `scripts/launchd/`，install.sh 引用）、
    `dashboard/mobile_gateway.py`、运行时工件 `mobile_access.json`。
  - 改：`components.yaml` 删 mobile-gateway / mobile-tailnet 两项；`restart.sh:351,:454-455`；
    `core/log_maintenance.py:37-38`；CLAUDE.md / ARCHITECTURE.md / DESIGN.md 的 :3458 描述。
- **步骤 2（代码清理）**：desk 配对/push/VAPID 代码（`core/mobile_access.py` 相关部分、
  `core/tailnet.py` funnel 路径、`delivery.py` push channel 分支）退役；
  `web_desk_url` 返回空后按死路铁律摘掉一切引用它的按钮（escrow docket「去事项处理」
  已按 no-dead-ends 设计自动消失，有测试护住）；dashboard settings 页配对 UI 摘除。
- **收尾**：关闭 mobile 相关 worktree 分支（feat/mobile-renewable-pairing、fix/mobile-pair-cookie）。

**验收**：`:3458` 无监听；`tailscale funnel status` 无条目；components 体检无 skipped 遗留项；
全站 grep 无到 :3458 的死路。

## 4. REQ-121 · 降噪减卡量（授权项 3）

目标：26 张/天 → 个位数，且高价值触达不受损。按源施策（数据依据见 §0）：

| 源 | 现状 | 改法 |
|---|---|---|
| metrics-digest | 30m 一轮逐条成卡，日报同构必死 | **只在状态翻转成卡**（✅恢复 / 🚨挂了 有批红实证）；稳态只写台账，晨匣兜底 |
| phronesis-monitor | 10m 一轮，批红 2/24，团队闲聊转述全 lapse | interval 10m→60m；只有点名 Pascal 或需他动作才成卡，其余 ledger-only |
| repos-sync | 2h 一轮，批红 2/24 | 改每日 rollup 一张（或 ledger-only + 晨匣） |
| eigenflux-feed-triage | 批红 20%，抽象架构讨论全 lapse | prompt 收紧：大厂/竞品具体事件才单卡；抽象讨论攒批入晨匣 |
| morning-anchor | 9 张 lapse 正文逐字相同 | 无新信息不重发（正文与前一日相同则跳过） |
| pgc-improvement / iteration-observe | 批红 0% | 并入自进化静默流（SILENT_TASKS），不成卡 |

机关：`HEARTBEAT.md` 任务定义 + `core/heartbeat.py:439 SILENT_TASKS` + 各 post 脚本。
注意 attention_roi 只做 decision→notice 降级，帮不上 notice 源，故直接改生成侧。

**验收**：改后 7 天日均卡量 ≤ 10；checkin / intention-check / calendar-sync 量不受影响；
状态翻转类告警仍实时到飞书。

## 5. REQ-122 · 账本口径合一（授权项 4）

- **方案 A（当轮）**：闭环定义统一 = `memorials.jsonl` folded status ∈ {decided, lapsed, resolved}；
  报表把「lapse 留中」单列为"未看自动归档"，不再混入"未闭环"。存量 106 张幽灵卡一次性对账标注。
- **方案 C（随 REQ-119）**：web 不再造 envelope，假 delivered 不再新增。
- **docket 卡文风修**：escrow docket 是仅有的两张 confused 卡之一——改为人话
  （第一句结论 + 哪几件最要紧 + 不需要动作就明说），数字与台账口径机械一致。
- 方案 B（lapse 时同步收尾 envelope）暂缓，A+C 后若仍有解释成本再上（解释第二遍=该修了）。

**验收**：docket 数字与台账查询同口径可复算；报表三分类（待批 / 已批 / 未看归档）加总 = 创建数。

## 6. REQ-123 · PRODUCT.md 宪法对齐（文档债，随轮清理）

- PRODUCT.md 删除「Items on phone/web: canonical batch-review surface」等旧世界观段落，
  改写为：飞书=产品本体；:3457=归档+ops；:3458=已退役；Routines=冻结待并入或退役。
- 冻结面条目统一标注 `frozen: archive+ops`，防止后来者往冻结面投功能。

## 7. 本轮不做、等拍板的 P2 背单（按建议优先级）

1. **投递时刻表调度器**：非紧急卡避开 01-08 死区、聚投 12/15-17/22 窗口；周日只发不可延期项。
   （数据最硬，建议下轮首选）
2. **alert 单独生命周期**：alert 卡不与 feed 卡同一条 lapse 流——25 张 alert 静默留中过
   （含阿里云欠费追踪、日历授权失效），应有升级/重投路径而非 7 天留中。
3. **留中回访机制**：他 8/9 原话「每隔 6 小时或者有空就和我再回顾一下，看这个事要不要继续」。
4. **「可接着做的事」清单卡**：他 8/9 原话「像个列表一样的卡片，跟我说有几个事情我可以接着做」。
5. **重发 bug 族清理**：好友申请 40 分钟 4 连发、广播草稿整点轰炸（3 次 cancel 全由它产生）、
   cross-session 同分钟双写、日程卡同日 3 连发。
6. **爆发预算接线**：一拍 ≤4 张（heartbeat_loop throttle_key 已有机关，等拍板）。
7. suggested-reply（r1/r2/r3）扩大覆盖——他亲口要过且点击有实证。

## 8. 实施顺序与部署

REQ-120 步骤 1（PR，零行为变化）→ REQ-119 + REQ-121（PR，测试改动面重叠一次改到位）→
REQ-122 + REQ-123（PR）→ REQ-120 步骤 2（PR，代码清理）。
每 PR 过 CI + release gate（完整 40 位 SHA + 评审证据）；运行时退役操作（bootout / funnel reset）
随部署执行；部署 = kickstart 路径，禁 restart.sh --full 双 daemon 坑。
